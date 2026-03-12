from __future__ import annotations

# main.py
# Application entry point.
# Responsible for wiring the data layer (store) and the presentation layer (ui):
# resolves the task file path, loads persisted data, triggers startup recurrence
# logic, and hands control to the Qt event loop.

import os
import sys
from datetime import date

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import store
from .ui import MainWindow, STYLESHEET


def get_tasks_path() -> str:
    return os.path.join(os.path.expanduser("~"), "tasks.txt")


def get_icon() -> QIcon:
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "assets")
    else:
        base = os.path.join(os.path.dirname(__file__), "..", "assets")
    return QIcon(os.path.join(base, "tedium.ico"))


# Orchestrates application startup: creates the Qt application, loads and
# prepares task data, constructs the main window, and enters the event loop.
def main():
    app = QApplication(sys.argv)
    icon = get_icon()
    app.setWindowIcon(icon)
    app.setStyleSheet(STYLESHEET)

    path = get_tasks_path()
    sections, last_date = store.load(path)
    sections, _ = store.check_recurrences(sections, last_date)
    # Always save on startup to record today's date (enables rollover detection tomorrow)
    store.save(path, sections, date.today())

    # Closure that captures path so callers need not know the storage location.
    def save_callback(secs):
        store.save(path, secs, date.today())

    window = MainWindow(sections, save_callback, last_date)
    window.setWindowIcon(icon)
    window.show()

    screen = app.primaryScreen().availableGeometry()
    window.move(screen.right() - window.width(), screen.top())

    sys.exit(app.exec())
