"""Обработать локальный CSV по тем же правилам, что файл с SFTP."""

from water_sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main("import-local"))
