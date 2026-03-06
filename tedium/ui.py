from __future__ import annotations

# ui.py
# Presentation layer.
# Responsible for all Qt UI components: the per-task widget (TaskWidget), the
# per-section widget (SectionWidget), the recurring-section collapse separator
# (RecurringSeparatorWidget), and the top-level main window (MainWindow).
# Also exports the global Qt stylesheet (STYLESHEET) consumed by main.py.
#
# Supporting classes extracted from TaskWidget for single-responsibility:
#   TaskStyler            — pure CSS computation (no widget state)
#   TaskEditController    — readOnly↔editing state machine
#   TaskContextMenuBuilder — constructs the right-click QMenu

from datetime import date
from typing import Callable, Optional

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QPainter, QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QVBoxLayout,
    QWidget,
)

from .store import Task, SECTION_ORDER, RECURRING, next_date_for, next_weekday_date, next_month_date

# Per-section background colours for non-recurring sections that differ from white.
SECTION_BG = {
    "Tomorrow": "#f5f3f1",
    "Whenever": "#eae6e1",
}
# Shared background colour applied to all recurring sections and their separator.
RECURRING_BG = "#d5cdc3"

# Weekday names in Python weekday() order (0=Monday … 6=Sunday).
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Month names in calendar order (index 0 = January = month number 1).
_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]

# Global Qt stylesheet applied to the entire application via QApplication.setStyleSheet().
STYLESHEET = """
QWidget {
    background-color: #ffffff;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 11pt;
    color: #222222;
}
QScrollArea {
    border: none;
}
QScrollBar:vertical {
    width: 6px;
    background: #f0f0f0;
}
QScrollBar::handle:vertical {
    background: #cccccc;
    border-radius: 3px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QLineEdit {
    border: none;
    background: transparent;
    padding: 0px;
    selection-background-color: #b3d4ff;
}
QLineEdit:focus {
    border-bottom: 1px solid #ddd;
}
QMenu {
    border: 1px solid #ddd;
    padding: 4px 0;
}
QMenu::item {
    padding: 4px 20px;
}
QMenu::item:selected {
    background-color: #f0f0f0;
}
"""


# Pure CSS computation for TaskWidget. All methods are static — no widget state
# is held. TaskWidget calls these with the current task and section name and
# receives ready-to-apply stylesheet strings back.
class TaskStyler:

    # Returns the hex background colour for the TaskWidget container, taking
    # overdue status and section membership into account.
    @staticmethod
    def widget_bg(section_name: str, task: Task) -> str:
        overdue = (
            not task.done
            and not task.urgent
            and not task.important
            and task.next_date is not None
            and task.next_date < date.today()
        )
        if overdue:
            return "#fffff0"
        if section_name in SECTION_BG:
            return SECTION_BG[section_name]
        if section_name in RECURRING:
            return RECURRING_BG
        return "#ffffff"

    # Returns the full QLineEdit stylesheet string reflecting the task's
    # done/urgent/important state and the section's font size.
    @staticmethod
    def edit_css(section_name: str, task: Task) -> str:
        size = "font-size: 13pt; " if section_name == "Today" else ""
        base = f"QLineEdit {{ border: none; background: transparent; padding: 1px 0; {size}"
        if task.done:
            extra = "color: #aaaaaa; text-decoration: line-through; "
        elif task.urgent and task.important:
            extra = "color: #cc2200; font-weight: bold; "
        elif task.urgent:
            extra = "color: #cc2200; "
        elif task.important:
            extra = "font-weight: bold; "
        else:
            extra = ""
        return base + extra + "}"

    # Returns the QLabel stylesheet for the completion checkmark, sized to
    # match the section's font size.
    @staticmethod
    def check_label_css(section_name: str) -> str:
        size_pt = "13pt" if section_name == "Today" else "11pt"
        return (
            f"QLabel {{ color: #aaaaaa; background: transparent; "
            f"font-size: {size_pt}; padding: 0 2px 0 2px; }}"
        )


