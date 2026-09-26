"""Building blocks for the Settings window: pages made of titled sections and aligned rows.

Every row is `label | control` with optional help text under the control, so pages read the same
way everywhere (no more form layouts stretching labels and boxes differently per tab). Rows and
sections know their own text, which is what the search box filters on.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QToolButton, QVBoxLayout,
                               QWidget)

LABEL_WIDTH = 236        # the label column; long labels wrap inside it instead of being cut off
CONTENT_MAX = 860        # pages don't stretch their controls across a huge window


class Section(QWidget):
    """A small caps heading with a rule under it."""

    def __init__(self, title: str):
        super().__init__()
        self.title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 18, 0, 4)
        layout.setSpacing(4)
        head = QLabel(title.upper())
        head.setObjectName("section")
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        layout.addWidget(head)
        layout.addWidget(rule)


class Row(QWidget):
    """label | control (+ help text). `text` is what the search box matches against."""

    def __init__(self, label: str, control: QWidget | None, help_text: str = "", keywords: str = "",
                 full_width: bool = False, resettable: bool = False):
        super().__init__()
        self.label_text = label
        self.reset: QToolButton | None = None
        self.text = f"{label} {help_text} {keywords}".lower()
        self.section: Section | None = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 5, 0, 5)
        outer.setSpacing(18)
        outer.setAlignment(Qt.AlignTop)
        if not full_width:
            left = QLabel(label)
            left.setWordWrap(True)
            left.setFixedWidth(LABEL_WIDTH)
            left.setAlignment(Qt.AlignRight | Qt.AlignTop)
            left.setContentsMargins(0, 6, 0, 0)          # baseline-align with the control's text
            outer.addWidget(left, 0, Qt.AlignTop)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        if control is not None:
            # A capped-width control (a number box) must not cap the whole column: that would squeeze the
            # help text under it into a narrow strip and clip it. So it sits in a line of its own, next to
            # the optional "reset to default" button, and the line takes the column's full width.
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)
            capped = control.maximumWidth() < 16777215
            line.addWidget(control, 0 if capped else 1, Qt.AlignTop)
            if resettable:                       # a small "back to default" button, shown only when it differs
                self.reset = QToolButton()
                self.reset.setText("↺")
                self.reset.setToolTip("Reset to the default")
                self.reset.setAccessibleName(f"Reset {label or 'this setting'} to the default")
                self.reset.setCursor(Qt.PointingHandCursor)
                self.reset.setAutoRaise(True)
                self.reset.hide()
                line.addWidget(self.reset, 0, Qt.AlignTop)
            if capped:
                line.addStretch(1)
            right.addLayout(line)
        if help_text:
            note = QLabel(help_text)
            note.setObjectName("muted")
            note.setWordWrap(True)
            right.addWidget(note)
        outer.addLayout(right, 1)


class SettingsPage(QScrollArea):
    def __init__(self, key: str, title: str, icon: str = "", description: str = "", live: bool = False):
        super().__init__()
        self.key, self.title, self.icon, self.live = key, title, icon, live
        self.keys: list[str] = []                      # config keys owned by this page (for "restore defaults")
        self._rows: list[Row] = []
        self._sections: list[Section] = []
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)   # let the dialog's background show through

        holder = QWidget()
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(30, 24, 30, 30)
        column = QWidget()
        column.setMaximumWidth(CONTENT_MAX)
        self._body = QVBoxLayout(column)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)
        outer.addWidget(column, 1)
        outer.addStretch(0)
        self.setWidget(holder)

        heading = QLabel(f"{title}")
        heading.setObjectName("pageTitle")
        self._body.addWidget(heading)
        if live:
            badge = QLabel("⚡ Changes on this page apply instantly")
            badge.setObjectName("liveBadge")
            self._body.addWidget(badge)
        if description:
            blurb = QLabel(description)
            blurb.setObjectName("muted")
            blurb.setWordWrap(True)
            blurb.setContentsMargins(0, 4, 0, 0)
            self._body.addWidget(blurb)
        self._body.addStretch(1)                       # rows are inserted before this

    # ---- building ---------------------------------------------------------------------------
    def _append(self, widget: QWidget) -> None:
        self._body.insertWidget(self._body.count() - 1, widget)

    def section(self, title: str) -> Section:
        sec = Section(title)
        self._sections.append(sec)
        self._append(sec)
        return sec

    def row(self, label: str, control: QWidget | None, help_text: str = "", keywords: str = "",
            full_width: bool = False, resettable: bool = False) -> Row:
        row = Row(label, control, help_text, keywords, full_width, resettable)
        row.section = self._sections[-1] if self._sections else None
        self._rows.append(row)
        self._append(row)
        return row

    def check(self, box: QWidget, help_text: str = "", keywords: str = "", resettable: bool = False) -> Row:
        """A checkbox sits in the control column, lined up with the other controls."""
        return self.row("", box, help_text, f"{box.text()} {keywords}", resettable=resettable)

    def note(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        row = self.row("", label, "", text)
        return row

    # ---- search -------------------------------------------------------------------------------------
    def matches(self, query: str) -> bool:
        return not query or query in self.title.lower() or any(query in r.text for r in self._rows)

    def filter(self, query: str) -> bool:
        """Shows only the rows matching `query` (all of them if the page title matches). Returns
        True if anything on the page is visible."""
        query = query.strip().lower()
        title_hit = not query or query in self.title.lower()
        visible_sections: set[int] = set()
        any_row = False
        for row in self._rows:
            show = (title_hit or query in row.text) and not getattr(row, "suppressed", False)
            row.setVisible(show)
            if show:
                any_row = True
                if row.section is not None:
                    visible_sections.add(id(row.section))
        for sec in self._sections:
            sec.setVisible(not query or id(sec) in visible_sections or query in sec.title.lower())
        return any_row or title_hit


class StripRulesEditor(QWidget):
    """Settings > Notifications > "Remove before reading": the rules (one per line; /pattern/ for a regular
    expression), a menu of common ones, and a line to try them on that shows the result as you type."""

    changed = Signal()
    SAMPLE = "Alex (My Server) - #general: are you free? [3 new messages]"

    def __init__(self):
        super().__init__()
        import notifications
        self._notifications = notifications
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.rules = QPlainTextEdit()
        self.rules.setMinimumHeight(90)
        self.rules.setMaximumHeight(130)
        self.rules.setPlaceholderText("(My Server)\n- Outlook\n/\\(\\d+ new\\)/")
        self.rules.setAccessibleName("Rules, one per line")
        layout.addWidget(self.rules)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add a common rule")
        menu = QMenu(self.add_button)
        for label, rule in notifications.COMMON_STRIP_RULES:
            menu.addAction(label).triggered.connect(lambda _c=False, r=rule: self.add_rule(r))
        self.add_button.setMenu(menu)
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        try_row = QHBoxLayout()
        try_label = QLabel("Try it on")
        try_label.setObjectName("muted")
        self.sample = QLineEdit(self.SAMPLE)
        self.sample.setAccessibleName("Sample notification to try the rules on")
        try_row.addWidget(try_label)
        try_row.addWidget(self.sample, 1)
        layout.addLayout(try_row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.result)
        self.problems = QLabel("")
        self.problems.setObjectName("error")
        self.problems.setWordWrap(True)
        self.problems.hide()
        layout.addWidget(self.problems)

        self.rules.textChanged.connect(self._update)
        self.rules.textChanged.connect(self.changed)
        self.sample.textChanged.connect(self._update)
        self._update()

    def add_rule(self, rule: str) -> None:
        lines = [line for line in self.rules.toPlainText().splitlines() if line.strip()]
        if rule not in lines:
            lines.append(rule)
            self.rules.setPlainText("\n".join(lines))

    def _update(self) -> None:
        n = self._notifications
        text = self.rules.toPlainText()
        read = n.strip_text(self.sample.text(), n.strip_rules(text))
        self.result.setText(f"Read as: {read}" if read else "Read as: (nothing left)")
        problems = n.strip_problems(text)
        self.problems.setText("\n".join(problems))
        self.problems.setVisible(bool(problems))

    def value(self) -> str:
        return self.rules.toPlainText().strip()

    def set_value(self, value) -> None:
        text = "\n".join(value) if isinstance(value, (list, tuple)) else str(value or "")
        if text != self.rules.toPlainText():
            self.rules.setPlainText(text)


class RulesEditor(QWidget):
    """A small table of per-app notification rules: 'app name contains' -> what to do."""

    changed = Signal()
    MODES = (("ask", "Ask whether to summarize"), ("summarize", "Summarize it out loud"), ("read", "Read it out loud"),
             ("message", "Read just the message"), ("card", "Card only"), ("silent", "Keep it quietly in the history"),
             ("ignore", "Ignore it completely"))

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["App name contains", "Then"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 210)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(210)
        self.table.itemChanged.connect(lambda _i: self.changed.emit())
        buttons = QHBoxLayout()
        add = QPushButton("Add rule")
        remove = QPushButton("Remove selected")
        add.clicked.connect(lambda: self.add_rule("", "ask"))
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def add_rule(self, app: str = "", mode: str = "ask") -> None:
        row = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(row)
        from PySide6.QtWidgets import QTableWidgetItem
        self.table.setItem(row, 0, QTableWidgetItem(app))
        combo = QComboBox()
        for value, label in self.MODES:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(mode)))
        combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.table.setCellWidget(row, 1, combo)
        self.table.blockSignals(False)
        self.changed.emit()
        if not app:
            self.table.editItem(self.table.item(row, 0))

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if rows:
            self.changed.emit()

    def value(self) -> list[dict]:
        rules = []
        for r in range(self.table.rowCount()):
            item, combo = self.table.item(r, 0), self.table.cellWidget(r, 1)
            app = item.text().strip() if item else ""
            if app and combo is not None:
                rules.append({"app": app, "mode": combo.currentData()})
        return rules

    def set_value(self, rules) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for rule in rules or []:
            if isinstance(rule, dict):
                self.add_rule(str(rule.get("app", "")), str(rule.get("mode", "ask")))
        self.changed.emit()


class RoutinesEditor(QWidget):
    """Named routines: a list of names on the left, that routine's steps on the right.

    A table would have been the obvious choice, but a routine's steps are a *list* of free text --
    they belong in a text box, one per line, where they can be read and reordered."""

    changed = Signal()

    PLACEHOLDER = ("One step per line, for example:\n"
                   "open: Visual Studio Code\n"
                   "url: https://github.com\n"
                   "set the volume to 25 percent\n"
                   "keys: win+d\n"
                   "wait: 2\n"
                   "say: Focus time.")

    def __init__(self):
        super().__init__()
        from PySide6.QtWidgets import QListWidget, QPlainTextEdit
        self._routines: list[dict] = []
        self._current = -1
        self._loading = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(6)
        self.list = QListWidget()
        self.list.setFixedWidth(200)
        self.list.setMinimumHeight(170)
        self.list.currentRowChanged.connect(self._select)
        self.list.itemChanged.connect(self._renamed)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.setToolTip("Add a routine and give it the phrase you'll say")
        remove = QPushButton("Remove")
        add.clicked.connect(lambda: self.add_routine("new routine", []))
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        left.addWidget(self.list, 1)
        left.addLayout(buttons)
        layout.addLayout(left)

        from PySide6.QtWidgets import QLineEdit
        right = QVBoxLayout()
        right.setSpacing(6)
        self.steps = QPlainTextEdit()
        self.steps.setPlaceholderText(self.PLACEHOLDER)
        self.steps.setMinimumHeight(170)
        self.steps.textChanged.connect(self._steps_edited)
        self.step_note = QLabel("")
        self.step_note.setObjectName("error")
        self.step_note.setWordWrap(True)
        self.step_note.hide()
        from PySide6.QtWidgets import QKeySequenceEdit
        keys_line = QHBoxLayout()
        keys_line.setSpacing(6)
        self.key_record = QKeySequenceEdit()
        self.key_record.setMaximumSequenceLength(1)
        self.key_record.setToolTip("Click here and press the keys the step should press")
        self.key_add = QPushButton("Add key press")
        self.key_add.setToolTip("Adds a step that presses the combination on the left")
        self.key_add.clicked.connect(self._add_key_step)
        keys_line.addWidget(self.key_record, 1)
        keys_line.addWidget(self.key_add)
        self.when = QLineEdit()
        self.when.setPlaceholderText("Run it by itself (optional): weekdays at 9:00, when discord starts, at startup")
        self.when.textChanged.connect(self._when_edited)
        self.when_note = QLabel("")
        self.when_note.setObjectName("muted")
        self.when_note.setWordWrap(True)
        right.addWidget(self.steps, 1)
        right.addWidget(self.step_note)
        right.addLayout(keys_line)
        right.addWidget(self.when)
        right.addWidget(self.when_note)
        layout.addLayout(right, 1)
        self._sync_enabled()

    # ---- editing -------------------------------------------------------------------------
    def add_routine(self, name: str = "new routine", steps=(), when: str = "") -> None:
        from PySide6.QtWidgets import QListWidgetItem
        self._routines.append({"name": str(name), "steps": [str(s) for s in steps], "when": str(when or "")})
        item = QListWidgetItem(str(name))
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self._loading = True
        self.list.addItem(item)
        self._loading = False
        self.list.setCurrentRow(self.list.count() - 1)
        self.changed.emit()
        if name == "new routine":
            self.list.editItem(item)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self._loading = True
        self.list.takeItem(row)
        self._loading = False
        del self._routines[row]
        self._current = min(row, len(self._routines) - 1)
        self.list.setCurrentRow(self._current)
        self._select(self._current)
        self.changed.emit()

    def _select(self, row: int) -> None:
        self._current = row
        self._loading = True
        valid = 0 <= row < len(self._routines)
        self.steps.setPlainText("\n".join(self._routines[row]["steps"]) if valid else "")
        self.when.setText(self._routines[row].get("when", "") if valid else "")
        self._loading = False
        self._describe_when()
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        valid = 0 <= self._current < len(self._routines)
        for w in (self.steps, self.when, self.key_record, self.key_add):
            w.setEnabled(valid)

    def _add_key_step(self) -> None:
        """Append "keys: ctrl+shift+esc" for the combination recorded in the box."""
        from PySide6.QtGui import QKeySequence
        combo = self.key_record.keySequence().toString(QKeySequence.PortableText)
        if not combo or not (0 <= self._current < len(self._routines)):
            return
        combo = combo.lower().replace("meta+", "win+")
        text = self.steps.toPlainText().rstrip("\n")
        self.steps.setPlainText(f"{text}\nkeys: {combo}" if text else f"keys: {combo}")
        self.key_record.clear()

    def _check_steps(self) -> None:
        import routines
        problems = [f"Line {i}: {problem}" for i, line in enumerate(self.steps.toPlainText().splitlines(), 1)
                    if line.strip() and (problem := routines.check_step(line))]
        self.step_note.setText("\n".join(problems))
        self.step_note.setVisible(bool(problems))

    def _when_edited(self) -> None:
        self._describe_when()
        if self._loading or not (0 <= self._current < len(self._routines)):
            return
        self._routines[self._current]["when"] = self.when.text().strip()
        self.changed.emit()

    def _describe_when(self) -> None:
        import routines
        text = self.when.text().strip()
        if not text:
            self.when_note.setText("Runs only when you say its name.")
            return
        trigger = routines.parse_trigger(text)
        self.when_note.setText(f"Also runs {routines.describe_trigger(trigger)}." if trigger else
                               "I don't understand that time. Try \"weekdays at 9:00\", \"every day at 10pm\", "
                               "\"when chrome starts\" or \"at startup\".")

    def _steps_edited(self) -> None:
        self._check_steps()
        if self._loading or not (0 <= self._current < len(self._routines)):
            return
        self._routines[self._current]["steps"] = [line.strip() for line in
                                                  self.steps.toPlainText().splitlines() if line.strip()]
        self.changed.emit()

    def _renamed(self, item) -> None:
        if self._loading:
            return
        row = self.list.row(item)
        if 0 <= row < len(self._routines):
            self._routines[row]["name"] = item.text().strip()
            self.changed.emit()

    # ---- value ---------------------------------------------------------------------------
    def value(self) -> list[dict]:
        out = []
        for r in self._routines:
            if r["name"].strip() and r["steps"]:
                item = {"name": r["name"].strip().lower(), "steps": list(r["steps"])}
                if r.get("when", "").strip():
                    item["when"] = r["when"].strip()
                out.append(item)
        return out

    def set_value(self, routines) -> None:
        self._loading = True
        self.list.clear()
        self._loading = False
        self._routines = []
        self._current = -1
        for routine in routines or []:
            if isinstance(routine, dict):
                steps = routine.get("steps", [])
                self.add_routine(str(routine.get("name", "")), steps if isinstance(steps, list) else [],
                                 str(routine.get("when", "") or ""))
        if self._routines:
            self.list.setCurrentRow(0)
        self._sync_enabled()
        self.changed.emit()


class MemoryList(QWidget):
    """What the assistant has been told to remember: a table you can search, edit in place (double-click
    the topic, the fact or its expiry), and forget rows from. Rows are addressed by the memory's id, so
    forgetting one never takes a similar-sounding one with it.

    `store` is the memory_store module (or anything with all_memories / update / forget_id / reset /
    describe_expiry / expiry_time)."""

    changed = Signal()
    COLUMNS = ("Topic", "What I remember", "Added", "Forget it")

    def __init__(self, store):
        super().__init__()
        from PySide6.QtWidgets import QAbstractItemView, QLineEdit
        self._store = store
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search what I remember...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(170)
        self.table.setMaximumHeight(260)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._edited)
        self.empty = QLabel("Nothing yet. Say \u201cremember that my gate code is 4821\u201d (or \u201cremember until "
                            "Friday that ...\u201d) and it will appear here.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        self.hint = QLabel("Double-click to edit. In \u201cForget it\u201d type when: \u201cfriday\u201d, "
                           "\u201c2 hours\u201d, \u201ctomorrow\u201d, or \u201cnever\u201d.")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        row = QHBoxLayout()
        forget_one = QPushButton("Forget selected")
        forget_one.clicked.connect(self._forget_selected)
        clear = QPushButton("Forget everything")
        clear.clicked.connect(self._forget_everything)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        for b in (forget_one, clear, refresh):
            row.addWidget(b)
        row.addStretch(1)
        layout.addWidget(self.filter)
        layout.addWidget(self.table)
        layout.addWidget(self.empty)
        layout.addWidget(self.hint)
        layout.addLayout(row)
        self.reload()

    def reload(self) -> None:
        from datetime import datetime

        from PySide6.QtWidgets import QTableWidgetItem
        items = list(reversed(self._store.all_memories()))         # newest first
        self._filling = True
        self.table.setRowCount(len(items))
        for r, item in enumerate(items):
            added = datetime.fromtimestamp(item["created"]).strftime("%b %d, %H:%M") if item.get("created") else ""
            expires = self._store.describe_expiry(item.get("expires", 0)).removeprefix("until ") or "never"
            cells = (item["key"], item["value"], added, expires)
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, item["id"])
                cell.setToolTip(item["value"] if c == 1 else text)
                if c == 2:
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, cell)
        self._filling = False
        self.table.setVisible(bool(items))
        self.filter.setVisible(len(items) > 5)
        self.hint.setVisible(bool(items))
        self.empty.setVisible(not items)
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        for r in range(self.table.rowCount()):
            words = " ".join((self.table.item(r, c).text() if self.table.item(r, c) else "") for c in (0, 1)).lower()
            self.table.setRowHidden(r, bool(query) and query not in words)

    def _edited(self, cell) -> None:
        if self._filling:
            return
        item_id, text, column = cell.data(Qt.UserRole), cell.text().strip(), cell.column()
        if column == 0:
            self._store.update(item_id, key=text)
        elif column == 1:
            if not text:
                self.reload()                   # an empty fact isn't a fact: put the old text back
                return
            self._store.update(item_id, value=text)
        elif column == 3:
            if text.lower() in ("", "never", "keep", "forever", "no"):
                self._store.update(item_id, expires=0)
            else:
                how = "for" if text.lower()[:1].isdigit() and any(u in text.lower() for u in ("min", "hour", "day", "week")) else "until"
                when = self._store.expiry_time(how, text.lower().removeprefix("until ").removeprefix("for "))
                if when:
                    self._store.update(item_id, expires=when)
        self.reload()
        self.changed.emit()

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows if self.table.item(r, 0)]

    def _forget_selected(self) -> None:
        ids = self.selected_ids()
        for item_id in ids:
            self._store.forget_id(item_id)
        if ids:
            self.reload()
            self.changed.emit()

    def _forget_everything(self) -> None:
        self._store.reset()
        self.reload()
        self.changed.emit()
