"""Чтение пропусков: весь файл проверяется до первого изменения в iiko."""

import re
from dataclasses import dataclass
from uuid import UUID

from .errors import SyncError
from .storage import read_json, save_json


@dataclass(frozen=True)
class Card:
    name: str
    track: str
    line: int = 0


def parse_card(value, context):
    if not isinstance(value, str):
        raise SyncError(f"{context}: идентификатор пропуска должен быть строкой.")
    # Убираем пробелы и только хвостовое заполнение NUL. Середину не исправляем.
    name = value.strip().replace(" ", "").rstrip("\x00").upper()
    if not re.fullmatch(r"[0-9A-F]{16}", name):
        received = repr(name[:80])
        raise SyncError(f"{context}: получено {received}, длина {len(name)}; ожидаются ровно 16 HEX-символов.")
    return Card(name, str(int(name[8:14], 16)).zfill(10))


def read_cards(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise SyncError(f"Не удалось прочитать CSV {path}: {type(error).__name__}; ожидается UTF-8.") from None
    cards, names, tracks = [], {}, {}
    for number, raw in enumerate(lines, 1):
        if not raw.strip(" \t\x00"):
            continue
        parsed = parse_card(raw, f"{path.name}, строка {number}")
        card = Card(parsed.name, parsed.track, number)
        if card.name in names:
            raise SyncError(f"{path.name}, строка {number}: пропуск {card.name} повторяет строку {names[card.name]}.")
        if card.track in tracks:
            previous = tracks[card.track]
            raise SyncError(f"{path.name}, строки {previous.line} и {number}: разные пропуска {previous.name} и {card.name} дают одну карту {card.track}.")
        names[card.name], tracks[card.track] = number, card
        cards.append(card)
    if not cards:
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
