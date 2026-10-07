"""Проверка гостей и последовательное начисление с журналом операций."""

import logging
from decimal import Decimal, InvalidOperation

from .cards import GuestCache, read_cards
from .errors import SyncError
from .storage import Journal


LOGGER = logging.getLogger("water_sync")
REFILL_TYPES = {"RefillWallet", "RefillWalletFromApi", "AutomaticRefillWallet", "RefillWalletFromOrder"}


def validate_guest(guest, card):
    """Имя не идентифицирует человека: сверяем статус и активный трек карты."""
    guest_id = guest["id"]
    for field in ("isDeleted", "isBlocked"):
        if type(guest.get(field)) is not bool:
            raise SyncError(f"Гость {guest_id}, карта {card.track}: API не вернул логический {field}; начисление запрещено.")
        if guest[field]:
            raise SyncError(f"Гость {guest_id}, карта {card.track}: {field}=true; начисление запрещено.")
    cards = guest.get("cards")
    if not isinstance(cards, list) or any(not isinstance(item, dict) for item in cards):
        raise SyncError(f"Гость {guest_id}: API не вернул корректный список карт.")
    matches = [item for item in cards if item.get("Track") == card.track and item.get("IsActivated") is True]
    if len(matches) != 1:
        raise SyncError(f"Гость {guest_id}: активных карт с треком {card.track} найдено {len(matches)}, ожидается одна.")
    if not isinstance(guest.get("categories"), list) or any(not isinstance(item, dict) for item in guest["categories"]):
        raise SyncError(f"Гость {guest_id}: API не вернул корректный список категорий.")
    balances = guest.get("walletBalances")
    if not isinstance(balances, list) or any(not isinstance(item, dict) or not isinstance(item.get("wallet"), dict) for item in balances):
        raise SyncError(f"Гость {guest_id}: API не вернул корректный список кошельков.")


def find_guest(client, cache, card):
    guest = client.by_card(card.track)
    cached = cache.get(card)
    if cached and (guest is None or cached["guestId"] != guest["id"]):
        old = client.by_id(cached["guestId"])
        # Удалённый дубль можно заменить только подтверждённым действующим владельцем карты.
        if old.get("isDeleted") is not True:
            raise SyncError(f"Карта {card.track}: кеш указывает на {cached['guestId']}, поиск вернул {guest['id'] if guest else 'отсутствие карты'}. Нужна ручная сверка.")
        if guest is None:
            raise SyncError(f"Карта {card.track}: кешированный гость удалён, действующий владелец не найден. Автоматическое восстановление отменено.")
        LOGGER.warning("Карта %s: вместо удалённого кешированного гостя %s найден %s.", card.track, old["id"], guest["id"])
    if guest is not None:
        validate_guest(guest, card)
    return guest


def category_assigned(guest, settings):
    categories = [item for item in guest["categories"] if item.get("id") == settings.category_id]
    if len(categories) > 1:
        raise SyncError(f"Гость {guest['id']}: API вернул несколько записей категории {settings.category_id}.")
    if categories:
        if categories[0].get("isActive") is not True:
            raise SyncError(f"Гость {guest['id']}: категория {settings.category_id} не действует; повторно не назначаем.")
        return True
    return False


def ensure_ready(client, guest, card, settings):
    validate_guest(guest, card)
    if not category_assigned(guest, settings):
        client.add_category(guest["id"])
        guest = client.by_id(guest["id"])
        validate_guest(guest, card)
        if not any(item.get("id") == settings.category_id and item.get("isActive") is True for item in guest["categories"]):
            raise SyncError(f"Гость {guest['id']}: после назначения API не подтверждает действующую категорию.")
    if not any(item["wallet"].get("id") == settings.wallet_id for item in guest["walletBalances"]):
        client.add_nutrition(guest["id"])
        guest = client.by_id(guest["id"])
        validate_guest(guest, card)
        if not any(item["wallet"].get("id") == settings.wallet_id for item in guest["walletBalances"]):
            raise SyncError(f"Гость {guest['id']}: после включения в программу API не подтверждает кошелёк {settings.wallet_id}.")
    return guest


