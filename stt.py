"""
stt.py -- speech-to-text engines.

  google   the free Google Web Speech endpoint (SpeechRecognition). Needs internet, and every
           command's audio is sent to Google.
  whisper  faster-whisper running on this PC (CPU, int8). Nothing leaves the machine, it works
           offline, and it can also transcribe *partial* audio while you are still talking, which
           is what lets the status bar show your words live. The model (75-480 MB depending on the
           size chosen) downloads once, on first use.

`Transcriber.transcribe(pcm)` takes raw 16 kHz int16 mono bytes. If whisper is selected but not
installed / not loadable, it says so once and quietly falls back to Google for that session.

Speed: on a machine with an NVIDIA GPU (and the CUDA libraries that ctranslate2 needs) whisper runs
there in float16 -- several times faster than the CPU. Otherwise it uses int8 on the CPU, with more
threads than the library default. The wake-word loop transcribes *everything it hears* and only needs
to spot one phrase, so it can use a smaller, quicker model (`wake_whisper_model`) than your commands.
"""

from __future__ import annotations

import os
import re
import threading

import numpy as np

import app_paths
import neon_log

log = neon_log.get("stt")
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
MIN_SECONDS = 0.3                       # anything shorter is a click or a cough, not speech
MODEL_MB = {"tiny.en": 75, "base.en": 145, "small.en": 480, "tiny": 75, "base": 145, "small": 480}
# What Whisper tends to "hear" in near-silence or steady noise. Dropped when it is the whole result.
_HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "the end",
                   "so", "okay", "oh", "uh", "um", "hmm", "mm", "mhm"}


NOISE_FRAME = 320                  # 20 ms frames for the steadiness check
STEADY_NOISE_CV = 0.3              # speech's loudness jumps around far more than a fan's does
NO_SPEECH_PROB = 0.6               # Whisper's own no-speech rule (with a low average log-probability)
NO_SPEECH_LOGPROB = -1.0


def looks_like_steady_noise(pcm: bytes) -> bool:
    """True for a stretch of audio whose loudness barely changes (a fan, a hum, rain, a hairdryer).
    Speech rises and falls with every syllable; measured by the spread of 20 ms frame energies
    (coefficient of variation). Used by the wake loop to skip transcribing things that can't be words."""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    count = len(audio) // NOISE_FRAME
    if count < 10:
        return False
    energy = np.sqrt((audio[:count * NOISE_FRAME].reshape(count, NOISE_FRAME) ** 2).mean(axis=1))
    mean = float(energy.mean())
    return mean > 0 and float(energy.std()) / mean < STEADY_NOISE_CV


