"""
tts.py -- Piper neural text-to-speech with sentence-level pipelining.

Text is split into sentences; a synthesis thread renders sentence N+1 while
sentence N is already playing through a persistent sounddevice OutputStream, so
first audio only costs the time to synthesize the *first* sentence (tens to a
few hundred ms). The playback callback also measures loudness (`level`) so the
UI can animate against the real audio.

Public API (matches assistant.SapiSpeaker so the controller doesn't care):
    say(text) -> threading.Event      speak text, event is set when finished
    begin_stream() -> Utterance       feed()/finish() for streamed LLM output
    stop_speaking()                   cut off immediately, drop everything queued
    stop()                            shut the worker threads down
    level, has_level, is_speaking     playback loudness 0..1 / state

Reads its settings live from the config dict it is given, so changes made in
the Settings dialog apply to the next sentence without a restart.
"""

from __future__ import annotations

import queue
import re
import threading
import unicodedata
from collections import OrderedDict, deque
from pathlib import Path

import numpy as np
import sounddevice as sd

import app_paths  # noqa: E402
import persona  # noqa: E402
import speech_text  # noqa: E402

VOICES_DIR = app_paths.data_path("voices")
DEFAULT_VOICE = "en_US-amy-medium"
WARMUP_TEXT = "Ready."
PHRASE_CACHE_SIZE = 48         # how many short phrases keep their audio
PHRASE_CACHE_MAX_CHARS = 90    # ...and what counts as short


# ---------------------------------------------------------------------------
# Voices on disk
# ---------------------------------------------------------------------------

def list_installed_voices() -> list[str]:
    """Names of voices in voices/ (both the .onnx and its .json must exist)."""
    if not VOICES_DIR.exists():
        return []
    return sorted(p.name[:-len(".onnx")] for p in VOICES_DIR.glob("*.onnx")
                  if (p.parent / (p.name + ".json")).exists())


# Voices offered for download, verified to exist in rhasspy/piper-voices (name -> download size in MB).
# "high" sounds best but is bigger and a little slower to speak; "medium" is the everyday choice.
VOICE_CATALOG: dict[str, int] = {
    "en_US-amy-medium": 63, "en_US-hfc_female-medium": 63, "en_US-hfc_male-medium": 63,
    "en_US-joe-medium": 63, "en_US-john-medium": 63, "en_US-kristin-medium": 63,
    "en_US-kusal-medium": 63, "en_US-lessac-medium": 63, "en_US-lessac-high": 113,
    "en_US-ljspeech-high": 114, "en_US-norman-medium": 63, "en_US-ryan-medium": 63,
    "en_US-ryan-high": 120, "en_US-sam-medium": 62, "en_US-bryce-medium": 63,
    "en_US-libritts_r-medium": 78,
    "en_GB-alba-medium": 63, "en_GB-jenny_dioco-medium": 63, "en_GB-alan-medium": 63,
    "en_GB-northern_english_male-medium": 63, "en_GB-cori-medium": 63,
}


def is_installed(name: str) -> bool:
    return (VOICES_DIR / f"{name}.onnx").exists() and (VOICES_DIR / f"{name}.onnx.json").exists()


def voice_label(name: str) -> str:
    """'en_GB-jenny_dioco-medium' -> 'Jenny Dioco (UK, medium)'; unknown names are shown as-is."""
    parts = name.split("-")
    if len(parts) != 3:
        return name
    lang, voice, quality = parts
    accent = {"en_US": "US", "en_GB": "UK"}.get(lang, lang)
    return f"{voice.replace('_', ' ').title()} ({accent}, {quality})"


_VOICE_NAME = re.compile(r"[a-z]{2,3}_[A-Z]{2}-[\w]+-(?:x_low|low|medium|high)")


def valid_voice_name(name: str) -> bool:
    """Piper voice names look like 'en_US-amy-medium' (language_REGION-voice-quality)."""
    return bool(_VOICE_NAME.fullmatch(name.strip()))


