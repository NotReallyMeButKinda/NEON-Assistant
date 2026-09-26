"""Telling a voice from everything else (vad.py): a quiet microphone, music underneath, and quiet recordings
raised before transcription. tests/fixtures/speech.wav is one sentence from Windows' built-in voice
("Make a card for dinner with mom at four pm tomorrow"), 16 kHz mono."""

import os
import unittest
import wave

import numpy as np

import listening as L
import vad

from tests.test_listening import Bench, StubTranscriber

HERE = os.path.dirname(os.path.abspath(__file__))


def sentence(pad_chunks=5) -> np.ndarray:
    with wave.open(os.path.join(HERE, "fixtures", "speech.wav")) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    pad = np.zeros(pad_chunks * L.CHUNK_SAMPLES, dtype=np.int16)
    audio = np.concatenate([pad, audio, pad, pad, pad, pad])
    return audio[:len(audio) // L.CHUNK_SAMPLES * L.CHUNK_SAMPLES]


def music(length: int) -> np.ndarray:
    t = np.arange(length) / 16000
    rng = np.random.default_rng(1)
    return (np.sin(2 * np.pi * 220 * t) * 3000 + np.sin(2 * np.pi * 330 * t) * 2000
            + rng.normal(0, 800, length)).astype(np.int32)


def chunks(audio):
    return [audio[i:i + L.CHUNK_SAMPLES] for i in range(0, len(audio), L.CHUNK_SAMPLES)]


@unittest.skipUnless(vad.available(), "the voice-activity model (faster-whisper's Silero VAD) isn't installed")
class VoiceActivityTests(unittest.TestCase):
    def voiced(self, audio):
        d = vad.SpeechDetector()
        return [d.probability(c) >= vad.START_PROB for c in chunks(audio)]

    def test_noise_and_tones_are_not_speech(self):
        rng = np.random.default_rng(0)
        self.assertFalse(any(self.voiced(rng.normal(0, 30, 16000).astype(np.int16))))
        self.assertFalse(any(self.voiced((np.sin(np.arange(16000) / 16000 * 2 * np.pi * 440) * 8000)
                                         .astype(np.int16))))

    def test_a_quiet_voice_is_heard_where_loudness_missed_it(self):
        quiet = (sentence() / 20).astype(np.int16)
        loud_enough = [float(np.abs(c).mean()) > 150 for c in chunks(quiet)]
        self.assertLess(sum(loud_enough), 6)                          # the old test caught almost nothing
        self.assertGreater(sum(self.voiced(quiet)), 20)               # the model hears the sentence

    def test_speech_over_music_still_ends(self):
        voice = sentence()
        mixed = np.clip(voice.astype(np.int32) + music(len(voice)), -32768, 32767).astype(np.int16)
        flags = self.voiced(mixed)
        self.assertFalse(any(flags[-8:]))                            # the music alone isn't "still talking"
        self.assertGreater(sum(flags), 20)

    def test_the_listener_records_a_quiet_command_and_raises_it(self):
        bench = Bench()
        cfg = {"silence_duration": 0.8, "fast_endpoint": False, "listen_timeout": 6.0, "phrase_time_limit": 12.0,
               "vad_enabled": True, "mic_auto_gain": True, "stt_partials": False}
        listener = L.Listener(cfg, mic_factory=bench._make, initial_noise=1.0)
        bench.mic = listener._mic
        heard = []
        listener.transcriber = StubTranscriber()
        listener.transcriber.transcribe = lambda pcm, role=None: heard.append(pcm) or "heard it"
        quiet = (sentence() / 20).astype(np.int16)
        bench.mic._ensure_open()
        for c in chunks(quiet):
            bench.mic._callback(c.reshape(-1, 1), len(c), None, None)
        self.assertEqual(listener.listen_once(), "heard it")
        peak = np.abs(np.frombuffer(heard[0], dtype=np.int16)).max()
        self.assertGreater(peak, 8000)                                 # raised before transcription
        self.assertGreater(listener.wake_gain, 1.0)


class GainTests(unittest.TestCase):
    def test_quiet_recordings_are_raised_and_loud_ones_left_alone(self):
        quiet = (np.sin(np.arange(16000) / 5) * 1600).astype(np.int16).tobytes()
        raised, gain = vad.normalize_pcm(quiet)
        self.assertAlmostEqual(gain, 16383.5 / 1600, delta=0.5)
        self.assertLess(np.abs(np.frombuffer(raised, dtype=np.int16)).max(), 32767)
        loud = (np.sin(np.arange(16000) / 5) * 20000).astype(np.int16).tobytes()
        self.assertEqual(vad.normalize_pcm(loud), (loud, 1.0))
        self.assertEqual(vad.gain_for(10), vad.MAX_GAIN)                # hiss is never raised past the cap


if __name__ == "__main__":
    unittest.main()
