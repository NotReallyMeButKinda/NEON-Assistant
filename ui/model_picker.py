"""The AI model chooser: what's already in Ollama, a short list of good models to download, or any name you
type. Downloads go through Ollama itself (its /api/pull), with a progress bar. Used by Settings > AI & chat
and by the welcome tour."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

import assistant as backend

# name -> (what it's like, rough download size). Sizes are approximate: Ollama's own numbers win once installed.
SUGGESTED: dict[str, tuple[str, str]] = {
    "qwen3:4b": ("Fast and light. Good for older or busy GPUs.", "2.5 GB"),
    "qwen3:8b": ("Recommended. Understands commands well, quick on a mid-range GPU.", "5.2 GB"),
    "qwen3:14b": ("Smarter, slower. Wants 12 GB of video memory.", "9.3 GB"),
    "gemma3:4b": ("Google's small model. Friendly, quick, lighter on facts.", "3.3 GB"),
    "gemma3:12b": ("Google's larger model. Good general knowledge; slower.", "8.1 GB"),
    "llama3.1:8b": ("Meta's all-rounder. Solid, a little chattier.", "4.9 GB"),
    "llama3.2:3b": ("Very fast and small. Makes up facts more often.", "2.0 GB"),
    "mistral-nemo:12b": ("Mistral's 12B. Good writing, longer answers.", "7.1 GB"),
    "phi4:14b": ("Microsoft's reasoning model. Strong at maths and logic; slower.", "9.1 GB"),
    "deepseek-r1:8b": ("Thinks step by step. Accurate but slow for a voice assistant.", "5.2 GB"),
}
HEADER_ROLE = Qt.UserRole + 1


def _size(num_bytes) -> str:
    try:
        gb = float(num_bytes) / 1e9
    except (TypeError, ValueError):
        return ""
    return f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB"


def installed_models() -> dict[str, str]:
    """Models Ollama already has: name -> size text. Empty if Ollama isn't running."""
    base = backend.ollama_base_url()
    data = backend._get_json(f"{base}/api/tags", timeout=1.5) if base else None
    return {m["name"]: _size(m.get("size")) for m in (data or {}).get("models", []) if m.get("name")}


def _same(a: str, b: str) -> bool:
    """'llama3.2' and 'llama3.2:latest' are the same model."""
    norm = lambda s: s if ":" in s else f"{s}:latest"
    return norm(a.strip()) == norm(b.strip())


class ModelPicker(QWidget):
    changed = Signal()
    _progress = Signal(int, str)          # percent (-1 = unknown), status -- from the download thread
    _done = Signal(bool, str)

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent)
        self._installed: dict[str, str] = {}
        self._pulling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.lineEdit().setPlaceholderText("Pick one, or type any Ollama model name")
        self.download = QPushButton("Download")
        self.refresh = QPushButton("Refresh")
        top.addWidget(self.combo, 1)
        top.addWidget(self.download)
        top.addWidget(self.refresh)
        layout.addLayout(top)

        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.bar.hide()
        layout.addWidget(self.info)
        layout.addWidget(self.bar)
        # Stretch across the row (a wrapped label otherwise wraps in a narrow column).
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.combo.currentIndexChanged.connect(lambda _i: self._sync())
        self.combo.lineEdit().editingFinished.connect(self._sync)
        self.combo.currentTextChanged.connect(lambda _t: self.changed.emit())
        self.download.clicked.connect(self._pull)
        self.refresh.clicked.connect(lambda: self.reload(self.value()))
        self._progress.connect(self._on_progress)
        self._done.connect(self._on_done)
        self.reload(value)

    # ---- value ------------------------------------------------------------------------------
    def value(self) -> str:
        return self.combo.currentText().strip()

    def set_value(self, value) -> None:
        name = str(value or "").strip()
        index = next((i for i in range(self.combo.count())
                      if not self.combo.itemData(i, HEADER_ROLE) and _same(self.combo.itemText(i), name)), -1)
        if index >= 0:
            self.combo.setCurrentIndex(index)
        else:
            self.combo.setEditText(name)
        self._sync()

    # ---- the list -----------------------------------------------------------------------------
    def reload(self, keep: str = "") -> None:
        self._installed = installed_models()
        self.combo.blockSignals(True)
        self.combo.clear()
        model: QStandardItemModel = self.combo.model()

        def header(text: str) -> None:
            self.combo.addItem(text)
            item = model.item(self.combo.count() - 1)
            item.setFlags(Qt.NoItemFlags)                      # a label, not a choice
            item.setData(True, HEADER_ROLE)

        if self._installed:
            header("On this PC")
            for name in sorted(self._installed):
                self.combo.addItem(name)
        others = [n for n in SUGGESTED if not any(_same(n, i) for i in self._installed)]
        if others:
            header("Download")
            for name in others:
                self.combo.addItem(name)
        self.combo.blockSignals(False)
        self.set_value(keep)

    def _describe(self, name: str) -> str:
        installed = next((size for i, size in self._installed.items() if _same(i, name)), None)
        blurb, rough = SUGGESTED.get(name, ("", ""))
        if not name:
            return "Type a model name, or pick one from the list."
        if installed is not None:
            return f"On this PC ({installed}). {blurb}".strip()
        if not self._installed and not backend.ollama_base_url():
            return "Ollama's address isn't set (below)."
        if name in SUGGESTED:
            return f"Not downloaded yet (about {rough}). {blurb}"
        return "Not on this PC. Download fetches it from the Ollama library, if it exists there."

    def _sync(self) -> None:
        name = self.value()
        self.info.setText(self._describe(name))
        installed = any(_same(i, name) for i in self._installed)
        self.download.setVisible(bool(name) and not installed)
        self.download.setEnabled(not self._pulling)

    # ---- downloading ----------------------------------------------------------------------------
    def _pull(self) -> None:
        name = self.value()
        base = backend.ollama_base_url()
        if not name or not base or self._pulling:
            return
        self._pulling = True
        self.download.setEnabled(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.show()
        self.info.setText(f"Downloading {name}...")

        def work() -> None:
            req = urllib.request.Request(f"{base}/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    for line in resp:
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("error"):
                            self._emit_done(False, str(event["error"]))
                            return
                        total, done = event.get("total"), event.get("completed")
                        percent = int(done * 100 / total) if total and done is not None else -1
                        status = str(event.get("status") or "")
                        if total and done is not None:
                            status = f"{status}  {_size(done)} of {_size(total)}"
                        self._emit_progress(percent, status)
                self._emit_done(True, f"Downloaded {name}.")
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._emit_done(False, f"Couldn't download it: {exc}. Is Ollama running?")

        threading.Thread(target=work, name="Nova-OllamaPull", daemon=True).start()

    def _emit_progress(self, percent: int, status: str) -> None:
        try:
            self._progress.emit(percent, status)
        except RuntimeError:
            pass                                               # the window closed; the download carries on

    def _emit_done(self, ok: bool, message: str) -> None:
        try:
            self._done.emit(ok, message)
        except RuntimeError:
            pass

    def _on_progress(self, percent: int, status: str) -> None:
        if percent < 0:
            self.bar.setRange(0, 0)                            # busy indicator
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(percent)
        self.info.setText(status)

    def _on_done(self, ok: bool, message: str) -> None:
        self._pulling = False
        self.bar.hide()
        if ok:
            self.reload(self.value())
        self.info.setText(message if not ok else f"{message} {self._describe(self.value())}")
        self.download.setEnabled(True)