# Manages the readOnly↔editing state machine for a single QLineEdit. Intercepts
# editingFinished to commit or delete, and exposes begin()/focus() so callers
# can enter editing mode with or without deferred focus.
class TaskEditController(QObject):
    # Emitted when the user commits a non-empty edit; carries the new text.
    text_committed = Signal(str)
    # Emitted when the user submits an empty edit (signals that the task should be deleted).
    edit_deleted = Signal()

    # Wires editingFinished on the provided QLineEdit to the internal handler.
    def __init__(self, edit: QLineEdit, task: Task, parent: QObject = None):
        super().__init__(parent)
        self._edit = edit
        self._task = task
        edit.editingFinished.connect(self._on_editing_finished)

    # Switches the edit to writable mode and populates it with the task's
    # current text. Does not focus — call focus() after begin().
    def begin(self) -> None:
        self._edit.setReadOnly(False)
        self._edit.setText(self._task.text)

    # Focuses the edit and moves the cursor to the end.
    # May be called directly (double-click) or via QTimer.singleShot (menu).
    def focus(self) -> None:
        self._edit.setFocus()
        self._edit.end(False)

    # Returns True while the edit is in writable (editing) mode.
    @property
    def is_editing(self) -> bool:
        return not self._edit.isReadOnly()

    # Commits or discards on editingFinished. Sets task.text and emits
    # text_committed for non-empty input; emits edit_deleted for empty input.
    def _on_editing_finished(self) -> None:
        text = self._edit.text().strip()
        self._edit.setReadOnly(True)
        if not text:
            self.edit_deleted.emit()
        else:
            self._task.text = text
            self.text_committed.emit(text)


# Constructs the right-click QMenu for a task. Decides which actions to include
# based on the task's state and section, then connects each action to the
# supplied callback. Returns the populated menu without showing it.
class TaskContextMenuBuilder:

    # Stores the task and section name used for conditional action inclusion.
    def __init__(self, task: Task, section_name: str):
        self._task = task
        self._section_name = section_name

    # Builds and returns a QMenu populated with the appropriate actions.
    # on_move_to_tomorrow should be None for sections other than Today.
    # on_set_due_date should be provided for Weekly tasks; when present, seven
    # weekday actions (Monday–Sunday) are added just above Delete.
    def build(
        self,
        parent: QWidget,
        *,
        on_edit: Callable,
        on_delete: Callable,
        on_move_to_tomorrow: Optional[Callable] = None,
        on_mark_urgent: Optional[Callable] = None,
        on_clear_urgent: Optional[Callable] = None,
        on_mark_important: Optional[Callable] = None,
        on_clear_important: Optional[Callable] = None,
        on_set_due_date: Optional[Callable] = None,
    ) -> QMenu:
        menu = QMenu(parent)

        title = menu.addAction(self._task.text)
        title.triggered.connect(on_edit)
        menu.addSeparator()

        if on_move_to_tomorrow is not None:
            act = menu.addAction("Move to Tomorrow")
            act.triggered.connect(on_move_to_tomorrow)
            menu.addSeparator()

        if self._section_name != "Whenever":
            if not self._task.urgent:
                if on_mark_urgent:
                    act = menu.addAction("Urgent")
                    act.triggered.connect(on_mark_urgent)
            else:
                if on_clear_urgent:
                    act = menu.addAction("Not urgent")
                    act.triggered.connect(on_clear_urgent)

        if not self._task.important:
            if on_mark_important:
                act = menu.addAction("Important")
                act.triggered.connect(on_mark_important)
        else:
            if on_clear_important:
                act = menu.addAction("Not important")
                act.triggered.connect(on_clear_important)

        if on_set_due_date is not None:
            menu.addSeparator()
            if self._section_name == "Weekly":
                current_weekday = (
                    self._task.next_date.weekday()
                    if self._task.next_date is not None else None
                )
                for weekday, name in enumerate(_WEEKDAYS):
                    label = f"{name} ✓" if weekday == current_weekday else name
                    d = next_weekday_date(weekday)
                    act = menu.addAction(label)
                    # Default arg captures d at loop iteration time, avoiding late-binding.
                    act.triggered.connect(lambda checked=False, d=d: on_set_due_date(d))
            elif self._section_name == "Annually":
                current_month = (
                    self._task.next_date.month
                    if self._task.next_date is not None else None
                )
                for month, name in enumerate(_MONTHS, start=1):
                    label = f"{name} ✓" if month == current_month else name
                    d = next_month_date(month)
                    act = menu.addAction(label)
                    act.triggered.connect(lambda checked=False, d=d: on_set_due_date(d))

        menu.addSeparator()
        delete_act = menu.addAction("Delete")
        delete_act.triggered.connect(on_delete)

        return menu


