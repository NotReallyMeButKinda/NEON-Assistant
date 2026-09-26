"""Main chat window."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QPushButton, QVBoxLayout, QWidget)

import assistant as backend
import wakeword

from . import frame
from .settings_dialog import SettingsDialog
from .theme import COLORS, STATE_COLORS, enable_dark_title_bar, text_on
from .theme import signals as theme_signals
from .widgets import ChatView, StateOrb

STATE_LABELS = {
    "idle": "Idle", "listening": "Listening...", "thinking": "Thinking...",
    "speaking": "Speaking...", "error": "Something went wrong",
}


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle(f"{backend.assistant_name()} - NEON ASSISTANT")
        self.resize(900, 700)
        self.setMinimumSize(620, 480)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(26, 20, 26, 18)
        outer.setSpacing(10)

        # -- header
        header = QHBoxLayout()
        title = QLabel(backend.assistant_name().upper())
        self.title = title
        self._glow = QGraphicsDropShadowEffect(title)
        self._glow.setBlurRadius(28)
        self._glow.setOffset(0, 0)
        title.setGraphicsEffect(self._glow)
        header.addWidget(title)
        header.addStretch(1)
        self.location = QLabel("locating...")
        header.addWidget(self.location)
        outer.addLayout(header)

        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        outer.addWidget(self.subtitle)

        self.accent_line = QLabel()
        self.accent_line.setFixedHeight(2)
        outer.addWidget(self.accent_line)

        # -- chat
        self.chat = ChatView()
        outer.addWidget(self.chat, 1)

        # -- status row
        status = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.status = QLabel("Starting up...")
        self.status.setObjectName("muted")
        status.addWidget(self.dot)
        status.addWidget(self.status)
        status.addStretch(1)
        outer.addLayout(status)
        self._state = "idle"

        # -- controls
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(f"Message {backend.assistant_name()} or click the mic...")
        self.entry.setEnabled(False)
        self.entry.returnPressed.connect(self._send)
        controls.addWidget(self.entry, 1)

        self.mic = StateOrb(52, clickable=True)
        self.mic.setToolTip("Push to talk")
        self.mic.setAccessibleName("Push to talk")
        self.mic.clicked.connect(controller.push_to_talk)
        controls.addWidget(self.mic)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._send)
        controls.addWidget(self.send_btn)

        self.wake_btn = QPushButton("Wake Word: OFF")
        self.wake_btn.setCheckable(True)
        self.wake_btn.toggled.connect(controller.set_wake)
        controls.addWidget(self.wake_btn)

        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Mute the microphone (wake word included)")
        self.mute_btn.toggled.connect(controller.set_muted)
        controls.addWidget(self.mute_btn)

        board_btn = QPushButton("\U0001f5c2")
        board_btn.setToolTip("Board (cards and due dates)")
        board_btn.setAccessibleName("Board")
        board_btn.setFixedWidth(44)
        board_btn.clicked.connect(controller.show_board)
        controls.addWidget(board_btn)

        stopwatch = QPushButton("\u23f2")
        stopwatch.setToolTip("Timers")
        stopwatch.setAccessibleName("Timers")
        stopwatch.setFixedWidth(44)
        stopwatch.clicked.connect(controller.show_timers)
        controls.addWidget(stopwatch)

        bell = QPushButton("\U0001f514")
        bell.setToolTip("Recent notifications")
        bell.setAccessibleName("Recent notifications")
        bell.setFixedWidth(44)
        bell.clicked.connect(controller.show_notification_history)
        controls.addWidget(bell)

        settings = QPushButton("⚙")
        settings.setToolTip("Settings")
        settings.setAccessibleName("Settings")
        settings.setFixedWidth(44)
        settings.clicked.connect(self.open_settings)
        controls.addWidget(settings)
        outer.addLayout(controls)

        # -- wiring
        controller.message.connect(self.chat.add_message)
        controller.message_chunk.connect(self.chat.add_chunk)
        controller.message_end.connect(self.chat.end_stream)
        controller.audio_level.connect(self.mic.set_level)
        controller.state.connect(self._on_state)
        controller.status.connect(self.status.setText)
        controller.location.connect(lambda c: self.location.setText(f"\U0001f4cd {c}"))
        controller.ready.connect(self._on_ready)
        controller.wake_changed.connect(self._on_wake_changed)
        controller.muted_changed.connect(self._on_muted)
        controller.name_changed.connect(self._refresh_name)
        controller.settings_saved.connect(lambda: self._refresh_name(backend.assistant_name()))
        self._refresh_name(backend.assistant_name())
        theme_signals.changed.connect(self._apply_styles)
        self._apply_styles()

        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.chat.clear)
        QShortcut(QKeySequence("Esc"), self, activated=controller.stop_speaking)
        frame.install(self, minimize=True, maximize=True)   # NEON's title bar, or the native one (Settings > Appearance)

    def _apply_styles(self) -> None:
        """Everything with colors baked into an inline style; re-run on every theme change."""
        self.title.setStyleSheet(
            f"color: {COLORS['accent']}; font-size: 26px; font-weight: 800; letter-spacing: 2px;")
        self._glow.setColor(QColor(COLORS["accent"]))
        self.location.setStyleSheet(
            f"color: {COLORS['muted']}; background: {COLORS['panel_alt']};"
            f"border: 1px solid {COLORS['border']}; border-radius: 12px; padding: 4px 12px;")
        self.accent_line.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {COLORS['accent']}, stop:1 transparent);")
        self.mute_btn.setStyleSheet(
            f"QPushButton:checked {{ background: {STATE_COLORS['muted']}; "
            f"color: {text_on(STATE_COLORS['muted'])}; border: none; }}")
        self._on_wake_changed(self.controller.wake_on)
        self._on_state(self._state)
        enable_dark_title_bar(self)   # light themes get a light title bar

    def _refresh_name(self, name: str) -> None:
        """Everything in this window that shows the assistant's name (or wake word)."""
        self.title.setText(name.upper())
        self.setWindowTitle(f"{name} - NEON ASSISTANT")
        self.entry.setPlaceholderText(f"Message {name} or click the mic...")
        wake = wakeword.label(backend.CONFIG)
        self.subtitle.setText(f'say "{wake}", click the mic, or type below.')
        self._on_wake_changed(self.controller.wake_on)  # the wake button text embeds the wake word

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enable_dark_title_bar(self)

    def closeEvent(self, event) -> None:
        # Closing only hides; the status bar and tray keep running.
        event.ignore()
        self.hide()

    # ---- slots -----------------------------------------------------------------
    def _set_dot(self, color: str) -> None:
        self.dot.setStyleSheet(f"background: {color}; border-radius: 5px;")

    def _on_state(self, state: str) -> None:
        self._state = state
        self.mic.set_state(state)
        if self.controller.muted and state in ("idle", "listening", "error"):
            self._set_dot(STATE_COLORS["muted"])
            self.status.setText("Microphone muted")
            return
        self._set_dot(STATE_COLORS.get(state, COLORS["muted"]))
        self.status.setText(STATE_LABELS.get(state, state))

    def _on_muted(self, on: bool) -> None:
        self.mic.set_muted(on)
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(on)
        self.mute_btn.blockSignals(False)
        self._on_state(self._state)  # refresh the dot + status text, keeping the real state

    def _on_ready(self) -> None:
        self.entry.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.entry.setFocus()

    def _on_wake_changed(self, on: bool) -> None:
        self.wake_btn.blockSignals(True)
        self.wake_btn.setChecked(on)
        self.wake_btn.blockSignals(False)
        self.wake_btn.setText(f"Wake: {wakeword.label(backend.CONFIG)}" if on else "Wake Word: OFF")
        self.wake_btn.setStyleSheet(
            f"background: {COLORS['accent']}; color: {COLORS['on_accent']}; border: none;" if on else "")

    def _send(self) -> None:
        text = self.entry.text().strip()
        if text:
            self.entry.clear()
            self.controller.submit(text)

    def open_settings(self) -> None:
        if SettingsDialog.raise_open():
            return
        dialog = SettingsDialog(self.controller, self)   # show(), not exec(): see SettingsDialog's modality
        dialog.finished.connect(self._settings_closed)
        dialog.show()

    def _settings_closed(self, _result: int) -> None:
        if self.controller.wake_on:
            self._on_wake_changed(True)  # wake word text may have changed
