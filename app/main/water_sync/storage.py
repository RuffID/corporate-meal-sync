"""Атомарное сохранение кеша и журнала; блокировка одновременных запусков."""

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import re
from uuid import UUID

from .errors import SyncError


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SyncError(f"Не удалось прочитать {path}: {type(error).__name__}. Файл не перезаписан.") from None


def atomic_write(path, data):
    """Сначала записываем временный файл, затем заменяем целевой целиком."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_json(path, data):
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write(path, content.encode("utf-8"))


@contextmanager
def import_lock(data_dir):
    """Блокировка снимается ОС даже при аварии процесса; файл удалять не надо."""
    path = data_dir / "State" / "import.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncError(f"Другой процесс уже выполняет импорт или сверку: {path}.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Journal:
    """Одна желаемая выплата на карту, дату, организацию и кошелёк."""

    STATES = {"prepared", "sending", "unknown", "confirmed", "reviewed-not-applied"}

    def __init__(self, settings, day):
        self.settings = settings
        self.day = day
        self.path = settings.data_dir / "State" / (
            f"{day:%Y%m%d}-{settings.organization_id}-{settings.wallet_id}.json"
        )
        expected = {"version": 1, "date": day.isoformat(), "organization_id": settings.organization_id,
                    "wallet_id": settings.wallet_id, "entries": {}}
        self.data = read_json(self.path, expected)
        if not isinstance(self.data, dict) or any(self.data.get(key) != value for key, value in expected.items() if key != "entries"):
            raise SyncError(f"Журнал {self.path}: неверная версия, дата, организация или кошелёк.")
        entries = self.data.get("entries")
        if not isinstance(entries, dict):
            raise SyncError(f"Журнал {self.path}: entries должен быть объектом.")
        for track, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("state") not in self.STATES or not isinstance(entry.get("history"), list):
                raise SyncError(f"Журнал {self.path}: некорректная запись карты {track}.")
            name = entry.get("guest_name")
            if not isinstance(track, str) or not re.fullmatch(r"[0-9]{10}", track) or not isinstance(name, str) or not re.fullmatch(r"[0-9A-F]{16}", name) or str(int(name[8:14], 16)).zfill(10) != track:
                raise SyncError(f"Журнал {self.path}: имя пропуска не соответствует карте {track}.")
            try:
                guest_id = str(UUID(entry.get("guest_id", "")))
            except (ValueError, TypeError, AttributeError):
                raise SyncError(f"Журнал {self.path}, карта {track}: некорректный guest_id.") from None
            if guest_id != entry["guest_id"] or any(not isinstance(event, dict) for event in entry["history"]):
                raise SyncError(f"Журнал {self.path}, карта {track}: некорректный ID или история.")
            if entry.get("amount") != format(settings.refill_amount, ".2f"):
                raise SyncError(f"Журнал {self.path}, карта {track}: сумма {entry.get('amount')} не совпадает с настройкой {settings.refill_amount}.")

    def get(self, track):
        return self.data["entries"].get(track)

    def marker(self, track):
        return f"water-sync:{self.settings.organization_id}:{self.settings.wallet_id}:{self.day.isoformat()}:{track}"

    def record(self, card, guest_id, state, reason):
        previous = self.get(card.track)
        if previous and previous.get("guest_id") != guest_id:
            raise SyncError(f"Карта {card.track}: журнал ссылается на гостя {previous.get('guest_id')}, получен {guest_id}. Нужна сверка.")
        entry = dict(previous or {})
        entry.update(guest_name=card.name, guest_id=guest_id, amount=format(self.settings.refill_amount, ".2f"), state=state)
        entry["history"] = list(entry.get("history", [])) + [
            {"at": datetime.now().isoformat(timespec="seconds"), "state": state, "reason": reason}
        ]
        self.data["entries"][card.track] = entry
        save_json(self.path, self.data)