# Displays a single task as a horizontally laid-out line edit plus a checkmark
# label, and handles all interaction (click to toggle done, double-click to edit,
# right-click for context menu, Delete/Backspace to remove).
class TaskWidget(QWidget):
    changed = Signal()
    delete_requested = Signal(object)  # self
    move_to_tomorrow = Signal(object)  # self
    promote_urgent = Signal(object)    # self — emitted when marked urgent
    marked_important = Signal(object)  # self — emitted when important marked
    cleared_urgent = Signal(object)    # self — emitted when urgent cleared
    cleared_important = Signal(object) # self — emitted when important cleared

    # Constructs the widget for the given task within the named section.
    def __init__(self, task: Task, section_name: str, parent=None):
        super().__init__(parent)
        self.task = task
        self.section_name = section_name
        self._setup_ui()
        self._apply_style()

    # Builds the internal layout: a QLineEdit for the task text and a QLabel
    # for the completion checkmark. Wires all interaction signals.
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.edit = QLineEdit(self.task.text)
        self.edit.setReadOnly(True)
        self.edit.setCursor(Qt.IBeamCursor)
        self.edit.setFrame(False)
        self.edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.edit.setMaxLength(70)

        self.edit.mousePressEvent = self._on_mouse_press
        self.edit.mouseDoubleClickEvent = self._on_double_click
        self.edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.edit.customContextMenuRequested.connect(self._show_context_menu)

        self._check_lbl = QLabel("✓")
        self._check_lbl.setVisible(False)

        layout.addWidget(self.edit)
        layout.addWidget(self._check_lbl)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # Wire the editing state machine.
        self._edit_ctrl = TaskEditController(self.edit, self.task, parent=self)
        self._edit_ctrl.text_committed.connect(self._on_text_committed)
        self._edit_ctrl.edit_deleted.connect(lambda: self.delete_requested.emit(self))

    # Recomputes and applies all visual styling for this widget based on the
    # current task state. Delegates CSS computation to TaskStyler.
    def _apply_style(self):
        self.setStyleSheet(
            f"background-color: {TaskStyler.widget_bg(self.section_name, self.task)};"
        )
        self.edit.setStyleSheet(TaskStyler.edit_css(self.section_name, self.task))
        self._update_check_label()
        self._refresh_display_text()

    # Sets the checkmark label's stylesheet and visibility based on done state.
    def _update_check_label(self):
        if self.task.done:
            self._check_lbl.setStyleSheet(TaskStyler.check_label_css(self.section_name))
            self._check_lbl.setVisible(True)
        else:
            self._check_lbl.setVisible(False)

    # Updates the edit's displayed text to an elided version that fits the current
    # widget width, and sets a tooltip with the full text when elision occurs.
    def _refresh_display_text(self):
        if not self.edit.isReadOnly():
            return  # don't interfere while user is editing
        available = self.edit.width() - 4  # approx padding
        if available <= 0:
            self.edit.setText(self.task.text)
            self.edit.setToolTip("")
            return
        fm = QFontMetrics(self.edit.font())
        elided = fm.elidedText(self.task.text, Qt.ElideRight, available)
        self.edit.setText(elided)
        self.edit.setToolTip(self.task.text if elided != self.task.text else "")

    # Triggers a display-text refresh whenever the widget is resized so that
    # elision stays in sync with the available width.
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_display_text()

    # Toggles the task's done state on a left-click when the edit is in read-only
    # mode. Passes through to the default handler for all other cases.
    def _on_mouse_press(self, event):
        if event.button() == Qt.LeftButton and self.edit.isReadOnly():
            # Recurring tasks cannot be completed — click is a no-op
            if self.section_name in RECURRING:
                return
            self.task.done = not self.task.done
            self._apply_style()
            self.changed.emit()
        else:
            QLineEdit.mousePressEvent(self.edit, event)

    # Activates inline editing on a left double-click, clearing the done flag
    # so that editing an already-completed task implicitly un-completes it.
    def _on_double_click(self, event):
        if event.button() == Qt.LeftButton:
            self._edit_ctrl.begin()
            if self.task.done:
                self.task.done = False
                self._apply_style()
                self.changed.emit()
            self._edit_ctrl.focus()

    # Refreshes styling and emits changed when the controller commits a non-empty edit.
    def _on_text_committed(self):
        self._apply_style()
        self.changed.emit()

    # Opens inline editing, clearing done status if needed. Defers focus via
    # QTimer to avoid conflicts when called from a context menu action.
    def _start_edit(self):
        self._edit_ctrl.begin()
        if self.task.done:
            self.task.done = False
            self._apply_style()
            self.changed.emit()
        QTimer.singleShot(0, self._edit_ctrl.focus)

    # Delegates menu construction to TaskContextMenuBuilder, then shows the menu.
    def _show_context_menu(self, pos):
        menu = self._build_context_menu()
        menu.exec(self.mapToGlobal(pos))

    # Constructs and returns the right-click context menu via TaskContextMenuBuilder.
    def _build_context_menu(self) -> QMenu:
        builder = TaskContextMenuBuilder(self.task, self.section_name)
        return builder.build(
            self,
            on_edit=self._start_edit,
            on_delete=lambda: self.delete_requested.emit(self),
            on_move_to_tomorrow=(
                (lambda: self.move_to_tomorrow.emit(self))
                if self.section_name == "Today" else None
            ),
            on_mark_urgent=self._mark_urgent,
            on_clear_urgent=self._clear_urgent,
            on_mark_important=self._mark_important,
            on_clear_important=self._clear_important,
            on_set_due_date=(
                self._set_due_date if self.section_name in ("Weekly", "Annually") else None
            ),
        )

    # Sets the task's next_date to the given date, refreshes styling, and
    # emits changed to trigger an auto-save.
    def _set_due_date(self, d: date) -> None:
        self.task.next_date = d
        self._apply_style()
        self.changed.emit()

    # Sets the task as urgent, refreshes styling, and emits promote_urgent so
    # the parent section can move this task to the top of Today if needed.
    def _mark_urgent(self):
        self.task.urgent = True
        self._apply_style()
        self.changed.emit()
        self.promote_urgent.emit(self)

    # Clears the urgent flag, refreshes styling, and emits cleared_urgent so
    # the parent section can re-sort tasks.
    def _clear_urgent(self):
        self.task.urgent = False
        self._apply_style()
        self.changed.emit()
        self.cleared_urgent.emit(self)

    # Sets the important flag, refreshes styling, and emits marked_important so
    # the parent section can re-sort tasks.
    def _mark_important(self):
        self.task.important = True
        self._apply_style()
        self.changed.emit()
        self.marked_important.emit(self)

    # Clears the important flag, refreshes styling, and emits cleared_important
    # so the parent section can re-sort tasks.
    def _clear_important(self):
        self.task.important = False
        self._apply_style()
        self.changed.emit()
        self.cleared_important.emit(self)

    # Handles Delete and Backspace in read-only mode as a keyboard shortcut
    # to delete the task; delegates all other key events to the base class.
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.edit.isReadOnly():
            self.delete_requested.emit(self)
        else:
            super().keyPressEvent(event)

    # Required override to make QSS background-color rules take effect on a
    # plain QWidget subclass (Qt only applies them when paintEvent calls drawPrimitive).
    def paintEvent(self, event):
        opt = QStyleOption()
        opt.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self)


