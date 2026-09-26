"""Heads-up alerts before calendar events."""

import unittest
from datetime import datetime, timedelta, timezone

import calendar_feed
from calendar_feed import CalendarAlerts, Event, alert_text, due_alerts

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


def event(title, minutes, all_day=False, length=30):
    start = NOW + timedelta(minutes=minutes)
    return Event(title, start, start + timedelta(minutes=length), all_day)


class DueAlerts(unittest.TestCase):
    def test_only_events_inside_the_lead_time_are_due(self):
        events = [event("soon", 4), event("later", 20), event("running", -10), event("holiday", 1, all_day=True)]
        self.assertEqual([e.title for e in due_alerts(events, NOW, 5, set())], ["soon"])

    def test_an_event_is_announced_once(self):
        seen = set()
        events = [event("standup", 4)]
        self.assertEqual(len(due_alerts(events, NOW, 5, seen)), 1)
        self.assertEqual(due_alerts(events, NOW + timedelta(seconds=30), 5, seen), [])

    def test_one_that_just_started_still_gets_its_alert(self):
        self.assertEqual(len(due_alerts([event("late", 0)], NOW + timedelta(seconds=20), 5, set())), 1)
        self.assertEqual(due_alerts([event("missed", -3)], NOW, 5, set()), [])

    def test_a_recurring_event_is_announced_again_on_its_next_day(self):
        seen = set()
        today, tomorrow = event("daily", 3), event("daily", 3 + 24 * 60)
        self.assertEqual(len(due_alerts([today], NOW, 5, seen)), 1)
        later = NOW + timedelta(days=1)
        self.assertEqual(len(due_alerts([tomorrow], later, 5, seen)), 1)

    def test_wording(self):
        self.assertEqual(alert_text(event("Standup", 5), NOW), "Standup in 5 minutes.")
        self.assertEqual(alert_text(event("Standup", 1), NOW), "Standup in a minute.")
        self.assertEqual(alert_text(event("Standup", 0), NOW), "Standup is starting now.")


class Watcher(unittest.TestCase):
    def make(self, config, events, clock=lambda: NOW):
        self.heard = []
        self.fetches = 0

        def fetch(source, now):
            self.fetches += 1
            if isinstance(events, Exception):
                raise events
            return events
        return CalendarAlerts(lambda: config, lambda e, text: self.heard.append(text), fetch, clock)

    def test_announces_then_stays_quiet(self):
        watcher = self.make({"calendar_ics": "x.ics", "calendar_alerts": True, "calendar_alert_minutes": 5},
                            [event("Standup", 4)])
        watcher.check()
        watcher.check()
        self.assertEqual(self.heard, ["Standup in 4 minutes."])
        self.assertEqual(self.fetches, 1)                 # the feed is not downloaded on every pass

    def test_nothing_without_a_calendar_or_when_switched_off(self):
        self.make({"calendar_ics": "", "calendar_alerts": True}, [event("x", 1)]).check()
        self.make({"calendar_ics": "x.ics", "calendar_alerts": False}, [event("x", 1)]).check()
        self.assertEqual(self.heard, [])
        self.assertEqual(self.fetches, 0)

    def test_an_unreadable_feed_is_not_fatal(self):
        watcher = self.make({"calendar_ics": "x.ics"}, OSError("offline"))
        self.assertEqual(watcher.check(), [])

    def test_lead_time_follows_the_setting(self):
        cfg = {"calendar_ics": "x.ics", "calendar_alerts": True, "calendar_alert_minutes": 15}
        self.make(cfg, [event("Review", 12)]).check()
        self.assertEqual(self.heard, ["Review in 12 minutes."])

    def test_module_exposes_the_poll_constants(self):
        self.assertGreater(calendar_feed.REFETCH_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
