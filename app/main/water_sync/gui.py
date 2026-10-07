"""Небольшой GUI над теми же сценариями, что использует планировщик."""

import sys
from datetime import date, timedelta

from PyQt5 import QtCore, QtWidgets

from .cli import run_job
from .errors import SyncError


class JobThread(QtCore.QThread):
    succeeded = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task, parent):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            self.succeeded.emit(self.task())
        except (SyncError, OSError) as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(f"Непредвиденная ошибка {type(error).__name__}. Подробности в Logs.")


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Чистая вода — СКД / iikoCard / 1С")
        self.resize(540, 380)
        self.worker = None
        self.report_window = None
        self.buttons = []
        layout = QtWidgets.QVBoxLayout(self)
        self.add_button(layout, "Загрузить карты за сегодня с SFTP", lambda: self.start_job(lambda: run_job("import-sftp")))
        self.add_button(layout, "Загрузить карты за сегодня из локального CSV", lambda: self.start_job(lambda: run_job("import-local")))
        self.add_button(layout, "Выгрузить списания за сегодня в 1С", lambda: self.start_job(lambda: run_job("export", date.today())))
        self.export_day = self.date_edit()
        layout.addWidget(self.export_day)
        self.add_button(layout, "Выгрузить списания за выбранный день", self.export_selected)
        periods = QtWidgets.QFormLayout()
        self.start_day, self.end_day = self.date_edit(), self.date_edit()
        periods.addRow("Отчёт с", self.start_day)
        periods.addRow("По день включительно", self.end_day)
        layout.addLayout(periods)
        self.add_button(layout, "Показать отчёт по списаниям", self.show_report)
        self.status = QtWidgets.QLabel("Готово к работе. Начисления защищены журналом операций.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def date_edit(self):
        control = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        control.setDisplayFormat("dd.MM.yyyy")
        control.setCalendarPopup(True)
        return control

    def add_button(self, layout, title, callback):
        button = QtWidgets.QPushButton(title)
        button.clicked.connect(callback)
        layout.addWidget(button)
        self.buttons.append(button)

    def export_selected(self):
        day = self.export_day.date().toPyDate()
        self.start_job(lambda: run_job("export", day))

    def show_report(self):
        start, end = self.start_day.date().toPyDate(), self.end_day.date().toPyDate()
        if end < start:
            QtWidgets.QMessageBox.warning(self, "Период отчёта", "Дата окончания раньше даты начала.")
            return
        self.start_job(lambda: run_job("report", start=start, end=end + timedelta(days=1)), self.display_report)

    def start_job(self, task, on_success=None):
        if self.worker is not None:
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.status.setText("Выполняется операция. Не закрывайте окно до завершения.")
        self.worker = JobThread(task, self)
        self.worker.succeeded.connect(on_success or self.display_result)
        self.worker.failed.connect(self.display_error)
        self.worker.finished.connect(self.finish_job)
        self.worker.start()

    def finish_job(self):
        self.worker.deleteLater()
        self.worker = None
        for button in self.buttons:
            button.setEnabled(True)

    def display_result(self, result):
        if "completed" in result:
            text = f"Импорт завершён: начислено {result['completed']}, уже подтверждено {result['skipped']}, пропущено из-за ошибок {result['rejected']}, всего {result['total']}."
        else:
            text = f"Выгрузка завершена: строк {result['exported']}."
        self.status.setText(text)

    def display_error(self, message):
        self.status.setText("Операция остановлена. Проверьте сообщение и журнал; не повторяйте неизвестное начисление без сверки.")
        QtWidgets.QMessageBox.critical(self, "Ошибка обмена", message)

    def display_report(self, rows):
        self.report_window = QtWidgets.QDialog(self)
        self.report_window.setWindowTitle("Списания из iikoCard")
        self.report_window.resize(560, 420)
        layout = QtWidgets.QVBoxLayout(self.report_window)
        table = QtWidgets.QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(("Идентификатор пропуска", "Списано, ₽"))
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        for index, (name, amount) in enumerate(rows):
            table.setItem(index, 0, QtWidgets.QTableWidgetItem(name))
            table.setItem(index, 1, QtWidgets.QTableWidgetItem(str(amount)))
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(table)
        layout.addWidget(QtWidgets.QLabel(f"Итого: {sum(amount for _, amount in rows)} ₽"))
        self.status.setText(f"Отчёт получен: строк {len(rows)}.")
        self.report_window.show()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            event.accept()


def main():
    application = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return application.exec_()