def _is_speech(segment) -> bool:
    """Whisper's own filter: a segment it thinks is probably silence *and* isn't confident about."""
    no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    return not (no_speech > NO_SPEECH_PROB and logprob < NO_SPEECH_LOGPROB)


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def whisper_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def cuda_available() -> bool:
    """True if ctranslate2 (which faster-whisper runs on) can see an NVIDIA GPU."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 -- not installed, no driver, an old build: simply "no"
        return False


def pick_device(wanted: str = "auto", cuda: bool | None = None) -> tuple[str, str]:
    """(device, compute_type) for a `whisper_device` setting of auto / cpu / cuda."""
    wanted = str(wanted or "auto").strip().lower()
    use_gpu = wanted in ("auto", "cuda") and (cuda_available() if cuda is None else cuda)
    return ("cuda", "float16") if use_gpu else ("cpu", "int8")


def cpu_threads(configured: int = 0, cores: int | None = None) -> int:
    """Worker threads for CPU decoding: what was asked for, else half the logical cores (4 to 8)."""
    if configured and configured > 0:
        return int(configured)
    cores = cores or os.cpu_count() or 4
    return max(4, min(8, cores // 2))


class Transcriber:
    def __init__(self, config: dict, on_status=None):
        self._cfg = config
        self._on_status = on_status or (lambda message: None)
        self._recognizer = None
        self._models: dict[tuple, object] = {}       # (model name, device) -> loaded model
        self._cpu_only = False                       # the GPU failed to load once: stay on the CPU
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()   # one whisper call at a time (a live partial vs the final result)
        self._broken = False                # whisper failed to load: use Google for the rest of the session

    @property
    def engine(self) -> str:
        wanted = str(self._cfg.get("stt_engine", "whisper")).strip().lower()
        return "google" if (wanted == "whisper" and self._broken and self._online_fallback) else wanted

    @property
    def _online_fallback(self) -> bool:
        """Setting "stt_online_fallback": may Google stand in when the offline engine can't run?"""
        value = self._cfg.get("stt_online_fallback", False)
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)

    def _instead(self) -> str:
        return ("using Google instead." if self._online_fallback else
                "so I can't understand speech until it's fixed (or pick Google in Settings > Listening).")

    def _model_name(self, role: str | None = None) -> str:
        """The model for commands, or (role="wake") the quicker one for the wake loop, if one is set."""
        main = str(self._cfg.get("whisper_model", "base.en")).strip() or "base.en"
        if role == "wake":
            return str(self._cfg.get("wake_whisper_model", "")).strip() or main
        return main

    def prepare(self) -> None:
        """Load the offline model(s) in the background so the first command doesn't wait for them."""
        if self.engine == "whisper":
            threading.Thread(target=self._prepare_all, name="Nova-STT-load", daemon=True).start()

    def _prepare_all(self) -> None:
        self._load_whisper(self._model_name())
        if str(self._cfg.get("wake_engine", "stt")) == "stt" and self._model_name("wake") != self._model_name():
            self._load_whisper(self._model_name("wake"))

    # ---- whisper -----------------------------------------------------------------------------
    def _load_whisper(self, name: str | None = None):
        name = name or self._model_name()
        with self._load_lock:
            device, compute = ("cpu", "int8") if self._cpu_only else pick_device(self._cfg.get("whisper_device", "auto"))
            model = self._models.get((name, device))
            if model is not None:
                return model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                self._fail(f"The offline speech engine isn't installed (pip install faster-whisper), "
                           f"{self._instead()}", exc)
                return None
            size = MODEL_MB.get(name)
            where = " on the GPU" if device == "cuda" else ""
            self._on_status(f"Loading the {name} speech model{where}"
                            + (f" (first use downloads about {size} MB)" if size else "") + "...")
            root = str(app_paths.data_path("whisper"))
            threads = cpu_threads(_int(self._cfg.get("whisper_threads", 0)))
            model = None
            try:
                model = WhisperModel(name, device=device, compute_type=compute, download_root=root, cpu_threads=threads)
            except Exception as exc:  # noqa: BLE001 -- offline first run, disk full, unknown model name, no CUDA libraries...
                if device != "cuda":
                    self._fail(f"Couldn't load the offline speech model ({exc}), {self._instead()}", exc)
                    return None
                log.warning("whisper on the GPU failed (%s); using the CPU", exc)
                self._cpu_only = True
                self._on_status("The GPU isn't usable for speech recognition; using the CPU.")
            if model is None:                            # the GPU route failed: carry on with the CPU
                device = "cpu"
                try:
                    model = WhisperModel(name, device="cpu", compute_type="int8", download_root=root, cpu_threads=threads)
                except Exception as exc:  # noqa: BLE001
                    self._fail(f"Couldn't load the offline speech model ({exc}), {self._instead()}", exc)
                    return None
            self._models[(name, device)] = model
            self._on_status("Speech model ready.")
            return model

    def _fail(self, message: str, exc: Exception) -> None:
        log.warning("%s (%s)", message, exc)
        if not self._broken:                         # said once, not on every utterance
            self._on_status(message)
        self._broken = True

    def _whisper_text(self, pcm: bytes, role: str | None = None) -> str | None:
        name = self._model_name(role)
        if self._broken and not self._online_fallback:
            return None                              # already said why; audio never goes online
        model = self._load_whisper(name)
        if model is None:
            return self._google_text(pcm) if self._online_fallback else None
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio) < MIN_SECONDS * SAMPLE_RATE:
            return None
        hint = ", ".join(w for w in (str(self._cfg.get("assistant_name", "")), str(self._cfg.get("wake_word", ""))) if w)
        with self._run_lock:
            segments, _info = model.transcribe(
                audio, language="en" if name.endswith(".en") else None, beam_size=1, temperature=0.0,
                condition_on_previous_text=False, vad_filter=False, without_timestamps=True,
                initial_prompt=hint or None)         # biases it toward your assistant's (unusual) name
            kept = [seg for seg in segments if role != "wake" or _is_speech(seg)]
            text = " ".join(seg.text.strip() for seg in kept).strip()
        if not re.search(r"\w", text) or re.sub(r"[^\w ]", "", text.lower()).strip() in _HALLUCINATIONS:
            return None
        return text

    # ---- google ---------------------------------------------------------------------------------
    def _google_text(self, pcm: bytes) -> str | None:
        try:
            import speech_recognition as sr
        except ImportError:
            log.error("SpeechRecognition isn't installed (pip install SpeechRecognition)")
            return None
        if self._recognizer is None:
            self._recognizer = sr.Recognizer()
        try:
            return self._recognizer.recognize_google(sr.AudioData(pcm, SAMPLE_RATE, SAMPLE_WIDTH))
        except (sr.UnknownValueError, sr.RequestError) as exc:
            if isinstance(exc, sr.RequestError):
                log.warning("Google speech recognition unavailable: %s", exc)
            return None

    # ---- public ----------------------------------------------------------------------------------
    def transcribe(self, pcm: bytes, role: str | None = None) -> str | None:
        """The text in `pcm` (16 kHz int16 mono), or None if nothing intelligible was said.
        role="wake" is the always-listening loop, which may use a smaller (faster) model and skips
        steady noise without transcribing it at all (setting "wake_skip_noise")."""
        skip = self._cfg.get("wake_skip_noise", True)
        skip = skip.strip().lower() in ("1", "true", "yes", "on") if isinstance(skip, str) else bool(skip)
        if role == "wake" and skip and looks_like_steady_noise(pcm):
            return None
        if self.engine == "whisper":
            try:
                return self._whisper_text(pcm, role)
            except Exception as exc:  # noqa: BLE001
                self._fail(f"The offline speech engine failed ({exc}), {self._instead()}", exc)
                if not self._online_fallback:
                    return None
        return self._google_text(pcm)
