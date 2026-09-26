"""A dropdown of Piper voices (installed first, then the downloadable catalog) with a
"Custom voice name..." entry that reveals a text field for any other Piper voice."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QVBoxLayout, QWidget

import tts

CUSTOM = "\0custom"     # the combo item's data for "Custom voice name..."
CUSTOM_LABEL = "Custom voice name..."


class PiperVoicePicker(QWidget):
    changed = Signal()

    def __init__(self, current: str = "", parent=None, none_label: str = ""):
        """`none_label`: an extra first entry meaning "no voice chosen here" (value '')."""
        super().__init__(parent)
        self._none = bool(none_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.combo = QComboBox()
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("e.g. en_US-kathleen-low")
        self.hint = QLabel("Voice names look like en_US-amy-medium (language_REGION-voice-quality). "
                           "A voice that isn't installed yet is downloaded the first time it is used.")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.combo)
        layout.addWidget(self.custom)
        layout.addWidget(self.hint)

        if none_label:
            self.combo.addItem(none_label, "")
        installed = tts.list_installed_voices()
        for name in installed + [v for v in tts.VOICE_CATALOG if v not in installed]:
            self.combo.addItem("", name)
        self.combo.insertSeparator(self.combo.count())
        self.combo.addItem(CUSTOM_LABEL, CUSTOM)
        self.refresh_labels()

        self.combo.currentIndexChanged.connect(self._on_combo)
        self.custom.textChanged.connect(lambda _t: self.changed.emit())
        self.set_value(current)

    # ---- value ---------------------------------------------------------------------
    def value(self) -> str:
        """The chosen voice name ('' if custom was picked but left empty)."""
        data = self.combo.currentData()
        return self.custom.text().strip() if data == CUSTOM else str(data or "")

    def set_value(self, name: str) -> None:
        name = str(name or "").strip()
        index = self.combo.findData(name) if name or self._none else 0
        if index >= 0:
            self.combo.setCurrentIndex(max(0, index))
        else:                                   # a voice that isn't in the list: show it as custom
            self.combo.setCurrentIndex(self.combo.findData(CUSTOM))
            self.custom.setText(name)
        self._sync()

    def is_custom(self) -> bool:
        return self.combo.currentData() == CUSTOM

    def refresh_labels(self) -> None:
        """Re-marks each voice installed / download (call after an install)."""
        for i in range(self.combo.count()):
            name = self.combo.itemData(i)
            if name and name != CUSTOM:
                self.combo.setItemText(i, tts.voice_choice_label(name))

    # ---- internals -------------------------------------------------------------------
    def _sync(self) -> None:
        self.custom.setVisible(self.is_custom())
        self.hint.setVisible(self.is_custom())

    def _on_combo(self, _index: int) -> None:
        self._sync()
        if self.is_custom():
            self.custom.setFocus()
        self.changed.emit()
