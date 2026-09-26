"""
listening.py -- microphone capture, phrase endpointing, and the Listener the controller drives.

  MicStream          ONE long-lived input stream. A callback thread queues 100 ms chunks and readers
                     pull them, so nothing is lost between two listens (the first syllable after a
                     wake phrase used to fall in that gap). It closes itself after a few idle seconds
                     so the microphone (and Windows' "in use" indicator) isn't held for nothing.
  record_utterance   waits for speech, keeps a short pre-roll so the onset isn't clipped, and stops
                     after a pause -- or, for hold-to-talk, when the key is released. "Speech" is decided
                     by a voice-activity model (vad.py) when it's available, else by loudness.
  Listener           the API used by controller.py: listen_once / listen_hold / wake loop / pause /
                     mute, with a noise threshold that re-measures the room as it goes. The wake loop
                     either transcribes what it hears and checks for the wake word ("stt"), or feeds the
                     audio to an openWakeWord detector (wakeword.py) and only transcribes commands.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

import neon_log
import stt
import vad
import wakeword

log = neon_log.get("listening")

SAMPLE_RATE = stt.SAMPLE_RATE      # Hz
CHUNK_MS = 100                     # size of each queued audio chunk
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000
PRE_ROLL_CHUNKS = 4                # 400 ms kept from before the threshold was crossed
BACKLOG_CHUNKS = 40                # audio older than 4 s is never worth reading
IDLE_CLOSE_SECONDS = 6.0           # release the microphone after this long without a read
PARTIAL_EVERY = 8                  # chunks (0.8 s) between live-caption transcriptions
MIN_HOLD_SECONDS = 0.3
_CFG = object()                    # sentinel: "use the configured value" for listen_once() arguments


class MicStream:
    """A long-lived microphone stream that queues fixed-size int16 chunks."""

    def __init__(self, device=None, factory=None):
        self.device = device or None
        self._factory = factory or self._open_real
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=300)
        self._stream = None
        self._lock = threading.Lock()
        self._last_read = time.monotonic()

    def _open_real(self, callback):
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES,
                                device=self.device, callback=callback)
        stream.start()
        return stream

    def _callback(self, indata, _frames, _time, _status) -> None:
        chunk = np.array(indata[:, 0], dtype=np.int16, copy=True)
        try:
            self._q.put_nowait(chunk)
        except queue.Full:                      # nobody is reading: drop the oldest, keep the newest
            try:
                self._q.get_nowait()
                self._q.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _ensure_open(self) -> None:
        with self._lock:
            if self._stream is None:
                self._stream = self._factory(self._callback)
                threading.Thread(target=self._watch, name="Nova-MicIdle", daemon=True).start()

    def _watch(self) -> None:
        """Closes the stream once nothing has read from it for IDLE_CLOSE_SECONDS."""
        while True:
            time.sleep(0.5)
            with self._lock:
                if self._stream is None:
                    return
                if time.monotonic() - self._last_read > IDLE_CLOSE_SECONDS:
                    self._close_locked()
                    return

    def read(self, timeout: float = 1.0):
        """The next 100 ms chunk, or None if none arrived within `timeout`."""
        self._ensure_open()
        self._last_read = time.monotonic()
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def trim(self, keep: int = BACKLOG_CHUNKS) -> None:
        while self._q.qsize() > keep:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def flush(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _close_locked(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            for step in ("stop", "close"):
                try:
                    getattr(stream, step)()
                except Exception:  # noqa: BLE001
                    pass
        self.flush()

    def close(self) -> None:
        with self._lock:
            self._close_locked()


def record_utterance(read_chunk, threshold, timeout, phrase_time_limit, silence_duration: float = 1.0,
                     abort=None, fast_endpoint: bool = False, on_idle=None, on_progress=None,
                     hold: "threading.Event | None" = None, voice=None) -> bytes | None:
    """Waits (up to `timeout` seconds, or forever if None) for audio above `threshold()`, then
    records until `silence_duration` seconds of quiet follow or `phrase_time_limit` is reached.
    Returns raw int16 PCM bytes, or None if nothing was said in time.

    hold: an Event for hold-to-talk. Recording starts at once and ends when it is set (no silence
    endpoint); `None` is returned if the key was let go in under MIN_HOLD_SECONDS.
    voice: optional callable(chunk, energy, started) -> bool that decides "is this speech" instead of
    comparing the loudness with `threshold()` (the voice-activity model)."""
    if voice is None:
        voice = lambda _data, level, _started: level > threshold()      # noqa: E731
    max_wait_chunks = None if timeout is None else max(1, int(timeout * 1000 / CHUNK_MS))
    max_total_chunks = max(1, int((phrase_time_limit or 12) * 1000 / CHUNK_MS))
    max_silence_chunks = max(1, int(silence_duration * 1000 / CHUNK_MS))

    frames: list[np.ndarray] = []
    pre: deque = deque(maxlen=PRE_ROLL_CHUNKS)
    started = hold is not None
    voiced_chunks = 1 if started else 0
    waited_chunks = silence_chunks = total_chunks = stalled = 0

    while True:
        if abort is not None and abort():            # muted / closing: stop, releasing the microphone
            return None
        data = read_chunk()
        if data is None:                             # the stream produced nothing for a second
            if hold is not None and hold.is_set():
                break                                # key released while the stream hiccuped: finish now
            stalled += 1
            if stalled >= 5:
                return None                          # device unplugged or wedged
            continue
        stalled = 0
        energy = float(np.abs(data).mean())

        if hold is not None:
            frames.append(data)
            total_chunks += 1
            if on_progress is not None and total_chunks % PARTIAL_EVERY == 0:
                on_progress(frames)
            if hold.is_set() or total_chunks >= max_total_chunks:
                break
            continue

        if not started:
            if voice(data, energy, False):
                started = True
                voiced_chunks = 1
                frames = list(pre) + [data]          # include the moments *before* it crossed the threshold
                total_chunks = len(frames) - 1       # ...and count them, so phrase_time_limit stays exact
                pre.clear()
            else:
                if on_idle is not None:
                    on_idle(energy)
                pre.append(data)
                waited_chunks += 1
                if max_wait_chunks is not None and waited_chunks >= max_wait_chunks:
                    return None
                continue
        else:
            frames.append(data)
            if not voice(data, energy, True):
                silence_chunks += 1
                needed = max_silence_chunks
                # A brief command ("hello", "open chrome") is over sooner than a long one with
                # thinking pauses, so it doesn't need the full silence wait.
                if fast_endpoint and voiced_chunks * CHUNK_MS < 2000:
                    needed = max(3, int(max_silence_chunks * 0.6))
                if silence_chunks >= needed:
                    break
            else:
                silence_chunks = 0
                voiced_chunks += 1
            if on_progress is not None and total_chunks % PARTIAL_EVERY == PARTIAL_EVERY - 1:
                on_progress(frames)

        total_chunks += 1
        if total_chunks >= max_total_chunks:
            break

    if not frames:
        return None
    if hold is not None and len(frames) * CHUNK_MS < MIN_HOLD_SECONDS * 1000:
        return None
    return np.concatenate(frames, axis=0).tobytes()


def _num(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _flag(cfg: dict, key: str, default: bool = False) -> bool:
    value = cfg.get(key, default)
    return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)


class Listener:
    """`config` is the live CONFIG dict; settings are read on every call."""

    def __init__(self, config: dict, on_status=None, on_partial=None, mic_factory=None,
                 initial_noise: float | None = None, on_message=None):
        self._cfg = config
        self._on_status = on_status or (lambda *_: None)      # state kinds: listening / thinking / idle
        self._on_message = on_message or (lambda _text: None)  # human-readable notes ("Loading the speech model...")
        self._on_partial = on_partial or (lambda _text: None)
        self.on_listen = None        # called as a listen for a *command* starts (the "I'm listening" sound)
        self.available = True
        self.device = config.get("mic_device") or None
        self._mic = MicStream(self.device, mic_factory)
        self.transcriber = stt.Transcriber(config, on_status=lambda message: self._on_message(message))
        self._noise = 0.0
        self._floor = 150.0
        self.energy_threshold = 0.0
        self._init_error = ""
        try:
            if initial_noise is None:
                sd.check_input_settings(device=self.device, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
                initial_noise = self._measure_noise()
            self._noise = initial_noise
            self._recompute_threshold()
            self._ceiling = max(self.energy_threshold * 4.0, self._floor * 4.0)
        except Exception as exc:  # noqa: BLE001 -- no mic / no PortAudio device: degrade gracefully
            self.available = False
            self._init_error = str(exc)
            log.warning("microphone unavailable: %s", exc)
        self._stop_wake = threading.Event()
        self._wake_thread: threading.Thread | None = None
        self._detector_factory = wakeword.Detector        # replaceable in tests
        # While set, nothing is transcribed (the assistant is talking and would hear herself).
        self.paused = threading.Event()
        self._pause_count = 0
        self._stale = False               # audio queued while paused must be dropped before the next listen
        # User-controlled mute (hotkey / button): no input at all -- not commands, not the wake word.
        self.muted = threading.Event()
        self._closing = threading.Event()  # set on app shutdown: stop recording, release the mic
        self._vad = vad.SpeechDetector()
        self.wake_gain = 1.0              # how much a quiet mic is raised for the wake-word detector (learned)

    # ---- noise level -----------------------------------------------------------------------------
    def _measure_noise(self, seconds: float = 0.6) -> float:
        """The mean loudness of the room, from a short burst of the real (persistent) stream."""
        levels = []
        for _ in range(max(2, int(seconds * 1000 / CHUNK_MS))):
            chunk = self._mic.read(timeout=1.0)
            if chunk is not None:
                levels.append(float(np.abs(chunk).mean()))
        self._mic.flush()
        return float(np.mean(levels)) if levels else self._floor

    def _recompute_threshold(self) -> None:
        self.energy_threshold = max(self._noise * _num(self._cfg, "energy_multiplier", 3.0), self._floor)

    def _note_idle_energy(self, energy: float) -> None:
        """Called with the loudness of every quiet chunk while waiting: a slow moving average, so a
        fan switching on (or moving to a louder room) doesn't leave the sensitivity wrong until restart."""
        if not _flag(self._cfg, "adaptive_threshold", True):
            return
        self._noise = 0.985 * self._noise + 0.015 * min(energy, self._noise * 3.0 + 50.0)
        self._recompute_threshold()
        self.energy_threshold = min(self.energy_threshold, self._ceiling)

    # ---- lifecycle --------------------------------------------------------------------------------
    def close(self) -> None:
        """Stops the wake loop and any recording in progress, promptly."""
        self._closing.set()
        self._stop_wake.set()
        thread = self._wake_thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=1.5)
        self._mic.close()

    def mute(self) -> None:
        self.muted.set()
        self._mic.close()                 # muted means the microphone is really released

    def unmute(self) -> None:
        self.muted.clear()

    def pause(self) -> None:
        self._pause_count += 1
        self._stale = True
        self.paused.set()

    def resume(self, delay: float = 0.0) -> None:
        """Un-mute, after `delay` seconds so the tail of the assistant's voice has died away."""
        if delay > 0:
            timer = threading.Timer(delay, self.paused.clear)
            timer.daemon = True          # never hold the process open on the way out
            timer.start()
        else:
            self.paused.clear()

    # ---- listening ----------------------------------------------------------------------------------
    def _prepare_read(self) -> None:
        if self._stale:                   # what was queued while the assistant talked is her own voice
            self._mic.flush()
            self._stale = False
        else:
            self._mic.trim()              # but keep the recent past: it may hold the start of a phrase

    def _capture(self, timeout, phrase_time_limit, partials: bool, hold=None, wake: bool = False) -> bytes | None:
        self._prepare_read()
        use_partials = partials and _flag(self._cfg, "stt_partials", True) and self.transcriber.engine == "whisper"
        state = {"busy": False, "done": False}

        def on_progress(frames) -> None:
            if not use_partials or state["busy"] or state["done"]:
                return
            state["busy"] = True
            pcm = np.concatenate(frames, axis=0).tobytes()

            def work() -> None:
                try:
                    text = self.transcriber.transcribe(pcm)
                    if text and not state["done"]:
                        self._on_partial(text)
                except Exception:  # noqa: BLE001 -- a live caption is a nicety; never break listening
                    pass
                finally:
                    state["busy"] = False
            threading.Thread(target=work, name="Nova-STT-partial", daemon=True).start()

        try:
            raw = record_utterance(
                lambda: self._mic.read(1.0), lambda: self.energy_threshold, timeout, phrase_time_limit,
                silence_duration=_num(self._cfg, "silence_duration", 1.0),
                abort=lambda: self.muted.is_set() or self._closing.is_set() or (wake and self._stop_wake.is_set()),
                fast_endpoint=_flag(self._cfg, "fast_endpoint", True), on_idle=self._note_idle_energy,
                on_progress=on_progress, hold=hold, voice=self._voice_test())
        finally:
            state["done"] = True
        return raw

    def _voice_test(self):
        """The voice-activity model as record_utterance's speech test (Settings > Listening > "Tell my voice
        apart from background sound"), or None for plain loudness. Starting takes a confident chunk,
        continuing a weaker one, so a word's soft ending doesn't count as the pause."""
        if not _flag(self._cfg, "vad_enabled", True) or not vad.available():
            return None
        self._vad.reset()
        broken = {"yes": False}

        def is_voice(data, energy, started) -> bool:
            if not broken["yes"]:
                try:
                    return self._vad.probability(data) >= (vad.KEEP_PROB if started else vad.START_PROB)
                except Exception as exc:  # noqa: BLE001 -- fall back to loudness for the rest of this phrase
                    broken["yes"] = True
                    log.warning("voice activity model failed: %s", exc)
            return energy > self.energy_threshold
        return is_voice

    def _finish(self, raw: bytes | None, pauses_before: int, role: str | None = None) -> str | None:
        if raw is None:
            return None
        if self.muted.is_set():
            return None                   # muted while this was being captured -- drop it
        if self.paused.is_set() or self._pause_count != pauses_before:
            return None                   # captured while the assistant was speaking -- that was it, not the user
        self._on_status("thinking")
        if _flag(self._cfg, "mic_auto_gain", True):
            raw, gain = vad.normalize_pcm(raw)
            # The wake-word detector hears the same quiet mic: raise it by about as much (smoothed, capped).
            self.wake_gain = min(8.0, max(1.0, 0.7 * self.wake_gain + 0.3 * gain))
            if gain > 1.0:
                log.info("a quiet recording (%.1f s) was raised %.1fx before transcribing",
                         len(raw) / 2 / SAMPLE_RATE, gain)
        return self.transcriber.transcribe(raw, role=role)

    def _announce_listen(self) -> None:
        hook = self.on_listen
        if hook is not None:
            try:
                hook()
            except Exception as exc:  # noqa: BLE001 -- a sound must never stop the listening
                log.warning("listen sound failed: %s", exc)

    def listen_once(self, timeout=_CFG, phrase_time_limit=_CFG, partials: bool = True,
                    wake: bool = False) -> str | None:
        """timeout / phrase_time_limit default to the configured values; pass None for 'no limit'
        on the timeout. `partials` allows live captions while you speak (offline engine only).
        `wake` marks the always-on wake loop: it stops promptly when the loop is stopped, and may use
        the smaller speech model chosen for it."""
        if not self.available or self.muted.is_set() or self._closing.is_set():
            return None
        if timeout is _CFG:
            timeout = _num(self._cfg, "listen_timeout", 6.0)
        if phrase_time_limit is _CFG:
            phrase_time_limit = _num(self._cfg, "phrase_time_limit", 12.0)
        pauses_before = self._pause_count
        self._on_status("listening")
        if not wake:
            self._announce_listen()
        try:
            raw = self._capture(timeout, phrase_time_limit, partials, wake=wake)
        except Exception as exc:  # noqa: BLE001 -- device dropped mid-recording, etc.
            log.warning("recording failed: %s", exc)
            raw = None
        finally:
            self._on_status("idle")
        return self._finish(raw, pauses_before, "wake" if wake else None)

    def listen_hold(self, release: threading.Event) -> str | None:
        """Hold-to-talk: record from now until `release` is set (the key was let go), then transcribe."""
        if not self.available or self.muted.is_set() or self._closing.is_set():
            return None
        pauses_before = self._pause_count
        self._on_status("listening")
        self._announce_listen()
        try:
            raw = self._capture(None, _num(self._cfg, "phrase_time_limit", 12.0) * 4, True, hold=release)
        except Exception as exc:  # noqa: BLE001
            log.warning("hold-to-talk recording failed: %s", exc)
            raw = None
        finally:
            self._on_status("idle")
        return self._finish(raw, pauses_before)

    def start_wake_loop(self, get_wake_word, on_command) -> None:
        if not self.available or (self._wake_thread and self._wake_thread.is_alive()):
            return
        self._stop_wake.clear()

        def loop():
            detector = self._open_detector() if wakeword.use_detector(self._cfg) else None
            if detector is not None:
                self._detector_loop(detector, get_wake_word, on_command)
            else:
                self._speech_loop(get_wake_word, on_command)

        self._wake_thread = threading.Thread(target=loop, name="Nova-WakeLoop", daemon=True)
        self._wake_thread.start()

    def _speech_loop(self, get_wake_word, on_command) -> None:
        """Wake word by speech recognition: transcribe every phrase heard, act on the ones that start with it."""
        while not self._stop_wake.is_set():
            if self.paused.is_set() or self.muted.is_set():
                time.sleep(0.05)
                continue
            text = self.listen_once(timeout=None, phrase_time_limit=_num(self._cfg, "wake_phrase_limit", 8.0),
                                    partials=False, wake=True)
            if not text or self._stop_wake.is_set():
                continue
            wake = get_wake_word().lower().strip()
            lower = text.lower().strip()
            if not lower.startswith(wake):
                continue
            remainder = lower[len(wake):].strip(" ,.")
            if remainder:
                on_command(remainder)
            else:
                follow = self.listen_once()
                if follow:
                    on_command(follow)

    def _open_detector(self):
        """A loaded openWakeWord detector, or None (after saying why) so the speech loop takes over."""
        try:
            detector = self._detector_factory(
                str(self._cfg.get("wake_model", wakeword.DEFAULT_PHRASE)),
                lambda: _num(self._cfg, "wake_threshold", 0.5), self._on_message)
            detector.load()
            return detector
        except ImportError as exc:
            log.warning("openWakeWord isn't installed: %s", exc)
            self._on_message("openWakeWord isn't installed (pip install openwakeword); listening for the wake "
                             "word with speech recognition instead.")
        except Exception as exc:  # noqa: BLE001 -- offline first run, damaged model file, onnxruntime trouble...
            log.warning("wake-word detector failed to load: %s", exc)
            self._on_message(f"Couldn't start the wake-word detector ({exc}); listening for the wake word "
                             "with speech recognition instead.")
        return None

    def _detector_loop(self, detector, get_wake_word, on_command) -> None:
        """Wake word by openWakeWord: audio goes to the detector; only what follows the phrase is transcribed."""
        phrase = wakeword.phrase_words(self._cfg)
        was_paused = False
        while not self._stop_wake.is_set():
            if self.paused.is_set() or self.muted.is_set():
                was_paused = True
                time.sleep(0.05)
                continue
            if was_paused:                                  # what was queued meanwhile is the assistant's own voice
                self._mic.flush()
                detector.reset()
                was_paused = False
            chunk = self._mic.read(1.0)
            if chunk is None:
                continue
            if float(np.abs(chunk).mean()) <= self.energy_threshold:
                self._note_idle_energy(float(np.abs(chunk).mean()))
            if self.wake_gain > 1.05 and _flag(self._cfg, "mic_auto_gain", True):
                chunk = np.clip(chunk.astype(np.float32) * self.wake_gain, -32768, 32767).astype(np.int16)
            try:
                heard = detector.feed(chunk)
            except Exception as exc:  # noqa: BLE001 -- the detector broke: don't leave the assistant deaf
                log.warning("wake-word detector failed: %s", exc)
                self._on_message("The wake-word detector stopped; listening with speech recognition instead.")
                self._speech_loop(get_wake_word, on_command)
                return
            if not heard:
                continue
            self._mic.flush()                               # the phrase itself is not part of the command
            follow = self.listen_once()
            if follow:
                command = wakeword.strip_phrase(follow, phrase)
                if command:
                    on_command(command)
            self._prepare_read()                            # drop what piled up while the reply played
            detector.reset()

    def stop_wake_loop(self, wait: bool = False) -> None:
        self._stop_wake.set()
        thread = self._wake_thread
        if wait and thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=3.0)
