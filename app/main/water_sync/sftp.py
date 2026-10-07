"""Обмен файлами: таймауты, необязательная проверка ключа и временные файлы."""

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
    client = paramiko.SSHClient()
    try:
        if settings.known_hosts_file.exists():
            # Подготовленный файл включает проверку ключа. Ошибки чтения,
            # неизвестный сервер и несовпадение ключа останавливают подключение.
            client.load_host_keys(str(settings.known_hosts_file))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # Совместимость со старой версией: без файла принимаем ключ сервера
            # автоматически. Его подлинность в этом режиме не проверяется.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
