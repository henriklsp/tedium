from __future__ import annotations

# store.py
# Data layer.
# Responsible for the Task data model and all persistence operations: parsing
# tasks from and serialising tasks to the plain-text task file, and applying
# startup recurrence logic (day rollover and recurring task injection).

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from typing import Optional

SECTION_ORDER = ["Today", "Tomorrow", "Whenever", "Daily", "Weekly", "Monthly", "Annually"]
RECURRING = frozenset({"Daily", "Weekly", "Monthly", "Annually"})

DATE_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2})\]$')
LAST_DATE_RE = re.compile(r'^# last_date: (\d{4}-\d{2}-\d{2})')


# Represents a single task and all its state flags.
@dataclass
class Task:
    text: str
    done: bool = False
    urgent: bool = False
    important: bool = False
    next_date: Optional[date] = None

    # Serialises the task to its canonical plain-text file format.
    def to_line(self) -> str:
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


# Parses a single line from the task file into a Task object.
# Returns None for blank lines, comment lines, or lines with an unrecognised format.
def _parse_task(line: str) -> Optional[Task]:
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


# Reads the task file and returns all sections together with the last-saved date.
# Done tasks in Today and Tomorrow are filtered out on load — they implement
# the "completed tasks disappear the next day" behaviour without modifying the file.
def load(path: str) -> tuple[dict[str, list[Task]], Optional[date]]:
    """Parse tasks.txt. Returns (sections, last_date).

    Done tasks in Today and Tomorrow are filtered out — they are hidden on next launch.
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
                # Skip done tasks in Today/Tomorrow — hidden on next launch
                if task.done and current_section in ("Today", "Tomorrow"):
                    continue
                # Recurring tasks must always have a due date; default to today
                if current_section in RECURRING and task.next_date is None:
                    task.next_date = date.today()
                sections[current_section].append(task)

    return sections, last_date


# Atomically writes all sections to the task file, prefixed with today's date as metadata.
# Uses a .tmp write-then-replace strategy to prevent data loss on crash.
def save(path: str, sections: dict[str, list[Task]], today: date) -> None:
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


# Computes the next recurrence date for a section by advancing the given date
# by the section's recurrence interval (daily, weekly, monthly, or annually).
def next_date_for(section: str, current: date) -> date:
    if section == "Daily":
        return current + timedelta(days=1)
    elif section == "Weekly":
        return current + timedelta(weeks=1)
    elif section == "Monthly":
        return current + relativedelta(months=1)
    elif section == "Annually":
        return current + relativedelta(years=1)
    return current


# Returns the next calendar date that falls on the given weekday (0=Monday,
# 6=Sunday), always strictly after today — so clicking the current weekday
# advances a full week rather than returning today.
def next_weekday_date(weekday: int) -> date:
    today = date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


# Returns the next 1st-of-month for the given month number (1=January,
# 12=December), always strictly after today. If the 1st of that month this
# calendar year is already past (or is today), the result falls in the next year.
def next_month_date(month: int) -> date:
    today = date.today()
    candidate = date(today.year, month, 1)
    if candidate <= today:
        candidate = date(today.year + 1, month, 1)
    return candidate


# Handles the Today/Tomorrow day-rollover when the calendar date has advanced
# since last launch. Drops non-important Today tasks (they expired with the
# previous day), then moves all Tomorrow tasks into Today. Returns True if
# any mutation was made.
def _apply_day_rollover(
    sections: dict[str, list[Task]], last_date: Optional[date], today: date
) -> bool:
    # If last_date is in the future (e.g. manually edited file), rollover is
    # skipped until that date passes. If None, this is a first launch.
    if last_date is None or last_date >= today:
        return False

    today_kept = [t for t in sections.get("Today", []) if t.important]
    tomorrow_tasks = sections.get("Tomorrow", [])
    changed = len(today_kept) != len(sections.get("Today", [])) or bool(tomorrow_tasks)
    sections["Today"] = today_kept + tomorrow_tasks
    sections["Tomorrow"] = []
    return changed


# Returns a sort key for a task's priority; lower = higher priority.
# Used to decide which of two duplicate tasks to keep.
def task_priority(task: Task) -> int:
    if task.urgent and task.important:
        return 0
    if task.urgent:
        return 1
    if task.important:
        return 2
    return 3


# Copies recurring tasks into Today or Tomorrow based on their schedule.
# Daily tasks are injected into Today only when due today or overdue.
# Weekly/Monthly/Annually tasks are injected into Tomorrow the day before
# they are due, and into Today on or after the due date. Advances each
# source task's next_date past tomorrow. Returns True if any task was injected.
def _inject_recurring_tasks(
    sections: dict[str, list[Task]], today: date
) -> bool:
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
            # Daily: always set to tomorrow (never skip a day due to catch-up).
            # Others: advance past tomorrow so the Tomorrow slot isn't re-filled.
            if section_name == "Daily":
                task.next_date = tomorrow
            else:
                nd = task.next_date
                while nd <= tomorrow:
                    nd = next_date_for(section_name, nd)
                task.next_date = nd
    return changed


# Applies startup recurrence logic. Coordinates day-rollover and recurring
# injection, returning the updated sections and a flag indicating any mutations.
def check_recurrences(
    sections: dict[str, list[Task]], last_date: Optional[date] = None
) -> tuple[dict[str, list[Task]], bool]:
    today = date.today()
    rolled = _apply_day_rollover(sections, last_date, today)
    injected = _inject_recurring_tasks(sections, today)
    return sections, rolled or injected
