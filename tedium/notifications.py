from __future__ import annotations

# notifications.py
# In-memory notification scheduling for Today tasks.
#
# A "work session" begins when the app opens or when a wake-from-sleep is
# detected via the heartbeat timer. Two checks fire per session:
#   - 2 h after session start: notify if Today has urgent uncompleted tasks.
#   - At min(session_start + 5 h, 14:00): notify if Today has important
#     (non-urgent) uncompleted tasks.  The 14:00 cap only applies when the
#     session started before 14:00; otherwise the 5 h rule governs.
# Nothing is persisted — state resets on every app launch.

from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

from .store import Task

_URGENT_DELAY = timedelta(hours=2)
_IMPORTANT_DELAY = timedelta(hours=5)
_IMPORTANT_DEADLINE = dtime(14, 0)
_HEARTBEAT_INTERVAL_MS = 30_000  # 30 s — short enough to detect wakes quickly
_WAKE_GAP_SECONDS = 120          # gap > 2 min between heartbeats → assume sleep/wake


class NotificationManager(QObject):
    """Schedules and fires Today-task notifications for the current work session."""

    def __init__(self, icon: QIcon, parent: Optional[QObject] = None):
        super().__init__(parent)

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setVisible(True)

        self._today_tasks: list[Task] = []
        self._work_start: datetime = datetime.now()
        self._urgent_notified = False
        self._important_notified = False
        self._last_heartbeat: datetime = datetime.now()

        # Heartbeat: detects sleep/wake cycles.
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_INTERVAL_MS)
        self._heartbeat.timeout.connect(self._on_heartbeat)
        self._heartbeat.start()

        # Check timer: evaluates notification conditions every minute.
        self._check_timer = QTimer(self)
        self._check_timer.setInterval(60_000)
        self._check_timer.timeout.connect(self._check)
        self._check_timer.start()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update_today_tasks(self, tasks: list[Task]) -> None:
        """Called by the UI whenever Today's task list changes."""
        self._today_tasks = list(tasks)

    def notify_overdue_urgent(self, task_texts: list[str]) -> None:
        """Immediately notify that urgent tasks were moved to the Overdue section."""
        if task_texts:
            self._show(
                "Urgent tasks moved to overdue",
                " · ".join(task_texts[:3]),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _new_session(self) -> None:
        """Reset per-session state for a new work session."""
        self._work_start = datetime.now()
        self._urgent_notified = False
        self._important_notified = False

    def _on_heartbeat(self) -> None:
        now = datetime.now()
        gap = (now - self._last_heartbeat).total_seconds()
        self._last_heartbeat = now
        # A gap significantly larger than the heartbeat interval means the
        # system was suspended. Treat the wake time as a new session start.
        if gap > _WAKE_GAP_SECONDS:
            self._new_session()
            self._check()  # run immediately after wake

    def _check(self) -> None:
        now = datetime.now()
        elapsed = now - self._work_start

        # --- Urgent: 2 h after session start ---
        if not self._urgent_notified and elapsed >= _URGENT_DELAY:
            urgent = [t for t in self._today_tasks if t.urgent and not t.done]
            if urgent:
                self._show(
                    "Urgent tasks pending",
                    " · ".join(t.text for t in urgent[:3]),
                )
            self._urgent_notified = True  # mark even if empty, to avoid repeats

        # --- Important (non-urgent): 5 h after session start, capped at 14:00 ---
        if not self._important_notified:
            at_5h = elapsed >= _IMPORTANT_DELAY
            # The 14:00 deadline only applies when the session started before 14:00,
            # so we don't fire immediately for late-day sessions.
            at_deadline = (
                now.time() >= _IMPORTANT_DEADLINE
                and self._work_start.time() < _IMPORTANT_DEADLINE
            )
            if at_5h or at_deadline:
                important = [
                    t for t in self._today_tasks
                    if t.important and not t.urgent and not t.done
                ]
                if important:
                    self._show(
                        "Important tasks pending",
                        " · ".join(t.text for t in important[:3]),
                    )
                self._important_notified = True

    def _show(self, title: str, body: str) -> None:
        self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 6000)
