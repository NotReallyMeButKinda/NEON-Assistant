"""The openWakeWord detector wrapper and the faster offline speech-recognition choices."""

import unittest

import numpy as np

import stt
import wakeword


class FakeModel:
    """Scores a frame by its first sample / 1000, so a test controls exactly which frames 'hit'."""

    def __init__(self):
        self.frames, self.resets = [], 0

    def predict(self, frame):
        self.frames.append(len(frame))
        return {"hey_jarvis_v0.1": float(frame[0]) / 1000.0, "other": 0.0}

    def reset(self):
        self.resets += 1


def detector(threshold=0.5):
    model = FakeModel()
    det = wakeword.Detector("hey_jarvis", threshold=lambda: threshold, loader=lambda: model)
    det.load()
    return det, model


class DetectorTests(unittest.TestCase):
    def test_audio_is_cut_into_80_ms_frames_whatever_the_chunk_size(self):
        det, model = detector()
        for size in (1600, 1600, 100, 3000):                 # 6300 samples: four full frames and a remainder
            det.feed(np.zeros(size, dtype=np.int16))
        self.assertEqual(model.frames, [wakeword.FRAME] * 4)

    def test_a_hit_needs_the_score_to_reach_the_threshold_and_resets_the_model(self):
        det, model = detector(0.5)
        self.assertFalse(det.feed(np.full(1280, 400, dtype=np.int16)))      # 0.4: too weak
        self.assertTrue(det.feed(np.full(1280, 600, dtype=np.int16)))       # 0.6: heard it
        self.assertEqual(model.resets, 1)                                    # so one phrase can't fire twice
        self.assertEqual(len(det._buf), 0)

    def test_the_threshold_is_read_on_every_call(self):
        level = {"t": 0.9}
        model = FakeModel()
        det = wakeword.Detector("hey_jarvis", threshold=lambda: level["t"], loader=lambda: model)
        det.load()
        self.assertFalse(det.feed(np.full(1280, 600, dtype=np.int16)))
        level["t"] = 0.5
        self.assertTrue(det.feed(np.full(1280, 600, dtype=np.int16)))

    def test_loading_errors_propagate_so_the_listener_can_fall_back(self):
        def broken():
            raise ImportError("no openwakeword")
        with self.assertRaises(ImportError):
            wakeword.Detector(loader=broken).load()

    def test_unknown_models_fall_back_to_the_default_phrase(self):
        self.assertEqual(wakeword.Detector("not-a-model", loader=FakeModel).name, wakeword.DEFAULT_PHRASE)


class TryItTests(unittest.TestCase):
    def test_the_last_score_is_the_best_frame_of_the_last_feed(self):
        det, _model = detector(0.9)
        det.feed(np.concatenate([np.full(1280, 300, dtype=np.int16), np.full(1280, 700, dtype=np.int16)]))
        self.assertAlmostEqual(det.last_score, 0.7)
        det.feed(np.full(1280, 100, dtype=np.int16))
        self.assertAlmostEqual(det.last_score, 0.1)

    def test_the_trainer_meter(self):
        import tempfile
        from pathlib import Path
        from tests import common
        from ui.wake_trainer import WakeTrainerDialog
        common.use_temp_config()
        saved = wakeword.models_dir
        folder = Path(tempfile.mkdtemp())
        wakeword.models_dir = lambda: folder
        try:
            dialog = WakeTrainerDialog(None, "hey tester")
            self.assertFalse(dialog.try_btn.isEnabled())                   # nothing trained yet
            self.assertEqual(dialog.try_label.text(), "Train it first")
            path = wakeword.custom_path("custom:hey_tester")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x")                                          # "trained" (never loaded here)
            dialog.phrase.setText("hey  tester")
            self.assertTrue(dialog.try_btn.isEnabled())
            import threading
            dialog._trying = threading.Event()                              # as if listening (no microphone)
            dialog._on_heard(0.42, False)
            self.assertEqual(dialog.meter.value(), 42)
            dialog._on_heard(0.8, True)
            self.assertIn("heard it 1x", dialog.try_label.text())
            dialog._on_try_stopped("")
            self.assertEqual((dialog.try_btn.text(), dialog.try_label.text()), ("Try it", "Heard it 1 time."))
            dialog.close()
        finally:
            wakeword.models_dir = saved


