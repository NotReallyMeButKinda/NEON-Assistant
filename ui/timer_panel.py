"""Timers without talking: one click for a common length, or type one ("25", "1:30", "10m", "half an
hour") with an optional name. Running timers count down in a list and can be cancelled one by one.

The timers are the same ones "set a timer for 10 minutes" makes: they go off with a card, a sound and
a spoken line, survive a restart, and show on the status bar.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

import timers

from . import frame
from .theme import COLORS, enable_dark_title_bar
from .theme import signals as theme_signals

PRESETS = (("1 min", 60), ("3 min", 180), ("5 min", 300), ("10 min", 600), ("15 min", 900), ("30 min", 1800),
           ("1 hour", 3600))
MAX_SECONDS = 24 * 3600


def clock(seconds: float) -> str:
    left = int(seconds + 0.5)
    hours, rest = divmod(left, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class TimerPanel(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Timers")
        self.resize(460, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.heading = QLabel("Timers")
        layout.addWidget(self.heading)

        presets = QHBoxLayout()
        presets.setSpacing(6)
        self.preset_buttons = []
        for label, seconds in PRESETS:
            b = QPushButton(label)
            b.setToolTip(f"Start a {label} timer")
            b.clicked.connect(lambda _c=False, s=seconds: self.start(s, self.name.text()))
            presets.addWidget(b)
            self.preset_buttons.append(b)
        layout.addLayout(presets)

        custom = QHBoxLayout()
        self.length = QLineEdit()
        self.length.setPlaceholderText("How long: 25, 1:30, 10m, half an hour")
        self.length.setAccessibleName("Timer length")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name (optional): pasta")
        self.name.setAccessibleName("Timer name")
        self.name.setMaxLength(40)
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._start_typed)
        self.length.returnPressed.connect(self._start_typed)
        self.name.returnPressed.connect(self._start_typed)
        custom.addWidget(self.length, 3)
        custom.addWidget(self.name, 2)
        custom.addWidget(self.start_btn)
        layout.addLayout(custom)
        self.message = QLabel("")
        self.message.setObjectName("muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.list = QListWidget()
        self.list.setAccessibleName("Running timers")
        self.list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        layout.addWidget(self.list, 1)
        self.empty = QLabel("No timers running. You can also say \"set a timer for 10 minutes\".")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel timer")
        self.cancel_btn.clicked.connect(self._cancel_selected)
        self.cancel_all_btn = QPushButton("Cancel all")
        self.cancel_all_btn.clicked.connect(self._cancel_all)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.cancel_all_btn)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        self._ticker = QTimer(self)                  # the countdowns, and timers that went off
        self._ticker.setInterval(500)
        self._ticker.timeout.connect(self.reload)
        theme_signals.changed.connect(self.restyle)
        self.restyle()
        self.reload()
        frame.install(self, minimize=True, maximize=False)   # NEON's title bar, or the native one (Settings > Appearance)

    def restyle(self) -> None:
        c = COLORS
        enable_dark_title_bar(self)
        self.heading.setStyleSheet(f"color: {c['accent']}; font-size: 20px; font-weight: 800;")
        self.setStyleSheet(
            f"QListWidget {{ background: {c['panel_alt']}; color: {c['text']}; border: 1px solid {c['border']}; "
            f"border-radius: 8px; padding: 4px; outline: 0; font-size: 15px; }}"
            f"QListWidget::item {{ padding: 8px; border-radius: 6px; }}"
            f"QListWidget::item:hover:!selected {{ background: {c['panel']}; }}"
            f"QListWidget::item:selected {{ background: {c['accent']}; color: {c.get('on_accent', c['bg'])}; }}"
            f"QLineEdit {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: 8px; "
            f"padding: 6px 8px; }}"
            f"QLabel#muted {{ color: {c['muted']}; }}")

    # ---- starting --------------------------------------------------------------------------------
    def start(self, seconds: float, name: str = "") -> bool:
        if seconds <= 0:
            self.message.setText("That isn't a length I understand. Try 25, 1:30, 10m or half an hour.")
            return False
        if seconds > MAX_SECONDS:
            self.message.setText("Timers go up to 24 hours.")
            return False
        self.message.setText(self._controller.start_timer_from_ui(seconds, " ".join(name.split())))
        self.name.clear()
        self.reload()
        return True

    def _start_typed(self) -> None:
        if self.start(timers.parse_duration_text(self.length.text()), self.name.text()):
            self.length.clear()

    # ---- the list --------------------------------------------------------------------------------
    def reload(self) -> None:
        keep = self._selected_id()
        running = timers.list_timers()
        if [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())] != [t["id"] for t in running]:
            self.list.clear()
            for t in running:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, t["id"])
                self.list.addItem(item)
        for i, t in enumerate(running):
            icon = "⏳" if t["reminder"] else "⏲"
            name = t["label"] or f"{clock(t['seconds'])} timer"
            self.list.item(i).setText(f"{icon}  {clock(t['remaining'])}    {name}")
            if t["id"] == keep:
                self.list.setCurrentRow(i)
        self.empty.setVisible(not running)
        self.cancel_all_btn.setEnabled(bool(running))
        self._sync_buttons()

    def _selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _sync_buttons(self) -> None:
        self.cancel_btn.setEnabled(self._selected_id() is not None)

    def _cancel_selected(self) -> None:
        timer_id = self._selected_id()
        if timer_id is not None and timers.cancel_timer(timer_id):
            self.message.setText("Cancelled.")
        self.reload()

    def _cancel_all(self) -> None:
        self.message.setText(timers.cancel_timers())
        self.reload()

    def showEvent(self, event) -> None:
        self.reload()
        self._ticker.start()
        super().showEvent(event)
        self.length.setFocus()

    def hideEvent(self, event) -> None:
        self._ticker.stop()
        super().hideEvent(event)