def voice_choice_label(name: str) -> str:
    """Dropdown text: the friendly name plus whether it's on disk or needs a download."""
    if is_installed(name):
        return f"{voice_label(name)}  -  installed"
    size = VOICE_CATALOG.get(name)
    return f"{voice_label(name)}  -  download" + (f", {size} MB" if size else "")


def ensure_voice(name: str) -> Path:
    """Returns the .onnx path for `name`, downloading it from the official
    rhasspy/piper-voices repo the first time."""
    model = VOICES_DIR / f"{name}.onnx"
    if model.exists() and (VOICES_DIR / f"{name}.onnx.json").exists():
        return model
    from piper.download_voices import download_voice
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    download_voice(name, VOICES_DIR)
    return model


# ---------------------------------------------------------------------------
# Text prep
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    "dr.": "doctor", "mr.": "mister", "mrs.": "missus", "ms.": "miss", "st.": "saint",
    "vs.": "versus", "etc.": "et cetera", "e.g.": "for example", "i.e.": "that is",
    "approx.": "approximately",
}
# abbreviations whose trailing period must not end a sentence
_NO_BREAK = re.compile(r"\b(?:dr|mr|mrs|ms|st|vs|etc|approx|e\.g|i\.e|no)\.$", re.I)


# Operators that sit between two operands, e.g. "12 * (3 + 4)". Each needs a digit or bracket
# on both sides so ordinary prose (bullets, "km/h", "and/or", hyphenated words) is left alone.
# Order matters: "**" before "*".
_MATH_OPS = [
    (r"(?<=[\d)])\s*(?:\*\*|\^)\s*(?=[\d(-])", " to the power of "),
    (r"(?<=[\d)])\s*[*×]\s*(?=[\d(-])", " times "),
    (r"(?<=\d)\s+x\s+(?=[\d(])", " times "),
    (r"(?<=[\d)])\s*[/÷]\s*(?=[\d(-])", " divided by "),
    (r"(?<=[\d)])\s*\+\s*(?=[\d(-])", " plus "),
    (r"(?<=[\d)])\s+[-−–]\s+(?=[\d(])", " minus "),
    (r"(?<=[\d)])\s+%\s+(?=[\d(])", " mod "),
]
_DATE = re.compile(r"\b\d{1,4}/\d{1,2}/\d{1,4}\b")


def _sqrt_spoken(m: "re.Match") -> str:
    arg = m.group(1).strip()
    if re.fullmatch(r"[\w.]+", arg):
        return f"square root of {arg}"
    return f"square root of open bracket {arg} close bracket"


def spoken_math(text: str) -> str:
    """Say math symbols the way a person would: `12 * (3 + 4) = 84` -> "12 times open bracket
    3 plus 4 close bracket equals 84". Only what is *spoken* changes; callers keep showing the
    symbols. Operators are converted only between operands, and dates (3/4/2025) are left alone."""
    text = _DATE.sub(lambda m: m.group(0).replace("/", "\x00"), text)   # protect dates
    text, n = re.subn(r"\bsqrt\s*\(\s*([^()]+?)\s*\)", _sqrt_spoken, text)
    is_math = n > 0
    for pattern, spoken in _MATH_OPS:
        text, n = re.subn(pattern, spoken, text)
        is_math = is_math or n > 0
    text = re.sub(r"(?<=[\w)])\s*=\s*(?=[\w(-])", " equals ", text)
    if is_math:  # brackets only matter (and are only read) inside an expression
        text = re.sub(r"\((?=\s*[\d-])", " open bracket ", text)
        text = re.sub(r"(?<=[\d)])\s*\)", " close bracket", text)
    text = re.sub(r"(?<![\w)\].])-(?=\d)", "negative ", text)
    text = re.sub(r"(?<=\d)\s*%", " percent", text)
    # 3.14 -> "3 point 1 4" (digits one by one, at most 4 places); versions/IPs (1.2.3) and
    # money ($3.50) are skipped.
    text = re.sub(r"(?<![\d.$£€])(\d+\.\d+)(?!\d|\.\d)", _spoken_decimal, text)
    return text.replace("\x00", "/")