# A QLineEdit pre-configured as the "add task" input at the bottom of each section.
class AddTaskEdit(QLineEdit):
    # Initialises the placeholder text, frame, cursor, and stylesheet.
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("add task…")
        self.setFrame(False)
        self.setStyleSheet(
            "QLineEdit { border: none; background: transparent; color: #aaa; padding: 2px 0; }"
            "QLineEdit:focus { color: #222; border-bottom: 1px solid #ddd; }"
        )
        self.setCursor(Qt.IBeamCursor)


# Displays one named section (e.g. "Today", "Daily") as a vertical stack of
# TaskWidgets beneath a section header, followed by an add-task input.
# Owns the authoritative task list for its section and handles add, delete,
# move, sort, and cross-section promotion operations.
class SectionWidget(QWidget):
    changed = Signal()
    task_moved_to_tomorrow = Signal(object, object)   # task, from_section_widget
    task_promoted_urgent = Signal(object, object)     # task, from_section_widget

    # Stores the section name and task list, applies the section background colour,
    # and delegates layout construction to _setup_ui.
    def __init__(self, section_name: str, tasks: list[Task], parent=None):
        super().__init__(parent)
        self.section_name = section_name
        self.tasks = tasks
        if section_name in SECTION_BG:
            self.setStyleSheet(f"background-color: {SECTION_BG[section_name]};")
        elif section_name in RECURRING:
            self.setStyleSheet(f"background-color: {RECURRING_BG};")
        self._setup_ui()

    # Builds the section layout: header label, task container, and add-task input.
    def _setup_ui(self):
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 8, 16, 4)
        self.layout_.setSpacing(0)

        # Section header — rich text for selective underline/italic formatting
        if self.section_name == "Today":
            d = date.today()
            label_html = f'<u>TODAY</u> ({d.strftime("%b")} {d.day})'
            label_css = ("font-size: 13pt; font-weight: 700; color: #555; "
                         "padding: 4px 0 8px 0; background: transparent;")
        else:
            name = self.section_name.upper()
            label_html = (f'<i><u>{name}</u></i>' if self.section_name in RECURRING
                          else f'<u>{name}</u>')
            label_css = ("font-size: 9pt; font-weight: 600; color: #999; "
                         "letter-spacing: 1px; padding: 4px 0 6px 0; background: transparent;")
        header = QLabel(label_html)
        header.setStyleSheet(f"QLabel {{ {label_css} }}")
        self.layout_.addWidget(header)

        # Task widgets
        self.task_container = QVBoxLayout()
        self.task_container.setSpacing(0)
        self.task_container.setContentsMargins(0, 0, 0, 0)

        for task in self.tasks:
            self._add_task_widget(task)

        self.layout_.addLayout(self.task_container)

        # Add task input
        self.add_edit = AddTaskEdit()
        self.add_edit.returnPressed.connect(self._on_add_task)
        self.layout_.addWidget(self.add_edit)

    # Connects all signals from a TaskWidget to the appropriate section-level handlers.
    def _connect_task_widget(self, tw: TaskWidget) -> None:
        tw.changed.connect(self.changed)
        tw.delete_requested.connect(self._on_delete_task)
        tw.move_to_tomorrow.connect(self._on_move_to_tomorrow)
        tw.promote_urgent.connect(lambda tw=tw: self.task_promoted_urgent.emit(tw.task, self))
        tw.marked_important.connect(lambda: self._sort_tasks())
        tw.cleared_urgent.connect(self._on_task_sort)
        tw.cleared_important.connect(self._on_task_sort)

    # Creates a TaskWidget for the given task, connects its signals, appends it
    # to the task container layout, and returns it.
    def _add_task_widget(self, task: Task) -> TaskWidget:
        tw = TaskWidget(task, self.section_name)
        self._connect_task_widget(tw)
        self.task_container.addWidget(tw)
        return tw

    # Inserts a task and its widget at the top of the section (used when an
    # urgent task is promoted into Today from another section).
    def insert_task_at_top(self, task: Task) -> TaskWidget:
        self.tasks.insert(0, task)
        tw = TaskWidget(task, self.section_name)
        self._connect_task_widget(tw)
        self.task_container.insertWidget(0, tw)
        self.changed.emit()
        return tw

    # Finds the TaskWidget for the given task object and triggers its deletion.
    # Used by MainWindow to remove a task that has been moved to another section.
    def _remove_task_object(self, task: Task) -> None:
        for i in range(self.task_container.count()):
            item = self.task_container.itemAt(i)
            if item and isinstance(item.widget(), TaskWidget) and item.widget().task is task:
                self._on_delete_task(item.widget())
                return

    # Reads the add-task input, creates a new Task (with a next_date for recurring
    # sections), appends it, and clears the input field.
    def _on_add_task(self):
        text = self.add_edit.text().strip()
        if not text:
            return
        task = Task(text=text)
        if self.section_name in RECURRING:
            task.next_date = next_date_for(self.section_name, date.today())
        self.tasks.append(task)
        self._add_task_widget(task)
        self.add_edit.clear()
        self.changed.emit()

    # Removes a TaskWidget from the layout and its underlying Task from the task
    # list, then emits changed to trigger an auto-save.
    def _on_delete_task(self, task_widget: TaskWidget):
        idx = self.task_container.indexOf(task_widget)
        if idx >= 0:
            self.task_container.removeWidget(task_widget)
            task_widget.deleteLater()
            if task_widget.task in self.tasks:
                self.tasks.remove(task_widget.task)
            self.changed.emit()

    # Forwards a move-to-tomorrow request to the main window via signal, then
    # removes the task from this section.
    def _on_move_to_tomorrow(self, task_widget: TaskWidget):
        self.task_moved_to_tomorrow.emit(task_widget.task, self)
        self._on_delete_task(task_widget)

    # Appends a task and its widget to this section from an external source
    # (e.g. when a task is moved here from another section).
    def add_task_from_outside(self, task: Task):
        self.tasks.append(task)
        self._add_task_widget(task)
        self.changed.emit()

    # Delegates to _sort_tasks with penalize=True so the changed task sinks
    # toward the bottom of its priority band after a flag is cleared.
    def _on_task_sort(self, changed_tw: TaskWidget):
        self._sort_tasks(changed_tw, penalize=True)

    # Re-sorts all TaskWidgets in the container by priority (urgent+important first,
    # then urgent, then important, then plain). When penalize=True the changed widget
    # is nudged one rank lower to reflect the cleared flag.
    def _sort_tasks(self, changed_tw=None, penalize=False):
        # Returns an integer sort key; lower = higher priority in the list.
        def sort_key(tw):
            t = tw.task
            c = penalize and (tw is changed_tw)
            if t.urgent and t.important:
                return 0
            elif t.urgent:
                return 2 if c else 1
            elif t.important:
                return 4 if c else 3
            else:
                return 6 if c else 5

        widgets = [
            self.task_container.itemAt(i).widget()
            for i in range(self.task_container.count())
            if isinstance(self.task_container.itemAt(i).widget(), TaskWidget)
        ]
        widgets.sort(key=sort_key)

        for tw in widgets:
            self.task_container.removeWidget(tw)
        for tw in widgets:
            self.task_container.addWidget(tw)

        self.tasks[:] = [tw.task for tw in widgets]

    # Required override — same reason as TaskWidget.paintEvent.
    def paintEvent(self, event):
        opt = QStyleOption()
        opt.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self)


