"""The Bitwarden unlock box: the one place the master password is typed. It goes straight to `bw` (see
bitwarden.py) from a background thread and is cleared from the field right away; nothing is saved."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

import bitwarden

from . import frame


class UnlockDialog(QDialog):
    finished_unlock = Signal(bool, str)        # from the worker thread: queued to the UI thread

    _open: "UnlockDialog | None" = None

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Unlock Bitwarden")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumWidth(380)

        title = QLabel("Unlock Bitwarden")
        title.setObjectName("dialogTitle")
        note = QLabel("Your master password goes straight to the Bitwarden app on this PC and isn't saved. "
                      "The vault locks itself again after a while.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Master password")
        self.password.returnPressed.connect(self._unlock)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox()
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.setDefault(True)
        buttons.addButton(self.unlock_button, QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.Cancel)
        self.unlock_button.clicked.connect(self._unlock)
        cancel.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.password)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.finished_unlock.connect(self._done)
        frame.install(self)

    @classmethod
    def raise_open(cls) -> bool:
        if cls._open is not None and cls._open.isVisible():
            cls._open.raise_()
            cls._open.activateWindow()
            cls._open.password.setFocus()
            return True
        return False

    def showEvent(self, event) -> None:
        UnlockDialog._open = self
        super().showEvent(event)
        self.password.setFocus()

    def done(self, result: int) -> None:
        UnlockDialog._open = None
        self.password.clear()
        super().done(result)

    def _unlock(self) -> None:
        secret = self.password.text()
        if not secret:
            return
        self.password.clear()
        self.password.setEnabled(False)
        self.unlock_button.setEnabled(False)
        self.status.setText("Unlocking...")

        def work(value: str) -> None:
            ok, message = bitwarden.unlock(value)
            self.finished_unlock.emit(ok, message)
        threading.Thread(target=work, args=(secret,), name="Nova-BitwardenUnlock", daemon=True).start()
        secret = ""                                                    # noqa: F841

    def _done(self, ok: bool, message: str) -> None:
        if ok:
            self.accept()
            self._controller.bitwarden_unlocked(message)
            return
        self.status.setText(message)
        self.password.setEnabled(True)
        self.unlock_button.setEnabled(True)
        self.password.setFocus()
