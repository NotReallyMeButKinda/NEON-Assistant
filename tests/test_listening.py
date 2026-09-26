import threading
import time
import unittest

import numpy as np

import listening as L
from tests import common

QT = common.APP          # importing tests.common also sets up Qt and the import paths
CH = L.CHUNK_SAMPLES


def chunk(level):
    return np.full(CH, level, dtype=np.int16)


class FakeStream:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def close(self):
        pass


class Bench:
    """A MicStream over a fake device that the test feeds by hand."""

    def __init__(self):
        self.made: list[FakeStream] = []
        self.mic = L.MicStream(factory=self._make)

    def _make(self, _callback):
        stream = FakeStream()
        self.made.append(stream)
        return stream

    def feed(self, *levels):
        self.mic._ensure_open()
        for level in levels:
            self.mic._callback(chunk(level).reshape(-1, 1), CH, None, None)


class RecordUtteranceTests(unittest.TestCase):
    def record(self, levels, threshold=500, **kw):
        it = iter(chunk(v) for v in levels)
        kw.setdefault("timeout", None)
        kw.setdefault("phrase_time_limit", 12)
        return L.record_utterance(lambda: next(it), lambda: threshold, **kw)

    def test_pre_roll_keeps_the_onset(self):
        raw = self.record([100] * 6 + [900] * 5 + [100] * 20)
        levels = [int(f[0]) for f in np.frombuffer(raw, dtype=np.int16).reshape(-1, CH)]
        self.assertEqual(levels[:L.PRE_ROLL_CHUNKS], [100] * L.PRE_ROLL_CHUNKS)      # the moments before speech
        self.assertEqual(levels[L.PRE_ROLL_CHUNKS], 900)

    def test_timeout_and_abort_and_dead_stream(self):
        self.assertIsNone(self.record([100] * 100, timeout=1.0))
        self.assertIsNone(L.record_utterance(lambda: chunk(900), lambda: 500, None, 12, abort=lambda: True))
        self.assertIsNone(L.record_utterance(lambda: None, lambda: 500, None, 12))

    def test_phrase_limit_counts_the_pre_roll(self):
        raw = self.record([100] * 3 + [900] * 200, phrase_time_limit=2.0)
        self.assertEqual(len(np.frombuffer(raw, dtype=np.int16)) // CH, 20)

    def test_fast_endpoint_ends_brief_commands_sooner(self):
        levels = [900] * 5 + [100] * 40
        fast = self.record(levels, fast_endpoint=True)
        slow = self.record(levels, fast_endpoint=False)
        self.assertLess(len(fast), len(slow))

    def test_hold_to_talk(self):
        release, n = threading.Event(), {"i": 0}

        def reader():
            n["i"] += 1
            if n["i"] == 6:
                release.set()
            return chunk(50)                                   # quiet: hold mode ignores the threshold
        raw = L.record_utterance(reader, lambda: 500, None, 30, hold=release)
        self.assertEqual(len(np.frombuffer(raw, dtype=np.int16)) // CH, 6)
        tap = threading.Event()
        tap.set()
        self.assertIsNone(L.record_utterance(lambda: chunk(50), lambda: 500, None, 30, hold=tap))   # too short

    def test_hold_ends_immediately_if_the_stream_stalls_after_release(self):
        release, n = threading.Event(), {"i": 0}

        def reader():
            n["i"] += 1
            if n["i"] <= 5:
                return chunk(900)
            release.set()
            return None
        self.assertIsNotNone(L.record_utterance(reader, lambda: 500, None, 30, hold=release))


class MicStreamTests(unittest.TestCase):
    def test_queue_order_trim_flush_and_bound(self):
        b = Bench()
        b.feed(1, 2, 3)
        self.assertEqual([int(b.mic.read(0.1)[0]) for _ in range(3)], [1, 2, 3])
        self.assertIsNone(b.mic.read(0.05))
        b.feed(*range(10, 60))
        b.mic.trim(10)
        self.assertEqual(int(b.mic.read(0.1)[0]), 50)                    # the newest ten survive
        b.mic.flush()
        self.assertEqual(b.mic._q.qsize(), 0)
        b.feed(*([1] * 400))
        self.assertLessEqual(b.mic._q.qsize(), 300)

    def test_closes_itself_when_idle_and_reopens(self):
        old = L.IDLE_CLOSE_SECONDS
        L.IDLE_CLOSE_SECONDS = 0.8
        try:
            b = Bench()
            b.feed(1)
            b.mic.read(0.05)
            time.sleep(1.8)
            self.assertFalse(b.mic.active)
            self.assertTrue(b.made[0].stopped)
            b.feed(5)
            self.assertTrue(b.mic.active)
            self.assertEqual(len(b.made), 2)
        finally:
            L.IDLE_CLOSE_SECONDS = old


class StubTranscriber:
    engine = "google"

    def __init__(self):
        self.roles = []

    def transcribe(self, pcm, role=None):
        self.roles.append(role)
        return f"heard {len(pcm) // 2 // CH} chunks"

    def prepare(self):
        pass


class ListenerTests(unittest.TestCase):
    def make(self, cfg=None, noise=100.0):
        base = {"energy_multiplier": 3.0, "silence_duration": 1.0, "fast_endpoint": False, "listen_timeout": 6.0,
                "phrase_time_limit": 12.0, "adaptive_threshold": True, "stt_partials": True,
                "vad_enabled": False, "mic_auto_gain": False, **(cfg or {})}      # made-up audio: loudness decides
        bench = Bench()
        listener = L.Listener(base, mic_factory=bench._make, initial_noise=noise)
        bench.mic = listener._mic
        listener.transcriber = StubTranscriber()
        return listener, bench

    def test_listen_once_and_speech_already_queued(self):
        listener, bench = self.make()
        self.assertEqual(listener.energy_threshold, 300.0)
        bench.feed(*([60] * 3 + [900] * 4 + [60] * 15))
        self.assertTrue(listener.listen_once().startswith("heard"))
        bench.mic.flush()
        bench.feed(*([900] * 4 + [60] * 15))                            # speech waiting before listening starts
        self.assertTrue(listener.listen_once().startswith("heard"))

    def test_threshold_adapts_to_a_noisier_room(self):
        listener, bench = self.make()
        before = listener.energy_threshold
        bench.feed(*([250] * 400))
        listener.listen_once(timeout=30.0)
        self.assertGreater(listener.energy_threshold, before)
        self.assertLessEqual(listener.energy_threshold, listener._ceiling)
        fixed, bench2 = self.make(cfg={"adaptive_threshold": False})
        before = fixed.energy_threshold
        bench2.feed(*([250] * 400))
        fixed.listen_once(timeout=30.0)
        self.assertEqual(fixed.energy_threshold, before)

    def test_audio_from_while_the_assistant_spoke_is_dropped(self):
        listener, bench = self.make()
        bench.feed(*([900] * 6 + [60] * 15))
        listener.pause()
        listener.resume()
        self.assertIsNone(listener.listen_once(timeout=0.5))

    def test_mute_releases_the_microphone(self):
        listener, bench = self.make()
        bench.feed(60)
        bench.mic.read(0.1)
        listener.mute()
        self.assertFalse(bench.mic.active)
        self.assertIsNone(listener.listen_once())

    def test_hold_to_talk_and_live_partials(self):
        listener, bench = self.make()
        release = threading.Event()
        bench.feed(*([900] * 5))
        threading.Timer(0.3, release.set).start()
        self.assertEqual(listener.listen_hold(release), "heard 5 chunks")
        listener, bench = self.make()
        listener.transcriber.engine = "whisper"
        parts = []
        listener._on_partial = parts.append
        bench.feed(*([900] * 30 + [60] * 15))
        self.assertIsNotNone(listener.listen_once())
        time.sleep(0.3)
        self.assertTrue(parts)
        listener, bench = self.make()
        listener.transcriber.engine = "whisper"
        parts = []
        listener._on_partial = parts.append
        bench.feed(*([900] * 30 + [60] * 15))
        listener.listen_once(partials=False)                             # the wake loop never shows captions
        self.assertEqual(parts, [])


class FakeDetector:
    """Stands in for wakeword.Detector: 'hears the phrase' whenever a chunk has level 777."""
    instances = []

    def __init__(self, model, threshold, on_status):
        self.model, self.threshold, self.resets, self.heard = model, threshold, 0, threading.Event()
        FakeDetector.instances.append(self)

    def load(self):
        pass

    def reset(self):
        self.resets += 1

    def feed(self, chunk):
        hit = int(chunk[0]) == 777
        if hit:
            self.heard.set()
        return hit


class WakeLoopTests(unittest.TestCase):
    def make(self, cfg=None):
        listener, bench = ListenerTests.make(self, {"wake_engine": "openwakeword", "wake_model": "hey_jarvis",
                                                    "wake_threshold": 0.6, "wake_phrase_limit": 8.0, **(cfg or {})})
        listener.messages = []
        listener._on_message = listener.messages.append
        FakeDetector.instances.clear()
        listener._detector_factory = FakeDetector
        return listener, bench

    def test_the_detector_hears_the_phrase_and_only_the_command_is_transcribed(self):
        listener, bench = self.make()
        commands = []
        listener.start_wake_loop(lambda: "hey nova", commands.append)
        bench.feed(*([60] * 5 + [777]))
        deadline = time.time() + 3
        while time.time() < deadline and not (FakeDetector.instances and FakeDetector.instances[0].heard.is_set()):
            time.sleep(0.02)
        detector = FakeDetector.instances[0]
        self.assertTrue(detector.heard.is_set())
        self.assertEqual((detector.model, detector.threshold()), ("hey_jarvis", 0.6))
        time.sleep(0.4)                                     # the loop drops the audio that held the phrase itself
        bench.feed(*([900] * 4 + [60] * 15))
        deadline = time.time() + 5
        while time.time() < deadline and not commands:
            time.sleep(0.02)
        listener.stop_wake_loop(wait=True)
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("heard"))
        self.assertEqual(listener.transcriber.roles, [None])   # the command uses the normal model, the wake phrase none

    def test_a_missing_detector_falls_back_to_speech_recognition_and_says_so(self):
        listener, bench = self.make({"wake_engine": "openwakeword"})

        def missing(*_args):
            raise ImportError("No module named 'openwakeword'")
        listener._detector_factory = missing
        commands = []
        listener.start_wake_loop(lambda: "heard", commands.append)
        bench.feed(*([900] * 4 + [60] * 15))
        deadline = time.time() + 5
        while time.time() < deadline and not commands:
            time.sleep(0.02)
        listener.stop_wake_loop(wait=True)
        self.assertTrue(any("pip install openwakeword" in m for m in listener.messages))
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].endswith(" chunks"))     # what followed "heard": the wake word was stripped
        self.assertEqual(listener.transcriber.roles[0], "wake")   # the fallback loop asks for the wake model

    def test_a_broken_detector_hands_over_to_speech_recognition(self):
        listener, bench = self.make()

        class Broken(FakeDetector):
            def feed(self, chunk):
                raise RuntimeError("onnx exploded")
        listener._detector_factory = Broken
        commands = []
        listener.start_wake_loop(lambda: "heard", commands.append)
        bench.feed(60)
        time.sleep(0.5)
        bench.feed(*([900] * 4 + [60] * 15))
        deadline = time.time() + 5
        while time.time() < deadline and not commands:
            time.sleep(0.02)
        listener.stop_wake_loop(wait=True)
        self.assertTrue(any("stopped" in m for m in listener.messages))
        self.assertTrue(commands)

    def test_stopping_the_loop_is_prompt_in_both_modes(self):
        for engine in ("stt", "openwakeword"):
            listener, _bench = self.make({"wake_engine": engine})
            listener.start_wake_loop(lambda: "hey nova", lambda _t: None)
            time.sleep(0.2)
            started = time.time()
            listener.stop_wake_loop(wait=True)
            self.assertLess(time.time() - started, 3.0, engine)
            self.assertFalse(listener._wake_thread.is_alive(), engine)


if __name__ == "__main__":
    unittest.main()
