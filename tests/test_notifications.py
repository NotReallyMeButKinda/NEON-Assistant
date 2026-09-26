import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import notifications as n

from tests import common
from tests.common import live_only


def note(app, title="Hi", body=""):
    return n.Notification(id=1, app=app, title=title, body=body)


BASE = {"notify_ask": "speech", "notify_rules": [], "notify_quiet_enabled": False, "notify_quiet_start": "22:00",
        "notify_quiet_end": "07:00", "notify_vip": ""}


def decide(app="Discord", cfg=None, paused=False, now=None, **kw):
    return n.decide(note(app, **kw), {**BASE, **(cfg or {})}, paused, now)


def at(h, m=0):
    return datetime(2026, 9, 21, h, m)


class DecisionTests(unittest.TestCase):
    def test_global_mode(self):
        self.assertEqual(decide(cfg={"notify_ask": "speech"}).action, "ask")
        self.assertEqual(decide(cfg={"notify_ask": "card"}).action, "card")
        self.assertEqual(decide(cfg={"notify_ask": "none"}).action, "show")

    def test_per_app_rules_first_match_wins(self):
        rules = [{"app": "steam", "mode": "ignore"}, {"app": "outlook", "mode": "card"},
                 {"app": "slack", "mode": "silent"}, {"app": "dis", "mode": "ignore"}]
        cfg = {"notify_rules": rules, "notify_ask": "none"}
        self.assertEqual(decide("Steam", cfg).action, "ignore")
        self.assertEqual(decide("Microsoft Outlook", cfg).action, "card")
        self.assertEqual(decide("Slack", cfg).action, "silent")
        self.assertEqual(decide("Discord", cfg).action, "ignore")        # matched by the later 'dis' rule
        self.assertEqual(decide("Spotify", cfg).action, "show")

    def test_quiet_hours_cross_midnight(self):
        cfg = {"notify_quiet_enabled": True}
        for hour, quiet in ((21, False), (22, True), (23, True), (3, True), (6, True), (7, False), (12, False)):
            self.assertEqual(n.in_quiet_hours({**BASE, **cfg}, at(hour, 30 if hour == 21 else 0)), quiet, hour)
        quiet = decide(cfg=cfg, now=at(23))
        self.assertEqual((quiet.action, quiet.sound), ("show", False))

    def test_bad_or_equal_quiet_times(self):
        odd = {**BASE, "notify_quiet_enabled": True, "notify_quiet_start": "25:99", "notify_quiet_end": "x"}
        self.assertTrue(n.in_quiet_hours(odd, at(23)))                    # falls back to 22:00-07:00
        same = {**BASE, "notify_quiet_enabled": True, "notify_quiet_start": "08:00", "notify_quiet_end": "08:00"}
        self.assertFalse(n.in_quiet_hours(same, at(8)))

    def test_vip_words_cut_through_quiet_hours(self):
        cfg = {"notify_quiet_enabled": True, "notify_vip": "mom, urgent"}
        self.assertEqual(decide(cfg=cfg, now=at(23)).action, "show")
        self.assertEqual(decide(cfg=cfg, now=at(23), title="URGENT: server down").action, "ask")
        self.assertEqual(decide(cfg=cfg, now=at(23), body="from Mom").action, "ask")

    def test_pause_is_absolute_but_ignore_stays_ignore(self):
        cfg = {"notify_quiet_enabled": True, "notify_vip": "urgent",
               "notify_rules": [{"app": "steam", "mode": "ignore"}]}
        self.assertEqual(decide(paused=True).action, "silent")
        self.assertEqual(decide(cfg=cfg, paused=True, now=at(23), title="urgent").action, "silent")
        self.assertEqual(decide("Steam", cfg, paused=True).action, "ignore")

    def test_strongest(self):
        self.assertEqual(n.strongest([n.Decision("silent"), n.Decision("show"), n.Decision("ask")]), "ask")
        self.assertEqual(n.strongest([]), "ignore")


