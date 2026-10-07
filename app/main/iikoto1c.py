"""Выгрузить вчерашние списания iikoCard в CSV для 1С и отправить на SFTP."""

from water_sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main("export"))
