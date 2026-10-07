"""Настройки подключения хранятся отдельно от исходников и не входят в Git."""

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit

from .errors import SyncError


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.local.json"


@dataclass(frozen=True)
class Settings:
    api_url: str
    api_login: str
    api_password: str
    organization_id: str
    program_id: str
    wallet_id: str
    category_id: str
    refill_amount: Decimal
    timeout_seconds: int
    data_dir: Path
    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_password: str
    sftp_remote_dir: str
    known_hosts_file: Path

    def csv_path(self, day, direction):
        return self.data_dir / "Csv" / (day.strftime("%Y%m%d") + "-" + direction + ".csv")


def load_settings(config_path=None):
    path = Path(config_path or os.environ.get("WATER_SYNC_CONFIG", DEFAULT_CONFIG)).resolve()
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SyncError(f"Не удалось прочитать настройки {path}: {type(error).__name__}.") from None
    if not isinstance(values, dict):
        raise SyncError(f"В {path} ожидается JSON-объект настроек.")
    fields = set(Settings.__dataclass_fields__)
    if set(values) != fields:
        missing = sorted(fields - set(values))
        extra = sorted(set(values) - fields)
        raise SyncError(f"Настройки {path}: отсутствуют поля {missing}; лишние поля {extra}.")
    for name in fields - {"refill_amount", "timeout_seconds", "sftp_port"}:
        if not isinstance(values[name], str):
            raise SyncError(f"Настройки {path}: поле {name} должно быть строкой.")
    for name in ("organization_id", "program_id", "wallet_id", "category_id"):
        try:
            values[name] = str(UUID(values[name]))
        except ValueError:
            raise SyncError(f"Настройки {path}: {name} должен содержать GUID.") from None
    parts = urlsplit(values["api_url"])
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SyncError("api_url должен содержать HTTPS-адрес сервера без логина, параметров и пути.")
    values["api_url"] = values["api_url"].rstrip("/")
    for name, maximum in (("timeout_seconds", 300), ("sftp_port", 65535)):
        if type(values[name]) is not int or not 1 <= values[name] <= maximum:
            raise SyncError(f"Поле {name} должно быть целым числом от 1 до {maximum}.")
    try:
        amount = Decimal(str(values["refill_amount"]))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise SyncError("refill_amount должен быть положительной суммой с точностью до копеек.") from None
    values["refill_amount"] = amount
    values["data_dir"] = (path.parent / values["data_dir"]).resolve()
    values["known_hosts_file"] = (path.parent / values["known_hosts_file"]).resolve()
    return Settings(**values)
