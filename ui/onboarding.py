"""The welcome tour: eight short steps down a rail on the left, a page on the right.

    Welcome -> Hearing you -> My voice -> My brain -> Look -> Connections -> Shortcuts -> All set

Opened automatically until `onboarding_done` is set, and any time from Settings > General. The theme
previews live as you pick one; everything else is gathered and saved on "Start using NEON". Skipping (or
closing the window) puts everything back as it was and doesn't ask again. The microphone is only opened
while you test it, never just because a page is showing.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout,
                               QWidget, QApplication)

import assistant as backend
import bitwarden
import browser_bridge
import hotkeys
import osinfo
import stt
import tts

from . import frame
from .model_picker import ModelPicker
from .motion import animations_enabled
from .theme import COLORS, THEMES, apply_theme, current_theme, effective_theme, enable_dark_title_bar
from .theme import signals as theme_signals
from .voice_picker import PiperVoicePicker
from .widgets import StateOrb

DEFAULT_MIC = "Default (system)"
SHORTCUTS = [("hotkey_talk", "Start listening"), ("hotkey_quick", "Quick command box"),
             ("hotkey_window", "Show / hide the window")]
# A few voices worth hearing first: (voice name, who they sound like)
FEATURED_VOICES = [("en_US-amy-medium", "Amy", "Warm, clear. The default."),
                   ("en_US-lessac-high", "Lessac", "Crisp and natural. Bigger download."),
                   ("en_US-ryan-high", "Ryan", "Relaxed male voice."),
                   ("en_US-kristin-medium", "Kristin", "Bright and quick."),
                   ("en_US-joe-medium", "Joe", "Friendly, conversational."),
                   ("en_US-norman-medium", "Norman", "Deeper, calm.")]


class ChoiceCard(QPushButton):
    """A checkable card: a bold title and a wrapped line under it. Sized by its contents, so the text wraps
    instead of being cut off however narrow the column gets."""

    def __init__(self, title: str, description: str, value: str, flat: bool = False):
        super().__init__()
        self.setObjectName("choiceFlat" if flat else "choice")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("value", value)
        self.setAccessibleName(f"{title}. {description}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("choiceTitle")
        self.description = QLabel(description)
        self.description.setObjectName("muted")
        self.description.setWordWrap(True)
        for label in (self.title, self.description):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self.layout().totalHeightForWidth(width)


class OnboardingDialog(QDialog):
    mic_level = Signal(float)              # 0..1 loudness, from the microphone callback
    mic_note = Signal(str)
    voice_status = Signal(str)             # a voice test / download finished ('' = nothing to say)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._snapshot = dict(backend.CONFIG)
        self._theme_before = current_theme()
        self._finished = False
        self._get: dict[str, callable] = {}
        self._mic_stream = None
        self._voice_busy = False
        self.setWindowTitle(f"Welcome to {backend.assistant_name()}")
        self.setObjectName("onboarding")
        self.setMinimumSize(900, 640)
        self.resize(960, 680)
        # Modal to its parent only, never to the whole app (that would lock the status bar).
        self.setWindowModality(Qt.WindowModal if parent is not None else Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- the rail
        self.rail = QFrame()
        self.rail.setObjectName("rail")
        self.rail.setFixedWidth(220)
        rail = QVBoxLayout(self.rail)
        rail.setContentsMargins(22, 26, 18, 22)
        rail.setSpacing(4)
        self.brand = QLabel("NEON")
        self.brand.setObjectName("brand")
        rail.addWidget(self.brand)
        rail.addSpacing(18)
        self._pages = [("Welcome", self._page_welcome), ("Hearing you", self._page_hearing),
                       ("My voice", self._page_voice), ("My brain", self._page_brain), ("Look", self._page_look),
                       ("Connections", self._page_connections), ("Shortcuts", self._page_shortcuts),
                       ("All set", self._page_done)]
        self._rail_items: list[QLabel] = []
        for i, (title, _build) in enumerate(self._pages):
            item = QLabel(f"{i + 1}   {title}")
            item.setObjectName("railItem")
            rail.addWidget(item)
            self._rail_items.append(item)
        rail.addStretch(1)
        self.skip_btn = QPushButton("Skip setup")
        self.skip_btn.setObjectName("link")
        self.skip_btn.setCursor(Qt.PointingHandCursor)
        self.skip_btn.clicked.connect(self.reject)
        rail.addWidget(self.skip_btn, 0, Qt.AlignLeft)
        root.addWidget(self.rail)

        # ---- the page and its footer
        right = QVBoxLayout()
        right.setContentsMargins(40, 30, 40, 22)
        right.setSpacing(12)
        self.pages = QStackedWidget()
        for _title, build in self._pages:
            self.pages.addWidget(self._scrolling(build()))
        right.addWidget(self.pages, 1)
        self._fade_fx = QGraphicsOpacityEffect(self.pages)
        self.pages.setGraphicsEffect(self._fade_fx)
        self._fade = QPropertyAnimation(self._fade_fx, b"opacity", self)
        self._fade.setDuration(200)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        self.error = QLabel("")
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        right.addWidget(self.error)
        footer = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setObjectName("muted")
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn = QPushButton("Continue")
        self.next_btn.setObjectName("primary")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(lambda: self._go(1))
        footer.addWidget(self.step_label)
        footer.addStretch(1)
        footer.addWidget(self.back_btn)
        footer.addWidget(self.next_btn)
        right.addLayout(footer)
        root.addLayout(right, 1)

        self.mic_level.connect(self._show_level)
        self.mic_note.connect(lambda text: self.mic_message.setText(text))
        self.voice_status.connect(self._on_voice_status)
        theme_signals.changed.connect(self._restyle)
        self._restyle()
        self._show_page(0)
        frame.install(self)

    # ================================================================================================
    # Building blocks
    # ================================================================================================
    @staticmethod
    def _scrolling(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(page)
        page.setAutoFillBackground(False)            # QScrollArea.setWidget() turns this on: undo it
        for label in page.findChildren(QLabel):       # wrapped text takes the full width, never a narrow column
            if label.wordWrap():
                label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        return scroll

    def _page(self, title: str, blurb: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("heading")
        text = QLabel(blurb)
        text.setObjectName("muted")
        text.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(text)
        layout.addSpacing(4)
        return page, layout

    @staticmethod
    def _card(title: str = "", hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 14, 18, 16)
        inner.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            inner.addWidget(label)
        if hint:
            note = QLabel(hint)
            note.setObjectName("muted")
            note.setWordWrap(True)
            inner.addWidget(note)
        return card, inner

    @staticmethod
    def _choices(options: list[tuple[str, str, str]], current: str, columns: int = 2) -> tuple[QWidget, QButtonGroup]:
        """Big option cards (title + one line), one of which is picked. options: (value, title, description)."""
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        group = QButtonGroup(box)
        group.setExclusive(True)
        for i, (value, title, desc) in enumerate(options):
            button = ChoiceCard(title, desc, value)
            group.addButton(button, i)
            grid.addWidget(button, i // columns, i % columns)
            if value == current:
                button.setChecked(True)
        if group.checkedButton() is None and group.buttons():
            group.buttons()[0].setChecked(True)
        return box, group

    @staticmethod
    def _picked(group: QButtonGroup) -> str:
        button = group.checkedButton()
        return str(button.property("value")) if button is not None else ""

    # ================================================================================================
    # 1. Welcome
    # ================================================================================================
    def _page_welcome(self) -> QWidget:
        page, layout = self._page("Hi. Let's get you set up.",
                                  "Two minutes, eight short steps. Everything here can be changed later in Settings, "
                                  "and anything you skip keeps a sensible default.")
        top = QHBoxLayout()
        self.orb = StateOrb(96)
        self.orb.set_state("listening")
        top.addWidget(self.orb, 0, Qt.AlignTop)
        top.addSpacing(18)
        name_col = QVBoxLayout()
        ask = QLabel("What should I be called?")
        ask.setObjectName("cardTitle")
        self.name = QLineEdit(backend.assistant_name())
        self.name.setPlaceholderText("Nova")
        self.name.setMaximumWidth(360)
        self._get["assistant_name"] = lambda: backend.sanitize_name(self.name.text()) or backend.DEFAULT_CONFIG["assistant_name"]
        name_col.addWidget(ask)
        name_col.addWidget(self.name)
        you = QLabel("And what should I call you?")
        you.setObjectName("cardTitle")
        self.user = QLineEdit(backend.user_name())
        self.user.setPlaceholderText("Your name (optional)")
        self.user.setMaximumWidth(360)
        self._get["user_name"] = lambda: backend.sanitize_name(self.user.text())
        name_col.addSpacing(8)
        name_col.addWidget(you)
        name_col.addWidget(self.user)
        name_col.addStretch(1)
        top.addLayout(name_col, 1)
        layout.addLayout(top)

        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (title, text) in enumerate((
                ("Private by default", "Your voice, the AI and your notes stay on this PC."),
                ("Talk normally", "\"Shut the music up for a sec\" works as well as \"pause\"."),
                ("Gets facts right", "Dates and names are looked up, not guessed."),
                ("Runs your PC", "Apps, windows, music, timers, the browser, passwords."))):
            card, inner = self._card(title, text)
            grid.addWidget(card, i // 2, i % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    # ================================================================================================
    # 2. Hearing you
    # ================================================================================================
    def _page_hearing(self) -> QWidget:
        page, layout = self._page("Hearing you", "Pick your microphone and check I can hear it, then choose how "
                                                 "I understand speech and what wakes me up.")
        card, inner = self._card("Microphone")
        self.mic = QComboBox()
        self.mic.addItem(DEFAULT_MIC, "")
        for device in backend.list_input_devices():
            self.mic.addItem(device, device)
        self.mic.setCurrentIndex(max(0, self.mic.findData(str(backend.CONFIG.get("mic_device") or ""))))
        self._get["mic_device"] = self.mic.currentData
        row = QHBoxLayout()
        self.mic_test = QPushButton("Test it")
        self.mic_test.setCheckable(True)
        self.mic_test.toggled.connect(self._toggle_mic_test)
        row.addWidget(self.mic, 1)
        row.addWidget(self.mic_test)
        inner.addLayout(row)
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(10)
        inner.addWidget(self.level)
        self.mic_message = QLabel("Press Test it and say something: the bar should jump. If you use a streaming "
                                  "mixer, pick your real microphone, not the mix.")
        self.mic_message.setObjectName("muted")
        self.mic_message.setWordWrap(True)
        inner.addWidget(self.mic_message)
        self.mic.currentIndexChanged.connect(lambda _i: self.mic_test.setChecked(False))
        layout.addWidget(card)

        label = QLabel("Understanding speech")
        label.setObjectName("cardTitle")
        layout.addWidget(label)
        whisper_note = "Offline. Audio never leaves this PC." + (
            "" if stt.whisper_available() else " Needs: pip install faster-whisper")
        box, self.stt_group = self._choices(
            [("whisper", "On this PC (Whisper)", whisper_note),
             ("google", "Google", "A little faster; audio is sent to Google.")],
            str(backend.CONFIG.get("stt_engine", "whisper")))
        self._get["stt_engine"] = lambda: self._picked(self.stt_group)
        layout.addWidget(box)

        label = QLabel("Waking me up")
        label.setObjectName("cardTitle")
        layout.addWidget(label)
        box, self.wake_group = self._choices(
            [("phrase", "Say \"hey nova\"", "A tiny offline detector. Light on the processor."),
             ("name", "Say my name", "Speech recognition listens for it. Uses more processor.")],
            "name" if backend.cfg_bool("name_is_wake_word") else "phrase")
        self._get["name_is_wake_word"] = lambda: self._picked(self.wake_group) == "name"
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _toggle_mic_test(self, on: bool) -> None:
        self._stop_mic()
        if not on:
            self.mic_test.setText("Test it")
            self.level.setValue(0)
            return
        self.mic_test.setText("Stop")
        device = self.mic.currentData() or None
        try:
            import numpy as np
            import sounddevice as sd

            def callback(data, _frames, _time, _status) -> None:
                peak = float(np.abs(data).max()) / 32768.0
                try:
                    self.mic_level.emit(peak)
                except RuntimeError:
                    pass
            self._mic_stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", device=device,
                                              blocksize=800, callback=callback)
            self._mic_stream.start()
            self.mic_message.setText("Listening... say something.")
        except Exception as exc:  # noqa: BLE001 -- no such device, no PortAudio, permission denied
            self.mic_message.setText(f"Couldn't open that microphone: {exc}")
            self.mic_test.blockSignals(True)
            self.mic_test.setChecked(False)
            self.mic_test.setText("Test it")
            self.mic_test.blockSignals(False)

    def _show_level(self, peak: float) -> None:
        self.level.setValue(min(100, int(peak * 250)))
        if peak > 0.04:
            self.mic_message.setText("I can hear you.")

    def _stop_mic(self) -> None:
        stream, self._mic_stream = self._mic_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # ================================================================================================
    # 3. My voice
    # ================================================================================================
    def _page_voice(self) -> QWidget:
        page, layout = self._page("My voice", "Pick how I sound. Press play to hear one; voices you haven't got "
                                              "yet download the first time (about 60 MB each).")
        piper_now = str(backend.CONFIG.get("tts_engine", "piper")).lower() == "piper"
        current = str(backend.CONFIG.get("tts_voice") or "")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.voice_group = QButtonGroup(self)
        self.voice_group.setExclusive(True)
        for i, (name, who, what) in enumerate(FEATURED_VOICES):
            card = QFrame()
            card.setObjectName("card")
            line = QHBoxLayout(card)
            line.setContentsMargins(14, 10, 10, 10)
            pick = ChoiceCard(who, what, name, flat=True)
            play = QPushButton("▶")
            play.setObjectName("play")
            play.setFixedSize(36, 36)
            play.setToolTip(f"Hear {who}")
            play.clicked.connect(lambda _c=False, n=name, b=pick: (b.setChecked(True), self._hear(n)))
            line.addWidget(pick, 1)
            line.addWidget(play)
            self.voice_group.addButton(pick, i)
            grid.addWidget(card, i // 2, i % 2)
            if piper_now and name == current:
                pick.setChecked(True)
        layout.addLayout(grid)

        more, inner = self._card("Another voice", "Any Piper voice, in any language. Picking one here replaces "
                                                  "the choice above.")
        featured = {name for name, *_ in FEATURED_VOICES}
        self.more_voice = PiperVoicePicker(current if piper_now and current not in featured else "",
                                           none_label="None: use the one picked above")
        inner.addWidget(self.more_voice)
        self.more_voice.changed.connect(self._other_voice_picked)
        self.voice_group.buttonClicked.connect(lambda _b: self.more_voice.set_value(""))
        self.use_windows = QCheckBox("Use the Windows voice instead (no download)" if osinfo.IS_WINDOWS
                                     else "Use the system voice instead (espeak-ng, no download)")
        self.use_windows.setChecked(not piper_now)
        inner.addWidget(self.use_windows)
        layout.addWidget(more)
        if self.voice_group.checkedButton() is None and not self.more_voice.value():
            self.voice_group.buttons()[0].setChecked(True)

        tune, inner = self._card("How I talk")
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed"))
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(70, 150)
        self.speed.setValue(int(round(backend.cfg_num("tts_speed") * 100)))
        self.speed_value = QLabel("")
        self.speed.valueChanged.connect(lambda v: self.speed_value.setText(f"{v / 100:.2f}x"))
        self.speed_value.setText(f"{self.speed.value() / 100:.2f}x")
        speed_row.addWidget(self.speed, 1)
        speed_row.addWidget(self.speed_value)
        inner.addLayout(speed_row)
        self.listen_sound = QCheckBox("Play a short sound when I start listening")
        self.listen_sound.setChecked(str(backend.CONFIG.get("listen_sound", "blip")) != "off")
        self.thinking = QCheckBox("Say something like \"one moment\" when an answer is slow")
        self.thinking.setChecked(backend.cfg_bool("thinking_lines_enabled"))
        inner.addWidget(self.listen_sound)
        inner.addWidget(self.thinking)
        self.voice_message = QLabel("")
        self.voice_message.setObjectName("muted")
        self.voice_message.setWordWrap(True)
        inner.addWidget(self.voice_message)
        layout.addWidget(tune)
        layout.addStretch(1)

        self._get["tts_engine"] = lambda: "sapi" if self.use_windows.isChecked() else "piper"
        self._get["tts_voice"] = self._chosen_voice
        self._get["tts_speed"] = lambda: round(self.speed.value() / 100, 2)
        self._get["listen_sound"] = self._listen_sound_value
        self._get["thinking_lines_enabled"] = self.thinking.isChecked
        return page

    def _listen_sound_value(self) -> str:
        """Off, or the sound that was chosen before (the default one if it was off)."""
        if not self.listen_sound.isChecked():
            return "off"
        before = str(self._snapshot.get("listen_sound") or "")
        return before if before and before != "off" else backend.DEFAULT_CONFIG["listen_sound"]

    def _other_voice_picked(self) -> None:
        """A voice from the full list replaces the featured choice (so only one looks picked)."""
        if not self.more_voice.value():
            return
        self.voice_group.setExclusive(False)
        for button in self.voice_group.buttons():
            button.setChecked(False)
        self.voice_group.setExclusive(True)

    def _chosen_voice(self) -> str:
        if self.more_voice.value():
            return self.more_voice.value()
        button = self.voice_group.checkedButton()
        return str(button.property("value")) if button is not None else str(backend.CONFIG.get("tts_voice") or "")

    def _hear(self, name: str) -> None:
        if self._voice_busy:
            return
        self._voice_busy = True
        who = self._get["assistant_name"]()
        needs_download = not tts.is_installed(name)
        self.voice_message.setText(f"Downloading {tts.voice_label(name)}..." if needs_download else "Loading the voice...")
        saved = {k: backend.CONFIG.get(k) for k in ("tts_engine", "tts_voice", "tts_speed")}
        backend.CONFIG.update(tts_engine="piper", tts_voice=name, tts_speed=round(self.speed.value() / 100, 2))

        def work() -> None:
            speaker = None
            try:
                if needs_download:
                    tts.ensure_voice(name)
                speaker = backend.make_speaker()     # a throwaway: the real one switches when you finish
                speaker.say(f"Hi, I'm {who}. This is how I sound.").wait(timeout=20)
                message = ""
            except Exception as exc:  # noqa: BLE001 -- offline, disk full...
                message = f"Couldn't play that voice: {exc}"
            finally:
                if speaker is not None:
                    try:
                        speaker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                backend.CONFIG.update(saved)
            try:
                self.voice_status.emit(message)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="Nova-VoicePreview", daemon=True).start()

    def _on_voice_status(self, message: str) -> None:
        self._voice_busy = False
        self.voice_message.setText(message)
        self.more_voice.refresh_labels()

    # ================================================================================================
    # 4. My brain
    # ================================================================================================
    def _page_brain(self) -> QWidget:
        page, layout = self._page("My brain", "Conversation, summaries and understanding free-form commands come "
                                              "from a language model running on this PC, in Ollama.")
        status, inner = self._card("Ollama")
        self.ollama_state = QLabel("")
        self.ollama_state.setWordWrap(True)
        inner.addWidget(self.ollama_state)
        get = QPushButton("Get Ollama")
        get.clicked.connect(lambda: backend.open_website("ollama.com/download"))
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self._check_ollama)
        row = QHBoxLayout()
        row.addWidget(get)
        row.addWidget(recheck)
        row.addStretch(1)
        inner.addLayout(row)
        self.ollama_buttons = get
        layout.addWidget(status)

        model, inner = self._card("Model", "qwen3:8b is the recommended balance of speed and smarts. Smaller models "
                                           "are faster; bigger ones know more.")
        self.model = ModelPicker(str(backend.CONFIG.get("ollama_model") or ""))
        inner.addWidget(self.model)
        self._get["ollama_model"] = self.model.value
        layout.addWidget(model)

        accuracy, inner = self._card("Accuracy")
        self.smart = QCheckBox("Understand commands however they're phrased")
        self.smart.setChecked(backend.cfg_bool("smart_commands"))
        self.ground = QCheckBox("Look up facts (dates, names, releases) instead of answering from memory")
        self.ground.setChecked(backend.cfg_bool("ground_facts"))
        self.check = QCheckBox("Double-check facts before saying them (a few seconds slower)")
        self.check.setChecked(backend.cfg_bool("fact_check"))
        for box in (self.smart, self.ground, self.check):
            inner.addWidget(box)
        self._get.update(smart_commands=self.smart.isChecked, ground_facts=self.ground.isChecked,
                         fact_check=self.check.isChecked)
        layout.addWidget(accuracy)
        layout.addStretch(1)
        self._check_ollama(refresh=False)               # the picker has only just read the list
        return page

    def _check_ollama(self, refresh: bool = True) -> None:
        if refresh:
            self.model.reload(self.model.value())
        models = self.model._installed
        if models:
            self.ollama_state.setText(f"Running, with {len(models)} model{'s' if len(models) != 1 else ''} "
                                      "downloaded.")
            self.ollama_buttons.hide()
        else:
            self.ollama_state.setText("Not found. Every command works without it, but answering questions and "
                                      "chatting need it. Install it, start it, then press Check again.")
            self.ollama_buttons.show()

    # ================================================================================================
    # 5. Look
    # ================================================================================================
    def _page_look(self) -> QWidget:
        page, layout = self._page("Look", "Pick a theme; it changes as you click. Accent and status colors are in "
                                          "Settings > Appearance.")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        now = str(backend.CONFIG.get("theme", "neon"))
        short = [spec["label"].split(" (")[0] for spec in THEMES.values()]
        for i, (key, spec) in enumerate(THEMES.items()):
            # "Neon (pink on black)" -> "Neon", unless two themes would then share a name
            tile = QPushButton(short[i] if short.count(short[i]) == 1 else spec["label"])
            tile.setObjectName("themeTile")
            tile.setCheckable(True)
            tile.setCursor(Qt.PointingHandCursor)
            tile.setProperty("value", key)
            tile.setFixedHeight(64)
            tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            accent2 = spec.get("accent2", spec["accent"])
            tile.setStyleSheet(
                f"QPushButton#themeTile {{ color: {spec['text']}; font-weight: 700; text-align: left; padding: 0 14px;"
                f" border-radius: 12px; border: 2px solid {spec.get('border', spec['panel'])};"
                f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {spec['bg']}, stop:0.62 {spec['panel']},"
                f" stop:0.63 {spec['accent']}, stop:1 {accent2}); }}"
                f"QPushButton#themeTile:checked {{ border: 3px solid {spec['accent']}; }}")
            self.theme_group.addButton(tile, i)
            grid.addWidget(tile, i // 3, i % 3)
            if key == now:
                tile.setChecked(True)
        self.theme_group.buttonClicked.connect(lambda b: apply_theme(
            str(b.property("value")), str(backend.CONFIG.get("theme_accent", "")), backend.CONFIG.get("state_colors")))
        self._get["theme"] = lambda: self._picked(self.theme_group) or now
        layout.addLayout(grid)

        more, inner = self._card("Windows and the status bar" if osinfo.IS_WINDOWS else "The status bar")
        self.titlebar = QCheckBox("Use NEON's own title bar on my windows")
        self.titlebar.setChecked(backend.cfg_bool("custom_titlebar"))
        self.titlebar.setVisible(osinfo.IS_WINDOWS)          # on Linux the desktop draws title bars
        self.bar_show = QCheckBox("Show the status bar (what I hear and say, plus a clock and widgets)")
        self.bar_show.setChecked(backend.cfg_bool("show_status_bar"))
        self.bar_pos = QComboBox()
        self.bar_pos.addItem("Along the top of the screen", "top")
        self.bar_pos.addItem("Along the bottom of the screen", "bottom")
        self.bar_pos.setCurrentIndex(max(0, self.bar_pos.findData(str(backend.CONFIG.get("bar_position", "top")))))
        self.bar_pos.setEnabled(self.bar_show.isChecked())
        self.bar_show.toggled.connect(self.bar_pos.setEnabled)
        for w in (self.titlebar, self.bar_show, self.bar_pos):
            inner.addWidget(w)
        self._get.update(custom_titlebar=self.titlebar.isChecked, show_status_bar=self.bar_show.isChecked,
                         bar_position=self.bar_pos.currentData)
        layout.addWidget(more)
        layout.addStretch(1)
        return page

    # ================================================================================================
    # 6. Connections
    # ================================================================================================
    def _page_connections(self) -> QWidget:
        page, layout = self._page("Connections", "Optional extras. Leave any of them off; they're all in Settings.")
        notify, inner = self._card("Windows notifications" if osinfo.IS_WINDOWS else "Notifications", "I show them in the status bar and can read or "
                                                             "summarize them for you.")
        self.notify = QCheckBox("Handle my notifications")
        self.notify.setChecked(backend.cfg_bool("notify_enabled"))
        self.notify_ask = QComboBox()
        for value, label in (("speech", "Ask out loud whether to summarize"), ("summarize", "Summarize them straight away"),
                             ("read", "Read them out straight away"), ("message", "Read just the message"),
                             ("card", "Show buttons, stay quiet"), ("none", "Just show them")):
            self.notify_ask.addItem(label, value)
        self.notify_ask.setCurrentIndex(max(0, self.notify_ask.findData(str(backend.CONFIG.get("notify_ask", "speech")))))
        self.notify_ask.setEnabled(self.notify.isChecked())
        self.notify.toggled.connect(self.notify_ask.setEnabled)
        inner.addWidget(self.notify)
        inner.addWidget(self.notify_ask)
        layout.addWidget(notify)

        music, inner = self._card("Music", "Play, pause and skip work with any player" +
                                  (" through Windows" if osinfo.IS_WINDOWS else "") +
                                  ". Pear Desktop (YouTube Music) also gets \"play Daft Punk\" and likes.")
        self.music = QCheckBox("Control Pear Desktop when it's running")
        self.music.setChecked(backend.cfg_bool("ytm_enabled"))
        inner.addWidget(self.music)
        layout.addWidget(music)

        browser, inner = self._card("Browser", "With the NEON extension in your browser: \"summarize this page\", "
                                               "\"what's the price on this page\", \"switch to the GitHub tab\".")
        self.browser = QCheckBox("Talk to the browser extension")
        self.browser.setChecked(backend.cfg_bool("browser_enabled"))
        copy = QPushButton("Copy the extension's token")
        copy.clicked.connect(lambda: (QApplication.clipboard().setText(browser_bridge.token()),
                                      copy.setText("Copied. Paste it into the extension.")))
        inner.addWidget(self.browser)
        inner.addWidget(copy, 0, Qt.AlignLeft)
        self.browser.toggled.connect(copy.setEnabled)
        copy.setEnabled(self.browser.isChecked())
        layout.addWidget(browser)

        vault, inner = self._card("Bitwarden", "\"What's my username for Proton Mail\", \"copy my GitHub password\". "
                                               "Passwords are never said out loud.")
        self.vault = QCheckBox("Use my Bitwarden vault")
        self.vault.setChecked(backend.cfg_bool("bitwarden_enabled"))
        inner.addWidget(self.vault)
        if not bitwarden.find_cli():
            note = QLabel("First install Bitwarden's command-line tool: winget install Bitwarden.CLI, then run "
                          "bw login once in a terminal.")
            note.setObjectName("muted")
            note.setWordWrap(True)
            inner.addWidget(note)
        layout.addWidget(vault)
        layout.addStretch(1)
        self._get.update(notify_enabled=self.notify.isChecked, notify_ask=self.notify_ask.currentData,
                         ytm_enabled=self.music.isChecked, browser_enabled=self.browser.isChecked,
                         bitwarden_enabled=self.vault.isChecked)
        return page

    # ================================================================================================
    # 7. Shortcuts
    # ================================================================================================
    def _page_shortcuts(self) -> QWidget:
        page, layout = self._page("Shortcuts", "Keys that work in any app. All optional: click a box and press a "
                                               "combination.")
        card, inner = self._card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        self._seqs: dict[str, QKeySequenceEdit] = {}
        for row, (key, label) in enumerate(SHORTCUTS):
            seq = QKeySequenceEdit()
            seq.setMaximumSequenceLength(1)
            seq.setKeySequence(QKeySequence(str(backend.CONFIG.get(key) or "")))
            clear = QPushButton("Clear")
            clear.clicked.connect(seq.clear)
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(seq, row, 1)
            grid.addWidget(clear, row, 2)
            self._seqs[key] = seq
        grid.setColumnStretch(1, 1)
        inner.addLayout(grid)
        layout.addWidget(card)

        copilot, inner = self._card("The Copilot key", "Laptops with a Copilot key can use it for me instead.")
        action = str(backend.CONFIG.get("copilot_key_action"))
        on = backend.cfg_bool("copilot_key_enabled")
        box, self.copilot_group = self._choices(
            [("off", "Leave it alone", "It does what Windows normally does."),
             ("talk", "Start listening", "Press it and speak."),
             ("quick", "Quick command box", "A small box to type a command.")],
            action if on and action in ("talk", "quick") and osinfo.IS_WINDOWS else "off", columns=3)
        inner.addWidget(box)
        layout.addWidget(copilot)
        copilot.setVisible(osinfo.IS_WINDOWS)                # a Windows key; on Linux any key is bound above

        import shortcuts
        opener, inner = self._card("Opening me", "Shortcuts to start me, like any other app.")
        self.start_menu_box = QCheckBox("Put me in the Start menu" if osinfo.IS_WINDOWS else "Put me in the app menu")
        self.start_menu_box.setChecked(True if not backend.cfg_bool("onboarding_done") else shortcuts.exists("start_menu"))
        self.desktop_box = QCheckBox("Put a shortcut to me on the desktop")
        self.desktop_box.setChecked(shortcuts.exists("desktop"))
        inner.addWidget(self.start_menu_box)
        inner.addWidget(self.desktop_box)
        layout.addWidget(opener)
        layout.addStretch(1)
        return page

    def _shortcut_values(self) -> dict[str, str]:
        return {key: seq.keySequence().toString(QKeySequence.PortableText) for key, seq in self._seqs.items()}

    def _validate_shortcuts(self) -> str | None:
        seen: dict[str, str] = {}
        labels = dict(SHORTCUTS)
        for key, spec in self._shortcut_values().items():
            if not spec:
                continue
            try:
                hotkeys.parse_hotkey(spec)
            except ValueError as exc:
                return f"{labels[key]}: {exc}"
            if spec.lower() in seen:
                return f"{labels[key]} and {seen[spec.lower()]} can't share {spec}."
            seen[spec.lower()] = labels[key]
        for key in ("hotkey_wake", "hotkey_mute"):          # set in Settings > Hotkeys, not shown here
            spec = str(backend.CONFIG.get(key) or "")
            if spec and spec.lower() in seen:
                return f"{seen[spec.lower()]} is already used in Settings > Hotkeys; pick another."
        return None

    # ================================================================================================
    # 8. All set
    # ================================================================================================
    def _page_done(self) -> QWidget:
        page, layout = self._page("All set", "Here's what you picked. Press Start and try one of these.")
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        card, inner = self._card("Your setup")
        inner.addWidget(self.summary)
        layout.addWidget(card)
        tries, inner = self._card("Things to try")
        self.tips = QLabel()
        self.tips.setTextFormat(Qt.RichText)
        self.tips.setWordWrap(True)
        inner.addWidget(self.tips)
        layout.addWidget(tries)
        layout.addStretch(1)
        return page

    def _summary_html(self) -> str:
        voice = (("the Windows voice" if osinfo.IS_WINDOWS else "the system voice") if self.use_windows.isChecked()
                 else tts.voice_label(self._chosen_voice()) or self._chosen_voice())
        wake = self._get["assistant_name"]().lower() if self._get["name_is_wake_word"]() else "hey nova"
        keys = [f"{label}: {spec}" for key, label in SHORTCUTS if (spec := self._shortcut_values().get(key))]
        copilot = self._picked(self.copilot_group)
        if copilot != "off":
            keys.append("Copilot key: " + ("start listening" if copilot == "talk" else "quick command box"))
        extras = [name for name, box in (("notifications", self.notify), ("Pear Desktop", self.music),
                                         ("the browser", self.browser), ("Bitwarden", self.vault)) if box.isChecked()]
        rows = [("Name", self._get["assistant_name"]()), ("Wake word", f"\"{wake}\""),
                ("Speech", "on this PC" if self._get["stt_engine"]() == "whisper" else "Google"),
                ("Voice", voice), ("AI model", self.model.value() or "none"),
                ("Theme", THEMES.get(self._get["theme"](), {}).get("label", "")),
                ("Connected", ", ".join(extras) if extras else "nothing extra"),
                ("Shortcuts", ", ".join(keys) if keys else "none yet")]
        return "".join(f"<p style='margin:3px 0'><b>{k}</b>&nbsp;&nbsp;{v}</p>" for k, v in rows)

    def _tips_html(self) -> str:
        wake = self._get["assistant_name"]().lower() if self._get["name_is_wake_word"]() else "hey nova"
        tips = [f"\"{wake}\", then \"what's the weather?\"", "\"open spotify\" or \"close discord\"",
                "\"when did Hollow Knight Silksong come out?\"", "\"set a timer for ten minutes\"",
                "\"remember that my gate code is 4821\""]
        if self.browser.isChecked():
            tips.append("\"summarize this page\" (with the extension installed)")
        return "".join(f"<p style='margin:3px 0'>&bull;&nbsp; {t}</p>" for t in tips)

    # ================================================================================================
    # Moving between steps
    # ================================================================================================
    def _show_page(self, index: int) -> None:
        if index != 1:
            self.mic_test.setChecked(False)                  # leaving the microphone page closes the mic
        self.pages.setCurrentIndex(index)
        last = self.pages.count() - 1
        self.back_btn.setVisible(index > 0)
        self.next_btn.setText("Start using NEON" if index == last else ("Let's go" if index == 0 else "Continue"))
        self.step_label.setText(f"Step {index + 1} of {last + 1}")
        self.error.setText("")
        for i, item in enumerate(self._rail_items):
            item.setProperty("state", "current" if i == index else ("done" if i < index else "todo"))
            item.setText(f"{'✓' if i < index else i + 1}   {self._pages[i][0]}")
            item.style().unpolish(item)
            item.style().polish(item)
        if self._pages[index][0] == "All set":
            self.summary.setText(self._summary_html())
            self.tips.setText(self._tips_html())
        self._fade.stop()
        if animations_enabled():
            self._fade.start()
        else:
            self._fade_fx.setOpacity(1.0)

    def _go(self, step: int) -> None:
        index = self.pages.currentIndex()
        name = self._pages[index][0]
        if step > 0 and name == "My voice" and not self.use_windows.isChecked():
            voice = self._chosen_voice()
            if voice and not tts.valid_voice_name(voice):
                self.error.setText(f"'{voice}' isn't a Piper voice name. They look like en_US-amy-medium.")
                return
        if step > 0 and name == "Shortcuts":
            problem = self._validate_shortcuts()
            if problem:
                self.error.setText(problem)
                return
        target = index + step
        if target >= self.pages.count():
            self._finish()
        elif target >= 0:
            self._show_page(target)

    # ================================================================================================
    # Finishing
    # ================================================================================================
    def _finish(self) -> None:
        old = self._snapshot
        new = {key: getter() for key, getter in self._get.items()}
        new["assistant_name"] = backend.sanitize_name(new.get("assistant_name")) or backend.DEFAULT_CONFIG["assistant_name"]
        if new.get("name_is_wake_word"):
            new["wake_word"] = new["assistant_name"].lower()
            new["wake_engine"] = "stt"                  # the detector only knows trained phrases, not any name
        elif old.get("name_is_wake_word"):
            new["wake_word"] = backend.DEFAULT_CONFIG["wake_word"]
            new["wake_engine"] = backend.DEFAULT_CONFIG["wake_engine"]
        new.update(self._shortcut_values())
        copilot = self._picked(self.copilot_group)
        if copilot in ("talk", "quick"):
            new.update(copilot_key_enabled=True, copilot_key_action=copilot)
        elif backend.cfg_bool("copilot_key_enabled") and str(backend.CONFIG.get("copilot_key_action")) in ("talk", "quick"):
            new["copilot_key_enabled"] = False
        new["onboarding_done"] = True

        listener_changed = new.get("mic_device") != (old.get("mic_device") or "")
        engine_changed = (str(new.get("tts_engine")) != str(old.get("tts_engine"))
                          or new.get("tts_voice") != old.get("tts_voice"))
        ai_changed = new.get("ollama_model") != old.get("ollama_model")
        titlebar_changed = bool(new.get("custom_titlebar")) != bool(old.get("custom_titlebar", True))
        browser_changed = bool(new.get("browser_enabled")) != bool(old.get("browser_enabled"))
        import shortcuts
        problems = shortcuts.apply({"start_menu": self.start_menu_box.isChecked(),
                                    "desktop": self.desktop_box.isChecked()})
        new["start_menu_shortcut"], new["desktop_shortcut"] = shortcuts.exists("start_menu"), shortcuts.exists("desktop")
        self._stop_mic()
        backend.CONFIG.update(new)
        backend.save_config(backend.CONFIG)
        self._finished = True
        for problem in problems:
            self._controller.message.emit("system", problem)
        self.accept()
        self._controller.apply_settings(listener_changed, engine_changed, ai_changed)
        if browser_changed:
            self._controller.apply_browser_setting()
        self._controller.settings_saved.emit()          # re-registers hotkeys, refreshes names and the bar
        if titlebar_changed:
            QTimer.singleShot(0, frame.refresh_all)

    def reject(self) -> None:
        """Skip / close: leave the settings as they were, but remember the tour was shown."""
        self._stop_mic()
        if not self._finished:
            backend.CONFIG.clear()
            backend.CONFIG.update(self._snapshot)
            backend.CONFIG["onboarding_done"] = True
            backend.save_config(backend.CONFIG)
            before = str(self._snapshot.get("theme", "neon"))
            if current_theme() != effective_theme(before if before in THEMES else "neon"):
                # Undo the theme preview. Only when there was one: restyling every open window is slow,
                # and this also runs when the app quits with the tour open.
                apply_theme(before, str(self._snapshot.get("theme_accent", "")), self._snapshot.get("state_colors"))
        super().reject()

    # ================================================================================================
    # Looks
    # ================================================================================================
    def _restyle(self) -> None:
        c = COLORS
        self.setStyleSheet(f"""
            QDialog#onboarding {{ background: {c['bg']}; }}
            QFrame#rail {{ background: {c['panel']}; border-right: 1px solid {c['border']}; }}
            QLabel#brand {{ color: {c['accent']}; font-size: 26px; font-weight: 900; letter-spacing: 4px; }}
            QLabel#railItem {{ color: {c['muted']}; padding: 8px 10px; border-radius: 8px; font-size: 13px; }}
            QLabel#railItem[state="current"] {{ color: {c['on_accent'] if 'on_accent' in c else c['bg']};
                                                background: {c['accent']}; font-weight: 700; }}
            QLabel#railItem[state="done"] {{ color: {c['text']}; }}
            QLabel#heading {{ color: {c['text']}; font-size: 26px; font-weight: 800; }}
            QLabel#cardTitle {{ color: {c['text']}; font-size: 14px; font-weight: 700; }}
            QLabel#error {{ color: {c['error']}; }}
            QFrame#card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 12px; }}
            QPushButton#choice, QPushButton#choiceFlat {{ text-align: left; padding: 10px 14px;
                border-radius: 10px; background: {c['panel_alt']}; border: 1px solid {c['border']};
                color: {c['text']}; }}
            QPushButton#choiceFlat {{ background: transparent; border: 1px solid transparent; }}
            QPushButton#choice:hover, QPushButton#choiceFlat:hover {{ border-color: {c['accent']}; }}
            QPushButton#choice:checked, QPushButton#choiceFlat:checked {{ border: 2px solid {c['accent']};
                background: {c['panel_alt']}; }}
            QPushButton#play {{ border-radius: 18px; padding: 0; }}
            QLabel#choiceTitle {{ color: {c['text']}; font-weight: 700; background: transparent; }}
            QPushButton#choice QLabel, QPushButton#choiceFlat QLabel {{ background: transparent; }}
            QPushButton#link {{ background: transparent; border: none; color: {c['muted']}; padding: 4px 2px;
                min-width: 80px; text-align: left; text-decoration: underline; }}
            QPushButton#link:hover {{ color: {c['text']}; }}
            QProgressBar {{ background: {c['panel_alt']}; border: none; border-radius: 5px; }}
            QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
        """)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enable_dark_title_bar(self)

    def closeEvent(self, event) -> None:
        self._stop_mic()
        super().closeEvent(event)
