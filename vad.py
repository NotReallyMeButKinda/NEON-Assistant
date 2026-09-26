"""
vad.py -- is someone talking? Silero VAD, the voice-activity model faster-whisper already ships
(faster_whisper/assets/silero_vad_v6.onnx), run live on the microphone with onnxruntime (already installed
for the wake word). Nothing is downloaded.

Loudness alone (the old test) can't tell a voice from music, a game or a fan, and it depends on how loud the
microphone is: a quiet virtual mic never crossed the threshold, and background music never let a phrase
end. The model gives a speech probability for every 32 ms, whatever the level.

  SpeechDetector.probability(chunk)  -> the highest speech probability in an int16 16 kHz chunk
  normalize_pcm(pcm)                 -> a quiet recording raised to a comfortable level for transcription
"""

from __future__ import annotations

import os
import threading

import numpy as np

import neon_log

log = neon_log.get("vad")

WINDOW = 512                     # samples per model step at 16 kHz (32 ms)
CONTEXT = 64                     # samples of the previous window the model sees with each one
START_PROB = 0.5                 # speech begins when a chunk reaches this...
KEEP_PROB = 0.3                  # ...and continues while chunks stay above this (hysteresis)

_SESSION: dict = {"session": None, "failed": False}
_SESSION_LOCK = threading.Lock()


def model_path() -> str:
    """The bundled model's path, or "" when faster-whisper (or the file) isn't there."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("faster_whisper")
    except (ImportError, ValueError):
        return ""
    if spec is None or not spec.origin:
        return ""
    folder = os.path.join(os.path.dirname(spec.origin), "assets")
    for name in ("silero_vad_v6.onnx", "silero_vad.onnx"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return ""


def _session():
    with _SESSION_LOCK:
        if _SESSION["session"] is None and not _SESSION["failed"]:
            path = model_path()
            try:
                import onnxruntime
                if not path:
                    raise FileNotFoundError("silero_vad_v6.onnx")
                opts = onnxruntime.SessionOptions()
                opts.inter_op_num_threads = opts.intra_op_num_threads = 1
                opts.log_severity_level = 4
                _SESSION["session"] = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"],
                                                                   sess_options=opts)
            except Exception as exc:  # noqa: BLE001 -- no model / no onnxruntime: loudness is used instead
                _SESSION["failed"] = True
                log.info("voice activity model unavailable (%s); using loudness", exc)
        return _SESSION["session"]


def available() -> bool:
    return _session() is not None


class SpeechDetector:
    """Streaming speech probability. Keeps the model's state and the last few samples between chunks, so
    it can be fed the microphone's 100 ms chunks as they come."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self.last = 0.0

    def probability(self, chunk: np.ndarray) -> float:
        """The highest speech probability among the complete 32 ms windows in `chunk` (int16, 16 kHz);
        leftover samples wait for the next chunk. Raises RuntimeError if the model isn't available."""
        session = _session()
        if session is None:
            raise RuntimeError("voice activity model unavailable")
        audio = np.concatenate([self._pending, np.asarray(chunk, dtype=np.float32).reshape(-1) / 32768.0])
        count = len(audio) // WINDOW
        self._pending = audio[count * WINDOW:]
        if count == 0:
            return self.last
        best = 0.0
        for i in range(count):                         # one window at a time: the state carries through
            window = audio[i * WINDOW:(i + 1) * WINDOW]
            frame = np.concatenate([self._context, window])[None, :]
            out, self._h, self._c = session.run(None, {"input": frame, "h": self._h, "c": self._c})
            self._context = window[-CONTEXT:]
            best = max(best, float(np.asarray(out).reshape(-1)[0]))
        self.last = best
        return best


TARGET_PEAK = 0.5 * 32767        # a recording is raised until its loudest moment reaches about half scale
MAX_GAIN = 16.0                  # ...but never by more than this (it would only raise the hiss)


def gain_for(peak: float) -> float:
    """How much to raise a recording whose loudest sample is `peak` (1.0 = leave it)."""
    if peak <= 0:
        return 1.0
    return float(min(MAX_GAIN, max(1.0, TARGET_PEAK / peak)))


def normalize_pcm(pcm: bytes) -> tuple[bytes, float]:
    """A quiet int16 recording raised to a comfortable level (loud ones are left alone): (pcm, gain used).
    Whisper hears a quiet virtual microphone much better at a normal level."""
    audio = np.frombuffer(pcm, dtype=np.int16)
    if audio.size == 0:
        return pcm, 1.0
    peak = float(np.percentile(np.abs(audio.astype(np.int32)), 99.9))   # a click shouldn't set the level
    gain = gain_for(peak)
    if gain <= 1.01:
        return pcm, 1.0
    raised = np.clip(audio.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
    return raised.tobytes(), gain
