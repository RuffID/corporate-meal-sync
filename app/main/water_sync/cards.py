"""Чтение пропусков: весь файл проверяется до первого изменения в iiko."""

import re
from dataclasses import dataclass
from uuid import UUID

from .errors import CardError, SyncError
from .storage import read_json, save_json


@dataclass(frozen=True)
class Card:
    name: str
    track: str
    line: int = 0


def parse_card(value, context):
    if not isinstance(value, str):
        raise CardError(f"{context}: идентификатор пропуска должен быть строкой.")
    # Убираем пробелы и только хвостовое заполнение NUL. Середину не исправляем.
    name = value.strip().replace(" ", "").rstrip("\x00").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", name):
        received = repr(name[:80])
        raise CardError(f"{context}: получено {received}, длина {len(name)}; ожидаются ровно 16 HEX-символов.")
    return Card(name, str(int(name[8:14], 16)).zfill(10))


def read_cards(path, on_invalid=None):
    """С обработчиком пропускаем плохие строки; без него проверяем файл строго."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise SyncError(f"Не удалось прочитать CSV {path}: {type(error).__name__}; ожидается UTF-8.") from None
    cards, names, tracks = [], {}, {}
    row_count = 0
    for number, raw in enumerate(lines, 1):
        if not raw.strip(" \t\x00"):
            continue
        row_count += 1
        try:
            parsed = parse_card(raw, f"{path.name}, строка {number}")
        except CardError as error:
            if on_invalid is None:
                raise
            on_invalid(error)
            continue
        card = Card(parsed.name, parsed.track, number)
        if card.name in names:
            error = CardError(f"{path.name}, строка {number}: пропуск {card.name} повторяет строку {names[card.name]}.")
            if on_invalid is None:
                raise error
            on_invalid(error)
            continue
        if card.track in tracks and on_invalid is None:
            previous = tracks[card.track][0]
            raise CardError(f"{path.name}, строки {previous.line} и {number}: разные пропуска {previous.name} и {card.name} дают одну карту {card.track}.")
        names[card.name] = number
        tracks.setdefault(card.track, []).append(card)
        cards.append(card)
    if on_invalid is not None:
        # При коллизии исключаем все разные пропуска с этим треком: выбор
        # первого мог бы привести к начислению другому владельцу карты.
        valid = []
        for card in cards:
            collisions = tracks[card.track]
            if len(collisions) > 1:
                conflicting_lines = ", ".join(str(item.line) for item in collisions)
                on_invalid(CardError(f"{path.name}, строка {card.line}: пропуск {card.name}, карта {card.track}; разные пропуска в строках {conflicting_lines} дают один трек."))
            else:
                valid.append(card)
        cards = valid
    if not cards and (on_invalid is None or row_count == 0):
        raise SyncError(f"CSV {path} не содержит пропусков; импорт не выполнен.")
    return cards


class GuestCache:
    """Кеш ускоряет сверку, но не подтверждает действительность гостя."""

    def __init__(self, path):
        self.path = path
        data = read_json(path, {"users": []})
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise SyncError(f"Кеш {path}: ожидается объект с массивом users.")
        self.entries = {}
        tracks = {}
        for number, user in enumerate(data["users"], 1):
            if not isinstance(user, dict):
                raise SyncError(f"Кеш {path}, users[{number}]: ожидается объект.")
            card = parse_card(user.get("guestName"), f"Кеш {path}, users[{number}]")
            try:
                guest_id = str(UUID(user.get("guestId", "")))
            except (ValueError, TypeError, AttributeError):
                raise SyncError(f"Кеш {path}, users[{number}]: guestId должен быть GUID.") from None
            if user.get("cardTrack") != card.track:
                raise SyncError(f"Кеш {path}, users[{number}]: cardTrack={user.get('cardTrack')!r}, ожидается {card.track}.")
            if card.name in self.entries or card.track in tracks:
                raise SyncError(f"Кеш {path}, users[{number}]: повтор имени {card.name} или карты {card.track}.")
            self.entries[card.name] = {"guestName": card.name, "guestId": guest_id, "cardTrack": card.track}
            tracks[card.track] = card.name

    def get(self, card):
        return self.entries.get(card.name)

    def remember(self, card, guest_id):
        self.entries[card.name] = {"guestName": card.name, "guestId": guest_id, "cardTrack": card.track}
        save_json(self.path, {"users": list(self.entries.values())})
