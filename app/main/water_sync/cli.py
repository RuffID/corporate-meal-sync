"""Точки входа для планировщика, ручного запуска и GUI."""

import argparse
import logging
import sys
import traceback
from datetime import date, datetime, timedelta
from uuid import UUID

from .api import IikoClient
from .cards import parse_card
from .config import load_settings
from .errors import SecretFilter, SyncError
from .exporter import report_rows, write_report
from .importer import import_cards
from .sftp import download, upload
from .storage import Journal, import_lock


LOGGER = logging.getLogger("water_sync")


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets):
        super().__init__("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        self.secrets = secrets

    def format(self, record):
        return self.secrets.clean(super().format(record))


def configure_logging(settings, command):
    folder = settings.data_dir / "Logs"
    folder.mkdir(parents=True, exist_ok=True)
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    secrets = SecretFilter(settings.api_password, settings.sftp_password)
    formatter = SafeFormatter(secrets)
    handlers = [logging.FileHandler(folder / f"{date.today():%Y-%m-%d}_{command}.log", encoding="utf-8"), logging.StreamHandler()]
    for handler in handlers:
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return secrets


def run_job(command, day=None, config_path=None, start=None, end=None):
    """GUI и консоль вызывают один сценарий, поэтому проверки не расходятся."""
    settings = load_settings(config_path)
    secrets = configure_logging(settings, command)
    client = IikoClient(settings)
    try:
        if command == "report":
            if start is None or end is None or end <= start:
                raise SyncError("Начало периода должно быть раньше его исключительной верхней границы.")
            client.authenticate()
            secrets.add(client.token)
            return report_rows(client.nutrition_report(start, end))
        with import_lock(settings.data_dir):
            if command in ("import-local", "import-sftp"):
                day = day or date.today()
                path = settings.csv_path(day, "SKD-IIKO")
                if command == "import-sftp":
                    download(settings, path)
                    LOGGER.info("CSV скачан с SFTP: %s.", path.name)
                # Проверяем чтение CSV до авторизации. Плохие строки не мешают
                # остальным картам; причины их пропуска запишет import_cards.
                from .cards import read_cards
                read_cards(path, on_invalid=lambda error: None)
                client.authenticate()
                secrets.add(client.token)
                result = import_cards(settings, client, path, day)
                LOGGER.info("Импорт %s завершён: начислено %s, уже подтверждено %s, пропущено из-за ошибок %s, всего %s.", day, result["completed"], result["skipped"], result["rejected"], result["total"])
                return result
            if command != "export":
                raise SyncError(f"Неизвестный сценарий {command}.")
            day = day or (date.today() - timedelta(days=1))
            client.authenticate()
            secrets.add(client.token)
            rows = report_rows(client.nutrition_report(day, day + timedelta(days=1)))
            path = settings.csv_path(day, "IIKO-1C")
            if not write_report(path, day, rows):
                LOGGER.info("За %s нет положительных списаний: CSV не создаётся и не отправляется.", day)
                return {"exported": 0}
            LOGGER.info("CSV сформирован: %s, строк %s.", path.name, len(rows))
            upload(settings, path)
            LOGGER.info("CSV отправлен на SFTP: %s.", path.name)
            return {"exported": len(rows)}
    except Exception as error:
        # В traceback не включаются локальные переменные. Секреты и токен
        # дополнительно удаляются из сетевых исключений и ответа API.
        LOGGER.error("Сценарий остановлен.\n%s", secrets.clean(traceback.format_exc()))
        # Наружу тоже передаём только очищенное сообщение, в том числе в GUI.
        raise SyncError(secrets.clean(error)) from None
    finally:
        client.close()


def resolve_refill(settings, day, card, guest_id, result, reason):
    """Только локальная отметка после сверки оператором; запросов к iiko нет."""
    if not reason or not reason.strip():
        raise SyncError("Для ручной сверки обязательна непустая причина --reason.")
    if result not in ("applied", "not-applied"):
        raise SyncError("Результат сверки должен быть applied или not-applied.")
    with import_lock(settings.data_dir):
        journal = Journal(settings, day)
        entry = journal.get(card.track)
        if entry and entry["state"] == "confirmed" and result == "not-applied":
            raise SyncError("Подтверждённое начисление нельзя снять этой командой. Сначала разберите финансовую операцию в iiko.")
        state = "confirmed" if result == "applied" else "reviewed-not-applied"
        journal.record(card, guest_id, state, "Ручная сверка: " + reason.strip())
        return journal.path


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Дата должна иметь формат YYYY-MM-DD.") from None


def parser():
    result = argparse.ArgumentParser(description="Обмен СКД → iikoCard → 1С")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("import-sftp", "Скачать CSV и выполнить начисления"),
                            ("import-local", "Выполнить начисления из локального CSV"),
                            ("export", "Выгрузить списания в 1С через SFTP")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--date", type=parse_day)
        command.add_argument("--config")
    command = commands.add_parser("report", help="Прочитать отчёт без выгрузки на SFTP")
    command.add_argument("--from", dest="start", type=parse_day, required=True)
    command.add_argument("--to", dest="end", type=parse_day, required=True, help="Последний день включительно")
    command.add_argument("--config")
    command = commands.add_parser("resolve-refill", help="Зафиксировать результат ручной сверки в локальном журнале")
    command.add_argument("--date", type=parse_day, required=True)
    command.add_argument("--card", required=True, help="16 HEX-символов идентификатора пропуска")
    command.add_argument("--guest-id", required=True)
    command.add_argument("--result", choices=("applied", "not-applied"), required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--config")
    return result


def main(default_command=None):
    arguments = sys.argv[1:]
    if default_command:
        arguments = [default_command] + arguments
    args = parser().parse_args(arguments)
    try:
        if args.command == "resolve-refill":
            settings = load_settings(args.config)
            card = parse_card(args.card, "--card")
            try:
                guest_id = str(UUID(args.guest_id))
            except ValueError:
                raise SyncError("--guest-id должен содержать GUID.") from None
            path = resolve_refill(settings, args.date, card, guest_id, args.result, args.reason)
            print(f"Результат сверки записан в {path}. Запросов к iiko не выполнялось.")
        elif args.command == "report":
            for name, amount in run_job("report", config_path=args.config, start=args.start, end=args.end + timedelta(days=1)):
                print(f"{name}: {amount}")
        else:
            run_job(args.command, args.date, args.config)
        return 0
    except (SyncError, OSError) as error:
        # До загрузки настроек секреты ещё не прочитаны; сообщения настроек
        # содержат только имя проблемного поля, а не его значение.
        print(f"ОШИБКА: {SecretFilter().clean(error)}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Непредвиденная ошибка {type(error).__name__}. Подробности в Logs.", file=sys.stderr)
        return 1
