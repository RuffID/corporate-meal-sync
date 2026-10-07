"""Изолированные проверки: временные файлы и подставные ответы, без сети и GUI."""

import ast
import copy
import json
import logging
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "main"))

import requests

from water_sync.api import IikoClient
from water_sync.cards import GuestCache, parse_card, read_cards
from water_sync.cli import parse_day, parser, resolve_refill
from water_sync.config import Settings, load_settings
from water_sync.errors import ApiError, SecretFilter, SyncError
from water_sync.exporter import report_rows, write_report
from water_sync.importer import import_cards
from water_sync.sftp import download, upload
from water_sync.storage import Journal, import_lock, save_json


DAY = date(2026, 10, 7)
GUEST_ID = "55555555-5555-4555-8555-555555555555"
OTHER_ID = "66666666-6666-4666-8666-666666666666"
NAME = "AA00000000002A01"
SECOND_NAME = "BB00000000002B01"


def settings(folder):
    return Settings("https://iikocard.example.invalid:9900", "example-login", "example-api-password",
                    "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                    "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444",
                    Decimal("100"), 30, folder, "sftp.example.invalid", 2202, "example-sftp-user", "example-sftp-password", "Exchange", folder / "known_hosts")


def guest(config, name=NAME, guest_id=GUEST_ID):
    card = parse_card(name, "Тест")
    return {"id": guest_id, "name": name, "isDeleted": False, "isBlocked": False,
            "cards": [{"Track": card.track, "Number": card.track, "IsActivated": True}],
            "categories": [{"id": config.category_id, "isActive": True}],
            "walletBalances": [{"wallet": {"id": config.wallet_id}}]}


class FakeIiko:
    def __init__(self, config):
        self.config = config
        self.guests = {GUEST_ID: guest(config)}
        self.events = {}
        self.mutations = []
        self.refill_error = None
        self.created_deleted = False

    def by_card(self, track):
        for value in self.guests.values():
            if any(card["Track"] == track for card in value["cards"]):
                return copy.deepcopy(value)
        return None

    def by_id(self, guest_id):
        return copy.deepcopy(self.guests[guest_id])

    def transactions(self, day, guest_id):
        return copy.deepcopy(self.events.get(guest_id, []))

    def create_guest(self, card):
        self.mutations.append(("create", card.name))
        value = guest(self.config, card.name, OTHER_ID)
        value["isDeleted"] = self.created_deleted
        self.guests[OTHER_ID] = value
        return OTHER_ID

    def add_category(self, guest_id):
        self.mutations.append(("category", guest_id))
        self.guests[guest_id]["categories"] = [{"id": self.config.category_id, "isActive": True}]

    def add_nutrition(self, guest_id):
        self.mutations.append(("nutrition", guest_id))
        self.guests[guest_id]["walletBalances"] = [{"wallet": {"id": self.config.wallet_id}}]

    def refill(self, guest_id, marker):
        self.mutations.append(("refill", guest_id))
        if self.refill_error:
            raise self.refill_error
        self.events.setdefault(guest_id, []).append(
            {"transactionType": "RefillWalletFromApi", "transactionSum": 100, "comment": marker}
        )


class FakeResponse:
    def __init__(self, status, data=None, text=None):
        self.status_code = status
        self.text = json.dumps(data) if text is None else text

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = settings(self.folder)
        self.client = FakeIiko(self.config)
        self.path = self.folder / "20261007-SKD-IIKO.csv"
        self.path.write_text(NAME + "\n", encoding="utf-8")
        # Любой случайный реальный HTTP-вызов немедленно проваливает проверку.
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Реальная сеть запрещена в тестах"))
        network.start()
        self.addCleanup(network.stop)
        handler = logging.NullHandler()
        logger = logging.getLogger("water_sync")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)