def check_refills(client, journal, card, guest_id):
    """Пустой отчёт не разрешает повторять запрос с неизвестным результатом."""
    entry = journal.get(card.track)
    if entry and entry["guest_id"] != guest_id:
        raise SyncError(f"Карта {card.track}: ID в журнале {entry['guest_id']} не совпадает с API {guest_id}.")
    transactions = client.transactions(journal.day, guest_id)
    refills, own = [], []
    for number, transaction in enumerate(transactions, 1):
        kind = transaction.get("transactionType")
        if not isinstance(kind, str):
            raise SyncError(f"Отчёт транзакций гостя {guest_id}, строка {number}: отсутствует transactionType.")
        if kind not in REFILL_TYPES:
            continue
        try:
            amount = Decimal(str(transaction["transactionSum"]))
            if not amount.is_finite():
                raise InvalidOperation
        except (KeyError, InvalidOperation, ValueError):
            raise SyncError(f"Отчёт транзакций гостя {guest_id}, строка {number}: некорректная transactionSum.") from None
        if amount > 0:
            refills.append(transaction)
        if transaction.get("comment") == journal.marker(card.track):
            if amount != journal.settings.refill_amount:
                raise SyncError(f"Карта {card.track}: операция с нашим маркером имеет сумму {amount}, ожидается {journal.settings.refill_amount}.")
            own.append(transaction)
    if len(own) > 1:
        raise SyncError(f"Карта {card.track}: в iiko несколько пополнений с одним маркером; нужна финансовая сверка.")
    if own:
        journal.record(card, guest_id, "confirmed", "Подтверждено отчётом iiko по маркеру операции.")
        return True
    if entry and entry["state"] in ("sending", "unknown"):
        raise SyncError(f"Карта {card.track}: результат прежнего запроса неизвестен. Отчёт пока не подтверждает операцию; повтор запрещён. После сверки используйте resolve-refill.")
    if refills and (not entry or entry["state"] != "reviewed-not-applied"):
        raise SyncError(f"Карта {card.track}, гость {guest_id}: за {journal.day} уже есть {len(refills)} положительных пополнений без нашего маркера. Нужна сверка через resolve-refill; повторное начисление запрещено.")
    return False


def import_cards(settings, client, path, day):
    cards = read_cards(path)
    # Старые кеши содержат обрезанные имена и неверные треки. Их не мигрируем
    # догадками: новый кеш заполняется только после проверки владельца в API.
    cache = GuestCache(settings.data_dir / "guests.verified.json")
    journal = Journal(settings, day)
    plans, skipped = [], 0
    # Проверяем все существующие карты до первого POST, чтобы ошибка в конце
    # файла не приводила к ненужной частичной обработке начала.
    for card in cards:
        entry = journal.get(card.track)
        if entry and entry["state"] == "confirmed":
            skipped += 1
            LOGGER.info("Карта %s: начисление за %s уже подтверждено журналом.", card.track, day)
            continue
        guest = find_guest(client, cache, card)
        if guest is None and entry:
            raise SyncError(f"Карта {card.track}: журнал уже содержит гостя {entry['guest_id']}, но поиск карты его не находит. Нужна сверка.")
        if guest is not None:
            category_assigned(guest, settings)
        if guest is not None and check_refills(client, journal, card, guest["id"]):
            skipped += 1
            continue
        plans.append((card, guest))
    completed = 0
    for card, guest in plans:
        if guest is None:
            guest_id = client.create_guest(card)
            # create_or_update способен вернуть существующего гостя. Его статус
            # и карту проверяем до категории, программы и пополнения.
            guest = client.by_id(guest_id)
            validate_guest(guest, card)
            if check_refills(client, journal, card, guest_id):
                skipped += 1
                continue
        guest_id = guest["id"]
        # Подготовка большого файла может занять время. Перед изменениями
        # перечитываем гостя: его могли удалить или перевыпустить карту.
        guest = client.by_id(guest_id)
        validate_guest(guest, card)
        cache.remember(card, guest_id)
        guest = ensure_ready(client, guest, card, settings)
        # Повторная сверка перед POST обнаруживает изменения с момента подготовки.
        if check_refills(client, journal, card, guest_id):
            skipped += 1
            continue
        journal.record(card, guest_id, "sending", "Запрос пополнения подготовлен; повтор до подтверждения запрещён.")
        try:
            client.refill(guest_id, journal.marker(card.track))
        except Exception:
            # Состояние sending уже на диске. Даже если запись unknown не удастся,
            # следующий запуск не отправит это начисление повторно.
            journal.record(card, guest_id, "unknown", "Запрос не завершился подтверждённым HTTP 200; требуется сверка.")
            raise
        journal.record(card, guest_id, "confirmed", "iikoCard подтвердил пополнение HTTP 200.")
        completed += 1
        LOGGER.info("%s %s: пополнено на %s рублей; ID гостя %s.", day, card.name, settings.refill_amount, guest_id)
    return {"completed": completed, "skipped": skipped, "total": len(cards)}
