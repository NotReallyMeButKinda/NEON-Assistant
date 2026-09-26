"""Recent notifications: the last ones the assistant saw (silent ones included), newest first.

Pick one to open the app it came from, hear a summary of it, or be reminded about it later.
Opened from the main window's bell, the status bar's right-click menu, or the tray.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
                               QVBoxLayout)

import assistant as backend

from . import frame
from .theme import COLORS, enable_dark_title_bar
from .theme import signals as theme_signals

REMIND_CHOICES = (("In 15 minutes", 900), ("In an hour", 3600), ("In 3 hours", 3 * 3600), ("Tonight (in 8 hours)", 8 * 3600))


def ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return time.strftime("%d %b", time.localtime(time.time() - seconds))


class NotificationHistory(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Recent notifications")
        self.resize(520, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.heading = QLabel("Recent notifications")
        layout.addWidget(self.heading)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setAccessibleName("Recent notifications")
        self.list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        self.list.itemDoubleClicked.connect(lambda _item: self._open())
        layout.addWidget(self.list, 1)
        self.empty = QLabel("Nothing yet. Notifications appear here once \"Take over Windows notifications\" "
                            "is on (Settings > Notifications).")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("muted")
        layout.addWidget(self.empty)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open app")
        self.summarize_btn = QPushButton("Summarize")
        self.remind_btn = QPushButton("Remind me...")
        remind_menu = QMenu(self.remind_btn)
        for label, seconds in REMIND_CHOICES:
            remind_menu.addAction(label).triggered.connect(lambda _c=False, s=seconds: self._remind(s))
        self.remind_btn.setMenu(remind_menu)
        self.clear_btn = QPushButton("Clear all")
        close = QPushButton("Close")
        self.open_btn.clicked.connect(self._open)
        self.summarize_btn.clicked.connect(self._summarize)
        self.clear_btn.clicked.connect(self._clear)
        close.clicked.connect(self.close)
        for b in (self.open_btn, self.summarize_btn, self.remind_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.clear_btn)
        row.addWidget(close)
        layout.addLayout(row)

        controller.notification.connect(lambda _n: self.reload() if self.isVisible() else None)
        self._ticker = QTimer(self)                  # keeps "3 min ago" honest while it's open
        self._ticker.setInterval(30_000)
        self._ticker.timeout.connect(self.reload)
        theme_signals.changed.connect(self.restyle)
        self.restyle()
        self.reload()
        frame.install(self, minimize=True, maximize=False)   # NEON's title bar, or the native one (Settings > Appearance)

    def restyle(self) -> None:
        c = COLORS
        enable_dark_title_bar(self)                  # light themes get a light title bar
        self.heading.setStyleSheet(f"color: {c['accent']}; font-size: 20px; font-weight: 800;")
        self.setStyleSheet(
            f"QListWidget {{ background: {c['panel_alt']}; color: {c['text']}; border: 1px solid {c['border']}; "
            f"border-radius: 8px; padding: 4px; outline: 0; }}"
            f"QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}"
            f"QListWidget::item:hover:!selected {{ background: {c['panel']}; }}"
            f"QListWidget::item:selected {{ background: {c['accent']}; color: {c.get('on_accent', c['bg'])}; }}"
            f"QLabel#muted {{ color: {c['muted']}; }}")

    def reload(self) -> None:
        keep = (self.selected() or {}).get("id")
        self.list.clear()
        now = time.time()
        notes = backend.recent_notifications()
        for n in notes:
            body = str(n.get("body") or "").strip()
            text = f"{n.get('app', 'An app')}  ·  {ago(now - float(n.get('received') or now))}\n{n.get('title', '')}"
            if body:
                text += f" - {body[:160]}" + ("..." if len(body) > 160 else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, n)
            self.list.addItem(item)
            if n.get("id") == keep:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.empty.setVisible(not notes)
        self.clear_btn.setEnabled(bool(notes))
        self._sync_buttons()

    def selected(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _sync_buttons(self) -> None:
        on = self.selected() is not None
        for b in (self.open_btn, self.summarize_btn, self.remind_btn):
            b.setEnabled(on)

    def _open(self) -> None:
        n = self.selected()
        if n:
            self._controller.open_notification_app(n)

    def _summarize(self) -> None:
        n = self.selected()
        if n:
            self._controller.summarize_one_notification(n)

    def _remind(self, seconds: float) -> None:
        n = self.selected()
        if n:
            self._controller.remind_about_notification(n, seconds)

    def _clear(self) -> None:
        backend.clear_notification_history()
        self.reload()

    def showEvent(self, event) -> None:
        self.reload()
        self._ticker.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._ticker.stop()
        super().hideEvent(event)