class ImportTests(IsolatedCase):
    def test_existing_category_is_not_assigned_again_and_rerun_does_not_refill(self):
        first = import_cards(self.config, self.client, self.path, DAY)
        second = import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(first["completed"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(self.client.mutations, [("refill", GUEST_ID)])
        stored = json.loads((self.folder / "guests.verified.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["users"][0]["cardTrack"], "0000000042")

    def test_missing_category_and_wallet_are_verified_before_refill(self):
        self.client.guests[GUEST_ID]["categories"] = []
        self.client.guests[GUEST_ID]["walletBalances"] = []
        import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual([kind for kind, _ in self.client.mutations], ["category", "nutrition", "refill"])

    def test_deleted_or_blocked_guest_is_never_modified(self):
        for field in ("isDeleted", "isBlocked"):
            with self.subTest(field=field):
                self.client.guests[GUEST_ID][field] = True
                with self.assertRaisesRegex(SyncError, field):
                    import_cards(self.config, self.client, self.path, DAY)
                self.client.guests[GUEST_ID][field] = False
        self.assertEqual(self.client.mutations, [])

    def test_missing_status_is_not_assumed_false(self):
        del self.client.guests[GUEST_ID]["isDeleted"]
        with self.assertRaisesRegex(SyncError, "не вернул"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_inactive_card_is_not_refilled(self):
        self.client.guests[GUEST_ID]["cards"][0]["IsActivated"] = False
        with self.assertRaisesRegex(SyncError, "активных карт"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_guest_changed_after_preflight_is_checked_again_before_mutation(self):
        original = self.client.by_id
        def changed(guest_id):
            result = original(guest_id)
            result["isDeleted"] = True
            return result
        self.client.by_id = changed
        with self.assertRaisesRegex(SyncError, "isDeleted=true"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_returned_deleted_guest_after_create_is_not_given_category(self):
        self.client.guests = {}
        self.client.created_deleted = True
        with self.assertRaisesRegex(SyncError, "isDeleted=true"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [("create", NAME)])

    def test_foreign_refill_requires_manual_reconciliation(self):
        self.client.events[GUEST_ID] = [{"transactionType": "RefillWallet", "transactionSum": 100, "comment": None}]
        with self.assertRaisesRegex(SyncError, "resolve-refill"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])
        resolve_refill(self.config, DAY, parse_card(NAME, "test"), GUEST_ID, "applied", "Сверено вручную")
        self.assertEqual(import_cards(self.config, self.client, self.path, DAY)["skipped"], 1)

    def test_timeout_is_persisted_and_empty_report_does_not_allow_replay(self):
        self.client.refill_error = requests.Timeout("Обрыв ответа")
        with self.assertRaises(requests.Timeout):
            import_cards(self.config, self.client, self.path, DAY)
        journal = Journal(self.config, DAY)
        self.assertEqual(journal.get("0000000042")["state"], "unknown")
        with self.assertRaisesRegex(SyncError, "результат прежнего запроса неизвестен"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [("refill", GUEST_ID)])

    def test_marker_recovers_success_without_a_local_receipt(self):
        journal = Journal(self.config, DAY)
        journal.record(parse_card(NAME, "test"), GUEST_ID, "sending", "Сбой сохранения ответа")
        self.client.events[GUEST_ID] = [{"transactionType": "RefillWalletFromApi", "transactionSum": 100,
                                        "comment": journal.marker("0000000042")}]
        self.assertEqual(import_cards(self.config, self.client, self.path, DAY)["skipped"], 1)
        self.assertEqual(self.client.mutations, [])

    def test_two_receipts_for_one_marker_are_not_silently_accepted(self):
        journal = Journal(self.config, DAY)
        event = {"transactionType": "RefillWalletFromApi", "transactionSum": 100, "comment": journal.marker("0000000042")}
        self.client.events[GUEST_ID] = [event, event]
        with self.assertRaisesRegex(SyncError, "несколько пополнений"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_last_guest_failure_is_found_before_first_refill(self):
        self.path.write_text(NAME + "\n" + SECOND_NAME + "\n", encoding="utf-8")
        second = guest(self.config, SECOND_NAME, OTHER_ID)
        second["categories"][0]["isActive"] = False
        self.client.guests[OTHER_ID] = second
        with self.assertRaisesRegex(SyncError, "не действует"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_deleted_cached_duplicate_is_replaced_by_live_card_owner(self):
        cache = GuestCache(self.folder / "guests.verified.json")
        cache.remember(parse_card(NAME, "test"), OTHER_ID)
        old = guest(self.config, NAME, OTHER_ID)
        old["isDeleted"], old["cards"] = True, []
        self.client.guests[OTHER_ID] = old
        import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(GuestCache(cache.path).get(parse_card(NAME, "test"))["guestId"], GUEST_ID)

    def test_active_cached_duplicate_requires_review(self):
        cache = GuestCache(self.folder / "guests.verified.json")
        cache.remember(parse_card(NAME, "test"), OTHER_ID)
        self.client.guests[OTHER_ID] = guest(self.config, NAME, OTHER_ID)
        with self.assertRaisesRegex(SyncError, "ручная сверка"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_amount_change_does_not_create_a_second_daily_credit(self):
        import_cards(self.config, self.client, self.path, DAY)
        changed = settings(self.folder)
        object.__setattr__(changed, "refill_amount", Decimal("300"))
        with self.assertRaisesRegex(SyncError, "сумма"):
            import_cards(changed, self.client, self.path, DAY)
        self.assertEqual(len(self.client.mutations), 1)

    def test_manual_not_applied_cannot_erase_confirmed_credit(self):
        import_cards(self.config, self.client, self.path, DAY)
        with self.assertRaisesRegex(SyncError, "нельзя снять"):
            resolve_refill(self.config, DAY, parse_card(NAME, "test"), GUEST_ID, "not-applied", "Ошибка оператора")

    def test_corrupt_journal_is_preserved(self):
        journal = Journal(self.config, DAY)
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        journal.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(SyncError, "не перезаписан"):
            import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(journal.path.read_text(encoding="utf-8"), "{broken")

    def test_concurrent_import_is_rejected(self):
        with import_lock(self.folder):
            with self.assertRaisesRegex(SyncError, "Другой процесс"):
                with import_lock(self.folder):
                    self.fail("Вторая блокировка получена")


class InputTests(IsolatedCase):
    def test_nul_padding_and_bom_are_removed_without_truncating_identifier(self):
        self.path.write_text(NAME.lower() + "\x00" * 400 + "\n", encoding="utf-8-sig")
        self.assertEqual(read_cards(self.path)[0].track, "0000000042")

    def test_long_identifier_and_embedded_nul_are_rejected(self):
        for name in (NAME + "AB", NAME[:8] + "\x00" + NAME[8:]):
            with self.subTest(name=repr(name)):
                self.path.write_text(name, encoding="utf-8")
                with self.assertRaisesRegex(SyncError, "ровно 16"):
                    read_cards(self.path)

    def test_duplicates_and_track_collisions_are_rejected_before_api(self):
        for other in (NAME, "CC00000000002A01"):
            self.path.write_text(NAME + "\n" + other + "\n", encoding="utf-8")
            with self.assertRaises(SyncError):
                import_cards(self.config, self.client, self.path, DAY)
        self.assertEqual(self.client.mutations, [])

    def test_export_validates_rows_before_replacing_file(self):
        self.path.write_bytes(b"previous export")
        with self.assertRaisesRegex(SyncError, "guestName"):
            rows = report_rows([{"guestName": "bad", "payFromWalletSum": 100}])
            write_report(self.path, DAY, rows)
        self.assertEqual(self.path.read_bytes(), b"previous export")

    def test_export_keeps_contract_and_does_not_upload_an_old_empty_day(self):
        rows = report_rows([{"guestName": NAME + "\x00", "payFromWalletSum": "12.50"},
                            {"guestName": NAME, "payFromWalletSum": 0}])
        self.assertTrue(write_report(self.path, DAY, rows))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "PassID;TransactionDate;TransactionSumm\n" + NAME + ";2026-10-07;12,50\n")
        self.assertFalse(write_report(self.path, DAY, []))
        self.assertIn("12,50", self.path.read_text(encoding="utf-8"))

    def test_cli_dates_and_inclusive_report_end_are_explicit(self):
        args = parser().parse_args(["report", "--from", "2026-10-07", "--to", "2026-10-07"])
        self.assertEqual(args.start, DAY)
        self.assertEqual(args.end, DAY)
        self.assertEqual(parse_day("2026-10-07"), DAY)

    def test_example_config_uses_fake_settings_and_paths_are_config_relative(self):
        example = json.loads((ROOT / "app/main/config.example.json").read_text(encoding="utf-8"))
        expected_credentials = {"api_login": "example-login", "api_password": "example-api-password",
                                "sftp_username": "example-sftp-user", "sftp_password": "example-sftp-password"}
        for name, expected in expected_credentials.items():
            self.assertEqual(example[name], expected)
        self.assertEqual(example["api_url"], self.config.api_url)
        self.assertEqual(example["sftp_host"], self.config.sftp_host)
        for name in ("organization_id", "program_id", "wallet_id", "category_id"):
            self.assertEqual(example[name], getattr(self.config, name))
        path = self.folder / "config.json"
        save_json(path, example)
        loaded = load_settings(path)
        self.assertEqual(loaded.data_dir, self.folder)
        self.assertEqual(loaded.known_hosts_file, self.folder / "known_hosts")


class ApiTests(IsolatedCase):
    def make_client(self, *responses):
        session = FakeSession(FakeResponse(200, "test-token-value"), *responses)
        client = IikoClient(self.config, session)
        client.authenticate()
        return client, session

    def test_only_known_card_not_found_error_allows_creation(self):
        for status, code, missing in ((400, "Card_CanNotFindByNumber", True), (400, "OtherError", False), (404, "Card_CanNotFindByNumber", False), (401, "Unauthorized", False)):
            with self.subTest(status=status, code=code):
                client, _ = self.make_client(FakeResponse(status, {"errorCode": code}))
                if missing:
                    self.assertIsNone(client.by_card("0000000042"))
                else:
                    with self.assertRaises(ApiError):
                        client.by_card("0000000042")

    def test_malformed_authentication_body_is_not_logged(self):
        client = IikoClient(self.config, FakeSession(FakeResponse(200, text="unquoted-secret-token")))
        with self.assertRaises(SyncError) as captured:
            client.authenticate()
        self.assertNotIn("unquoted-secret-token", str(captured.exception))

    def test_error_body_is_available_but_tokens_and_passwords_are_hidden(self):
        client, _ = self.make_client(FakeResponse(400, {"errorCode": "DuplicateCategory", "message": "test-token-value example-api-password", "accessToken": "another-token"}))
        with self.assertRaises(ApiError) as captured:
            client.add_category(GUEST_ID)
        text = str(captured.exception)
        self.assertIn("DuplicateCategory", text)
        for secret in ("test-token-value", "example-api-password", "another-token"):
            self.assertNotIn(secret, text)

    def test_timeout_on_refill_is_not_retried(self):
        client, session = self.make_client(requests.Timeout("access_token=test-token-value"))
        with self.assertRaises(ApiError) as captured:
            client.refill(GUEST_ID, "marker")
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("test-token-value", str(captured.exception))
        self.assertFalse(session.calls[-1][2]["allow_redirects"])

    def test_existing_post_contract_keeps_query_and_json_fields(self):
        client, session = self.make_client(FakeResponse(200, GUEST_ID), FakeResponse(200, None), FakeResponse(200, None))
        self.assertEqual(client.create_guest(parse_card(NAME, "test")), GUEST_ID)
        client.add_category(GUEST_ID)
        client.add_nutrition(GUEST_ID)
        create, category, nutrition = [item[2] for item in session.calls[1:]]
        self.assertEqual(create["json"]["customer"]["magnetCardNumber"], "0000000042")
        self.assertEqual(category["params"]["categoryId"], self.config.category_id)
        self.assertEqual(category["json"]["customerId"], GUEST_ID)
        self.assertEqual(nutrition["json"]["corporateNutritionId"], self.config.program_id)
        self.assertEqual(nutrition["params"]["corporate_nutrition_id"], self.config.program_id)

    def test_success_status_with_non_json_report_is_rejected(self):
        client, _ = self.make_client(FakeResponse(200, text="<html>error</html>"))
        with self.assertRaisesRegex(SyncError, "ожидался JSON"):
            client.transactions(DAY, GUEST_ID)

    def test_wrong_guest_id_or_wrong_report_type_is_rejected(self):
        client, _ = self.make_client(FakeResponse(200, guest(self.config, guest_id=OTHER_ID)))
        with self.assertRaisesRegex(SyncError, "вместо запрошенного"):
            client.by_id(GUEST_ID)
        client, _ = self.make_client(FakeResponse(200, {}))
        with self.assertRaisesRegex(SyncError, "массив объектов"):
            client.transactions(DAY, GUEST_ID)


class TransferTests(IsolatedCase):
    @contextmanager
    def connection(self, fake):
        yield fake

    def test_failed_or_invalid_download_does_not_replace_previous_csv(self):
        class FakeSftp:
            def get(self, remote, local):
                Path(local).write_bytes(b"bad identifier")
        self.path.write_bytes(b"previous csv")
        with patch("water_sync.sftp.connection", return_value=self.connection(FakeSftp())):
            with self.assertRaises(SyncError):
                download(self.config, self.path)
        self.assertEqual(self.path.read_bytes(), b"previous csv")
        self.assertEqual(list(self.folder.glob("*.part")), [])

    def test_upload_failure_removes_only_remote_temporary_file(self):
        class FakeSftp:
            removed = []
            def put(self, source, remote, confirm):
                self.remote = remote
            def posix_rename(self, temporary, target):
                raise OSError("rename not supported")
            def remove(self, remote):
                self.removed.append(remote)
        fake = FakeSftp()
        with patch("water_sync.sftp.connection", return_value=self.connection(fake)):
            with self.assertRaises(OSError):
                upload(self.config, self.path)
        self.assertEqual(fake.removed, [fake.remote])
        self.assertTrue(fake.remote.endswith(".part"))
        self.assertTrue(self.path.exists())


class SourceTests(unittest.TestCase):
    def test_all_working_sources_parse_with_python_39_syntax(self):
        paths = list((ROOT / "app/main").rglob("*.py"))
        for path in paths:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
