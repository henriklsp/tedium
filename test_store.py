"""
test_store.py
Unit tests for store.py (the data layer).

Covers: Task.to_line, _parse_task, round-trip serialisation, next_date_for,
        next_weekday_date, next_month_date, check_recurrences (day-rollover +
        recurring injection), load, save.

Run with:  pytest test_store.py
"""

import os
import tempfile
from datetime import date, timedelta

import pytest

from store import (
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


# ---------------------------------------------------------------------------
# Task.to_line
# ---------------------------------------------------------------------------

class TestToLine:
    def test_plain_task(self):
        assert Task(text="Buy groceries").to_line() == "- Buy groceries"

    def test_done_task(self):
        assert Task(text="Buy groceries", done=True).to_line() == "[x] Buy groceries"

    def test_urgent_only(self):
        assert Task(text="Fix bug", urgent=True).to_line() == "- [!] Fix bug"

    def test_important_only(self):
        assert Task(text="Review PR", important=True).to_line() == "- [*] Review PR"

    def test_urgent_and_important(self):
        assert Task(text="Deploy", urgent=True, important=True).to_line() == "- [!*] Deploy"

    def test_done_with_urgent_marker(self):
        # Both status prefix and marker block must appear together
        assert Task(text="T", done=True, urgent=True).to_line() == "[x] [!] T"

    def test_done_with_both_markers(self):
        assert Task(text="T", done=True, urgent=True, important=True).to_line() == "[x] [!*] T"

    def test_with_date(self):
        assert Task(text="Walk", next_date=date(2026, 3, 10)).to_line() == "- Walk [2026-03-10]"

    def test_date_suffix_has_leading_space(self):
        line = Task(text="Walk", next_date=date(2026, 3, 10)).to_line()
        assert " [2026-03-10]" in line  # space before bracket

    def test_done_with_date(self):
        assert Task(text="Walk", done=True, next_date=date(2026, 3, 10)).to_line() == "[x] Walk [2026-03-10]"

    def test_urgent_with_date(self):
        assert Task(text="Run", urgent=True, next_date=date(2026, 3, 10)).to_line() == "- [!] Run [2026-03-10]"

    def test_no_date_suffix_when_none(self):
        line = Task(text="Something").to_line()
        assert "[" not in line

    def test_leap_day_date(self):
        assert Task(text="Leap", next_date=date(2024, 2, 29)).to_line() == "- Leap [2024-02-29]"


# ---------------------------------------------------------------------------
# _parse_task
# ---------------------------------------------------------------------------

class TestParseTask:

    # --- blank / unrecognised lines ---

    def test_empty_string_returns_none(self):
        assert _parse_task("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_task("   ") is None

    def test_hash_comment_returns_none(self):
        assert _parse_task("# last_date: 2026-03-06") is None

    def test_no_recognised_prefix_returns_none(self):
        assert _parse_task("Buy groceries") is None

    def test_section_header_returns_none(self):
        assert _parse_task("## Today") is None

    # --- done prefix ---

    def test_done_lowercase_x(self):
        t = _parse_task("[x] Buy groceries")
        assert t is not None and t.done and t.text == "Buy groceries"

    def test_done_uppercase_x(self):
        t = _parse_task("[X] Buy groceries")
        assert t is not None and t.done

    def test_not_done(self):
        t = _parse_task("- Buy groceries")
        assert t is not None and not t.done and t.text == "Buy groceries"

    # --- urgent / important markers ---

    def test_urgent_only(self):
        t = _parse_task("- [!] Fix bug")
        assert t is not None
        assert t.urgent and not t.important
        assert t.text == "Fix bug"

    def test_important_only(self):
        t = _parse_task("- [*] Review PR")
        assert t is not None
        assert t.important and not t.urgent
        assert t.text == "Review PR"

    def test_urgent_and_important(self):
        t = _parse_task("- [!*] Deploy now")
        assert t is not None
        assert t.urgent and t.important
        assert t.text == "Deploy now"

    def test_reversed_marker_order_not_matched(self):
        # [*!] does not match the regex — treated as plain text
        t = _parse_task("- [*!] text")
        assert t is not None
        # Neither flag set; the bracket block stays in the text as-is
        assert not t.urgent and not t.important

    def test_empty_brackets_treated_as_no_marker(self):
        # "- [] text" matches the regex with an empty group
        t = _parse_task("- [] text")
        assert t is not None
        assert not t.urgent and not t.important
        assert t.text == "text"

    # --- date suffix ---

    def test_with_trailing_date(self):
        t = _parse_task("- Walk [2026-03-10]")
        assert t is not None
        assert t.text == "Walk"
        assert t.next_date == date(2026, 3, 10)

    def test_date_not_at_end_is_ignored(self):
        # DATE_RE is anchored with $ — an inline date is not parsed
        t = _parse_task("- Walk [2026-03-10] extra")
        assert t is not None
        assert t.next_date is None
        assert "[2026-03-10]" in t.text  # bracket stays in text

    def test_non_date_bracket_at_end_left_in_text(self):
        # "[not-a-date]" does not match DATE_RE, so no date parsed and text kept whole
        t = _parse_task("- Walk [not-a-date]")
        assert t is not None
        assert t.next_date is None
        assert "[not-a-date]" in t.text

    def test_invalid_date_value_ignored(self):
        # Regex matches the bracket but the date value is invalid (Feb 29 in non-leap year)
        t = _parse_task("- Walk [2023-02-29]")
        assert t is not None
        assert t.next_date is None
        assert t.text == "Walk"  # text trimmed up to bracket start

    def test_leap_day_date_valid(self):
        t = _parse_task("- Walk [2024-02-29]")
        assert t is not None
        assert t.next_date == date(2024, 2, 29)

    def test_done_with_urgent_and_date(self):
        t = _parse_task("[x] [!*] Run [2026-03-10]")
        assert t is not None
        assert t.done and t.urgent and t.important
        assert t.text == "Run"
        assert t.next_date == date(2026, 3, 10)

    # --- whitespace ---

    def test_leading_and_trailing_whitespace_stripped(self):
        t = _parse_task("  - Buy groceries  ")
        assert t is not None
        assert t.text == "Buy groceries"


# ---------------------------------------------------------------------------
# Round-trip: to_line → _parse_task must produce the original Task
# ---------------------------------------------------------------------------

class TestRoundTrip:

    def _rt(self, task: Task) -> Task:
        return _parse_task(task.to_line())

    def test_plain(self):
        t = Task(text="Buy groceries")
        assert self._rt(t) == t

    def test_done(self):
        t = Task(text="Buy groceries", done=True)
        assert self._rt(t) == t

    def test_urgent(self):
        t = Task(text="Fix bug", urgent=True)
        assert self._rt(t) == t

    def test_important(self):
        t = Task(text="Review PR", important=True)
        assert self._rt(t) == t

    def test_urgent_and_important(self):
        t = Task(text="Deploy", urgent=True, important=True)
        assert self._rt(t) == t

    def test_with_date(self):
        t = Task(text="Walk", next_date=date(2026, 3, 10))
        assert self._rt(t) == t

    def test_done_with_both_markers_and_date(self):
        t = Task(text="Run", done=True, urgent=True, important=True, next_date=date(2026, 3, 10))
        assert self._rt(t) == t


# ---------------------------------------------------------------------------
# next_date_for
# ---------------------------------------------------------------------------

class TestNextDateFor:

    def setup_method(self):
        self.base = date(2026, 3, 6)  # fixed Friday

    def test_daily_advances_one_day(self):
        assert next_date_for("Daily", self.base) == date(2026, 3, 7)

    def test_weekly_advances_seven_days(self):
        assert next_date_for("Weekly", self.base) == date(2026, 3, 13)

    def test_monthly_advances_one_calendar_month(self):
        assert next_date_for("Monthly", self.base) == date(2026, 4, 6)

    def test_annually_advances_one_calendar_year(self):
        assert next_date_for("Annually", self.base) == date(2027, 3, 6)

    def test_unknown_section_returns_same_date(self):
        assert next_date_for("Whenever", self.base) == self.base
        assert next_date_for("", self.base) == self.base

    def test_monthly_clamps_jan31_to_feb28(self):
        # dateutil clamps to the last valid day of the month
        assert next_date_for("Monthly", date(2026, 1, 31)) == date(2026, 2, 28)

    def test_monthly_does_not_bleed_into_next_month(self):
        # Ensure we get April 30, not May 1
        assert next_date_for("Monthly", date(2026, 3, 31)) == date(2026, 4, 30)

    def test_annually_from_leap_day_clamps(self):
        # 2024-02-29 + 1 year → 2025-02-28 (2025 is not a leap year)
        assert next_date_for("Annually", date(2024, 2, 29)) == date(2025, 2, 28)

    def test_weekly_crosses_year_boundary(self):
        assert next_date_for("Weekly", date(2025, 12, 28)) == date(2026, 1, 4)


# ---------------------------------------------------------------------------
# next_weekday_date
# ---------------------------------------------------------------------------

class TestNextWeekdayDate:

    def setup_method(self):
        self.today = date.today()

    def test_result_is_always_strictly_in_the_future(self):
        for weekday in range(7):
            assert next_weekday_date(weekday) > self.today

    def test_result_has_the_correct_weekday(self):
        for weekday in range(7):
            assert next_weekday_date(weekday).weekday() == weekday

    def test_result_is_at_most_seven_days_away(self):
        # The furthest a result can be is 7 days (same weekday as today).
        for weekday in range(7):
            assert next_weekday_date(weekday) <= self.today + timedelta(days=7)

    def test_same_weekday_as_today_advances_a_full_week(self):
        today_weekday = self.today.weekday()
        assert next_weekday_date(today_weekday) == self.today + timedelta(days=7)

    def test_next_calendar_day_is_one_day_ahead(self):
        tomorrow_weekday = (self.today.weekday() + 1) % 7
        assert next_weekday_date(tomorrow_weekday) == self.today + timedelta(days=1)


# ---------------------------------------------------------------------------
# next_month_date
# ---------------------------------------------------------------------------

class TestNextMonthDate:

    def setup_method(self):
        self.today = date.today()

    def test_result_is_always_strictly_in_the_future(self):
        for month in range(1, 13):
            assert next_month_date(month) > self.today

    def test_result_is_always_the_first_of_the_month(self):
        for month in range(1, 13):
            assert next_month_date(month).day == 1

    def test_result_has_the_requested_month(self):
        for month in range(1, 13):
            assert next_month_date(month).month == month

    def test_result_year_is_never_more_than_one_year_ahead(self):
        for month in range(1, 13):
            assert next_month_date(month).year <= self.today.year + 1

    def test_current_month_always_advances_to_next_year(self):
        # The 1st of the current month is always <= today (today.day >= 1),
        # so the result must be in the following year.
        result = next_month_date(self.today.month)
        assert result.year == self.today.year + 1
        assert result.month == self.today.month
        assert result.day == 1

    def test_next_calendar_month_uses_this_year(self):
        # The 1st of any month after the current one is guaranteed to be in the
        # future this year, so we should get this year's date back.
        # (Not applicable in December since there is no month 13.)
        if self.today.month < 12:
            result = next_month_date(self.today.month + 1)
            assert result.year == self.today.year
            assert result.month == self.today.month + 1


# ---------------------------------------------------------------------------
# check_recurrences
# ---------------------------------------------------------------------------

class TestCheckRecurrences:

    def setup_method(self):
        self.today     = date.today()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow  = self.today + timedelta(days=1)
        self.day_after = self.today + timedelta(days=2)

    # --- day-rollover: when does the block trigger? ---

    def test_no_rollover_when_last_date_is_none(self):
        sections = blank_sections(Today=[Task("A")], Tomorrow=[Task("B")])
        result, changed = check_recurrences(sections, last_date=None)
        assert result["Today"] == [Task("A")]
        assert result["Tomorrow"] == [Task("B")]

    def test_no_rollover_when_last_date_equals_today(self):
        sections = blank_sections(Today=[Task("A")], Tomorrow=[Task("B")])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert result["Today"] == [Task("A")]
        assert result["Tomorrow"] == [Task("B")]

    def test_rollover_triggers_when_last_date_is_yesterday(self):
        task_b = Task("B")
        sections = blank_sections(Today=[], Tomorrow=[task_b])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert result["Today"] == [task_b]
        assert result["Tomorrow"] == []

    # --- what the rollover does to Today ---

    def test_rollover_drops_plain_today_tasks(self):
        sections = blank_sections(Today=[Task("Plain")])
        result, changed = check_recurrences(sections, last_date=self.yesterday)
        assert result["Today"] == []
        assert changed

    def test_rollover_keeps_important_today_tasks(self):
        task_imp   = Task("Important one", important=True)
        task_plain = Task("Plain one")
        sections = blank_sections(Today=[task_imp, task_plain])
        result, changed = check_recurrences(sections, last_date=self.yesterday)
        assert result["Today"] == [task_imp]
        assert changed

    def test_rollover_keeps_urgent_today_tasks(self):
        # Urgent is not important; the filter retains only important=True tasks
        task_urg = Task("Urgent one", urgent=True)
        sections = blank_sections(Today=[task_urg])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        # urgent-only tasks are NOT kept (important=False)
        assert result["Today"] == []

    def test_rollover_merges_kept_today_with_tomorrow(self):
        task_imp  = Task("Carry forward", important=True)
        task_tmrw = Task("From tomorrow")
        sections = blank_sections(Today=[task_imp], Tomorrow=[task_tmrw])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert result["Today"] == [task_imp, task_tmrw]
        assert result["Tomorrow"] == []

    def test_rollover_clears_tomorrow(self):
        sections = blank_sections(Today=[], Tomorrow=[Task("B")])
        result, _ = check_recurrences(sections, last_date=self.yesterday)
        assert result["Tomorrow"] == []

    # --- changed flag for rollover ---

    def test_changed_true_when_plain_today_task_dropped(self):
        sections = blank_sections(Today=[Task("Plain")])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed

    def test_changed_true_when_tomorrow_task_moved(self):
        sections = blank_sections(Today=[], Tomorrow=[Task("B")])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed

    def test_changed_false_when_all_today_important_and_tomorrow_empty(self):
        # Rollover runs but nothing actually changes
        sections = blank_sections(Today=[Task("Keep me", important=True)], Tomorrow=[])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert not changed

    def test_changed_false_when_both_sections_empty_on_rollover(self):
        sections = blank_sections(Today=[], Tomorrow=[])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert not changed

    # --- recurring injection: which tasks are injected ---

    def test_injection_when_next_date_is_today_goes_to_today(self):
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk" for t in result["Today"])
        assert result["Tomorrow"] == []
        assert changed

    def test_injection_when_next_date_is_tomorrow_goes_to_tomorrow(self):
        task = Task("Walk", next_date=self.tomorrow)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk" for t in result["Tomorrow"])
        assert result["Today"] == []
        assert changed

    def test_injection_when_overdue_goes_to_today(self):
        task = Task("Walk", next_date=self.yesterday)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk" for t in result["Today"])
        assert result["Tomorrow"] == []
        assert changed

    def test_today_due_and_tomorrow_due_split_across_sections(self):
        today_task    = Task("Today task",    next_date=self.today)
        tomorrow_task = Task("Tomorrow task", next_date=self.tomorrow)
        sections = blank_sections(Daily=[today_task, tomorrow_task])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Today task"    for t in result["Today"])
        assert any(t.text == "Tomorrow task" for t in result["Tomorrow"])
        assert not any(t.text == "Today task"    for t in result["Tomorrow"])
        assert not any(t.text == "Tomorrow task" for t in result["Today"])

    def test_no_injection_when_next_date_after_tomorrow(self):
        task = Task("Walk", next_date=self.day_after)
        sections = blank_sections(Daily=[task])
        result, changed = check_recurrences(sections, last_date=self.today)
        assert result["Tomorrow"] == []
        assert not changed

    def test_no_injection_when_recurring_sections_empty(self):
        sections = blank_sections()
        _, changed = check_recurrences(sections, last_date=self.today)
        assert not changed

    # --- injected task properties ---

    def test_injected_task_is_not_done(self):
        task = Task("Walk", done=True, next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Walk")
        assert not injected.done

    def test_injected_task_has_no_next_date(self):
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Walk")
        assert injected.next_date is None

    def test_injected_task_preserves_text(self):
        task = Task("Walk the dog", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert any(t.text == "Walk the dog" for t in result["Today"])

    def test_injected_task_preserves_urgent_and_important(self):
        task = Task("Run", urgent=True, important=True, next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        injected = next(t for t in result["Today"] if t.text == "Run")
        assert injected.urgent and injected.important

    # --- next_date advancement on the source task ---

    def test_source_task_next_date_advanced_past_tomorrow(self):
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date > self.tomorrow

    def test_source_task_advanced_multiple_steps_when_overdue(self):
        # A task 5 days overdue must be advanced all the way past tomorrow in one call
        overdue = self.today - timedelta(days=5)
        task = Task("Walk", next_date=overdue)
        sections = blank_sections(Daily=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date > self.tomorrow

    def test_weekly_task_advanced_one_week(self):
        task = Task("Run", next_date=self.today)
        sections = blank_sections(Weekly=[task])
        check_recurrences(sections, last_date=self.today)
        assert task.next_date == self.today + timedelta(weeks=1)

    def test_original_task_stays_in_recurring_section(self):
        # Injection copies to Tomorrow; the original must remain in Daily
        task = Task("Walk", next_date=self.today)
        sections = blank_sections(Daily=[task])
        result, _ = check_recurrences(sections, last_date=self.today)
        assert task in result["Daily"]

    # --- interaction between rollover and injection ---

    def test_rollover_and_injection_both_set_changed(self):
        # Rollover: last_date < today, Tomorrow has a task → changed
        # Injection: Daily task due today → also changed (but changed is already True)
        task_daily = Task("Walk", next_date=self.today)
        sections = blank_sections(Tomorrow=[Task("From tomorrow")], Daily=[task_daily])
        _, changed = check_recurrences(sections, last_date=self.yesterday)
        assert changed

    def test_all_recurring_sections_are_checked(self):
        sections = blank_sections(
            Daily=[Task("D", next_date=self.today)],
            Weekly=[Task("W", next_date=self.today)],
            Monthly=[Task("M", next_date=self.today)],
            Annually=[Task("A", next_date=self.today)],
        )
        result, _ = check_recurrences(sections, last_date=self.today)
        # All tasks are due today so they all land in Today
        injected_texts = {t.text for t in result["Today"]}
        assert injected_texts == {"D", "W", "M", "A"}


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

class TestLoadSave:

    def _tmp_path(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        os.unlink(path)  # let save() create it fresh
        return path

    # --- load with missing file ---

    def test_load_missing_file_returns_empty_sections(self):
        sections, last_date = load("/nonexistent/path/tasks.txt")
        assert all(sections[s] == [] for s in SECTION_ORDER)
        assert last_date is None

    # --- last_date metadata ---

    def test_save_records_last_date_and_load_reads_it(self):
        path = self._tmp_path()
        try:
            today = date(2026, 3, 6)
            save(path, blank_sections(), today=today)
            _, last_date = load(path)
            assert last_date == today
        finally:
            os.unlink(path)

    def test_load_ignores_invalid_last_date(self):
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# last_date: not-a-date\n\n## Today\n- Task\n")
            sections, last_date = load(path)
            assert last_date is None
            assert sections["Today"][0].text == "Task"
        finally:
            os.unlink(path)

    def test_load_defaults_today_when_no_metadata(self):
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("## Today\n- Task\n")
            _, last_date = load(path)
            assert last_date is None
        finally:
            os.unlink(path)

    # --- done-task filtering ---

    def test_load_filters_done_tasks_in_today(self):
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

    def test_load_filters_done_tasks_in_tomorrow(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(Tomorrow=[Task("Done", done=True)])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Tomorrow"] == []
        finally:
            os.unlink(path)

    def test_load_keeps_done_tasks_in_whenever(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(Whenever=[Task("Done", done=True)])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert any(t.done for t in restored["Whenever"])
        finally:
            os.unlink(path)

    def test_load_keeps_done_tasks_in_recurring_sections(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(Daily=[Task("Walk", done=True, next_date=date(2026, 3, 10))])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert any(t.done for t in restored["Daily"])
        finally:
            os.unlink(path)

    # --- recurring default date ---

    def test_recurring_task_without_date_gets_today_on_load(self):
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# last_date: 2026-03-06\n\n## Daily\n- Walk\n")
            restored, _ = load(path)
            assert restored["Daily"][0].next_date == date.today()
        finally:
            os.unlink(path)

    def test_recurring_task_with_explicit_date_keeps_it(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(Daily=[Task("Walk", next_date=date(2026, 4, 1))])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Daily"][0].next_date == date(2026, 4, 1)
        finally:
            os.unlink(path)

    # --- section ordering ---

    def test_standard_sections_saved_in_canonical_order(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(
                Today=[Task("A")],
                Tomorrow=[Task("B")],
                Whenever=[Task("C")],
            )
            save(path, sections, today=date(2026, 3, 6))
            with open(path, encoding="utf-8") as f:
                content = f.read()
            today_pos   = content.index("## Today")
            tomorrow_pos = content.index("## Tomorrow")
            whenever_pos = content.index("## Whenever")
            assert today_pos < tomorrow_pos < whenever_pos
        finally:
            os.unlink(path)

    def test_extra_section_saved_after_standard_sections(self):
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

    # --- atomic write ---

    def test_no_tmp_file_left_after_successful_save(self):
        path = self._tmp_path()
        try:
            save(path, blank_sections(), today=date(2026, 3, 6))
            assert not os.path.exists(path + ".tmp")
        finally:
            os.unlink(path)

    # --- round-trip fidelity ---

    def test_full_roundtrip_preserves_all_task_fields(self):
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

    def test_unicode_text_survives_roundtrip(self):
        path = self._tmp_path()
        try:
            sections = blank_sections(Today=[Task("Ångström café résumé 日本語")])
            save(path, sections, today=date(2026, 3, 6))
            restored, _ = load(path)
            assert restored["Today"][0].text == "Ångström café résumé 日本語"
        finally:
            os.unlink(path)

    def test_tasks_before_any_section_header_are_skipped(self):
        path = self._tmp_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("- Orphan task\n## Today\n- Real task\n")
            restored, _ = load(path)
            assert len(restored["Today"]) == 1
            assert restored["Today"][0].text == "Real task"
        finally:
            os.unlink(path)
