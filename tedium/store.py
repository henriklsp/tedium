"""store.py — Data layer.

Responsible for the Task data model and all persistence operations: parsing
tasks from and serialising tasks to the plain-text task file, and applying
startup recurrence logic (day rollover and recurring task injection).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from typing import Optional

SECTION_ORDER = ["Today", "Tomorrow", "Whenever", "Overdue", "Daily", "Weekly", "Monthly", "Annually"]
RECURRING = frozenset({"Daily", "Weekly", "Monthly", "Annually"})

DATE_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2})\]$')
LAST_DATE_RE = re.compile(r'^# last_date: (\d{4}-\d{2}-\d{2})')


@dataclass
class Task:
    """A single task and all its state flags."""

    text: str
    done: bool = False
    urgent: bool = False
    important: bool = False
    next_date: Optional[date] = None

    def to_line(self) -> str:
        """Serialise the task to its canonical plain-text file format."""
        markers = ""
        if self.urgent and self.important:
            markers = "[!*] "
        elif self.urgent:
            markers = "[!] "
        elif self.important:
            markers = "[*] "
        status = "[x] " if self.done else "- "
        date_suffix = f" [{self.next_date.isoformat()}]" if self.next_date is not None else ""
        return f"{status}{markers}{self.text}{date_suffix}"


def _parse_task(line: str) -> Optional[Task]:
    """Parse one line from the task file into a Task.

    Returns None for blank lines, comment lines, or unrecognised formats.
    """
    line = line.strip()
    if not line:
        return None

    done = False
    if line.startswith("[x] ") or line.startswith("[X] "):
        done = True
        line = line[4:]
    elif line.startswith("- "):
        line = line[2:]
    else:
        return None

    urgent = False
    important = False
    m = re.match(r'^\[(!?\*?)\]\s*', line)
    if m and m.group(1):
        marker = m.group(1)
        urgent = "!" in marker
        important = "*" in marker
        line = line[m.end():]

    next_date = None
    dm = DATE_RE.search(line)
    if dm:
        try:
            next_date = date.fromisoformat(dm.group(1))
        except ValueError:
            pass
        line = line[:dm.start()].rstrip()

    return Task(text=line, done=done, urgent=urgent, important=important, next_date=next_date)


_DONE_FILTERED_SECTIONS = frozenset({"Today", "Tomorrow", "Whenever"})


def _remove_done_tasks(sections: dict[str, list[Task]]) -> bool:
    """Remove completed tasks from Today, Tomorrow, and Whenever. Returns True if any were removed."""
    changed = False
    for name in _DONE_FILTERED_SECTIONS:
        if name in sections:
            before = len(sections[name])
            sections[name] = [t for t in sections[name] if not t.done]
            changed = changed or len(sections[name]) != before
    return changed


def load(path: str) -> tuple[dict[str, list[Task]], Optional[date]]:
    """Parse tasks.txt and return (sections, last_date).

    Done tasks in Today, Tomorrow, and Whenever are filtered out on load.
    """
    sections: dict[str, list[Task]] = {s: [] for s in SECTION_ORDER}
    current_section: Optional[str] = None
    last_date: Optional[date] = None

    if not os.path.exists(path):
        return sections, None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            # Metadata line
            m = LAST_DATE_RE.match(line)
            if m:
                try:
                    last_date = date.fromisoformat(m.group(1))
                except ValueError:
                    pass
                continue

            if line.startswith("## "):
                name = line[3:].strip()
                current_section = name
                if name not in sections:
                    sections[name] = []
            elif current_section is not None:
                task = _parse_task(line)
                if task is None:
                    continue
                # Skip done tasks in Today/Tomorrow/Whenever on load
                if task.done and current_section in _DONE_FILTERED_SECTIONS:
                    continue
                # Recurring tasks must always have a due date; default to today
                if current_section in RECURRING and task.next_date is None:
                    task.next_date = date.today()
                sections[current_section].append(task)

    return sections, last_date


def save(path: str, sections: dict[str, list[Task]], today: date) -> None:
    """Atomically write all sections to the task file.

    Prefixes the file with today's date as metadata (used for rollover detection).
    Uses a write-to-.tmp-then-replace strategy to prevent data loss on crash.
    """
    tmp_path = path + ".tmp"
    lines = [f"# last_date: {today.isoformat()}", ""]

    for section_name in SECTION_ORDER:
        tasks = sections.get(section_name, [])
        lines.append(f"## {section_name}")
        for task in tasks:
            lines.append(task.to_line())
        lines.append("")

    # Write any extra sections not in the standard order
    for section_name, tasks in sections.items():
        if section_name not in SECTION_ORDER:
            lines.append(f"## {section_name}")
            for task in tasks:
                lines.append(task.to_line())
            lines.append("")

    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    os.replace(tmp_path, path)


def next_date_for(section: str, current: date) -> date:
    """Return the next recurrence date for a section after current."""
    if section == "Daily":
        return current + timedelta(days=1)
    elif section == "Weekly":
        return current + timedelta(weeks=1)
    elif section == "Monthly":
        return current + relativedelta(months=1)
    elif section == "Annually":
        return current + relativedelta(years=1)
    return current


def next_weekday_date(weekday: int) -> date:
    """Return the next date falling on weekday (0=Monday … 6=Sunday), strictly after today.

    Choosing the current weekday advances a full week rather than returning today.
    """
    today = date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def next_month_date(month: int) -> date:
    """Return the next 1st-of-month for the given month number (1–12), strictly after today.

    If the 1st of that month this calendar year is already past (or is today),
    the result falls in the following year.
    """
    today = date.today()
    candidate = date(today.year, month, 1)
    if candidate <= today:
        candidate = date(today.year + 1, month, 1)
    return candidate


def _apply_day_rollover(
    sections: dict[str, list[Task]], last_date: Optional[date], today: date
) -> bool:
    """Apply Today/Tomorrow rollover when the calendar date has advanced.

    Removes completed tasks from Today, Tomorrow, and Whenever first. Then:
    important Today tasks are kept, unimportant tasks move to Overdue (with an
    expiry date based on their recurring origin), except Daily-recurring tasks
    which are simply dropped. All Tomorrow tasks move into Today.
    Returns True if any mutation was made.

    Rollover is skipped if last_date is None (first launch) or in the future
    (e.g. manually edited file).
    """
    if last_date is None or last_date >= today:
        return False

    _remove_done_tasks(sections)

    daily_texts = {t.text for t in sections.get("Daily", [])}
    weekly_texts = {t.text for t in sections.get("Weekly", [])}
    monthly_annual_texts = {
        t.text
        for t in sections.get("Monthly", []) + sections.get("Annually", [])
    }

    overdue: list[Task] = sections.get("Overdue", [])
    existing_overdue = {t.text for t in overdue}
    original_overdue_len = len(overdue)

    today_kept = []
    for task in sections.get("Today", []):
        if task.important:
            today_kept.append(task)
        elif task.text in daily_texts:
            pass  # daily recurring tasks are dropped on rollover, not moved to Overdue
        elif task.text not in existing_overdue:
            expiry: Optional[date] = None
            if task.text in weekly_texts:
                expiry = today + timedelta(days=2)
            elif task.text in monthly_annual_texts:
                expiry = today + timedelta(days=7)
            overdue.append(Task(
                text=task.text, done=False, urgent=False, important=False,
                next_date=expiry,
            ))

    tomorrow_tasks = sections.get("Tomorrow", [])
    changed = (
        len(today_kept) != len(sections.get("Today", []))
        or bool(tomorrow_tasks)
        or len(overdue) != original_overdue_len
    )
    sections["Today"] = today_kept + tomorrow_tasks
    sections["Tomorrow"] = []
    sections["Overdue"] = overdue
    return changed


def _clean_overdue(sections: dict[str, list[Task]], today: date) -> bool:
    """Remove expired Overdue tasks (those whose next_date has passed). Returns True if any were removed."""
    overdue = sections.get("Overdue", [])
    kept = [t for t in overdue if t.next_date is None or t.next_date > today]
    if len(kept) != len(overdue):
        sections["Overdue"] = kept
        return True
    return False


def task_priority(task: Task) -> int:
    """Return a sort key for a task's priority (lower = higher priority).

    Used to decide which of two duplicate tasks to keep when deduplicating.
    """
    if task.urgent and task.important:
        return 0
    if task.urgent:
        return 1
    if task.important:
        return 2
    return 3


def _inject_recurring_tasks(
    sections: dict[str, list[Task]], today: date
) -> bool:
    """Copy recurring tasks into Today or Tomorrow based on their schedule.

    Daily tasks are injected into Today only when due today or overdue.
    Weekly/Monthly/Annually tasks appear in Tomorrow the day before they are
    due, then in Today on or after the due date (rollover moves Tomorrow→Today).
    Each source task's next_date is advanced past tomorrow to prevent
    re-injection within the same session. Returns True if any task was injected.
    """
    tomorrow = today + timedelta(days=1)
    changed = False
    for section_name in (s for s in SECTION_ORDER if s in RECURRING):
        for task in sections.get(section_name, []):
            if task.next_date is None:
                continue
            # Daily tasks go straight to Today only (no day-early lookahead).
            if section_name == "Daily":
                if task.next_date > today:
                    continue
                target = "Today"
            else:
                # Weekly/Monthly/Annually: appear in Tomorrow the day before,
                # then in Today on the due date (rollover moves Tomorrow→Today).
                if task.next_date > tomorrow:
                    continue
                target = "Today" if task.next_date <= today else "Tomorrow"
            new_task = Task(text=task.text, done=False, urgent=task.urgent, important=task.important)
            existing = next((t for t in sections[target] if t.text == task.text), None)
            if existing is None:
                sections[target].append(new_task)
                changed = True
            elif task_priority(new_task) < task_priority(existing):
                sections[target].remove(existing)
                sections[target].append(new_task)
                changed = True
            # else: existing is same or higher priority — skip injection
            # Advance next_date so the task isn't re-injected this session.
            # Daily: always advance to tomorrow (don't skip days on catch-up).
            # Others: advance past tomorrow so the Tomorrow slot isn't re-filled.
            if section_name == "Daily":
                task.next_date = tomorrow
            else:
                nd = task.next_date
                while nd <= tomorrow:
                    nd = next_date_for(section_name, nd)
                task.next_date = nd
    return changed


def check_recurrences(
    sections: dict[str, list[Task]], last_date: Optional[date] = None
) -> tuple[dict[str, list[Task]], bool]:
    """Apply all startup recurrence logic and return (sections, changed).

    Runs day-rollover, recurring task injection, and Overdue expiry cleanup
    in sequence. changed is True if any mutation was made.
    """
    today = date.today()
    rolled = _apply_day_rollover(sections, last_date, today)
    injected = _inject_recurring_tasks(sections, today)
    cleaned = _clean_overdue(sections, today)
    return sections, rolled or injected or cleaned