# A thin horizontal bar between the non-recurring and recurring sections that
# provides a collapse/expand toggle for all recurring section widgets.
class RecurringSeparatorWidget(QWidget):
    toggled = Signal(bool)  # True = collapsed

    # Builds the separator: a full-width line, a collapsed-state task count label,
    # and a toggle button.
    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self.setStyleSheet(f"background-color: {RECURRING_BG};")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 10, 4)
        layout.setSpacing(8)

        # Thin black separator line
        self._line = QFrame()
        self._line.setFixedHeight(1)
        self._line.setStyleSheet("background-color: #000000; border: none;")
        layout.addWidget(self._line, stretch=1)

        # Count label — only visible when collapsed
        self._count_lbl = QLabel()
        self._count_lbl.setStyleSheet(
            "QLabel { font-size: 8pt; color: #666; background: transparent; }"
        )
        self._count_lbl.setVisible(False)
        layout.addWidget(self._count_lbl)

        # Toggle button
        self._btn = QPushButton("▾")
        self._btn.setFlat(True)
        self._btn.setFixedSize(20, 20)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            "QPushButton { border: none; font-size: 10pt; background: transparent; "
            "color: #666; padding: 0; }"
            "QPushButton:hover { color: #000; }"
        )
        self._btn.clicked.connect(self._toggle)
        layout.addWidget(self._btn)

    # Flips the collapsed state, updates the button arrow and count label
    # visibility, and emits toggled so the main window can show/hide sections.
    def _toggle(self):
        self._collapsed = not self._collapsed
        self._btn.setText("▸" if self._collapsed else "▾")
        self._count_lbl.setVisible(self._collapsed)
        self.toggled.emit(self._collapsed)

    # Updates the task count shown in the count label when the separator is collapsed.
    def update_count(self, count: int):
        self._count_lbl.setText(f"{count} recurring tasks")

    # Required override — same reason as TaskWidget.paintEvent.
    def paintEvent(self, event):
        opt = QStyleOption()
        opt.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self)


