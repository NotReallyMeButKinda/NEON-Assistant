"""
wakeword.py -- an alternative way to notice the wake phrase: openWakeWord.

Two engines can wait for the wake phrase (Settings > Listening > Wake word > "Detect it with"):

  stt            speech recognition -- your normal engine transcribes everything it hears and checks
                 whether it starts with your wake word. Any phrase works, including the assistant's
                 own name, but it costs a transcription for every sound in the room.
  openwakeword   a small neural detector that runs on this PC (no internet after the first download).
                 Much lighter, and it doesn't depend on the speech engine. It listens for one phrase:
                   - openWakeWord's own models (PHRASES: "hey jarvis", "alexa"...), which need
                     `pip install openwakeword`, or
                   - a custom model ("custom:<name>"), trained on this PC by wake_training.py from
                     Piper-synthesized speech plus your own recordings. "hey nova" and "hey dan" are
                     offered ready to train (PRESETS); any other phrase can be trained in Settings.
                     These only need onnxruntime, which Piper already brings.

Custom models are two parts: openWakeWord's shared feature models (a melspectrogram and a speech
embedding, downloaded once, `FeatureStream` below) and a tiny classifier over the last 1.28 s of
embeddings, stored as <data>/wakeword/custom/<name>.npz (`CustomModel`).

`Detector.feed(chunk)` takes int16 mono 16 kHz audio of any length and returns True when the phrase
was just heard. If openWakeWord isn't installed (pip install openwakeword) or can't load, the Listener
says so and carries on with the speech-recognition engine.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

import app_paths
import neon_log

log = neon_log.get("wakeword")

FRAME = 1280                                    # openWakeWord works on 80 ms frames (16 kHz)
PHRASES = {"hey_jarvis": "hey jarvis", "alexa": "alexa", "hey_mycroft": "hey mycroft", "hey_rhasspy": "hey rhasspy"}
DEFAULT_PHRASE = "hey_jarvis"
ENGINES = ("stt", "openwakeword")
_SHARED = ("melspectrogram.onnx", "embedding_model.onnx")     # the feature models every phrase model builds on
_SHARED_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{}"
CUSTOM = "custom:"
PRESETS = {"hey_nova": "hey nova", "hey_dan": "hey dan"}      # custom phrases offered before they are trained
WINDOW = 16                                                    # embeddings the classifier looks at (16 x 80 ms)
_MEL_CONTEXT = 480                                             # extra samples the melspectrogram needs per hop


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("openwakeword") is not None


def use_detector(cfg: dict) -> bool:
    """True if the settings ask for openWakeWord."""
    return str(cfg.get("wake_engine", "stt")).strip().lower() == "openwakeword"


def signature(cfg: dict) -> tuple:
    """What a running wake loop is built from: change any of it and the loop must be restarted
    (a custom model's file time too, so retraining it takes effect)."""
    model = str(cfg.get("wake_model", DEFAULT_PHRASE))
    try:
        stamp = custom_path(model).stat().st_mtime if is_custom(model) else 0.0
    except OSError:
        stamp = 0.0
    return (str(cfg.get("wake_engine", "stt")).strip().lower(), model, stamp)


def is_custom(model: str) -> bool:
    return str(model).startswith(CUSTOM)


def slug(phrase: str) -> str:
    """'Hey, Nova!' -> 'hey_nova' (the file and setting name of a custom phrase)."""
    return re.sub(r"[^a-z0-9]+", "_", str(phrase).lower()).strip("_")[:40]


def custom_dir(directory: Path | None = None) -> Path:
    return (Path(directory) if directory else models_dir()) / "custom"


def custom_path(model: str, directory: Path | None = None) -> Path:
    return custom_dir(directory) / f"{slug(str(model).removeprefix(CUSTOM))}.npz"


def custom_info(model: str, directory: Path | None = None) -> dict | None:
    """The saved description of a trained custom model (phrase, samples, when), or None."""
    try:
        with np.load(custom_path(model, directory), allow_pickle=False) as data:
            return json.loads(str(data["meta"]))
    except (OSError, KeyError, ValueError):
        return None


def trained(model: str, directory: Path | None = None) -> bool:
    return is_custom(model) and custom_path(model, directory).exists()


def list_models(directory: Path | None = None) -> list[tuple[str, str, str]]:
    """Every phrase the detector can use: (setting value, phrase, kind), where kind is
    'custom' (trained here), 'preset' (offered, trained on first use) or 'builtin' (openWakeWord's)."""
    found: dict[str, tuple[str, str, str]] = {}
    folder = custom_dir(directory)
    if folder.exists():
        for file in sorted(folder.glob("*.npz")):
            key = CUSTOM + file.stem
            info = custom_info(key, directory) or {}
            found[key] = (key, str(info.get("phrase") or file.stem.replace("_", " ")), "custom")
    for name, phrase in PRESETS.items():
        found.setdefault(CUSTOM + name, (CUSTOM + name, phrase, "preset"))
    return list(found.values()) + [(key, phrase, "builtin") for key, phrase in PHRASES.items()]


def delete_custom(model: str, directory: Path | None = None) -> bool:
    try:
        custom_path(model, directory).unlink()
        return True
    except OSError:
        return False


def phrase_of(model: str) -> str:
    model = str(model)
    if is_custom(model):
        info = custom_info(model)
        name = model.removeprefix(CUSTOM)
        return str((info or {}).get("phrase") or PRESETS.get(name) or name.replace("_", " "))
    return PHRASES.get(model, PHRASES[DEFAULT_PHRASE])


def phrase_words(cfg: dict) -> str:
    """The spoken phrase of the chosen detector model: 'hey jarvis', 'hey nova'."""
    return phrase_of(str(cfg.get("wake_model", DEFAULT_PHRASE)))


def label(cfg: dict) -> str:
    """The phrase to tell the user to say: the detector's phrase, or the configured wake word."""
    return phrase_words(cfg) if use_detector(cfg) else str(cfg.get("wake_word", "")).strip()


def strip_phrase(text: str, phrase: str) -> str:
    """Drops leading words of the wake phrase that leaked into a transcription ("jarvis what time is it"
    when the detector fired a moment before the tail of "hey jarvis" ended)."""
    words = set(re.findall(r"[a-z']+", phrase.lower()))
    tokens = text.split()
    while tokens and re.sub(r"[^a-z']", "", tokens[0].lower()) in words:
        tokens.pop(0)
    return " ".join(tokens).strip(" ,.")


def models_dir() -> Path:
    return app_paths.data_path("wakeword")


def ensure_feature_models(on_status=None, directory: Path | None = None) -> Path:
    """The two shared feature models, downloaded (about 2.4 MB) the first time. Returns their folder."""
    folder = Path(directory) if directory else models_dir()
    missing = [name for name in _SHARED if not (folder / name).exists()]
    if missing:
        (on_status or (lambda _m: None))("Downloading the wake-word feature models (2 MB, once)...")
        folder.mkdir(parents=True, exist_ok=True)
        for name in missing:
            tmp = folder / (name + ".part")
            with urllib.request.urlopen(_SHARED_URL.format(name), timeout=60) as response:
                tmp.write_bytes(response.read())
            tmp.replace(folder / name)
    return folder


class FeatureStream:
    """openWakeWord's front end, run with onnxruntime: int16 16 kHz audio in, one 96-number speech
    embedding out per 80 ms. Training (wake_training.py) and detection both go through this class,
    so the classifier always sees features computed exactly the same way."""

    def __init__(self, directory: Path | None = None):
        import onnxruntime as ort
        folder = Path(directory) if directory else models_dir()
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self._mel = ort.InferenceSession(str(folder / _SHARED[0]), options, providers=providers)
        self._emb = ort.InferenceSession(str(folder / _SHARED[1]), options, providers=providers)
        self.reset()

    def reset(self) -> None:
        self._raw = np.zeros(FRAME + _MEL_CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._mels = np.ones((76, 32), dtype=np.float32)
        self.embeddings: deque = deque(maxlen=WINDOW * 4)

    def feed(self, audio: np.ndarray, keep_all: bool = False) -> int:
        """Add audio; returns how many new embeddings it produced. `keep_all` (training) keeps every
        embedding instead of only the recent ones."""
        if keep_all and self.embeddings.maxlen is not None:
            self.embeddings = deque(self.embeddings)
        self._pending = np.concatenate([self._pending, np.asarray(audio, dtype=np.float32)])
        made = 0
        while len(self._pending) >= FRAME:
            hop, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            self._raw = np.concatenate([self._raw, hop])[-(FRAME + _MEL_CONTEXT):]
            mel = self._mel.run(None, {"input": self._raw[None, :]})[0].squeeze()
            mel = np.atleast_2d(mel) / 10.0 + 2.0
            self._mels = np.concatenate([self._mels, mel.astype(np.float32)])[-76:]
            self.embeddings.append(self._emb.run(None, {"input_1": self._mels[None, :, :, None]})[0].reshape(96))
            made += 1
        return made

    def window(self) -> np.ndarray | None:
        """The last WINDOW embeddings, flattened, or None until there are enough."""
        if len(self.embeddings) < WINDOW:
            return None
        return np.concatenate(list(self.embeddings)[-WINDOW:])


class Classifier:
    """A one-hidden-layer network over a window of embeddings (the trained part of a custom model)."""

    KEYS = ("w1", "b1", "w2", "b2", "mean", "scale")

    def __init__(self, weights: dict):
        self.w1, self.b1 = weights["w1"], weights["b1"]
        self.w2, self.b2 = weights["w2"], float(np.asarray(weights["b2"]).reshape(-1)[0])
        self.mean, self.scale = weights["mean"], weights["scale"]

    def score(self, windows: np.ndarray) -> np.ndarray:
        x = (np.atleast_2d(windows) - self.mean) / self.scale
        hidden = np.maximum(0.0, x @ self.w1 + self.b1)
        z = np.clip(hidden @ self.w2 + self.b2, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-z))


class CustomModel:
    """A trained custom phrase, with the same predict() / reset() shape as openWakeWord's Model."""

    def __init__(self, path: Path, directory: Path | None = None):
        with np.load(path, allow_pickle=False) as data:
            self.classifier = Classifier({k: data[k] for k in Classifier.KEYS})
            self.meta = json.loads(str(data["meta"]))
        self.name = Path(path).stem
        self.features = FeatureStream(directory)

    def reset(self) -> None:
        self.features.reset()

    def predict(self, frame: np.ndarray) -> dict:
        score = 0.0
        if self.features.feed(frame):
            window = self.features.window()
            if window is not None:
                score = float(self.classifier.score(window)[0])
        return {self.name: score}


class Detector:
    """One loaded openWakeWord model. Not thread-safe: use it from a single loop."""

    def __init__(self, model: str = DEFAULT_PHRASE, threshold=lambda: 0.5, on_status=None,
                 directory: Path | None = None, loader=None):
        self.name = model if model in PHRASES or is_custom(model) else DEFAULT_PHRASE
        self._threshold = threshold if callable(threshold) else (lambda: float(threshold))
        self._on_status = on_status or (lambda _message: None)
        self._dir = Path(directory) if directory else models_dir()
        self._loader = loader or self._load_real
        self._model = None
        self._buf = np.zeros(0, dtype=np.int16)
        self._streak = 0
        self._lock = threading.Lock()
        self.last_score = 0.0                  # the highest frame score in the last feed() (the trainer's meter)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load_real(self):
        if is_custom(self.name):
            return self._load_custom()
        import openwakeword
        from openwakeword.model import Model
        phrase_file = f"{self.name}_v0.1.onnx"
        if not all((self._dir / f).exists() for f in (phrase_file, *_SHARED)):
            self._on_status("Downloading the wake-word model (a few MB, once)...")
            self._dir.mkdir(parents=True, exist_ok=True)
            openwakeword.utils.download_models([self.name], target_directory=str(self._dir))
        return Model(wakeword_models=[str(self._dir / phrase_file)], inference_framework="onnx",
                     melspec_model_path=str(self._dir / _SHARED[0]),
                     embedding_model_path=str(self._dir / _SHARED[1]))

    def _load_custom(self) -> CustomModel:
        ensure_feature_models(self._on_status, self._dir)
        path = custom_path(self.name, self._dir)
        if not path.exists():
            name = self.name.removeprefix(CUSTOM)
            if name not in PRESETS:
                raise FileNotFoundError(f"the wake phrase '{name.replace('_', ' ')}' hasn't been trained yet")
            import wake_training                       # a preset: train it now, from synthesized speech
            self._on_status(f"Training the \"{PRESETS[name]}\" wake word (a few minutes, once)...")
            wake_training.train(PRESETS[name], directory=self._dir)
            self._on_status(f"\"{PRESETS[name]}\" is ready.")
        return CustomModel(path, self._dir)

    def load(self) -> None:
        """Loads (downloading the first time) the model. Raises if the detector isn't usable."""
        with self._lock:
            if self._model is None:
                self._model = self._loader()

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        self._streak = 0
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:  # noqa: BLE001 -- older builds lack reset(); the next frames overwrite the state anyway
                pass

    def score(self, frame: np.ndarray) -> float:
        scores = self._model.predict(frame)
        return max((float(v) for v in scores.values()), default=0.0)

    def feed(self, chunk: np.ndarray) -> bool:
        """Add audio; True if the phrase was heard in it. Detection resets the model so one utterance
        can't trigger twice. A custom model must be sure on two frames in a row (it was trained to
        fire on three), which filters out single-frame flukes in ordinary speech."""
        self._buf = np.concatenate([self._buf, np.asarray(chunk, dtype=np.int16)])
        threshold, hit = self._threshold(), False
        needed = 2 if is_custom(self.name) else 1
        self.last_score = 0.0
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            score = self.score(frame)
            self.last_score = max(self.last_score, score)
            self._streak = self._streak + 1 if score >= threshold else 0
            if self._streak >= needed:
                hit = True
        if hit:
            self.reset()
        return hit
