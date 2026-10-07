"""Формирование файла списаний для 1С без частично записанного результата."""

import csv
import io
from decimal import Decimal, InvalidOperation

from .cards import parse_card
from .errors import SyncError
from .storage import atomic_write


def report_rows(transactions):
    rows = []
    for number, transaction in enumerate(transactions, 1):
        try:
            amount = Decimal(str(transaction["payFromWalletSum"]))
            if not amount.is_finite():
                raise InvalidOperation
        except (KeyError, InvalidOperation, ValueError):
            raise SyncError(f"Отчёт iiko, строка {number}: payFromWalletSum должен быть конечным числом.") from None
        if amount <= 0:
            continue
        card = parse_card(transaction.get("guestName"), f"Отчёт iiko, строка {number}, guestName")
        rows.append((card.name, amount))
    return rows


def write_report(path, day, rows):
    if not rows:
        return False
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerow(("PassID", "TransactionDate", "TransactionSumm"))
    for name, amount in rows:
        writer.writerow((name, day.isoformat(), format(amount, "f").replace(".", ",")))
    atomic_write(path, stream.getvalue().encode("utf-8"))
    return True