class ParsingTests(unittest.TestCase):
    def test_parse_toast(self):
        parsed = n.parse_toast({"id": 7, "app": "Discord", "texts": ["Alex", "hey", "you free?"], "created": 2000,
                                "backlog": True})
        self.assertEqual((parsed.app, parsed.title, parsed.body, parsed.backlog), ("Discord", "Alex", "hey you free?", True))
        self.assertEqual(parsed.created, 2.0)
        self.assertEqual(n.parse_toast({"id": 1, "app": "", "texts": ["Only title"]}).app, "an app")
        self.assertIsNone(n.parse_toast({"id": 2, "texts": []}))
        self.assertEqual(n.parse_toast({"id": 3, "app": "X", "texts": "single"}).title, "single")

    def test_watcher_routes_backlog_then_new(self):
        new, backlog = [], []
        watcher = n.NotificationWatcher(new.append, None, lambda: ["ignoreme"], on_backlog=backlog.extend)
        feed = lambda obj: watcher._handle_line(json.dumps(obj).encode())      # noqa: E731
        feed({"type": "toast", "id": 1, "app": "Old", "texts": ["before"], "backlog": True})
        feed({"type": "toast", "id": 2, "app": "IgnoreMe", "texts": ["dropped"], "backlog": True})
        self.assertEqual(backlog, [])                                       # held until "ready"
        feed({"type": "ready"})
        self.assertTrue(watcher.ready.is_set())
        self.assertEqual([b.title for b in backlog], ["before"])
        feed({"type": "toast", "id": 3, "app": "New", "texts": ["after"]})
        feed({"type": "toast", "id": 4, "app": "ignoreme", "texts": ["dropped too"]})
        self.assertEqual([x.title for x in new], ["after"])

    def test_expected_toasts_never_reach_the_app(self):
        seen = []
        watcher = n.NotificationWatcher(seen.append)
        event = watcher.expect("delivery check 123")
        watcher._handle_line(json.dumps({"type": "toast", "id": 9, "app": "PS", "texts": ["delivery check 123"]}).encode())
        self.assertTrue(event.is_set())
        self.assertEqual(seen, [])


class SilenceStateTests(unittest.TestCase):
    """The registry write is replaced, so no test touches the user's real notification settings."""

    def setUp(self):
        self.writes = []
        self.original_write, self.original_read = n._write_toast_enabled, n._read_toast_enabled
        n._write_toast_enabled = self.writes.append
        n._read_toast_enabled = lambda: 1
        self.file = Path(tempfile.mkdtemp()) / "restore.json"
        self.state = n.SilenceState(self.file)

    def tearDown(self):
        n._write_toast_enabled, n._read_toast_enabled = self.original_write, self.original_read

    def test_engage_remembers_the_original_and_release_restores_it(self):
        self.state.engage()
        self.state.engage()                                    # engaging twice must not overwrite the saved original
        self.assertEqual(json.loads(self.file.read_text())["original"], 1)
        self.assertEqual(self.writes, [0, 0])
        self.state.release()
        self.assertEqual(self.writes[-1], 1)
        self.assertFalse(self.file.exists())

    def test_a_leftover_marker_from_a_crash_is_recovered(self):
        self.state.engage()
        self.assertTrue(n.SilenceState(self.file).recover())
        self.assertEqual(self.writes[-1], 1)
        self.assertFalse(n.SilenceState(self.file).recover())


@live_only
class LiveWatcherTests(unittest.TestCase):
    def test_a_real_toast_is_delivered_dismissed_and_backlogged(self):
        got, status = [], []
        watcher = n.NotificationWatcher(got.append, status.append)
        watcher.start()
        try:
            self.assertTrue(watcher.ready.wait(20), status)
            n.send_test_toast("Neon unit test", "body text")
            self.assertTrue(common.pump_until(lambda: got, 10))
            self.assertEqual((got[0].title, got[0].body), ("Neon unit test", "body text"))
            watcher.remove(got[0].id)
            time.sleep(2.5)
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
