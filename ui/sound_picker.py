"""A sound chooser: the built-in sounds, the ones you made (ui/beep_maker.py), optionally "No sound", and
a custom .wav file, with a preview that plays as you pick. Used for the "I heard you" sound and the
notification sound."""

from __future__ import annotations

import weakref

from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

import sounds

OFF = "off"
_PICKERS: "weakref.WeakSet[SoundPicker]" = weakref.WeakSet()     # all open pickers: a new sound shows in each


class SoundPicker(QWidget):
    def __init__(self, allow_off: bool = False, volume_fn=lambda: 0.5, device_fn=lambda: None, parent=None):
        super().__init__(parent)
        self._volume_fn, self._device_fn = volume_fn, device_fn
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.combo = QComboBox()
        if allow_off:
            self.combo.addItem("No sound", OFF)
        self._allow_off = allow_off
        self._fill()
        self.preview_btn = QPushButton("Preview")
        self.make_btn = QPushButton("Make a sound...")
        self.make_btn.setToolTip("Make your own sound from a few notes")
        top.addWidget(self.combo, 1)
        top.addWidget(self.preview_btn)
        top.addWidget(self.make_btn)
        layout.addLayout(top)
        _PICKERS.add(self)

        self.file_row = QWidget()
        row = QHBoxLayout(self.file_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit()
        self.path.setPlaceholderText("Path to a 16-bit .wav file (up to 10 seconds)")
        browse = QPushButton("Browse...")
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        layout.addWidget(self.file_row)

        self.message = QLabel("")
        self.message.setObjectName("muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.combo.activated.connect(lambda _i: self.preview())     # only when the user picks one
        self.combo.currentIndexChanged.connect(lambda _i: self._sync())
        self.preview_btn.clicked.connect(self.preview)
        self.make_btn.clicked.connect(self._make)
        browse.clicked.connect(self._browse)
        self._sync()

    def _fill(self) -> None:
        """(Re)build the list: no sound, the built-in ones, yours, a file."""
        keep = self.combo.currentData()
        self.combo.blockSignals(True)
        self.combo.clear()
        if self._allow_off:
            self.combo.addItem("No sound", OFF)
        for name, label in sounds.SOUND_LABELS.items():
            self.combo.addItem(label, name)
        for key in sounds.custom_sounds():
            self.combo.addItem(sounds.custom_label(key), key)
        self.combo.addItem(sounds.CUSTOM_LABEL, sounds.CUSTOM)
        index = self.combo.findData(keep) if keep is not None else -1
        self.combo.setCurrentIndex(index if index >= 0 else (1 if self._allow_off else 0))
        self.combo.blockSignals(False)

    def _make(self) -> None:
        from .beep_maker import BeepMaker
        try:
            volume = float(self._volume_fn())
        except (TypeError, ValueError):
            volume = 0.5
        current = self.name() if self.name().startswith(sounds.BEEP_PREFIX) else ""
        dialog = BeepMaker(self.window(), current, volume, self._device_fn())
        result = dialog.exec()
        for picker in list(_PICKERS):
            try:
                picker._fill()
            except RuntimeError:                   # deleted on the C++ side
                _PICKERS.discard(picker)
        if dialog.saved_key:
            self.set_name(dialog.saved_key)
        elif result == 2:                          # the chosen sound was deleted
            self.set_name(sounds.DEFAULT_SOUND)
        self._sync()

    # ---- value (registered with the settings dialog as two keys: name + file) -----------------
    def name(self) -> str:
        return str(self.combo.currentData() or "")

    def set_name(self, value) -> None:
        index = self.combo.findData(str(value or ""))
        self.combo.setCurrentIndex(index if index >= 0 else (1 if self.combo.itemData(0) == OFF else 0))
        self._sync()

    def file(self) -> str:
        return self.path.text().strip()

    def set_file(self, value) -> None:
        self.path.setText(str(value or ""))

    # ---- behavior -------------------------------------------------------------------------------
    def _sync(self) -> None:
        custom = self.name() == sounds.CUSTOM
        self.file_row.setVisible(custom)
        self.make_btn.setText("Edit sound..." if self.name().startswith(sounds.BEEP_PREFIX) else "Make a sound...")
        self.preview_btn.setEnabled(self.name() != OFF)
        self.message.setText("")

    def _browse(self) -> None:
        picked, _ = QFileDialog.getOpenFileName(self, "Choose a sound", self.file(), "WAV audio (*.wav)")
        if picked:
            self.path.setText(picked)
            self.preview()

    def preview(self) -> None:
        name = self.name()
        if name == OFF:
            return
        try:
            volume = float(self._volume_fn())
        except (TypeError, ValueError):
            volume = 0.5
        if not sounds.play(name, volume, self._device_fn(), self.file()):
            self.message.setText("Couldn't play that. Custom sounds need a 16-bit .wav file that exists.")
        else:
            self.message.setText("")
