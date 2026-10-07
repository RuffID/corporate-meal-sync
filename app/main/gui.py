"""Графический интерфейс обмена; бизнес-логика находится в water_sync."""

import sys


if __name__ == "__main__":
    try:
        from water_sync.gui import main
    except ImportError as error:
        print(f"Не удалось загрузить GUI: отсутствует зависимость {error.name}. Установите requirements-gui.txt.", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(main())
