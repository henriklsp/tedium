from __future__ import annotations

# main.py
# Application entry point.
# Responsible for wiring the data layer (store) and the presentation layer (ui):
# resolves the task file path, loads persisted data, triggers startup recurrence
# logic, and hands control to the Qt event loop.

import os
import sys
from datetime import date

from PySide6.QtWidgets import QApplication

import store
from ui import MainWindow, STYLESHEET


# Returns the absolute path to the tasks file.
# In a frozen (PyInstaller) build the file lives beside the executable;
# in development it lives in the user's home directory.
def get_tasks_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.expanduser("~")
    return os.path.join(base, "tasks.txt")


# Orchestrates application startup: creates the Qt application, loads and
# prepares task data, constructs the main window, and enters the event loop.
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    path = get_tasks_path()
    sections, last_date = store.load(path)
    sections, _ = store.check_recurrences(sections, last_date)
    # Always save on startup to record today's date (enables rollover detection tomorrow)
    store.save(path, sections, date.today())

    # Closure that captures path so callers need not know the storage location.
    def save_callback(secs):
        store.save(path, secs, date.today())

    window = MainWindow(sections, save_callback)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