def _spoken_decimal(m: "re.Match") -> str:
    whole, frac = m.group(1).split(".")
    if len(frac) > 4:
        whole, _, frac = f"{float(m.group(1)):.4f}".partition(".")
        frac = frac.rstrip("0")
    return f"{whole} point {' '.join(frac)}" if frac else whole


# ---------------------------------------------------------------------------
# Emoji, symbols and "fancy" Unicode text
# ---------------------------------------------------------------------------
# Piper (espeak underneath) and SAPI don't know emoji or styled letters, and read out the code point
# instead ("U plus 1 F 6 0 0"). So styled text is folded back to plain letters (NFKC turns 𝓗𝓮𝓵𝓵𝓸
# and ｆｕｌｌｗｉｄｔｈ into Hello and fullwidth) and every symbol is replaced by its name.

# Names that read badly straight from the Unicode tables ("heavy black heart"), or common ones worth
# saying the way people do.
_SYMBOL_NAMES = {
    "\u2764": "heart", "\U0001f44d": "thumbs up", "\U0001f44e": "thumbs down", "\U0001f602": "laughing",
    "\U0001f923": "laughing", "\U0001f62d": "crying", "\U0001f64f": "please", "\U0001f525": "fire",
    "\U0001f480": "skull", "\U0001f440": "eyes", "\U0001f389": "party", "\U0001f4af": "a hundred",
    "\U0001f60a": "smiling", "\U0001f642": "smiling", "\U0001f600": "grinning", "\U0001f601": "grinning",
    "\U0001f605": "sweat smile", "\U0001f609": "winking", "\U0001f60d": "heart eyes", "\U0001f618": "kiss",
    "\U0001f914": "thinking", "\U0001f644": "eye roll", "\U0001f97a": "pleading", "\U0001f60e": "cool",
    "\U0001f621": "angry", "\U0001f622": "sad", "\U0001f633": "flushed", "\U0001f631": "screaming",
    "\U0001f973": "party face", "\U0001f44b": "waving", "\U0001f44f": "clapping", "\U0001f4aa": "flexing",
    "\U0001f440\ufe0f": "eyes", "\u2705": "check", "\u2714": "check", "\u2713": "check", "\u274c": "cross",
    "\u2716": "cross", "\u2717": "cross", "\u26a0": "warning", "\u2757": "exclamation mark",
    "\u2753": "question mark", "\u2b50": "star", "\u2728": "sparkles", "\U0001f6a8": "siren",
    "\u2122": " trademark", "\u00ae": " registered", "\u00a9": "copyright", "\u00b0": " degrees",
    "\u2022": ",", "\u00b7": ",", "\u2023": ",", "\u25cf": ",", "\u25aa": ",", "\u25b8": ",", "\u25ba": ",",
    "\u2192": " to ", "\u27a1": " to ", "\u2190": ",", "\u2194": " and ", "\u21d2": " so ",
    "\u00bd": " one half", "\u00bc": " one quarter", "\u00be": " three quarters",
}
_NAME_NOISE = re.compile(r"\b(?:heavy|black|white|medium|large|small|sign|symbol|emoji|ornament|"
                         r"with|variation selector|full|face)\b")
_SILENT = re.compile("[\u200b-\u200f\u2060-\u2064\ufe00-\ufe0f\U000e0000-\U000e007f\U0001f3fb-\U0001f3ff\u20e3]")
_COMBINING = re.compile("[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")
_ZWJ_TAIL = re.compile("\u200d[^\\s\u200d]+")          # 👩‍💻 -> 👩: a joined emoji is named by its first part
_FLAG = re.compile("[\U0001f1e6-\U0001f1ff]{2}")
_LOOKALIKE_LETTER = re.compile(r"^LATIN (?:LETTER SMALL CAPITAL|SMALL CAPITAL LETTER|SMALL LETTER TURNED|"
                               r"CAPITAL LETTER TURNED|SUBSCRIPT SMALL LETTER|LETTER) ([A-Z])$")


def _symbol_name(char: str) -> str:
    if char in _SYMBOL_NAMES:
        return _SYMBOL_NAMES[char]
    name = unicodedata.name(char, "")
    if not name:
        return " "
    letter = _LOOKALIKE_LETTER.match(name)
    if letter:
        return letter.group(1).lower()
    words = " ".join(_NAME_NOISE.sub(" ", name.lower()).split())
    return words or " "


