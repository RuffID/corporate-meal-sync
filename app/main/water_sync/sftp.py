"""Обмен файлами: таймауты, проверка ключа сервера и временные файлы."""

import posixpath
from contextlib import contextmanager
from uuid import uuid4

from .cards import read_cards
from .errors import SyncError


@contextmanager
def connection(settings):
    import paramiko

    if not settings.sftp_username or not settings.sftp_password:
        raise SyncError("Заполните sftp_username и sftp_password в локальных настройках.")
    if not settings.known_hosts_file.is_file():
        raise SyncError(f"Не найден файл ключей SFTP {settings.known_hosts_file}. Сверьте отпечаток сервера с администратором и добавьте проверенный ключ; порядок описан в README.")
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(settings.known_hosts_file))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(hostname=settings.sftp_host, port=settings.sftp_port,
                       username=settings.sftp_username, password=settings.sftp_password,
                       timeout=settings.timeout_seconds, banner_timeout=settings.timeout_seconds,
                       auth_timeout=settings.timeout_seconds, look_for_keys=False, allow_agent=False)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(settings.timeout_seconds)
            yield sftp
    except (paramiko.SSHException, OSError) as error:
        raise SyncError(f"Ошибка SFTP {settings.sftp_host}:{settings.sftp_port}: {error}") from None
    finally:
        client.close()


def download(settings, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".part")
    remote = posixpath.join(settings.sftp_remote_dir, destination.name)
    try:
        with connection(settings) as sftp:
            try:
                sftp.get(remote, str(temporary))
            except FileNotFoundError:
                raise SyncError(f"На SFTP отсутствует {remote}. Ранее скачанный CSV не использован. Получите актуальный файл и выполните import-local.") from None
        read_cards(temporary)
        # Незаконченная или некорректная загрузка не заменяет исправный CSV.
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def upload(settings, source):
    remote = posixpath.join(settings.sftp_remote_dir, source.name)
    temporary = remote + "." + uuid4().hex + ".part"
    with connection(settings) as sftp:
        try:
            sftp.put(str(source), temporary, confirm=True)
            # OpenSSH умеет атомарно заменить существующий файл. Отказ расширения
            # не маскируем удалением старого файла или неатомарной перезаписью.
            sftp.posix_rename(temporary, remote)
        except Exception:
            try:
                sftp.remove(temporary)
            except OSError:
                pass  # Удаление временного файла не должно скрывать исходную ошибку.
            raise