class SynthCacheTests(unittest.TestCase):
    def test_a_retrain_reuses_the_synthesized_takes(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        import wake_training

        calls = []

        class Voice:
            config = SimpleNamespace(num_speakers=1, sample_rate=16000)

            def synthesize(self, text, config):
                calls.append((text, config.length_scale))
                return [SimpleNamespace(audio_int16_array=np.full(4000, 1234, dtype=np.int16))]

        folder = Path(tempfile.mkdtemp())
        voice_file = folder / "voice.onnx"
        voice_file.write_bytes(b"model")
        synth = wake_training.Synth.__new__(wake_training.Synth)
        synth.files, synth.voices, synth.cache_dir, synth.cache_hits = [voice_file], [Voice()], folder / "cache", 0
        first = synth.say("hey nova", speeds=(0.9, 1.1), variation=(0.5,))
        self.assertEqual(len(calls), 2)
        again = synth.say("hey nova", speeds=(0.9, 1.1), variation=(0.5,))
        self.assertEqual((len(calls), synth.cache_hits), (2, 2))            # no new synthesis
        self.assertTrue(all(np.array_equal(a, b) for a, b in zip(first, again)))
        synth.say("hey now", speeds=(0.9,), variation=(0.5,))              # different words: synthesized
        self.assertEqual(len(calls), 3)
        voice_file.write_bytes(b"a newer model")                            # an updated voice: not mixed up
        synth.say("hey nova", speeds=(0.9,), variation=(0.5,))
        self.assertEqual(len(calls), 4)
        wake_training.trim_synth_cache(folder / "cache", limit_mb=0)
        self.assertEqual(list((folder / "cache").glob("*.npy")), [])


class PhraseTests(unittest.TestCase):
    def test_label_shows_what_to_say(self):
        self.assertEqual(wakeword.label({"wake_engine": "stt", "wake_word": "hey nova"}), "hey nova")
        self.assertEqual(wakeword.label({"wake_engine": "openwakeword", "wake_model": "alexa", "wake_word": "hey nova"}),
                         "alexa")
        self.assertEqual(wakeword.label({"wake_engine": "openwakeword", "wake_model": "bogus"}), "hey jarvis")

    def test_leaked_phrase_words_are_dropped_from_the_command(self):
        self.assertEqual(wakeword.strip_phrase("jarvis what time is it", "hey jarvis"), "what time is it")
        self.assertEqual(wakeword.strip_phrase("Hey, Jarvis. open chrome", "hey jarvis"), "open chrome")
        self.assertEqual(wakeword.strip_phrase("open chrome", "hey jarvis"), "open chrome")
        self.assertEqual(wakeword.strip_phrase("hey jarvis", "hey jarvis"), "")
        self.assertEqual(wakeword.strip_phrase("what is the weather in hey jarvis land", "hey jarvis"),
                         "what is the weather in hey jarvis land")           # only leading words

    def test_every_offered_phrase_has_a_spoken_form(self):
        self.assertIn(wakeword.DEFAULT_PHRASE, wakeword.PHRASES)
        self.assertTrue(all(v == v.lower() for v in wakeword.PHRASES.values()))


class SpeedChoiceTests(unittest.TestCase):
    def test_device_and_precision(self):
        self.assertEqual(stt.pick_device("auto", cuda=True), ("cuda", "float16"))
        self.assertEqual(stt.pick_device("auto", cuda=False), ("cpu", "int8"))
        self.assertEqual(stt.pick_device("cpu", cuda=True), ("cpu", "int8"))
        self.assertEqual(stt.pick_device("cuda", cuda=False), ("cpu", "int8"))     # asked for, but there isn't one
        self.assertEqual(stt.pick_device("", cuda=True), ("cuda", "float16"))

    def test_cpu_threads(self):
        self.assertEqual(stt.cpu_threads(6, cores=32), 6)
        self.assertEqual(stt.cpu_threads(0, cores=2), 4)
        self.assertEqual(stt.cpu_threads(0, cores=12), 6)
        self.assertEqual(stt.cpu_threads(0, cores=64), 8)

    def test_the_wake_loop_can_use_a_smaller_model(self):
        cfg = {"whisper_model": "base.en", "wake_whisper_model": "tiny.en"}
        t = stt.Transcriber(cfg)
        self.assertEqual((t._model_name(), t._model_name("wake")), ("base.en", "tiny.en"))
        cfg["wake_whisper_model"] = ""
        self.assertEqual(t._model_name("wake"), "base.en")                          # blank: same as commands

    def test_a_broken_offline_engine_never_falls_back_to_google_unless_allowed(self):
        said = []
        t = stt.Transcriber({"stt_engine": "whisper", "wake_skip_noise": False}, on_status=said.append)
        t._load_whisper = lambda name=None: t._fail("Couldn't load it, " + t._instead(), RuntimeError("x")) or None
        t._google_text = lambda pcm: self.fail("sent audio to Google")
        pcm = b"\x00\x10" * 16000
        self.assertIsNone(t.transcribe(pcm))
        self.assertIsNone(t.transcribe(pcm))
        self.assertEqual(len(said), 1)                                            # said once, not every time
        self.assertIn("pick Google", said[0])
        self.assertEqual(t.engine, "whisper")
        t._cfg["stt_online_fallback"] = True
        t._google_text = lambda pcm: "via google"
        self.assertEqual(t.engine, "google")
        self.assertEqual(t.transcribe(pcm), "via google")

    def test_models_are_loaded_once_per_name_and_device(self):
        t = stt.Transcriber({"whisper_model": "base.en", "whisper_device": "cpu"})
        t._models[("base.en", "cpu")] = sentinel = object()
        self.assertIs(t._load_whisper("base.en"), sentinel)
        self.assertIs(t._load_whisper(), sentinel)


if __name__ == "__main__":
    unittest.main()