def speakable_symbols(text: str) -> str:
    """Emoji and symbols -> their names, styled letters -> plain ones, invisible joiners and skin
    tones dropped. A run of the same emoji ("😂😂😂") is said once. Letters of any language stay."""
    # NFKC would turn these into letters ("TM", "1⁄2") before they could be named
    text = "".join(_SYMBOL_NAMES.get(c, c) if c in "\u2122\u00ae\u00bd\u00bc\u00be" else c for c in str(text))
    text = unicodedata.normalize("NFKC", text)
    text = _FLAG.sub(" flag ", text)
    text = _ZWJ_TAIL.sub("", text)
    text = _SILENT.sub("", text)
    text = _COMBINING.sub("", unicodedata.normalize("NFC", text))
    out: list[str] = []
    last_symbol = ""
    for char in text:
        if char.isascii():
            out.append(char)
            if not char.isspace():
                last_symbol = ""
            continue
        if char in _SYMBOL_NAMES:
            name = _SYMBOL_NAMES[char].strip()
            if name != last_symbol or name == ",":
                out.append(", " if name == "," else f" {name} ")
            last_symbol = name
            continue
        category = unicodedata.category(char)
        if category[0] in "LNPZ" and not _LOOKALIKE_LETTER.match(unicodedata.name(char, "")):
            out.append(char)                           # letters, digits and punctuation of any language
            last_symbol = ""
            continue
        if category[0] == "M":                         # a vowel sign or similar, attached to a letter
            out.append(char)
            continue
        name = _symbol_name(char)
        if category[0] == "L":                         # a look-alike letter: part of the word
            out.append(name)
            continue
        if name.strip() and name == last_symbol:        # the same emoji again
            continue
        last_symbol = name
        out.append(f" {name.strip()} " if name.strip() else " ")
    return re.sub(r"[ \t]+", " ", "".join(out)).replace(" ,", ",")


