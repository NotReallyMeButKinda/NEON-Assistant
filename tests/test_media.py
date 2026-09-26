"""Now playing from any player (media.py): the helper's reports, commands, and how the assistant
uses them when Pear Desktop doesn't answer. No real player is read or controlled here."""

import tempfile
import time
import unittest
from pathlib import Path

import media

from tests import common
from tests.common import backend, pump_until
from ui import status_bar as sb

REPORT = {"type": "media", "app": "Spotify.exe", "title": "Song", "artist": "Band", "status": "Playing",
          "position": 12.5, "duration": 200.0}


class FakeMedia(common.NoMedia):
    def __init__(self, song=None):
        self._song, self.sent = song, []

    def ensure(self, wait=0.0):
        return True

    def song(self):
        return self._song

    def now_playing(self):
        return "Song by Band, in Spotify." if self._song else None

    def command(self, action):
        if self._song is None:
            return False
        self.sent.append(action)
        return True


class ReportTests(unittest.TestCase):
    def test_a_report_becomes_a_pear_shaped_song(self):
        song = media.to_song(REPORT)
        self.assertEqual(song, {"title": "Song", "artist": "Band", "isPaused": False, "elapsedSeconds": 12.5,
                                "songDuration": 200.0, "app": "Spotify", "source": "windows"})
        self.assertTrue(media.to_song({**REPORT, "status": "Paused"})["isPaused"])
        self.assertIsNone(media.to_song({"type": "media", "app": ""}))           # nothing playing anywhere
        self.assertIsNone(media.to_song({**REPORT, "title": ""}))

    def test_friendly_app_names(self):
        cases = {"Spotify.exe": "Spotify", "com.github.th-ch.youtube-music": "YouTube Music",
                 "F0DC299D809B9700": "your browser", "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic": "Media Player",
                 "Foo.Bar_abc!App": "Bar", "": ""}
        for aumid, name in cases.items():
            self.assertEqual(media.friendly_app(aumid), name, aumid)

    def test_watcher_state_and_commands_without_a_process(self):
        w = media.MediaWatcher()
        w._cmd_file = Path(tempfile.mkdtemp()) / "cmd.txt"
        w.start = lambda: None                                                # no PowerShell here
        self.assertFalse(w.command("next"))                                   # no report yet
        w._handle_line(b'{"type":"media","app":"Spotify.exe","title":"Song","artist":"Band","status":"Paused",'
                       b'"position":1,"duration":2}')
        self.assertTrue(w.ready.is_set())
        self.assertEqual(w.song()["title"], "Song")
        self.assertEqual(w.now_playing(), "Song by Band, in Spotify. It's paused.")
        self.assertTrue(w.command("play"))
        self.assertFalse(w.command("format c:"))                              # only the five commands
        self.assertEqual(w._cmd_file.read_text(encoding="utf-8"), "play\n")
        w._at = time.monotonic() - media.STALE_SECONDS - 1                    # the helper went quiet
        self.assertIsNone(w.song())


class AssistantTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_media_commands_go_to_the_session_when_pear_is_away(self):
        backend.MEDIA = fake = FakeMedia(media.to_song(REPORT))
        self.assertEqual(backend.media_control("pause"), "Paused.")
        self.assertEqual(backend.media_control("play"), "Playing.")
        self.assertEqual(backend.media_control("next"), "Skipping to the next track.")
        self.assertEqual(backend.media_control("playpause"), "Toggled play and pause.")
        self.assertEqual(fake.sent, ["pause", "play", "next", "toggle"])

    def test_whats_playing_falls_back_to_any_player(self):
        backend.MEDIA = FakeMedia(media.to_song(REPORT))
        self.assertEqual(backend.handle_music_command("what's playing"), "Song by Band, in Spotify.")
        backend.CONFIG["media_any_player"] = False
        self.assertIsNone(backend.handle_music_command("what's playing"))


class StatusBarTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        from controller import Assistant
        self.bar = sb.StatusBar(Assistant())
        self.bar.resize(1500, 44)
        self.bar.show()

    def tearDown(self):
        for timer in (self.bar._music_timer, self.bar._music_tick):
            timer.stop()
        self.bar.hide()

    def test_the_bar_shows_another_players_song(self):
        backend.MEDIA = FakeMedia(media.to_song(REPORT))
        backend.CONFIG.update(bar_show_music=True, ytm_enabled=False)
        self.bar.apply_config()
        self.assertTrue(pump_until(lambda: self.bar.media_box.isVisible(), 5))
        self.assertIn("Band - Song", self.bar.music._text)


if __name__ == "__main__":
    unittest.main()
