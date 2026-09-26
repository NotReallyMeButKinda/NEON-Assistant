"""Data-layer pieces: calendar feed, system meters, sounds, phrase helpers, voice names, secrets."""

import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import calendar_feed as cf
import secrets_store
import sounds
import sysinfo
import tts
from tests import common

QT = common.APP          # importing tests.common also sets up Qt and the import paths


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.local = datetime.now().astimezone().tzinfo
        self.now = datetime(2026, 9, 21, 9, 0, tzinfo=self.local)              # a Monday, 09:00

    def loc(self, dt):
        return dt.strftime("%Y%m%dT%H%M%S")

    def test_recurring_excluded_cancelled_and_all_day(self):
        now, loc = self.now, self.loc
        ics = "\r\n".join([
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT", "SUMMARY:UTC meeting", f"DTSTART:{(now + timedelta(minutes=25)).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"DTEND:{(now + timedelta(minutes=55)).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}", "END:VEVENT",
            "BEGIN:VEVENT", "SUMMARY:Cancelled", f"DTSTART:{loc(now + timedelta(hours=1))}", "STATUS:CANCELLED", "END:VEVENT",
            "BEGIN:VEVENT", "SUMMARY:Holiday", "DTSTART;VALUE=DATE:20260921", "DTEND;VALUE=DATE:20260922", "END:VEVENT",
            "BEGIN:VEVENT", "SUMMARY:Standup", f"DTSTART:{loc(datetime(2026, 9, 7, 9, 30))}",
            f"DTEND:{loc(datetime(2026, 9, 7, 9, 45))}", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR", "END:VEVENT",
            "BEGIN:VEVENT", "SUMMARY:Used up", f"DTSTART:{loc(datetime(2026, 9, 1, 8, 0))}", "RRULE:FREQ=DAILY;COUNT=3", "END:VEVENT",
            "BEGIN:VEVENT", "SUMMARY:Daily skip", f"DTSTART:{loc(datetime(2026, 9, 1, 15, 0))}", "RRULE:FREQ=DAILY",
            f"EXDATE:{loc(datetime(2026, 9, 21, 15, 0))}", "END:VEVENT",
            "END:VCALENDAR"])
        events = cf.parse_events(ics, now)
        titles = [e.title for e in events]
        self.assertIn("UTC meeting", titles)
        self.assertIn("Holiday", titles)
        self.assertNotIn("Cancelled", titles)
        self.assertNotIn("Used up", titles)
        self.assertTrue(any(e.title == "Standup" and e.start.date() == now.date() for e in events))
        self.assertFalse(any(e.title == "Standup" and e.start.day == 22 for e in events))         # not a Tuesday
        self.assertFalse(any(e.title == "Daily skip" and e.start.date() == now.date() for e in events))  # EXDATE
        self.assertTrue(any(e.title == "Daily skip" and e.start.day == 22 for e in events))

    def test_time_zone_and_describe(self):
        tz = ("BEGIN:VEVENT\r\nSUMMARY:NY call\r\nDTSTART;TZID=America/New_York:20260921T130000\r\n"
              "DTEND;TZID=America/New_York:20260921T140000\r\nEND:VEVENT")
        event = cf.parse_events(tz, datetime(2026, 9, 21, 0, 0, tzinfo=self.local))[0]
        self.assertEqual(event.start.astimezone(timezone.utc).strftime("%H:%M"), "17:00")   # EDT is UTC-4
        now = self.now
        self.assertEqual(cf.describe(cf.Event("Sync", now + timedelta(minutes=25), now + timedelta(minutes=55)), now), "Sync in 25 min")
        self.assertEqual(cf.describe(cf.Event("Live", now - timedelta(minutes=5), now + timedelta(minutes=20)), now), "Live (now)")
        self.assertEqual(cf.describe(cf.Event("Dentist", now + timedelta(hours=8, minutes=30), now + timedelta(hours=9)), now,
                                     use_24h=True), "Dentist 17:30")

    def test_file_source_and_garbage(self):
        path = Path(tempfile.mkdtemp()) / "c.ics"
        path.write_text(f"BEGIN:VEVENT\nSUMMARY:Soon\nDTSTART:{self.loc(datetime.now() + timedelta(minutes=30))}\nEND:VEVENT\n")
        event, upcoming = cf.next_event(str(path))
        self.assertEqual(event.title, "Soon")
        self.assertEqual(cf.parse_events("not a calendar", self.now), [])
        with self.assertRaises(OSError):
            cf.fetch(str(path) + ".missing")


class SysInfoTests(unittest.TestCase):
    def test_meters_return_sane_values(self):
        self.assertTrue(0 <= sysinfo.ram_percent() <= 100)
        self.assertTrue(0 <= sysinfo.CpuMeter().percent() <= 100)
        battery = sysinfo.battery()
        self.assertTrue(battery is None or 0 <= battery[0] <= 100)


class SoundTests(unittest.TestCase):
    def test_every_builtin_sound_is_audible_and_ends_cleanly(self):
        for name in sounds.SOUND_LABELS:
            wave_ = sounds.render(name, 1.0)
            self.assertGreater(int(np.abs(wave_).max()), 3000, name)
            self.assertLess(abs(int(wave_[-1])), 2500, name)              # no click at the end

    def test_custom_wav_loading(self):
        path = Path(tempfile.mkdtemp()) / "t.wav"
        tone = (np.sin(np.arange(4410) / 10) * 12000).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(np.column_stack([tone, tone]).tobytes())
        data, rate = sounds.load_wav(str(path))
        self.assertEqual((rate, len(data)), (16000, 4410))                 # stereo folded to mono
        self.assertEqual(len(sounds._resample(np.zeros(8000, dtype=np.float32), 16000)), 22050)
        with self.assertRaises(ValueError):
            sounds.load_wav(str(path) + ".missing")
        self.assertFalse(sounds.play("custom", 0.0, None, str(path) + ".missing"))


class VoiceNameTests(unittest.TestCase):
    def test_names(self):
        self.assertTrue(tts.valid_voice_name("en_US-amy-medium"))
        self.assertTrue(tts.valid_voice_name("en_US-x-x_low"))
        for bad in ("amy", "en_US-amy", "en_US-amy-huge", ""):
            self.assertFalse(tts.valid_voice_name(bad), bad)
        self.assertEqual(tts.voice_label("en_GB-northern_english_male-medium"), "Northern English Male (UK, medium)")
        self.assertTrue(all(name in tts.VOICE_CATALOG for name in ("en_US-amy-medium", "en_US-ryan-high")))


class SecretsTests(unittest.TestCase):
    def test_round_trip_with_unicode(self):
        name = "unittest-secret"
        secrets_store.delete_secret(name)
        try:
            self.assertIsNone(secrets_store.get_secret(name))
            self.assertTrue(secrets_store.set_secret(name, "tokén-✓ 123"))
            self.assertEqual(secrets_store.get_secret(name), "tokén-✓ 123")
        finally:
            secrets_store.delete_secret(name)
        self.assertIsNone(secrets_store.get_secret(name))


if __name__ == "__main__":
    unittest.main()
