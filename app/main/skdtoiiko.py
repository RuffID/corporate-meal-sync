"""Скачать сегодняшний CSV с SFTP и выполнить проверенные начисления."""

from water_sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main("import-sftp"))