# The top-level application window. Owns a scrollable column of SectionWidgets,
# a RecurringSeparatorWidget, a debounced auto-save timer, and all cross-section
# coordination logic (task moves, urgent promotions, recurring collapse).
class MainWindow(QMainWindow):
    # Stores the sections dict and save callback, initialises the debounce timer,
    # and delegates layout construction to _setup_ui.
    def __init__(self, sections: dict, save_callback: Callable, parent=None):
        super().__init__(parent)
        self.sections = sections
        self.save_callback = save_callback
        self.section_widgets: dict[str, SectionWidget] = {}

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._do_save)

        self._setup_ui()

    # Builds the main window layout: a scroll area containing all section widgets
    # in SECTION_ORDER, with the recurring separator inserted before "Daily".
    def _setup_ui(self):
        self.setWindowTitle("tedium")
        self.setMinimumSize(230, 600)
        self.resize(270, 900)
        self.menuBar().setVisible(False)
        self.statusBar().setVisible(False)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        self._main_layout = QVBoxLayout(container)
        self._main_layout.setContentsMargins(0, 8, 0, 16)
        self._main_layout.setSpacing(0)

        self._recurring_sep = RecurringSeparatorWidget()
        self._recurring_sep.toggled.connect(self._on_recurring_toggled)

        for section_name in SECTION_ORDER:
            if section_name == "Daily":  # first recurring section — insert separator above
                self._main_layout.addWidget(self._recurring_sep)
            tasks = self.sections.get(section_name, [])
            sw = SectionWidget(section_name, tasks)
            sw.changed.connect(self._on_change)
            sw.task_moved_to_tomorrow.connect(self._on_task_moved_to_tomorrow)
            sw.task_promoted_urgent.connect(self._on_task_promoted_urgent)
            self.section_widgets[section_name] = sw
            self._main_layout.addWidget(sw)

        filler = QFrame()
        filler.setStyleSheet(f"background-color: {RECURRING_BG}; border: none;")
        filler.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._main_layout.addWidget(filler)

        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    # Restarts the debounce timer on every task change; the actual save fires
    # 500 ms after the last change, preventing excessive disk writes during rapid edits.
    def _on_change(self):
        self._save_timer.start()

    # Invokes the save callback with the current sections dict.
    def _do_save(self):
        self.save_callback(self.sections)

    # Handles a task being moved from Today to Tomorrow: creates a copy of the
    # task with urgency dropped (urgency is Today-specific) and adds it to Tomorrow.
    def _on_task_moved_to_tomorrow(self, task: Task, from_sw: SectionWidget):
        tomorrow_sw = self.section_widgets.get("Tomorrow")
        if tomorrow_sw:
            moved_task = Task(
                text=task.text,
                done=False,
                urgent=False,  # Urgency is dropped when deferring to tomorrow
                important=task.important,
            )
            tomorrow_sw.add_task_from_outside(moved_task)

    # Routes an urgent-promotion event to the correct cross-section operation
    # depending on whether the source is a recurring section, Today, or elsewhere.
    def _on_task_promoted_urgent(self, task: Task, from_sw: SectionWidget):
        today_sw = self.section_widgets["Today"]
        if from_sw.section_name in RECURRING:
            self._promote_from_recurring(task, today_sw)
        elif from_sw.section_name == "Today":
            today_sw._sort_tasks()  # task.urgent already set; just re-sort
        else:
            self._promote_from_other(task, from_sw, today_sw)

    # Copies a recurring task to Today (keeping the original in the recurring
    # section). No-ops if a task with the same text is already present in Today.
    def _promote_from_recurring(self, task: Task, today_sw: SectionWidget):
        if any(t.text == task.text for t in today_sw.tasks):
            return  # Already present — no duplicate
        today_sw.add_task_from_outside(Task(
            text=task.text, urgent=True, important=task.important
        ))
        today_sw._sort_tasks()

    # Moves a task from its current section (e.g. Tomorrow) into Today.
    # No-ops if a task with the same text is already present in Today.
    def _promote_from_other(self, task: Task, from_sw: SectionWidget, today_sw: SectionWidget):
        if any(t.text == task.text for t in today_sw.tasks):
            return  # Already present — no duplicate
        from_sw._remove_task_object(task)
        today_sw.add_task_from_outside(task)
        today_sw._sort_tasks()

    # Shows or hides all recurring SectionWidgets and updates the separator's
    # task count label when the collapse state changes.
    def _on_recurring_toggled(self, collapsed: bool):
        count = sum(len(self.section_widgets[s].tasks) for s in RECURRING)
        self._recurring_sep.update_count(count)
        for s in RECURRING:
            self.section_widgets[s].setVisible(not collapsed)
