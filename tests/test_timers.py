"""Timers that survive a restart."""

import json
import tempfile
import time
import unittest
from pathlib import Path

import timers


class TimerPersistence(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "timers.json"
        timers.cancel_timers()
        timers.enable_persistence(self.path)

    def tearDown(self):
        timers.cancel_timers()
        timers._STORE["path"] = None

    def test_a_running_timer_is_saved_and_cancelling_forgets_it(self):
        timers.start_timer(600, "pasta")
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([(r["label"], r["seconds"], r["reminder"]) for r in rows], [("pasta", 600, False)])
        timers.cancel_timers()
        self.assertFalse(self.path.exists())

    def test_restart_resumes_what_is_left_and_reports_what_was_missed(self):
        now = time.time()
        self.path.write_text(json.dumps([
            {"label": "pasta", "seconds": 600, "reminder": False, "due": now + 300},
            {"label": "call sam", "seconds": 3600, "reminder": True, "due": now - 120},
        ]), encoding="utf-8")
        missed = timers.restore_timers(now)
        self.assertEqual([m["label"] for m in missed], ["call sam"])
        self.assertAlmostEqual(missed[0]["late"], 120, delta=1)
        self.assertEqual([t["label"] for t in timers._TIMERS], ["pasta"])
        self.assertAlmostEqual(timers._TIMERS[0]["ends"] - time.monotonic(), 300, delta=2)
        self.assertEqual(timers._TIMERS[0]["seconds"], 600)            # the announcement still says the full length
        rows = json.loads(self.path.read_text(encoding="utf-8"))       # the missed one is no longer kept
        self.assertEqual([r["label"] for r in rows], ["pasta"])
        self.assertIn("call sam", timers.missed_announcement(missed[0]))
        self.assertIn("2 minutes ago", timers.missed_announcement(missed[0]))

    def test_a_damaged_file_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(timers.restore_timers(), [])
        self.path.write_text(json.dumps([{"label": "x"}, 5]), encoding="utf-8")
        self.assertEqual(timers.restore_timers(), [])
        self.assertEqual(timers._TIMERS, [])

    def test_nothing_is_written_when_persistence_is_off(self):
        timers._STORE["path"] = None
        timers.start_timer(60)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
