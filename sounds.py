"""
sounds.py -- the short "I heard you" sounds, synthesized in code (numpy only), so there are
no audio files to ship. `play()` is fire-and-forget and never raises: a missing audio device
must not break answering.

Sounds you make yourself (Settings > any sound > "Make a sound...", ui/beep_maker.py) are recipes, not
files: a tone, a few notes with their lengths, a gap and an optional echo. They live in the settings
("custom_beeps"), are named "beep:<name>", and play anywhere a built-in sound can (`synth_beep`).
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd

import neon_log

log = neon_log.get("sounds")

SAMPLE_RATE = 44100

# name -> label shown in Settings
SOUND_LABELS = {
    "chime": "Chime (two rising bell notes)",
    "ping": "Ping (single bright note)",
    "double": "Double tap",
    "blip": "Blip (quick rising sweep)",
    "pop": "Pop (soft bloop)",
    "soft": "Soft (gentle pad)",
    "glass": "Glass (high, tinkling)",
    "marimba": "Marimba (warm wooden note)",
    "sparkle": "Sparkle (rising arpeggio)",
    "drop": "Water drop",
    "click": "Click (tiny tick)",
    "swoosh": "Swoosh (airy sweep)",
    "harp": "Harp (three plucked notes)",
    "coin": "Coin (bright two-note)",
    "radar": "Radar (soft sonar ping)",
    "knock": "Knock (double thump)",
}
CUSTOM = "custom"                # a .wav file the user picked (16-bit PCM)
CUSTOM_LABEL = "Custom sound file..."
DEFAULT_SOUND = "chime"


def _t(duration: float) -> np.ndarray:
    return np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE


def _bell(freq: float, duration: float, decay: float, attack: float = 0.004) -> np.ndarray:
    """A struck-bell-ish note: fundamental + a quieter octave, exponential decay."""
    t = _t(duration)
    wave = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * freq * 2 * t)
    envelope = np.exp(-decay * t) * np.minimum(1.0, t / attack)
    return wave * envelope


def _mix(parts: list[tuple[float, np.ndarray]]) -> np.ndarray:
    """Layers (start_seconds, samples) pairs into one buffer."""
    length = max(int(start * SAMPLE_RATE) + len(x) for start, x in parts)
    out = np.zeros(length)
    for start, x in parts:
        i = int(start * SAMPLE_RATE)
        out[i:i + len(x)] += x
    return out


def _chirp(f0: float, f1: float, duration: float, decay: float) -> np.ndarray:
    t = _t(duration)
    freq = f0 + (f1 - f0) * (t / duration)
    phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
    return np.sin(phase) * np.exp(-decay * t) * np.minimum(1.0, t / 0.003)


def _noise_sweep(duration: float) -> np.ndarray:
    """Airy 'whoosh': smoothed noise that swells and fades (seeded, so it always sounds the same)."""
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(int(SAMPLE_RATE * duration))
    width = 40
    smooth = np.convolve(noise, np.ones(width) / width, mode="same")      # crude low-pass
    t = _t(duration)
    return smooth * np.sin(np.pi * t / duration) ** 2


def _build(name: str) -> np.ndarray:
    if name == "glass":
        return _mix([(0.0, _bell(2093.0, 0.7, 9.0)), (0.0, 0.5 * _bell(3136.0, 0.6, 12.0)),
                     (0.06, 0.4 * _bell(2637.0, 0.5, 14.0))])
    if name == "marimba":
        t = _t(0.45)
        return (np.sin(2 * np.pi * 587.33 * t) + 0.45 * np.sin(2 * np.pi * 4 * 587.33 * t) * np.exp(-40 * t)) \
            * np.exp(-9.0 * t) * np.minimum(1.0, t / 0.002)
    if name == "sparkle":
        notes = (1046.5, 1318.5, 1568.0, 2093.0)
        return _mix([(0.07 * i, _bell(f, 0.35, 16.0)) for i, f in enumerate(notes)])
    if name == "drop":
        return _mix([(0.0, _chirp(950.0, 380.0, 0.12, 28.0)), (0.11, 0.35 * _chirp(700.0, 330.0, 0.1, 30.0))])
    if name == "click":
        t = _t(0.035)
        return np.sin(2 * np.pi * 3200.0 * t) * np.exp(-150.0 * t)
    if name == "swoosh":
        return _noise_sweep(0.32)
    if name == "harp":
        t = lambda d: _t(d)                                     # noqa: E731
        pluck = lambda f: np.sin(2 * np.pi * f * t(0.5)) * np.exp(-6.5 * t(0.5)) * np.minimum(1.0, t(0.5) / 0.002)  # noqa: E731
        return _mix([(0.0, pluck(392.0)), (0.09, pluck(523.25)), (0.18, pluck(659.25))])
    if name == "coin":
        return _mix([(0.0, _bell(987.77, 0.1, 30.0)), (0.07, _bell(1318.5, 0.45, 9.0))])
    if name == "radar":
        return _mix([(0.0, _bell(880.0, 0.9, 5.0)), (0.32, 0.4 * _bell(880.0, 0.7, 6.0))])
    if name == "knock":
        return _mix([(0.0, _chirp(190.0, 90.0, 0.09, 38.0)), (0.13, _chirp(190.0, 90.0, 0.09, 38.0))])
    if name == "ping":
        return _bell(1319.0, 0.45, 11.0)
    if name == "double":
        return _mix([(0.0, _bell(987.8, 0.18, 22.0)), (0.13, _bell(987.8, 0.3, 16.0))])
    if name == "blip":
        return _chirp(600.0, 1500.0, 0.11, 14.0)
    if name == "pop":
        return _chirp(260.0, 95.0, 0.14, 24.0)
    if name == "soft":
        t = _t(0.5)
        env = np.minimum(1.0, t / 0.06) * np.exp(-5.0 * t)
        return (np.sin(2 * np.pi * 523.25 * t) + 0.25 * np.sin(2 * np.pi * 784.0 * t)) * env
    # "chime" (also the fallback for unknown names)
    return _mix([(0.0, _bell(659.25, 0.55, 7.0)), (0.09, _bell(987.77, 0.6, 7.0))])


# ---------------------------------------------------------------------------
# Sounds you make yourself
# ---------------------------------------------------------------------------

BEEP_PREFIX = "beep:"
RECIPES = {"source": lambda: {}}         # set by assistant.py: {name: recipe} from CONFIG["custom_beeps"]
TONES = {"bell": "Bell (rings on)", "soft": "Soft (gentle, rounded)", "bright": "Bright (buzzy, retro)",
         "pluck": "Pluck (short, like a string)"}
MAX_NOTES = 6
NOTE_MS = (40, 800)                      # shortest and longest note
GAP_MS = (0, 400)
_NOTE_LETTERS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_NAMES = [f"{letter}{octave}" for octave in range(3, 8) for letter in _NOTE_LETTERS][:-11]   # C3 .. C7

# Starting points for the editor: (label, recipe).
PRESETS = (
    ("Rising two-tone", {"tone": "bell", "notes": [["E5", 140], ["B5", 260]], "gap_ms": 30, "echo": False}),
    ("Falling two-tone", {"tone": "bell", "notes": [["B5", 140], ["E5", 260]], "gap_ms": 30, "echo": False}),
    ("Triple ping", {"tone": "pluck", "notes": [["A5", 90], ["A5", 90], ["A5", 160]], "gap_ms": 60, "echo": False}),
    ("Coin", {"tone": "bright", "notes": [["B5", 70], ["E6", 300]], "gap_ms": 0, "echo": False}),
    ("Doorbell", {"tone": "bell", "notes": [["E5", 350], ["C5", 500]], "gap_ms": 60, "echo": True}),
    ("Little tune", {"tone": "soft", "notes": [["C5", 120], ["E5", 120], ["G5", 120], ["C6", 320]], "gap_ms": 20,
                     "echo": False}),
    ("Low alert", {"tone": "bright", "notes": [["A3", 180], ["A3", 180]], "gap_ms": 90, "echo": False}),
)


def note_freq(name: str) -> float:
    """'A4' -> 440.0, 'C#5' -> 554.37. Unknown names are A4."""
    import re as _re
    m = _re.fullmatch(r"([A-G]#?)(\d)", str(name).strip().upper())
    if not m or m.group(1) not in _NOTE_LETTERS:
        return 440.0
    semitones = _NOTE_LETTERS.index(m.group(1)) - 9 + (int(m.group(2)) - 4) * 12
    return 440.0 * 2 ** (semitones / 12)


def clean_recipe(recipe) -> dict:
    """A recipe with everything in range (whatever the settings file says): 1-6 notes, known tone."""
    recipe = recipe if isinstance(recipe, dict) else {}
    notes = []
    for item in recipe.get("notes") or []:
        try:
            note, ms = (item["note"], item["ms"]) if isinstance(item, dict) else (item[0], item[1])
            notes.append([note if str(note).upper() in NOTE_NAMES else "A4",
                          int(max(NOTE_MS[0], min(NOTE_MS[1], float(ms))))])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    try:
        gap = int(max(GAP_MS[0], min(GAP_MS[1], float(recipe.get("gap_ms", 40)))))
    except (TypeError, ValueError):
        gap = 40
    return {"tone": recipe.get("tone") if recipe.get("tone") in TONES else "bell",
            "notes": notes[:MAX_NOTES] or [["A5", 200]], "gap_ms": gap, "echo": bool(recipe.get("echo"))}


def _tone_note(tone: str, freq: float, seconds: float) -> np.ndarray:
    """One note in the chosen tone. Bells and plucks ring past their length (the next note overlaps
    the tail, as a real one would); soft and bright stop where the note ends."""
    if tone == "bell":
        t = _t(seconds + 0.35)
        wave = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)
        return wave * np.exp(-t * 3.2 / max(0.08, seconds)) * np.minimum(1.0, t / 0.004)
    if tone == "pluck":
        t = _t(seconds + 0.15)
        wave = np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-30 * t)
        return wave * np.exp(-t * 6.0 / max(0.06, seconds)) * np.minimum(1.0, t / 0.002)
    t = _t(seconds)
    release = np.minimum(1.0, (seconds - t) / 0.025)                # no click at the end
    if tone == "bright":
        wave = sum(np.sin(2 * np.pi * k * freq * t) / k for k in (1, 3, 5, 7) if k * freq < 12000)
        return wave * np.minimum(1.0, t / 0.003) * release * 0.8
    wave = np.sin(2 * np.pi * freq * t) + 0.2 * np.sin(2 * np.pi * 1.5 * freq * t)          # soft
    return wave * np.minimum(1.0, t / 0.03) * release


def synth_beep(recipe) -> np.ndarray:
    """A recipe -> float samples at SAMPLE_RATE (not yet normalised)."""
    recipe = clean_recipe(recipe)
    parts, at = [], 0.0
    for note, ms in recipe["notes"]:
        parts.append((at, _tone_note(recipe["tone"], note_freq(note), ms / 1000.0)))
        at += (ms + recipe["gap_ms"]) / 1000.0
    wave = _mix(parts)
    if recipe["echo"]:
        wave = _mix([(0.0, wave), (0.18, 0.35 * wave)])
    return wave


def custom_sounds() -> dict[str, dict]:
    """{"beep:<name>": recipe} for the sounds the user made."""
    try:
        found = RECIPES["source"]() or {}
    except Exception:  # noqa: BLE001 -- the built-in sounds must keep working
        found = {}
    return {BEEP_PREFIX + str(name): recipe for name, recipe in found.items() if str(name).strip()}


def custom_label(key: str) -> str:
    return "Your sound: " + key[len(BEEP_PREFIX):]


_cache: dict[str, np.ndarray] = {}


def _normalised(wave: np.ndarray) -> np.ndarray:
    fade = min(len(wave), int(SAMPLE_RATE * 0.012))  # short fade-out so nothing ends in a click
    wave = wave.copy()
    wave[-fade:] *= np.linspace(1.0, 0.0, fade)
    return wave / max(1e-9, float(np.max(np.abs(wave))))


def render_recipe(recipe, volume: float = 0.5) -> np.ndarray:
    """int16 samples of a recipe (the editor plays unsaved ones through this)."""
    volume = max(0.0, min(1.0, float(volume)))
    return (_normalised(synth_beep(recipe)) * volume * 0.9 * 32767).astype(np.int16)


def render(name: str, volume: float = 0.5) -> np.ndarray:
    """int16 samples at SAMPLE_RATE, peak-normalised then scaled by `volume` (0..1)."""
    if str(name).startswith(BEEP_PREFIX):
        recipe = custom_sounds().get(name)
        if recipe is not None:
            return render_recipe(recipe, volume)
        name = DEFAULT_SOUND                                        # deleted since it was chosen
    if name not in SOUND_LABELS:
        name = DEFAULT_SOUND
    if name not in _cache:
        wave = _build(name)
        fade = min(len(wave), int(SAMPLE_RATE * 0.012))  # short fade-out so nothing ends in a click
        wave[-fade:] *= np.linspace(1.0, 0.0, fade)
        _cache[name] = wave / max(1e-9, float(np.max(np.abs(wave))))
    volume = max(0.0, min(1.0, float(volume)))
    return (_cache[name] * volume * 0.9 * 32767).astype(np.int16)


_file_cache: dict[tuple[str, float], tuple[np.ndarray, int]] = {}


def load_wav(path: str) -> tuple[np.ndarray, int]:
    """(mono float samples -1..1, sample rate) of a 16-bit PCM .wav file. Raises ValueError with a
    readable message for anything else."""
    import wave
    from pathlib import Path
    p = Path(path)
    key = (str(p), p.stat().st_mtime if p.exists() else 0.0)
    if key in _file_cache:
        return _file_cache[key]
    try:
        with wave.open(str(p), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, OSError, EOFError) as exc:
        raise ValueError(f"couldn't read that sound file ({exc})") from exc
    if width != 2:
        raise ValueError("only 16-bit .wav files are supported")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    data = data[: rate * 10]                                       # a notification sound, not a song
    _file_cache[key] = (data, rate)
    return data, rate


class _Player:
    """One low-latency output stream that stays open for a while after each sound. Opening a
    stream costs tens of milliseconds, which used to delay the "I heard you" sound on every command;
    now only the first sound of a burst pays it. The stream closes itself after IDLE_SECONDS so the
    audio device (and a Bluetooth headset's mode) isn't held forever."""

    IDLE_SECONDS = 20.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream = None
        self._key = None
        self._buf = np.zeros(0, dtype=np.float32)
        self._pos = 0
        self._closer: threading.Timer | None = None

    def _callback(self, outdata, frames, _time, _status) -> None:
        out = outdata[:, 0]
        out[:] = 0
        with self._lock:
            n = min(frames, len(self._buf) - self._pos)
            if n > 0:
                out[:n] = self._buf[self._pos:self._pos + n]
                self._pos += n

    def play(self, samples: np.ndarray, device) -> None:
        with self._lock:
            key = str(device)
            if self._stream is None or key != self._key:
                self._close_locked()
                self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=device,
                                               callback=self._callback, latency="low")
                self._stream.start()
                self._key = key
            self._buf, self._pos = samples.astype(np.float32, copy=False), 0    # a new sound replaces the old
            if self._closer is not None:
                self._closer.cancel()
            self._closer = threading.Timer(self.IDLE_SECONDS, self.close)
            self._closer.daemon = True
            self._closer.start()

    def _close_locked(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            self._close_locked()


_player = _Player()


def _resample(data: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE or len(data) == 0:
        return data
    n = int(len(data) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(np.float32)


def play_samples(samples: np.ndarray, device=None) -> bool:
    """Play int16 samples (the beep editor's preview). Never raises."""
    try:
        _player.play(samples.astype(np.float32) / 32768.0, device or None)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("couldn't play a sound: %s", exc)
        return False


def play(name: str, volume: float = 0.5, device=None, path: str = "") -> bool:
    """Starts playback and returns immediately. Returns False if it couldn't play. `name` may be
    "custom" with `path` pointing at a 16-bit .wav file."""
    try:
        volume = max(0.0, min(1.0, float(volume)))
        if name == CUSTOM:
            data, rate = load_wav(path)
            samples = _resample(data, rate) * volume * 0.9
        else:
            samples = render(name, volume).astype(np.float32) / 32768.0
        _player.play(samples, device or None)
        return True
    except Exception as exc:  # noqa: BLE001 -- no output device, device busy, bad file, etc.
        log.warning("couldn't play sound %r: %s", name, exc)
        return False
