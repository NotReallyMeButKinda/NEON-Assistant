"""
selftest.py -- `NeonAssistant.exe --selftest [speech.wav]`: start the real app off-screen, open every window,
load the heavy engines (Whisper, Piper, the wake word, speech detection), quit through the normal shutdown,
and write what happened to selftest.txt in the data folder.

It exists to catch what only breaks in the packaged .exe (a module or data file PyInstaller left out, a
DLL that won't load, code that assumes a console) without anyone clicking through the app. Nothing is
shown, spoken or sent: audio is synthesized into memory and the microphone isn't opened.
"""

from __future__ import annotations

import threading
import time
import traceback
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QListWidget

import app_paths

REPORT = "selftest.txt"


class _Relay(QObject):
    done = Signal()


def _speech_pcm(wav_path: str | None) -> bytes:
    """16 kHz mono int16: the given recording, or two seconds of a quiet tone."""
    if wav_path and Path(wav_path).is_file():
        with wave.open(wav_path, "rb") as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                return w.readframes(w.getnframes())
    t = np.arange(32000) / 16000
    return (np.sin(2 * np.pi * 220 * t) * 3000).astype(np.int16).tobytes()


def _engine_checks(config: dict, wav_path: str | None) -> list[tuple[str, str]]:
    results = []

    def check(name, fn):
        start = time.time()
        try:
            detail = fn()
            results.append((f"ok    {name}", f"{detail or ''} ({time.time() - start:.1f}s)"))
        except Exception:  # noqa: BLE001 -- recorded, never raised
            results.append((f"FAIL  {name}", traceback.format_exc()))

    pcm = _speech_pcm(wav_path)

    def speech_detection():
        import vad
        chunk = np.frombuffer(pcm, dtype=np.int16)
        return f"probability {vad.SpeechDetector().probability(chunk):.2f}"

    def whisper():
        import stt
        transcriber = stt.Transcriber(dict(config, stt_engine="whisper"))
        if transcriber._load_whisper() is None:
            raise RuntimeError("the model didn't load (see neon.log)")
        return f"heard {transcriber._whisper_text(pcm)!r}"

    def piper():
        import tts
        from piper import PiperVoice
        names = tts.list_installed_voices()
        if not names:
            return "no voice downloaded yet: skipped"
        name = config.get("tts_voice") if config.get("tts_voice") in names else names[0]
        voice = PiperVoice.load(str(tts.ensure_voice(name)))
        samples = sum(len(chunk.audio_int16_bytes) for chunk in voice.synthesize("Testing one two three."))
        return f"{name}: {samples} bytes of audio"

    def wake_word():
        import wakeword
        model = str(config.get("wake_model", ""))
        if not (wakeword.is_custom(model) and wakeword.trained(model)):
            return f"{model or 'none'} not trained here: skipped"
        detector = wakeword.Detector(model)
        detector.load()
        detector.feed(np.frombuffer(pcm, dtype=np.int16))
        return f"{model}: score {detector.last_score:.2f}"

    def audio_devices():
        import sounddevice as sd
        return f"{len(sd.query_devices())} devices"

    for name, fn in (("speech detection", speech_detection), ("whisper", whisper), ("piper voice", piper),
                     ("wake word", wake_word), ("audio devices", audio_devices)):
        check(name, fn)
    return results


def schedule(opens: dict, quit_app, config: dict, wav_path: str | None = None) -> None:
    """Called from main() once everything is built. `opens` maps a window's name to the function that
    opens it; `quit_app` is the tray's Quit."""
    app = QApplication.instance()
    results: list[tuple[str, str]] = []
    relay = _Relay(app)
    app._selftest_relay = relay                    # keep it alive

    def open_windows() -> None:
        for name, fn in opens.items():
            try:
                fn()
                app.processEvents()
                results.append((f"ok    open {name}", ""))
            except Exception:  # noqa: BLE001
                results.append((f"FAIL  open {name}", traceback.format_exc()))
        # Walk every Settings page (they're built when first shown).
        for widget in app.topLevelWidgets():
            if type(widget).__name__ != "SettingsDialog" or not widget.isVisible():
                continue
            for pages in widget.findChildren(QListWidget):
                for row in range(pages.count()):
                    try:
                        pages.setCurrentRow(row)
                        app.processEvents()
                    except Exception:  # noqa: BLE001
                        results.append((f"FAIL  settings page {row}", traceback.format_exc()))
            results.append(("ok    settings pages walked", ""))
        threading.Thread(target=engines, name="Nova-selftest", daemon=True).start()

    def engines() -> None:
        results.extend(_engine_checks(config, wav_path))
        relay.done.emit()

    def finish() -> None:
        failed = sum(1 for status, _ in results if status.startswith("FAIL"))
        lines = [f"NEON self-test: {len(results) - failed} passed, {failed} failed", ""]
        for status, detail in results:
            lines.append(f"{status}  {detail}".rstrip())
        app_paths.data_path(REPORT).write_text("\n".join(lines) + "\n", encoding="utf-8")
        QTimer.singleShot(500, quit_app)           # the real shutdown path is part of the test

    relay.done.connect(finish)
    QTimer.singleShot(4000, open_windows)          # let start-up settle first
