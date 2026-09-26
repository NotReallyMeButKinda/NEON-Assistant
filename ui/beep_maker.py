"""The beep maker: make a short notification sound from a few notes, hear it as you change it, and save it
for use anywhere NEON plays a sound (the notification sound, the listening sound...). Sounds are recipes
(see sounds.synth_beep), kept in the settings under "custom_beeps"; nothing is written as audio files.

Opened from every SoundPicker's "Make a sound..." button."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

import assistant as backend
import sounds

from . import frame
from .theme import COLORS, enable_dark_title_bar


class Waveform(QWidget):
    """The sound's shape: how loud it is over time, so a change is visible as well as audible."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self._levels = np.zeros(0)
        self._seconds = 0.0

    def set_samples(self, samples: np.ndarray) -> None:
        data = np.abs(samples.astype(np.float32)) / 32768.0
        buckets = 160
        if len(data) >= buckets:
            data = data[: len(data) // buckets * buckets].reshape(buckets, -1).max(axis=1)
        self._levels = data
        self._seconds = len(samples) / sounds.SAMPLE_RATE
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.setBrush(QColor(COLORS["panel_alt"]))
        p.drawRoundedRect(r, 8, 8)
        if len(self._levels):
            mid, half = r.center().y(), r.height() / 2 - 6
            step = (r.width() - 16) / max(1, len(self._levels) - 1)
            top, bottom = QPainterPath(), QPainterPath()
            for i, level in enumerate(self._levels):
                x = r.left() + 8 + i * step
                point_top, point_bottom = QPointF(x, mid - level * half), QPointF(x, mid + level * half)
                (top.moveTo if i == 0 else top.lineTo)(point_top)
                (bottom.moveTo if i == 0 else bottom.lineTo)(point_bottom)
            shape = QPainterPath(top)
            shape.connectPath(bottom.toReversed())
            fill = QColor(COLORS["accent"])
            fill.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawPath(shape)
        p.setPen(QColor(COLORS["muted"]))
        p.drawText(r.adjusted(0, 0, -8, -4), Qt.AlignRight | Qt.AlignBottom, f"{self._seconds:.2f} s")
        p.end()


class _NoteRow(QWidget):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, note: str, ms: int, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.note = QComboBox()
        self.note.addItems(sounds.NOTE_NAMES)
        self.note.setCurrentText(note if note in sounds.NOTE_NAMES else "A5")
        self.note.setAccessibleName("Note")
        self.ms = QSpinBox()
        self.ms.setRange(*sounds.NOTE_MS)
        self.ms.setSingleStep(10)
        self.ms.setSuffix(" ms")
        self.ms.setValue(int(ms))
        self.ms.setAccessibleName("Note length")
        self.remove = QPushButton("✕")
        self.remove.setFixedSize(32, 32)
        self.remove.setStyleSheet("padding: 0;")
        self.remove.setAccessibleName("Remove this note")
        self.remove.setToolTip("Remove this note")
        row.addWidget(self.note, 1)
        row.addWidget(self.ms)
        row.addWidget(self.remove)
        self.note.currentIndexChanged.connect(self.changed)
        self.ms.valueChanged.connect(self.changed)
        self.remove.clicked.connect(lambda: self.removed.emit(self))

    def value(self) -> list:
        return [self.note.currentText(), self.ms.value()]


class BeepMaker(QDialog):
    """Returns (via .saved_key after exec) the "beep:<name>" key of the sound that was saved, or ""."""

    def __init__(self, parent=None, start_key: str = "", volume: float = 0.5, device=None):
        super().__init__(parent)
        self.setWindowTitle("Make a sound")
        self.setMinimumWidth(460)
        self.saved_key = ""
        self._volume, self._device = volume, device
        self._editing = start_key[len(sounds.BEEP_PREFIX):] if start_key.startswith(sounds.BEEP_PREFIX) else ""
        recipe = sounds.custom_sounds().get(start_key) or sounds.PRESETS[0][1]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel("A few notes, a tone, and it's yours. It plays as you change it.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.preset = QComboBox()
        self.preset.addItem("Keep what's here", None)
        for label, preset in sounds.PRESETS:
            self.preset.addItem(label, preset)
        self.tone = QComboBox()
        for key, label in sounds.TONES.items():
            self.tone.addItem(label, key)
        self.gap = QSpinBox()
        self.gap.setRange(*sounds.GAP_MS)
        self.gap.setSingleStep(10)
        self.gap.setSuffix(" ms")
        self.echo = QCheckBox("Echo")
        for i, (label, widget) in enumerate((("Start from", self.preset), ("Tone", self.tone),
                                             ("Gap between notes", self.gap), ("", self.echo))):
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(widget, i, 1)
        layout.addLayout(grid)

        notes_title = QLabel("Notes")
        notes_title.setObjectName("cardTitle")
        layout.addWidget(notes_title)
        self.notes_box = QVBoxLayout()
        self.notes_box.setSpacing(6)
        layout.addLayout(self.notes_box)
        self.add_note = QPushButton("+ Add a note")
        self.add_note.clicked.connect(lambda: self._add_note(*self._next_note()))
        layout.addWidget(self.add_note, 0, Qt.AlignLeft)

        self.wave = Waveform()
        layout.addWidget(self.wave)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.name = QLineEdit(self._editing)
        self.name.setPlaceholderText("My ping")
        self.name.setMaxLength(30)
        name_row.addWidget(self.name, 1)
        layout.addLayout(name_row)
        self.auto = QCheckBox("Play as I change it")
        self.auto.setChecked(True)
        layout.addWidget(self.auto)
        self.message = QLabel("")
        self.message.setObjectName("error")
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)

        buttons = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play")
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setVisible(bool(self._editing))
        cancel = QPushButton("Cancel")
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        buttons.addWidget(self.play_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._auto_play)
        self._loading = False
        self._rows: list[_NoteRow] = []
        self.load(recipe)

        self.preset.activated.connect(self._pick_preset)
        self.tone.currentIndexChanged.connect(self._changed)
        self.gap.valueChanged.connect(self._changed)
        self.echo.toggled.connect(self._changed)
        self.play_btn.clicked.connect(self.play)
        self.delete_btn.clicked.connect(self._delete)
        cancel.clicked.connect(self.reject)
        self.save_btn.clicked.connect(self._save)
        c = COLORS
        self.setStyleSheet(f"QLineEdit, QComboBox, QSpinBox {{ background: {c['panel_alt']}; border: 1px solid "
                           f"{c['border']}; border-radius: 8px; padding: 5px 8px; }}")
        enable_dark_title_bar(self)
        frame.install(self, minimize=False, maximize=False)

    # ---- the recipe --------------------------------------------------------------------------
    def recipe(self) -> dict:
        return sounds.clean_recipe({"tone": self.tone.currentData(), "notes": [r.value() for r in self._rows],
                                    "gap_ms": self.gap.value(), "echo": self.echo.isChecked()})

    def load(self, recipe) -> None:
        recipe = sounds.clean_recipe(recipe)
        self._loading = True
        try:
            self.tone.setCurrentIndex(max(0, self.tone.findData(recipe["tone"])))
            self.gap.setValue(recipe["gap_ms"])
            self.echo.setChecked(recipe["echo"])
            for row in list(self._rows):
                self._drop_row(row)
            for note, ms in recipe["notes"]:
                self._add_note(note, ms)
        finally:
            self._loading = False
        self._changed()

    def _next_note(self) -> tuple[str, int]:
        """A new note starts a step above the last one, as long as it."""
        if not self._rows:
            return "A5", 150
        note, ms = self._rows[-1].value()
        index = sounds.NOTE_NAMES.index(note) if note in sounds.NOTE_NAMES else 0
        return sounds.NOTE_NAMES[min(len(sounds.NOTE_NAMES) - 1, index + 4)], ms

    def _add_note(self, note: str, ms: int) -> None:
        if len(self._rows) >= sounds.MAX_NOTES:
            return
        row = _NoteRow(note, ms, self)
        row.changed.connect(self._changed)
        row.removed.connect(self._remove_row)
        self._rows.append(row)
        self.notes_box.addWidget(row)
        self._sync_rows()
        if not self._loading:
            self._changed()

    def _drop_row(self, row: _NoteRow) -> None:
        self._rows.remove(row)
        self.notes_box.removeWidget(row)
        row.deleteLater()

    def _remove_row(self, row: _NoteRow) -> None:
        if len(self._rows) > 1:
            self._drop_row(row)
            self._sync_rows()
            self._changed()

    def _sync_rows(self) -> None:
        self.add_note.setEnabled(len(self._rows) < sounds.MAX_NOTES)
        for row in self._rows:
            row.remove.setEnabled(len(self._rows) > 1)

    def _pick_preset(self, index: int) -> None:
        preset = self.preset.itemData(index)
        if preset:
            self.load(preset)

    def _changed(self, *_args) -> None:
        if self._loading:
            return
        self.wave.set_samples(sounds.render_recipe(self.recipe(), 1.0))
        if self.auto.isChecked():
            self._debounce.start()

    def _auto_play(self) -> None:
        if self.isVisible():
            self.play()

    def play(self) -> None:
        sounds.play_samples(sounds.render_recipe(self.recipe(), self._volume), self._device)

    # ---- saving ------------------------------------------------------------------------------
    def _save(self) -> None:
        name = " ".join(self.name.text().replace(":", " ").split())
        if not name:
            self._say("Give it a name first.")
            self.name.setFocus()
            return
        library = dict(backend.CONFIG.get("custom_beeps") or {})
        if name in library and name != self._editing:
            answer = QMessageBox.question(self, "Replace sound", f"You already have a sound called \"{name}\". "
                                                                 "Replace it?")
            if answer != QMessageBox.Yes:
                return
        if self._editing and self._editing != name:
            library.pop(self._editing, None)                  # renamed
        library[name] = self.recipe()
        backend.update_config(custom_beeps=library)
        self.saved_key = sounds.BEEP_PREFIX + name
        self.accept()

    def _delete(self) -> None:
        answer = QMessageBox.question(self, "Delete sound", f"Delete \"{self._editing}\"? Anything using it goes "
                                                            "back to the chime.")
        if answer != QMessageBox.Yes:
            return
        library = dict(backend.CONFIG.get("custom_beeps") or {})
        library.pop(self._editing, None)
        backend.update_config(custom_beeps=library)
        self.saved_key = ""
        self.done(2)                                          # 2 = deleted

    def _say(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
