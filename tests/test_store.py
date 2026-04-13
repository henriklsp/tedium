"""
test_store.py
BDD-style unit tests for store.py (the data layer).

Each test describes one observable behaviour of the system using
Given / When / Then structure. Tests verify behaviour, not implementation.

Run with:  pytest tests/test_store.py  (from repo root)
"""

import os
import tempfile
from datetime import date, timedelta

import pytest

from tedium.store import (
    Task,
    _parse_task,
    check_recurrences,
    load,
    next_date_for,
    next_weekday_date,
    next_month_date,
    save,
    SECTION_ORDER,
    RECURRING,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def blank_sections(**overrides) -> dict:
    """Return a fully-populated sections dict with every section empty,
    then apply any keyword overrides (section_name=[...tasks...])."""
    sections = {s: [] for s in SECTION_ORDER}
    sections.update(overrides)
    return sections


# ===========================================================================
# FEATURE: Serialising a task to a plain-text line
# ===========================================================================

class DescribeTaskSerialisation:
    """A Task can be written to a human-readable plain-text line."""

    def prefix_plain_task_with_dash(self):
        """
        Given a task with no flags set
        When serialised to a line
        Then the line is "- <text>"
        """
        assert Task(text="Buy groceries").to_line() == "- Buy groceries"

    def mark_done_task_with_x_no_dash(self):
        """
        Given a task marked done
        When serialised
        Then the line starts with "[x] " (no dash)
        """
        assert Task(text="Buy groceries", done=True).to_line() == "[x] Buy groceries"

    def insert_urgent_marker_between_prefix_and_text(self):
        """
        Given an urgent task
        When serialised
        Then "[!] " appears between the "- " prefix and the text
        """
        assert Task(text="Fix bug", urgent=True).to_line() == "- [!] Fix bug"

    def insert_important_marker_between_prefix_and_text(self):
        """
        Given an important task
        When serialised
        Then "[*] " appears between the "- " prefix and the text
        """
        assert Task(text="Review PR", important=True).to_line() == "- [*] Review PR"

    def use_combined_marker_for_urgent_and_important(self):
        """
        Given a task that is both urgent and important
        When serialised
        Then a single "[!*] " marker appears, not two separate markers
        """
        assert Task(text="Deploy", urgent=True, important=True).to_line() == "- [!*] Deploy"

    def combine_done_with_urgent_marker(self):
        """
        Given a done task that is also urgent
        When serialised
        Then the line reads "[x] [!] <text>"
        """
        assert Task(text="T", done=True, urgent=True).to_line() == "[x] [!] T"

    def combine_done_with_both_markers(self):
        """
        Given a done task that is urgent and important
        When serialised
        Then the line reads "[x] [!*] <text>"
        """
        assert Task(text="T", done=True, urgent=True, important=True).to_line() == "[x] [!*] T"

    def append_date_with_space_in_brackets(self):
        """
        Given a task with a scheduled date
        When serialised
        Then the date appears at the end as " [YYYY-MM-DD]"
        """
        line = Task(text="Walk", next_date=date(2026, 3, 10)).to_line()
        assert line == "- Walk [2026-03-10]"
        assert " [2026-03-10]" in line

    def produce_no_bracket_without_date(self):
        """
        Given a task with no scheduled date
        When serialised
        Then the line contains no bracket character
        """
        assert "[" not in Task(text="Something").to_line()

    def combine_done_with_date(self):
        """
        Given a done task with a date
        When serialised
        Then the line reads "[x] <text> [date]"
        """
        assert Task(text="Walk", done=True, next_date=date(2026, 3, 10)).to_line() == "[x] Walk [2026-03-10]"

    def combine_urgent_with_date(self):
        """
        Given an urgent task with a date
        When serialised
        Then both the marker and the date appear in the correct positions
        """
        assert Task(text="Run", urgent=True, next_date=date(2026, 3, 10)).to_line() == "- [!] Run [2026-03-10]"

    def handle_leap_day_date(self):
        """
        Given a task scheduled on Feb 29 of a leap year
        When serialised
        Then the date is written as "2024-02-29"
        """
        assert Task(text="Leap", next_date=date(2024, 2, 29)).to_line() == "- Leap [2024-02-29]"


# ===========================================================================
# FEATURE: Parsing a plain-text line into a Task
# ===========================================================================

class DescribeTaskLineParsing:
    """The store can parse hand-written or serialised lines back into Tasks."""

    # --- lines that produce no task ---

    def ignore_empty_lines(self):
        """
        Given an empty string
        When parsed
        Then no task is returned
        """
        assert _parse_task("") is None

    def ignore_whitespace_lines(self):
        """
        Given a line containing only spaces
        When parsed
        Then no task is returned
        """
        assert _parse_task("   ") is None

    def ignore_comment_lines(self):
        """
        Given a metadata comment line (e.g. "# last_date: …")
        When parsed
        Then no task is returned
        """
        assert _parse_task("# last_date: 2026-03-06") is None

    def ignore_lines_without_task_prefix(self):
        """
        Given a plain line of text with no "- " or "[x] " prefix
        When parsed
        Then no task is returned
        """
        assert _parse_task("Buy groceries") is None

    def ignore_section_headers(self):
        """
        Given a section header line ("## Today")
        When parsed
        Then no task is returned
        """
        assert _parse_task("## Today") is None

    # --- done flag ---

    def mark_done_for_lowercase_x(self):
        """
        Given a line starting with "[x] "
        When parsed
        Then done is True
        """
        t = _parse_task("[x] Buy groceries")
        assert t is not None and t.done and t.text == "Buy groceries"

    def mark_done_for_uppercase_x(self):
        """
        Given a line starting with "[X] "
        When parsed
        Then done is True (both cases accepted)
        """
        t = _parse_task("[X] Buy groceries")
        assert t is not None and t.done

    def leave_done_false_for_dash_prefix(self):
        """
        Given a line starting with "- "
        When parsed
        Then done is False
        """
        t = _parse_task("- Buy groceries")
        assert t is not None and not t.done and t.text == "Buy groceries"

    # --- priority markers ---

    def set_urgent_for_exclamation_marker(self):
        """
        Given a line with the "[!] " marker
        When parsed
        Then urgent is True and important is False
        """
        t = _parse_task("- [!] Fix bug")
        assert t is not None
        assert t.urgent and not t.important
        assert t.text == "Fix bug"

    def set_important_for_star_marker(self):
        """
        Given a line with the "[*] " marker
        When parsed
        Then important is True and urgent is False
        """
        t = _parse_task("- [*] Review PR")
        assert t is not None
        assert t.important and not t.urgent
        assert t.text == "Review PR"

    def set_both_flags_for_combined_marker(self):
        """
        Given a line with the "[!*] " marker
        When parsed
        Then both urgent and important are True
        """
        t = _parse_task("- [!*] Deploy now")
        assert t is not None
        assert t.urgent and t.important
        assert t.text == "Deploy now"

    def reject_markers_in_wrong_order(self):
        """
        Given a line with "[*!] " (reversed order)
        When parsed
        Then neither flag is set — the format is not recognised
        """
        t = _parse_task("- [*!] text")
        assert t is not None
        assert not t.urgent and not t.important

    def leave_empty_brackets_in_text(self):
        """
        Given a line with "[] " (empty brackets)
        When parsed
        Then neither flag is set and the brackets remain in the text
        (empty brackets are not a recognised marker format)
        """
        t = _parse_task("- [] text")
        assert t is not None
        assert not t.urgent and not t.important
        assert "[]" in t.text

    # --- date suffix ---

    def parse_trailing_date_into_next_date(self):
        """
        Given a line ending with " [YYYY-MM-DD]"
        When parsed
        Then next_date is set to that date and the bracket is not in the text
        """
        t = _parse_task("- Walk [2026-03-10]")
        assert t is not None
        assert t.text == "Walk"
        assert t.next_date == date(2026, 3, 10)

    def ignore_date_not_at_end_of_line(self):
        """
        Given a line where a date bracket appears in the middle (not at end)
        When parsed
        Then next_date is None and the bracket stays in the task text
        """
        t = _parse_task("- Walk [2026-03-10] extra")
        assert t is not None
        assert t.next_date is None
        assert "[2026-03-10]" in t.text

    def leave_non_date_brackets_in_text(self):
        """
        Given a line ending with a bracket that does not contain a valid date
        When parsed
        Then next_date is None and the bracket content remains in the text
        """
        t = _parse_task("- Walk [not-a-date]")
        assert t is not None
        assert t.next_date is None
        assert "[not-a-date]" in t.text

    def ignore_impossible_calendar_date(self):
        """
        Given a line ending with a bracket containing an impossible date
        (e.g. Feb 29 in a non-leap year)
        When parsed
        Then next_date is None and the text is trimmed up to the bracket
        """
        t = _parse_task("- Walk [2023-02-29]")
        assert t is not None
        assert t.next_date is None
        assert t.text == "Walk"

    def accept_feb_29_on_leap_year(self):
        """
        Given a line ending with "[2024-02-29]" (2024 is a leap year)
        When parsed
        Then next_date is set to 2024-02-29
        """
        t = _parse_task("- Walk [2024-02-29]")
        assert t is not None
        assert t.next_date == date(2024, 2, 29)

    def parse_all_flags_and_date_together(self):
        """
        Given a line with done, urgent+important markers, text, and a date
        When parsed
        Then all fields are extracted correctly
        """
        t = _parse_task("[x] [!*] Run [2026-03-10]")
        assert t is not None
        assert t.done and t.urgent and t.important
        assert t.text == "Run"
        assert t.next_date == date(2026, 3, 10)

    def strip_surrounding_whitespace(self):
        """
        Given a line with surrounding whitespace
        When parsed
        Then the task text has no leading or trailing spaces
        """
        t = _parse_task("  - Buy groceries  ")
        assert t is not None
        assert t.text == "Buy groceries"


# ===========================================================================
# FEATURE: Serialise / parse round-trip fidelity
# ===========================================================================

class DescribeSerialiseParseRoundTrip:
    """A Task serialised to a line and then parsed back must be identical
    to the original — the two operations are exact inverses."""

    def _rt(self, task: Task) -> Task:
        return _parse_task(task.to_line())

    def preserve_plain_task(self):
        t = Task(text="Buy groceries")
        assert self._rt(t) == t

    def preserve_done_flag(self):
        t = Task(text="Buy groceries", done=True)
        assert self._rt(t) == t

    def preserve_urgent_flag(self):
        t = Task(text="Fix bug", urgent=True)
        assert self._rt(t) == t

    def preserve_important_flag(self):
        t = Task(text="Review PR", important=True)
        assert self._rt(t) == t

    def preserve_both_priority_flags(self):
        t = Task(text="Deploy", urgent=True, important=True)
        assert self._rt(t) == t

    def preserve_scheduled_date(self):
        t = Task(text="Walk", next_date=date(2026, 3, 10))
        assert self._rt(t) == t

    def preserve_all_fields_at_once(self):
        t = Task(text="Run", done=True, urgent=True, important=True, next_date=date(2026, 3, 10))
        assert self._rt(t) == t


# ===========================================================================
# FEATURE: Recurrence date arithmetic
# ===========================================================================

class DescribeRecurrenceDateArithmetic:
    """next_date_for returns the next due date for a recurring task."""

    def setup_method(self):
        self.base = date(2026, 3, 6)  # Friday

    def advance_daily_by_one_day(self):
        """
        Given a Daily task due on a Friday
        When the next date is computed
        Then it falls on the following Saturday
        """
        assert next_date_for("Daily", self.base) == date(2026, 3, 7)

    def advance_weekly_by_seven_days(self):
        """
        Given a Weekly task
        When the next date is computed
        Then it falls exactly one week later
        """
        assert next_date_for("Weekly", self.base) == date(2026, 3, 13)

    def advance_monthly_by_one_month(self):
        """
        Given a Monthly task
        When the next date is computed
        Then it falls on the same day-of-month in the following month
        """
        assert next_date_for("Monthly", self.base) == date(2026, 4, 6)

    def advance_annually_by_one_year(self):
        """
        Given an Annually task
        When the next date is computed
        Then it falls on the same date in the following year
        """
        assert next_date_for("Annually", self.base) == date(2027, 3, 6)

    def return_same_date_for_unknown_section(self):
        """
        Given an unknown section name
        When the next date is computed
        Then the original date is returned unchanged
        """
        assert next_date_for("Whenever", self.base) == self.base
        assert next_date_for("", self.base) == self.base

    def clamp_monthly_jan_31_to_feb_28(self):
        """
        Given a Monthly task due on Jan 31
        When the next date is computed
        Then it clamps to Feb 28, not Mar 1
        """
        assert next_date_for("Monthly", date(2026, 1, 31)) == date(2026, 2, 28)

    def clamp_monthly_mar_31_to_apr_30(self):
        """
        Given a Monthly task due on Mar 31
        When the next date is computed
        Then it clamps to Apr 30, not May 1
        """
        assert next_date_for("Monthly", date(2026, 3, 31)) == date(2026, 4, 30)

    def clamp_annual_leap_day_to_feb_28(self):
        """
        Given an Annually task due on Feb 29 of a leap year (2024)
        When the next date is computed for the following non-leap year
        Then it clamps to Feb 28
        """
        assert next_date_for("Annually", date(2024, 2, 29)) == date(2025, 2, 28)

    def advance_weekly_across_year_boundary(self):
        """
        Given a Weekly task due on Dec 28
        When the next date is computed
        Then it falls on Jan 4 of the following year
        """
        assert next_date_for("Weekly", date(2025, 12, 28)) == date(2026, 1, 4)


# ===========================================================================
# FEATURE: Scheduling the next occurrence of a weekday
# ===========================================================================

class DescribeNextWeekdayScheduling:
    """next_weekday_date returns the next calendar date for a given weekday,
    always strictly after today."""

    def setup_method(self):
        self.today = date.today()

    def always_return_future_date(self):
        """
        Given any weekday number (0–6)
        When the next occurrence is computed
        Then the result is strictly after today
        """
        for weekday in range(7):
            assert next_weekday_date(weekday) > self.today

    def return_correct_weekday(self):
        """
        Given a specific weekday number
        When the next occurrence is computed
        Then the resulting date falls on that weekday
        """
        for weekday in range(7):
            assert next_weekday_date(weekday).weekday() == weekday

    def never_more_than_seven_days_away(self):
        """
        Given any weekday
        When the next occurrence is computed
        Then it is at most 7 days from today
        """
        for weekday in range(7):
            assert next_weekday_date(weekday) <= self.today + timedelta(days=7)

    def advance_full_week_for_todays_weekday(self):
        """
        Given the weekday number of today
        When the next occurrence is computed
        Then it is exactly 7 days from today (not today itself)
        """
        assert next_weekday_date(self.today.weekday()) == self.today + timedelta(days=7)

    def return_tomorrow_for_tomorrows_weekday(self):
        """
        Given the weekday that falls tomorrow
        When the next occurrence is computed
        Then it is exactly 1 day from today
        """
        tomorrow_weekday = (self.today.weekday() + 1) % 7
        assert next_weekday_date(tomorrow_weekday) == self.today + timedelta(days=1)


# ===========================================================================
# FEATURE: Scheduling the next first-of-month
# ===========================================================================

class DescribeNextMonthScheduling:
    """next_month_date returns the next 1st-of-month for a given month,
    always strictly after today."""

    def setup_method(self):
        self.today = date.today()

    def always_return_future_date(self):
        """
        Given any month number (1–12)
        When the next 1st-of-month is computed
        Then the result is strictly after today
        """
        for month in range(1, 13):
            assert next_month_date(month) > self.today

    def always_return_first_of_month(self):
        """
        Given any month number
        When the next occurrence is computed
        Then the result's day is always 1
        """
        for month in range(1, 13):
            assert next_month_date(month).day == 1

    def return_correct_month_number(self):
        """
        Given a specific month number
        When the next occurrence is computed
        Then the result's month matches the request
        """
        for month in range(1, 13):
            assert next_month_date(month).month == month

    def never_more_than_one_year_away(self):
        """
        Given any month
        When the next occurrence is computed
        Then it is at most 1 year from today
        """
        for month in range(1, 13):
            assert next_month_date(month).year <= self.today.year + 1

    def advance_to_next_year_for_current_month(self):
        """
        Given the current month number
        When the next 1st-of-month is computed
        Then it falls in the following year (this year's 1st has already passed)
        """
        result = next_month_date(self.today.month)
        assert result.year == self.today.year + 1
        assert result.month == self.today.month
        assert result.day == 1

    def stay_in_current_year_for_next_month(self):
        """
        Given the month immediately after the current one (not applicable in December)
        When the next 1st-of-month is computed
        Then it falls this year because that date is still in the future
        """
        if self.today.month < 12:
            result = next_month_date(self.today.month + 1)
            assert result.year == self.today.year
            assert result.month == self.today.month + 1


# ===========================================================================
# FEATURE: Day rollover
# ===========================================================================

class DescribeDayRollover:
    """When the app opens on a new calendar day, Today/Tomorrow are rolled
    over: important Today tasks are kept, plain tasks move to Overdue (with
    expiry), daily-recurring Today tasks are dropped, and Tomorrow becomes
    the new Today."""

    def setup_method(self):
        self.today     = date.today()
        self.yesterday = self.today - timedelta(days=1)

    # --- when rollover triggers ---

    def skip_rollover_on_first_launch(self):
        """
        Given the app has never been saved (last_date is None)
        When recurrences are checked
        Then Today and Tomorrow are unchanged
        """
        sections = blank_sections(Today=[Task("A")], Tomorrow=[Task("B")])
        result, _ = check_recurrences(sections, last_date=None)
        assert result["Today"] == [Task("A")]
        assert result["Tomorrow"] == [Task("B")]

    def skip_rollover_when_saved_today(self):
        """
        Given the app was last saved today
        When recurrences are checked
        Then Today and Tomorrow are unchanged
        """
        sections = blank_sections(Today=[Task("A")], Tomorrow=[Task("B")])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert result["Today"] == [Task("A")]
        assert result["Tomorrow"] == [Task("B")]

    def trigger_rollover_when_saved_before_today(self):
        """
        Given the app was last saved yesterday
        When recurrences are checked
        Then Tomorrow tasks are moved into Today
        """
        task = Task("B")
        sections = blank_sections(Today=[], Tomorrow=[task])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert task in result["Today"]
        assert result["Tomorrow"] == []

    # --- fate of Today tasks on rollover ---

    def keep_important_tasks_in_today(self):
        """
        Given Today contains an important task
        When rollover occurs
        Then that task remains in Today
        """
        task_imp = Task("Important one", important=True)
        sections = blank_sections(Today=[task_imp])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert task_imp in result["Today"]

    def move_plain_tasks_to_overdue(self):
        """
        Given Today contains a plain (non-important, non-daily) task
        When rollover occurs
        Then the task appears in Overdue, not Today
        """
        sections = blank_sections(Today=[Task("Plain task")])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert not any(t.text == "Plain task" for t in result["Today"])
        assert any(t.text == "Plain task" for t in result["Overdue"])

    def move_urgent_only_tasks_to_overdue(self):
        """
        Given Today contains an urgent (but not important) task
        When rollover occurs
        Then it moves to Overdue — urgency alone does not keep a task in Today
        """
        sections = blank_sections(Today=[Task("Urgent one", urgent=True)])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert not any(t.text == "Urgent one" for t in result["Today"])
        assert any(t.text == "Urgent one" for t in result["Overdue"])

    def drop_daily_tasks_from_today(self):
        """
        Given Today contains a task that also appears in the Daily section
        When rollover occurs
        Then the task is dropped — not kept in Today, not moved to Overdue
        (daily tasks will be re-injected fresh by the recurring injection step)
        """
        sections = blank_sections(
            Today=[Task("Walk")],
            Daily=[Task("Walk")],
        )
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert not any(t.text == "Walk" for t in result["Today"])
        assert not any(t.text == "Walk" for t in result["Overdue"])

    def move_tomorrow_into_today(self):
        """
        Given Tomorrow contains tasks
        When rollover occurs
        Then those tasks appear in Today and Tomorrow becomes empty
        """
        task = Task("From tomorrow")
        sections = blank_sections(Tomorrow=[task])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert task in result["Today"]
        assert result["Tomorrow"] == []

    def place_kept_tasks_before_tomorrow_tasks(self):
        """
        Given Today has an important task and Tomorrow has a task
        When rollover occurs
        Then the important task comes before the tomorrow task in the new Today
        """
        task_imp  = Task("Carry forward", important=True)
        task_tmrw = Task("From tomorrow")
        sections = blank_sections(Today=[task_imp], Tomorrow=[task_tmrw])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert result["Today"] == [task_imp, task_tmrw]

    def skip_duplicate_overdue_entries(self):
        """
        Given a task already in Overdue and the same task appears in Today
        When rollover occurs
        Then the task is not added to Overdue a second time
        """
        sections = blank_sections(
            Today=[Task("Plain task")],
            Overdue=[Task("Plain task")],
        )
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert sum(1 for t in result["Overdue"] if t.text == "Plain task") == 1

    # --- changed flag ---

    def report_changed_when_task_moved_to_overdue(self):
        """
        Given Today has a plain task
        When rollover occurs
        Then changed is True
        """
        sections = blank_sections(Today=[Task("Plain")])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed

    def report_changed_when_tomorrow_moves_to_today(self):
        """
        Given Tomorrow has tasks
        When rollover occurs
        Then changed is True
        """
        sections = blank_sections(Tomorrow=[Task("B")])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed

    def report_unchanged_when_only_important_tasks(self):
        """
        Given Today contains only important tasks and Tomorrow is empty
        When rollover occurs
        Then changed is False (nothing was modified)
        """
        sections = blank_sections(Today=[Task("Keep me", important=True)], Tomorrow=[])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert not changed

    def report_unchanged_when_both_sections_empty(self):
        """
        Given both Today and Tomorrow are empty
        When rollover occurs
        Then changed is False
        """
        sections = blank_sections(Today=[], Tomorrow=[])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert not changed


# ===========================================================================
# FEATURE: Recurring task injection
# ===========================================================================

class DescribeRecurringTaskInjection:
    """When the app starts, recurring tasks that are due are copied into
    Today or Tomorrow so the user sees them."""

    def setup_method(self):
        self.today     = date.today()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow  = self.today + timedelta(days=1)
        self.day_after = self.today + timedelta(days=2)

    # --- Daily tasks (no look-ahead — Today only) ---

    def inject_daily_task_into_today_when_due(self):
        """
        Given a Daily task whose next_date is today
        When recurrences are checked
        Then a copy appears in Today
        """
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk" for t in result["Today"])
        assert changed

    def inject_overdue_daily_task_into_today(self):
        """
        Given a Daily task whose next_date is in the past
        When recurrences are checked
        Then the overdue task is still injected into Today
        """
        task = Task("Walk", next_date=self.yesterday)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk" for t in result["Today"])
        assert changed

    def skip_daily_task_due_tomorrow(self):
        """
        Given a Daily task whose next_date is tomorrow
        When recurrences are checked
        Then no copy appears in Tomorrow or Today
        (Daily has no day-early look-ahead unlike Weekly/Monthly/Annually)
        """
        task = Task("Walk", next_date=self.tomorrow)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert not any(t.text == "Walk" for t in result["Today"])
        assert not any(t.text == "Walk" for t in result["Tomorrow"])

    # --- Weekly / Monthly / Annually (one-day look-ahead into Tomorrow) ---

    def inject_weekly_task_into_tomorrow_as_preview(self):
        """
        Given a Weekly task whose next_date is tomorrow
        When recurrences are checked
        Then a preview copy appears in Tomorrow
        """
        task = Task("Run", next_date=self.tomorrow)
        sections = blank_sections(Weekly=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Run" for t in result["Tomorrow"])
        assert result["Today"] == []
        assert changed

    def inject_weekly_task_into_today_when_due(self):
        """
        Given a Weekly task whose next_date is today
        When recurrences are checked
        Then a copy appears in Today
        """
        task = Task("Run", next_date=self.today)
        sections = blank_sections(Weekly=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Run" for t in result["Today"])
        assert changed

    def skip_task_due_after_tomorrow(self):
        """
        Given a recurring task whose next_date is the day after tomorrow
        When recurrences are checked
        Then no copy is injected and nothing changes
        """
        task = Task("Walk", next_date=self.day_after)
        sections = blank_sections(Weekly=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert result["Today"] == []
        assert result["Tomorrow"] == []
        assert not changed

    def skip_injection_when_no_recurring_tasks(self):
        """
        Given all recurring sections are empty
        When recurrences are checked
        Then changed is False
        """
        _, changed = check_recurrences(blank_sections(), last_date=self.today)
        assert not changed

    def check_all_recurring_sections(self):
        """
        Given tasks in Daily, Weekly, Monthly, and Annually all due today
        When recurrences are checked
        Then all four are injected into Today
        """
        sections = blank_sections(
            Daily=[Task("D", next_date=self.today)],
            Weekly=[Task("W", next_date=self.today)],
            Monthly=[Task("M", next_date=self.today)],
            Annually=[Task("A", next_date=self.today)],
        )
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = {t.text for t in result["Today"]}
        assert {"D", "W", "M", "A"}.issubset(injected)

    # --- properties of the injected copy ---

    def inject_undone_copy_regardless_of_source_state(self):
        """
        Given a recurring source task marked as done
        When it is injected into Today
        Then the injected copy has done=False
        """
        task = Task("Walk", done=True, next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Walk")
        assert not injected.done

    def inject_copy_without_next_date(self):
        """
        Given a recurring source task with a scheduled date
        When injected
        Then the injected copy has next_date=None (it is a plain today-task)
        """
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Walk")
        assert injected.next_date is None

    def preserve_priority_flags_on_injected_copy(self):
        """
        Given a recurring source task flagged urgent and important
        When injected
        Then the injected copy retains both flags
        """
        task = Task("Run", urgent=True, important=True, next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Run")
        assert injected.urgent and injected.important

    # --- source task date advancement ---

    def advance_daily_source_to_tomorrow(self):
        """
        Given a Daily task due today that is injected
        When recurrences are checked
        Then the source task's next_date is advanced to tomorrow
        """
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date == self.tomorrow

    def advance_overdue_daily_to_tomorrow_in_one_step(self):
        """
        Given a Daily task that is several days overdue
        When recurrences are checked
        Then the source task's next_date is advanced to tomorrow in one step
        (Daily always advances exactly to tomorrow, not one interval at a time)
        """
        task = Task("Walk", next_date=self.today - timedelta(days=5))
        sections = blank_sections(Daily=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date == self.tomorrow

    def advance_weekly_source_by_one_week(self):
        """
        Given a Weekly task due today that is injected
        When recurrences are checked
        Then the source task's next_date is advanced by one week
        """
        task = Task("Run", next_date=self.today)
        sections = blank_sections(Weekly=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date == self.today + timedelta(weeks=1)

    def keep_source_task_in_recurring_section(self):
        """
        Given a Daily task that is injected into Today
        When recurrences are checked
        Then the original task object is still present in the Daily section
        """
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert task in result["Daily"]

    # --- combined rollover + injection ---

    def report_changed_when_rollover_and_injection_both_occur(self):
        """
        Given yesterday as last_date (triggers rollover) and a Daily task due today (triggers injection)
        When recurrences are checked
        Then changed is True
        """
        sections = blank_sections(
            Tomorrow=[Task("From tomorrow")],
            Daily=[Task("Walk", next_date=self.today)],
        )
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed


# ===========================================================================
# FEATURE: Overdue task expiry
# ===========================================================================

class DescribeOverdueTaskExpiry:
    """Tasks in the Overdue section carry an optional expiry date. Once that
    date has passed they are automatically removed."""

    def setup_method(self):
        self.today     = date.today()
        self.yesterday = self.today - timedelta(days=1)

    def assign_short_expiry_to_weekly_overdue_tasks(self):
        """
        Given a Today task that also appears in the Weekly section
        When rollover occurs
        Then the overdue entry has an expiry of today + 2 days
        """
        sections = blank_sections(
            Today=[Task("Run")],
            Weekly=[Task("Run")],
        )
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        overdue_task = next(t for t in result["Overdue"] if t.text == "Run")
        assert overdue_task.next_date == self.today + timedelta(days=2)

    def assign_long_expiry_to_monthly_overdue_tasks(self):
        """
        Given a Today task that also appears in the Monthly section
        When rollover occurs
        Then the overdue entry has an expiry of today + 7 days
        """
        sections = blank_sections(
            Today=[Task("Budget review")],
            Monthly=[Task("Budget review")],
        )
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        overdue_task = next(t for t in result["Overdue"] if t.text == "Budget review")
        assert overdue_task.next_date == self.today + timedelta(days=7)

    def remove_expired_overdue_tasks(self):
        """
        Given an Overdue task whose next_date was yesterday
        When recurrences are checked
        Then the task is removed and changed is True
        """
        sections = blank_sections(Overdue=[Task("Old task", next_date=self.yesterday)])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert not any(t.text == "Old task" for t in result["Overdue"])
        assert changed

    def keep_overdue_tasks_with_future_expiry(self):
        """
        Given an Overdue task whose next_date is tomorrow
        When recurrences are checked
        Then the task remains in Overdue
        """
        sections = blank_sections(Overdue=[Task("Still valid", next_date=self.today + timedelta(days=1))])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Still valid" for t in result["Overdue"])

    def keep_overdue_tasks_with_no_expiry(self):
        """
        Given an Overdue task with no expiry date (next_date=None)
        When recurrences are checked
        Then the task is never automatically removed
        """
        sections = blank_sections(Overdue=[Task("Permanent", next_date=None)])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Permanent" for t in result["Overdue"])


# ===========================================================================
# FEATURE: File persistence (load / save)
# ===========================================================================

class DescribeFilePersistence:
    """Tasks are stored in a plain-text file and can be loaded back with
    full fidelity across save/load cycles."""

    def _tmp_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        os.unlink(path)
        return path

    # --- missing file ---

    def return_empty_sections_for_missing_file(self):
        """
        Given a path that does not exist
        When loaded
        Then all sections are empty and last_date is None
        """
        sections, last_date = load("/nonexistent/path/tasks.txt")
        assert all(sections[s] == [] for s in SECTION_ORDER)
        assert last_date is None

    # --- last_date metadata ---

    def persist_and_recover_save_date(self):
        """
        Given tasks are saved with a specific today date
        When the file is reloaded
        Then last_date equals the date passed to save()
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(), today=date(2026, 3, 6))
            _, last_date = load(path)
            assert last_date == date(2026, 3, 6)
        finally:
            os.unlink(path)

    def return_no_date_for_malformed_metadata(self):
        """
        Given a file with a malformed last_date comment
        When loaded
        Then last_date is None but tasks are still parsed normally
        """
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# last_date: not-a-date\n\n## Today\n- Task\n")
            sections, last_date = load(path)
            assert last_date is None
            assert sections["Today"][0].text == "Task"
        finally:
            os.unlink(path)

    def return_no_date_when_metadata_absent(self):
        """
        Given a file with no last_date comment
        When loaded
        Then last_date is None
        """
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("## Today\n- Task\n")
            _, last_date = load(path)
            assert last_date is None
        finally:
            os.unlink(path)

    # --- done-task filtering ---

    def drop_done_tasks_from_today_on_load(self):
        """
        Given Today contains a mix of done and pending tasks when saved
        When reloaded
        Then done tasks are absent and pending tasks are retained
        """
        path = self._tmp_path()
        try:
            sections = blank_sections(Today=[
                Task("Done one", done=True),
                Task("Pending one"),
            ])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert all(not t.done for t in restored["Today"])
            assert any(t.text == "Pending one" for t in restored["Today"])
            assert not any(t.text == "Done one" for t in restored["Today"])
        finally:
            os.unlink(path)

    def drop_done_tasks_from_tomorrow_on_load(self):
        """
        Given Tomorrow contains a done task when saved
        When reloaded
        Then the done task is absent
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(Tomorrow=[Task("Done", done=True)]), today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Tomorrow"] == []
        finally:
            os.unlink(path)

    def drop_done_tasks_in_whenever_on_load(self):
        """
        Given Whenever contains a done task
        When saved and reloaded
        Then the done task is absent
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(Whenever=[Task("Done", done=True)]), today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Whenever"] == []
        finally:
            os.unlink(path)

    def retain_done_tasks_in_recurring_sections(self):
        """
        Given Daily contains a done recurring task
        When saved and reloaded
        Then the done task is still present
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(Daily=[Task("Walk", done=True, next_date=date(2026, 3, 10))]),
                 today=date(2026, 3, 6))
            restored, _ = load(path)
            assert any(t.done for t in restored["Daily"])
        finally:
            os.unlink(path)

    # --- default date for recurring tasks ---

    def assign_today_as_default_date_for_recurring_tasks(self):
        """
        Given a recurring task written to the file with no date suffix
        When loaded
        Then next_date defaults to today so check_recurrences can process it
        """
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# last_date: 2026-03-06\n\n## Daily\n- Walk\n")
            restored, _ = load(path)
            assert restored["Daily"][0].next_date == date.today()
        finally:
            os.unlink(path)

    def preserve_explicit_date_on_recurring_task(self):
        """
        Given a recurring task saved with an explicit future date
        When reloaded
        Then next_date is preserved exactly as written
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(Daily=[Task("Walk", next_date=date(2026, 4, 1))]),
                 today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Daily"][0].next_date == date(2026, 4, 1)
        finally:
            os.unlink(path)

    # --- section ordering ---

    def write_sections_in_canonical_order(self):
        """
        Given tasks in Today, Tomorrow, and Whenever
        When saved
        Then the sections appear in canonical order in the file (Today < Tomorrow < Whenever)
        """
        path = self._tmp_path()
        try:
            sections = blank_sections(Today=[Task("A")], Tomorrow=[Task("B")], Whenever=[Task("C")])
            save(path, sections, today=date(2026, 3, 6))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert content.index("## Today") < content.index("## Tomorrow") < content.index("## Whenever")
        finally:
            os.unlink(path)

    def write_custom_sections_after_standard_ones(self):
        """
        Given a sections dict containing a custom section not in SECTION_ORDER
        When saved
        Then the custom section appears after Annually (the last standard section)
        """
        path = self._tmp_path()
        try:
            sections = blank_sections()
            sections["Custom"] = [Task("Custom task")]
            save(path, sections, today=date(2026, 3, 6))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert content.index("## Custom") > content.index("## Annually")
        finally:
            os.unlink(path)

    # --- atomic write safety ---

    def leave_no_tmp_file_after_save(self):
        """
        Given a save operation completes successfully
        When checking for leftover temp files
        Then no .tmp file exists at the save path
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(), today=date(2026, 3, 6))
            assert not os.path.exists(path + ".tmp")
        finally:
            os.unlink(path)

    # --- round-trip fidelity ---

    def preserve_tasks_through_save_reload(self):
        """
        Given tasks with a variety of fields spread across multiple sections
        When saved and reloaded
        Then every field of every task is identical to the original
        """
        path = self._tmp_path()
        try:
            original = blank_sections(
                Today=[Task("Buy groceries")],
                Tomorrow=[Task("Fix bug", urgent=True)],
                Whenever=[Task("Read book", important=True)],
                Daily=[Task("Walk", next_date=date(2026, 4, 1))],
            )
            save(path, original, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Today"]    == original["Today"]
            assert restored["Tomorrow"] == original["Tomorrow"]
            assert restored["Whenever"] == original["Whenever"]
            assert restored["Daily"]    == original["Daily"]
        finally:
            os.unlink(path)

    def preserve_unicode_through_save_reload(self):
        """
        Given a task whose text contains non-ASCII Unicode characters
        When saved and reloaded
        Then the text is byte-perfect identical to the original
        """
        path = self._tmp_path()
        try:
            save(path, blank_sections(Today=[Task("Ångström café résumé 日本語")]),
                 today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Today"][0].text == "Ångström café résumé 日本語"
        finally:
            os.unlink(path)

    def ignore_tasks_before_first_section_header(self):
        """
        Given a file where task lines appear before any "## Section" header
        When loaded
        Then those orphan lines are ignored; only properly sectioned tasks appear
        """
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("- Orphan task\n## Today\n- Real task\n")
            restored, _ = load(path)
            assert len(restored["Today"]) == 1
            assert restored["Today"][0].text == "Real task"
        finally:
            os.unlink(path)
