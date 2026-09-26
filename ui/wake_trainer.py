"""Settings > Listening > "Train a wake word...": record yourself saying a phrase, then train the detector.

Recording is optional -- a phrase can be learned from Piper's voices alone -- but a handful of takes in
your own voice, through your own microphone, make it far more reliable. The assistant's microphone is
paused while this window is open so it doesn't react to you saying the phrase over and over.
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout)

import assistant as backend
import tts
import wake_training
import wakeword

from . import frame
from .theme import COLORS, enable_dark_title_bar

RATE = 16000
TAKE_SECONDS = 2.0
ROOM_SECONDS = 8.0
SUGGESTED_TAKES = 6


class WakeTrainerDialog(QDialog):
    progress = Signal(float, str)        # worker thread -> UI
    finished_training = Signal(object)   # the report dict, or an error message (str)
    recorded = Signal(object, str)       # (int16 array or None, "take" | "room")
    voices_ready = Signal(str)           # "" or an error message
    heard = Signal(float, bool)          # try-it mode: (score of the last 80 ms, the phrase was detected)
    trying_stopped = Signal(str)         # try-it mode ended ("" or why)

    def __init__(self, controller=None, phrase: str = "", parent=None):
        super().__init__(parent)
        self._controller = controller
        self.takes: list[np.ndarray] = []
        self.room: list[np.ndarray] = []
        self.report: dict | None = None
        self._busy = False
        self._trying: threading.Event | None = None      # set = stop the try-it loop
        self._hits = 0
        self.setWindowTitle("Train a wake word")
        self.setWindowModality(Qt.WindowModal)   # an app-modal dialog would disable the status bar
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)
        title = QLabel("Train a wake word")
        title.setObjectName("pageTitle")
        title.setStyleSheet(f"color: {COLORS['accent']}; font-size: 20px; font-weight: 800;")
        root.addWidget(title)
        intro = QLabel("I learn a phrase from my own voices saying it, plus recordings of you. Two words work "
                       "best (\"hey nova\"); very short or very common words wake me by accident more often.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        self.phrase = QLineEdit(phrase)
        self.phrase.setPlaceholderText("hey nova")
        root.addWidget(QLabel("Phrase"))
        root.addWidget(self.phrase)

        steps = QLabel(f"1. Record yourself saying it, about {SUGGESTED_TAKES} times (each take is "
                       f"{TAKE_SECONDS:.0f} seconds: press, then say it once, normally).\n"
                       f"2. Optionally record {ROOM_SECONDS:.0f} seconds of your room as it usually sounds "
                       f"(TV, fan, music), without the phrase.\n3. Train. It takes one to four minutes.")
        steps.setWordWrap(True)
        root.addWidget(steps)

        row = QHBoxLayout()
        self.take_btn = QPushButton("Record a take")
        self.take_btn.clicked.connect(lambda: self._record("take"))
        self.room_btn = QPushButton("Record the room")
        self.room_btn.clicked.connect(lambda: self._record("room"))
        self.clear_btn = QPushButton("Start over")
        self.clear_btn.clicked.connect(self._clear)
        for b in (self.take_btn, self.room_btn, self.clear_btn):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)
        self.counts = QLabel()
        self.counts.setObjectName("muted")
        root.addWidget(self.counts)

        voices = QHBoxLayout()
        self.voices_label = QLabel()
        self.voices_label.setWordWrap(True)
        self.voices_label.setObjectName("muted")
        self.voices_btn = QPushButton("Get more voices (78 MB)")
        self.voices_btn.setToolTip("Downloads one Piper model with hundreds of different speakers. Training takes "
                                   "longer, and recognises voices unlike mine much better.")
        self.voices_btn.clicked.connect(self._download_voices)
        voices.addWidget(self.voices_label, 1)
        voices.addWidget(self.voices_btn)
        root.addLayout(voices)
        self._describe_voices()

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.hide()
        root.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # Try it: the trained detector listens live, and shows how sure it is as you speak.
        trial = QHBoxLayout()
        self.try_btn = QPushButton("Try it")
        self.try_btn.setToolTip("Say the phrase (and other things) and watch how sure the detector is.")
        self.try_btn.clicked.connect(self._toggle_try)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(10)
        self.meter.setAccessibleName("Wake word score")
        self.try_label = QLabel("")
        self.try_label.setObjectName("muted")
        trial.addWidget(self.try_btn)
        trial.addWidget(self.meter, 1)
        trial.addWidget(self.try_label)
        root.addLayout(trial)
        self.phrase.textChanged.connect(lambda _t: self._sync_try())

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        self.train_btn = QPushButton("Train")
        self.train_btn.setObjectName("primary")
        self.train_btn.clicked.connect(self._train)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.train_btn)
        root.addLayout(buttons)

        self.voices_ready.connect(self._on_voices)
        self.progress.connect(self._on_progress)
        self.finished_training.connect(self._on_finished)
        self.recorded.connect(self._on_recorded)
        self.heard.connect(self._on_heard)
        self.trying_stopped.connect(self._on_try_stopped)
        self._countdown = QTimer(self)
        self._countdown.timeout.connect(self._tick)
        self._left = 0.0
        self._update_counts()
        self._sync_try()
        if controller is not None:
            controller.hold_microphone(True)
        frame.install(self, minimize=False, maximize=False)   # NEON's title bar, or the native one (Settings > Appearance)

    # ---- example voices -------------------------------------------------------------------------
    def _describe_voices(self) -> None:
        names = tts.list_installed_voices()
        many = wake_training.MANY_SPEAKERS_VOICE in names
        self.voices_label.setText(
            f"Examples are generated with {len(names)} installed voice{'s' if len(names) != 1 else ''}"
            + (" (one of them has hundreds of speakers)." if many else
               ". More voices make it better at recognising someone who sounds unlike them."))
        self.voices_btn.setVisible(not many)

    def _download_voices(self) -> None:
        self._set_busy(True)
        self.voices_btn.setEnabled(False)
        self.status.setText("Downloading the multi-speaker voice (78 MB)...")

        def work() -> None:
            try:
                tts.ensure_voice(wake_training.MANY_SPEAKERS_VOICE)
                self._emit(self.voices_ready, "")
            except Exception as exc:  # noqa: BLE001 -- offline, disk full...
                self._emit(self.voices_ready, str(exc))
        threading.Thread(target=work, name="Nova-VoiceDownload", daemon=True).start()

    def _on_voices(self, problem: str) -> None:
        self._set_busy(False)
        self.voices_btn.setEnabled(True)
        self.status.setText(f"Couldn't download it: {problem}" if problem else "Downloaded.")
        self._describe_voices()

    # ---- recording ------------------------------------------------------------------------------
    def _record(self, kind: str) -> None:
        seconds = TAKE_SECONDS if kind == "take" else ROOM_SECONDS
        self._set_busy(True)
        self._left = seconds
        self.status.setStyleSheet(f"color: {COLORS['accent_bright']};")
        self._tick()
        self._countdown.start(100)
        device = backend.CONFIG.get("mic_device") or None

        def work() -> None:
            try:
                audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16", device=device)
                sd.wait()
                self._emit(self.recorded, audio.reshape(-1).copy(), kind)
            except Exception as exc:  # noqa: BLE001 -- no microphone, device busy...
                self._emit(self.recorded, None, f"error:{exc}")
        threading.Thread(target=work, name="Nova-WakeRecord", daemon=True).start()

    def _tick(self) -> None:
        self._left = max(0.0, self._left - 0.1)
        self.status.setText(f"Recording... {self._left:.1f} s")

    def _on_recorded(self, audio, kind: str) -> None:
        self._countdown.stop()
        self._set_busy(False)
        self.status.setStyleSheet("")
        if audio is None:
            self.status.setText(f"Couldn't record: {kind.removeprefix('error:')}")
            return
        level = float(np.abs(audio).mean())
        if kind == "take":
            peak = float(np.abs(audio).max())
            if peak < 600:
                self.status.setText("That take was nearly silent, so I didn't keep it. Check the microphone "
                                    "in Settings > Listening, or speak up a little.")
                return
            self.takes.append(audio)
            self.status.setText(f"Got it (take {len(self.takes)}).")
        else:
            self.room.append(audio)
            self.status.setText(f"Room recorded (average level {level:.0f}).")
        self._update_counts()

    def _clear(self) -> None:
        self.takes.clear()
        self.room.clear()
        self.status.setText("")
        self._update_counts()

    def _update_counts(self) -> None:
        takes = len(self.takes)
        self.counts.setText(f"{takes} take{'s' if takes != 1 else ''} of you"
                            + (" (a few more would help)" if 0 < takes < SUGGESTED_TAKES else "")
                            + f", {len(self.room)} room recording{'s' if len(self.room) != 1 else ''}.")

    # ---- training -------------------------------------------------------------------------------
    def _train(self) -> None:
        phrase = " ".join(self.phrase.text().lower().split())
        if len(wakeword.slug(phrase)) < 2:
            self.status.setText("Type the phrase first, like \"hey nova\".")
            return
        self._set_busy(True)
        self.bar.setValue(0)
        self.bar.show()
        takes, room = list(self.takes), list(self.room)

        def work() -> None:
            try:
                report = wake_training.train(phrase, takes, room,
                                             on_progress=lambda f, t: self._emit(self.progress, float(f), t))
                self._emit(self.finished_training, report)
            except wake_training.TrainingError as exc:
                self._emit(self.finished_training, str(exc))
            except Exception as exc:  # noqa: BLE001 -- offline on first use, damaged voice file...
                self._emit(self.finished_training, f"Training failed: {exc}")
        threading.Thread(target=work, name="Nova-WakeTrain", daemon=True).start()

    def _on_progress(self, fraction: float, text: str) -> None:
        self.bar.setValue(int(fraction * 1000))
        self.status.setText(text)

    def _on_finished(self, result) -> None:
        self._set_busy(False)
        self.bar.hide()
        if isinstance(result, str):
            self.status.setStyleSheet(f"color: {COLORS['error']};")
            self.status.setText(result)
            return
        self.report = result
        self.status.setStyleSheet(f"color: {COLORS['success']};")
        self.status.setText(wake_training.describe(result))
        self.train_btn.setText("Train again")
        self.close_btn.setText("Use it")
        self.close_btn.setObjectName("primary")
        self.close_btn.style().unpolish(self.close_btn)
        self.close_btn.style().polish(self.close_btn)
        self.close_btn.clicked.disconnect()
        self.close_btn.clicked.connect(self.accept)
        self._sync_try()

    # ---- try it ---------------------------------------------------------------------------------
    def _model_key(self) -> str:
        return wakeword.CUSTOM + wakeword.slug(" ".join(self.phrase.text().lower().split()))

    def _sync_try(self) -> None:
        ready = wakeword.trained(self._model_key())
        self.try_btn.setEnabled(self._trying is not None or (ready and not self._busy))
        if self._trying is None:
            self.meter.setValue(0)
            self.try_label.setText("" if ready else "Train it first")

    def _toggle_try(self) -> None:
        if self._trying is not None:
            self._trying.set()
            return
        key = self._model_key()
        if not wakeword.trained(key):
            return
        stop = self._trying = threading.Event()
        self._hits = 0
        self.try_btn.setText("Stop")
        for w in (self.take_btn, self.room_btn, self.clear_btn, self.train_btn, self.phrase):
            w.setEnabled(False)
        threshold = backend.cfg_num("wake_threshold") or 0.5
        self.try_label.setText(f"Say it... (wakes at {threshold:.2f})")
        device = backend.CONFIG.get("mic_device") or None

        def work() -> None:
            problem = ""
            try:
                detector = wakeword.Detector(key, threshold=lambda: backend.cfg_num("wake_threshold") or 0.5)
                detector.load()
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", device=device,
                                    blocksize=wakeword.FRAME) as stream:
                    while not stop.is_set():
                        chunk, _overflow = stream.read(wakeword.FRAME)
                        hit = detector.feed(chunk.reshape(-1))
                        self._emit(self.heard, detector.last_score, hit)
            except Exception as exc:  # noqa: BLE001 -- no microphone, device busy, damaged model...
                problem = str(exc)
            self._emit(self.trying_stopped, problem)
        threading.Thread(target=work, name="Nova-WakeTry", daemon=True).start()

    def _on_heard(self, score: float, hit: bool) -> None:
        if self._trying is None:
            return
        self.meter.setValue(int(round(score * 100)))
        if hit:
            self._hits += 1
        threshold = backend.cfg_num("wake_threshold") or 0.5
        self.try_label.setText(f"{score:.2f} / {threshold:.2f}"
                               + (f"  -  heard it {self._hits}x" if self._hits else ""))

    def _on_try_stopped(self, problem: str) -> None:
        self._trying = None
        self.try_btn.setText("Try it")
        self._set_busy(False)
        if problem:
            self.status.setText(f"Couldn't listen: {problem}")
        self._sync_try()
        if self._hits:
            self.try_label.setText(f"Heard it {self._hits} time{'s' if self._hits != 1 else ''}.")

    # ---- plumbing -------------------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (self.take_btn, self.room_btn, self.clear_btn, self.train_btn, self.phrase):
            w.setEnabled(not busy)
        if hasattr(self, "try_btn"):
            self._sync_try()

    def _emit(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            pass                             # the window was closed meanwhile

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enable_dark_title_bar(self)

    def done(self, result: int) -> None:
        if self._trying is not None:
            self._trying.set()                # the try-it loop closes the microphone on its own thread
        if self._busy and self.bar.isVisible():
            self.status.setText("Still training -- it will finish in the background and be listed afterwards.")
        if self._controller is not None:
            self._controller.hold_microphone(False)
            self._controller = None
        super().done(result)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self._busy and not self.bar.isVisible():
            return                            # don't close mid-recording
        super().keyPressEvent(event)
