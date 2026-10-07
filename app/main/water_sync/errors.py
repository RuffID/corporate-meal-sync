"""Ожидаемые ошибки, которые можно понятно показать оператору."""

import re
from urllib.parse import quote, quote_plus


class SyncError(Exception):
    """Ошибка данных, настройки или состояния операции."""


class ApiError(SyncError):
    def __init__(self, status, code, message, body):
        self.status = status
        self.code = code
        self.body = body
        super().__init__(message)


class SecretFilter:
    """Удаляет секреты и из сообщений сервера, и из сетевых исключений."""

    def __init__(self, *secrets):
        self.secrets = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret):
        if secret:
            self.secrets.update((secret, quote(secret, safe=""), quote_plus(secret)))

    def clean(self, message):
        text = str(message)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "<скрыто>")
        text = re.sub(
            r"(?i)(access_token|accessToken|user_secret|password|api_password|sftp_password)"
            r"(\s*[=:]\s*)[^&\s,\"']+",
            r"\1\2<скрыто>", text,
        )
        text = re.sub(
            r'(?i)("(?:access_token|accessToken|user_secret|password|api_password|sftp_password)"\s*:\s*)"(?:\\.|[^"\\])*"',
            r'\1"<скрыто>"', text,
        )
        return text.replace("\x00", "\\u0000")