def clean_for_speech(text: str) -> str:
    """Strip markup/symbols a voice would read out loud and expand abbreviations."""
    text = re.sub(r"```.*?```", " ", str(text), flags=re.S)
    text = re.sub(r"https?://\S+", "link", text)
    text = speakable_symbols(text)
    text = spoken_math(text)   # before the symbol strip below, which would eat "*"
    text = re.sub(r"#\s?(?=\d)", "number ", text)     # "#1" is a rank, not a markdown heading
    text = re.sub(r"[*_`#>~|]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = text.replace("/", ", ")  # the default prompt uses slashes as pause markers
    for abbr, spoken in _ABBREVIATIONS.items():
        text = re.sub(rf"\b{re.escape(abbr)}", spoken, text, flags=re.I)
    return speech_text.words_for_speech(text)       # numbers, prices, times and symbols as words; no brackets


_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")


def split_sentences(text: str, min_len: int = 24, max_len: int = 220) -> list[str]:
    """Sentence-split for pipelining. Very short fragments are merged into their
    neighbour (choppy prosody otherwise); overlong ones are broken at commas."""
    pieces: list[str] = []
    buf = ""
    for part in _SENTENCE_END.split(text.strip()):
        part = part.strip()
        if not part:
            continue
        buf = f"{buf} {part}".strip() if buf else part
        if _NO_BREAK.search(buf):
            continue
        if len(buf) >= min_len:
            pieces.append(buf)
            buf = ""
    if buf:
        if pieces and len(buf) < min_len:
            pieces[-1] = f"{pieces[-1]} {buf}"
        else:
            pieces.append(buf)

    out: list[str] = []
    for piece in pieces:
        while len(piece) > max_len:
            cut = piece.rfind(", ", 0, max_len)
            if cut < min_len:
                cut = piece.rfind(" ", 0, max_len)
            if cut < min_len:
                break
            out.append(piece[:cut + 1].strip())
            piece = piece[cut + 1:].strip()
        out.append(piece)
    return out


class SentenceBuffer:
    """Accumulates streamed text chunks and releases complete sentences."""

    _FIRST_SENTENCE = re.compile(r"(.+?[.!?…][\"')\]]*)(?:\s+|$)", re.S)

    def __init__(self, min_len: int = 24, first_min_len: int = 6):
        self._pending = ""
        self._min_len = min_len
        self._first_min_len = first_min_len   # the opening sentence may be much shorter
        self._released = False                # has anything been handed out yet?

    def push(self, chunk: str) -> list[str]:
        self._pending += chunk
        # Only sentences followed by whitespace are known to be complete.
        m = None
        for m in re.finditer(r"[.!?…][\"')\]]*\s|\n", self._pending):
            pass
        if m is None:
            return []
        head, self._pending = self._pending[:m.end()], self._pending[m.end():]
        out: list[str] = []

        # Speak the very first sentence as soon as it is complete, however short ("Hi there!"):
        # normally short sentences are merged into the next one for smoother prosody, but that
        # would make the reply wait for the following sentence to be generated first.
        if not self._released:
            first = self._FIRST_SENTENCE.match(head)
            if first:
                sentence = first.group(1).strip()
                if len(sentence) >= self._first_min_len and not _NO_BREAK.search(sentence):
                    out.append(sentence)
                    head = head[first.end():]
                    self._released = True

        if head.strip():
            sentences = split_sentences(head, self._min_len)
            # split_sentences may hold back a short tail; keep it for the next push.
            if sentences and len(sentences[-1]) < self._min_len and not sentences[-1].endswith("\n"):
                self._pending = sentences.pop() + " " + self._pending
            if sentences:
                self._released = True
            out.extend(sentences)
        return out

    def flush(self) -> list[str]:
        rest, self._pending = self._pending.strip(), ""
        return split_sentences(rest, 1) if rest else []


# ---------------------------------------------------------------------------
# Speaker
# ---------------------------------------------------------------------------

class Utterance:
    """One spoken reply. `done` is set once every sentence has been played (or the
    utterance was cancelled)."""

    def __init__(self, speaker: "PiperSpeaker"):
        self._speaker = speaker
        self.done = threading.Event()
        self.gen = speaker._gen
        self._buffer = SentenceBuffer()
        self._closed = False

    def feed(self, chunk: str) -> None:
        if self._closed:
            return
        for sentence in self._buffer.push(chunk):
            self._speaker._submit(self, sentence)

    def finish(self) -> None:
        if self._closed:
            return
        for sentence in self._buffer.flush():
            self._speaker._submit(self, sentence)
        self._closed = True
        self._speaker._submit(self, None)  # end marker


SPECTRUM_BANDS = 9             # how many bars the status-bar visualiser draws
_FFT_WINDOW = 512              # samples analysed per callback: enough resolution, still ~20 us


class PiperSpeaker:
    has_level = True
    has_bands = True

    def __init__(self, config: dict, on_error=None, on_status=None):
        self._cfg = config
        self._on_error = on_error or (lambda exc: None)
        self._on_status = on_status or (lambda msg: None)

        self._gen = 0                       # bumped by stop_speaking() to invalidate queued work
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._buf: deque = deque()          # ["audio", int16 array, pos] / ["end", Utterance]
        self._buf_lock = threading.Lock()
        self._active: set[Utterance] = set()
        self._stop = threading.Event()

        self._voice = None
        self._voice_name = None
        self._stream: sd.OutputStream | None = None
        self._stream_key: tuple | None = None
        self.level = 0.0
        self.bands = [0.0] * SPECTRUM_BANDS      # live frequency bars, read by the UI while speaking
        self._window = np.hanning(_FFT_WINDOW).astype(np.float32)
        self._band_slices: list[tuple[int, int]] | None = None    # built once the sample rate is known
        # Short phrases ("Okay.", "Opening github.com.") repeat a lot; keep their audio so they play
        # instantly. Keyed by everything that changes how they sound.
        self._phrases: "OrderedDict[tuple, list[np.ndarray]]" = OrderedDict()
        self.ready = threading.Event()      # set after the first voice load + warm-up
        self.load_error: Exception | None = None

        self._thread = threading.Thread(target=self._synth_loop, name="Nova-Piper", daemon=True)
        self._thread.start()

    # ---- config (read live) ----------------------------------------------------
    def _num(self, key: str, default: float) -> float:
        try:
            return float(self._cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _voice_choice(self) -> str:
        # A persona may bring its own voice (installed ones only; see persona.voice_for).
        own = persona.voice_for(self._cfg.get("persona"), self._cfg, list_installed_voices())
        return own or str(self._cfg.get("tts_voice") or DEFAULT_VOICE)

    def _output_device(self):
        dev = self._cfg.get("output_device") or None
        return dev

    # ---- public API ------------------------------------------------------------
    @property
    def is_speaking(self) -> bool:
        with self._buf_lock:
            return bool(self._buf) or not self._jobs.empty()

    def begin_stream(self) -> Utterance:
        utt = Utterance(self)
        self._active.add(utt)
        return utt

    def say(self, text: str) -> threading.Event:
        utt = self.begin_stream()
        if not clean_for_speech(text):        # nothing speakable; the synth loop cleans the real text once
            utt.finish()
            return utt.done
        utt.feed(text + "\n")
        utt.finish()
        return utt.done

    def stop_speaking(self) -> None:
        """Cut off playback now and drop everything queued."""
        self._gen += 1
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        with self._buf_lock:
            self._buf.clear()
        for utt in list(self._active):
            utt.done.set()
        self._active.clear()
        self.level = 0.0

    def stop(self) -> None:
        self._stop.set()
        self.stop_speaking()
        self._jobs.put((None, None, 0))
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    # ---- internals -------------------------------------------------------------
    def _submit(self, utt: Utterance, sentence: str | None) -> None:
        self._jobs.put((utt, sentence, utt.gen))

    def _ensure_voice(self):
        name = self._voice_choice()
        if self._voice is not None and name == self._voice_name:
            return self._voice
        from piper import PiperVoice
        self._on_status(f"Loading voice {name}...")
        model = ensure_voice(name)  # may download on first use
        voice = PiperVoice.load(str(model))
        # The first synthesis is ~1s slower than later ones; pay it now, not on the first reply.
        for _ in voice.synthesize(WARMUP_TEXT):
            pass
        self._voice, self._voice_name = voice, name
        self._on_status("Voice ready.")
        return voice

    def _ensure_stream(self, sample_rate: int) -> None:
        key = (sample_rate, str(self._output_device()))
        if self._stream is not None and key == self._stream_key:
            return
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            with self._buf_lock:
                self._buf.clear()
        self._stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="int16",
            device=self._output_device(), callback=self._callback, blocksize=0, latency="low")
        self._stream.start()
        self._stream_key = key

    def _callback(self, outdata, frames, _time, _status) -> None:
        out = outdata[:, 0]
        out[:] = 0
        filled = 0
        with self._buf_lock:
            while filled < frames and self._buf:
                item = self._buf[0]
                if item[0] == "end":
                    self._buf.popleft()
                    item[1].done.set()
                    self._active.discard(item[1])
                    continue
                arr, pos = item[1], item[2]
                n = min(frames - filled, len(arr) - pos)
                out[filled:filled + n] = arr[pos:pos + n]
                filled += n
                if pos + n >= len(arr):
                    self._buf.popleft()
                else:
                    item[2] = pos + n
        if filled:
            rms = float(np.sqrt(np.mean(np.square(out[:filled].astype(np.float32))))) / 32768.0
            self.level = max(min(1.0, rms * 4.0), self.level * 0.85)
            self._update_bands(out[:filled])
        else:
            self.level *= 0.85
            self.bands = [b * 0.78 for b in self.bands]

    def _band_edges(self, sample_rate: int) -> list[tuple[int, int]]:
        """Log-spaced bin ranges from ~80 Hz to ~8 kHz: roughly how the ear groups pitch, so the
        bars move with the voice instead of all rising together."""
        bins = _FFT_WINDOW // 2 + 1
        low, high = 80.0, min(8000.0, sample_rate / 2)
        edges = []
        for i in range(SPECTRUM_BANDS + 1):
            freq = low * (high / low) ** (i / SPECTRUM_BANDS)
            edges.append(min(bins - 1, max(1, int(freq * _FFT_WINDOW / sample_rate))))
        return [(edges[i], max(edges[i] + 1, edges[i + 1])) for i in range(SPECTRUM_BANDS)]

    def _update_bands(self, samples) -> None:
        """Called from the audio callback, so it must stay cheap and never raise."""
        try:
            if self._band_slices is None:
                rate = int(self._stream.samplerate) if self._stream is not None else 22050
                self._band_slices = self._band_edges(rate)
            if len(samples) < _FFT_WINDOW:
                block = np.zeros(_FFT_WINDOW, dtype=np.float32)
                block[:len(samples)] = samples
            else:
                block = samples[-_FFT_WINDOW:].astype(np.float32)
            spectrum = np.abs(np.fft.rfft(block * self._window)) / (_FFT_WINDOW * 4096.0)
            fresh = []
            for i, (start, stop) in enumerate(self._band_slices):
                # A little tilt upwards: speech energy falls off with frequency, and flat bars
                # would leave the right-hand half of the display permanently asleep.
                value = float(spectrum[start:stop].mean()) * (1.0 + 0.55 * i)
                fresh.append(min(1.0, value ** 0.55))
            self.bands = [max(new, old * 0.72) for new, old in zip(fresh, self.bands)]
        except Exception:  # noqa: BLE001 -- a visualiser must never interrupt playback
            self.bands = [0.0] * SPECTRUM_BANDS

    def _synth_loop(self) -> None:
        from piper import SynthesisConfig
        try:  # load + warm up (and download, first run) before the first reply needs it
            self._ensure_voice()
        except Exception as exc:  # noqa: BLE001
            self.load_error = exc
        finally:
            self.ready.set()
        while not self._stop.is_set():
            utt, sentence, gen = self._jobs.get()
            if utt is None:
                return
            try:
                if gen != self._gen:
                    utt.done.set()  # cancelled before we got to it
                    continue
                if sentence is None:  # end-of-utterance marker: done fires after its audio plays
                    with self._buf_lock:
                        self._buf.append(["end", utt])
                    continue
                text = clean_for_speech(sentence)
                if not text:
                    continue
                voice = self._ensure_voice()
                self.load_error = None
                # A persona's pace multiplies the user's own speed rather than replacing it, so a
                # drawling noir detective is slower than a chirpy hype coach without either of them
                # overriding what you set in Settings > Voice.
                speed = max(0.4, self._num("tts_speed", 1.0) * persona.rate(self._cfg.get("persona")))
                syn = SynthesisConfig(
                    length_scale=1.0 / speed,
                    noise_scale=self._num("tts_noise", 0.35),
                    noise_w_scale=self._num("tts_noise_w", 0.5),
                    volume=max(0.0, self._num("tts_volume", 1.0)),
                )
                self._ensure_stream(voice.config.sample_rate)
                key = (self._voice_name, text, round(speed, 3), syn.noise_scale, syn.noise_w_scale, syn.volume)
                cached = self._phrases.get(key) if len(text) <= PHRASE_CACHE_MAX_CHARS else None
                if cached is not None:
                    self._phrases.move_to_end(key)
                    with self._buf_lock:
                        if gen == self._gen:
                            for audio in cached:
                                self._buf.append(["audio", audio.copy(), 0])
                    continue
                pieces: list[np.ndarray] = []
                complete = True
                for chunk in voice.synthesize(text, syn):
                    if gen != self._gen:
                        complete = False
                        break
                    audio = chunk.audio_int16_array.copy()
                    pieces.append(audio)
                    with self._buf_lock:
                        if gen == self._gen:
                            self._buf.append(["audio", audio.copy(), 0])
                if complete and pieces and len(text) <= PHRASE_CACHE_MAX_CHARS:
                    self._phrases[key] = pieces
                    while len(self._phrases) > PHRASE_CACHE_SIZE:
                        self._phrases.popitem(last=False)
            except Exception as exc:  # noqa: BLE001
                utt.done.set()
                self._on_error(exc)
