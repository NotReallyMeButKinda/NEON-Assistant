"""The Timers window and the timer functions behind it (no timer here runs long enough to go off)."""

import unittest

import timers

from controller import Assistant
from tests import common
from tests.common import backend


class TimerApiTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        timers.cancel_timers()

    def tearDown(self):
        timers.cancel_timers()

    def test_typed_lengths(self):
        cases = {"25": 1500, "1:30": 90, "1:05:00": 3900, "10m": 600, "90s": 90, "1h30m": 5400, "1h 30": 5400,
                 "1m30s": 90, "5 min": 300, "2 hours": 7200, "half an hour": 1800, "abc": 0, "": 0}
        for text, seconds in cases.items():
            self.assertEqual(timers.parse_duration_text(text), seconds, text)

    def test_list_and_cancel_one(self):
        timers.start_timer(600, "pasta")
        timers.start_timer(300)
        listed = timers.list_timers()
        self.assertEqual([t["label"] for t in listed], ["", "pasta"])              # soonest first
        self.assertTrue(timers.cancel_timer(listed[0]["id"]))
        self.assertFalse(timers.cancel_timer(listed[0]["id"]))                    # already gone
        self.assertEqual([t["label"] for t in timers.list_timers()], ["pasta"])


class TimerPanelTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        timers.cancel_timers()
        from ui.timer_panel import TimerPanel
        self.ctl = Assistant()
        self.panel = TimerPanel(self.ctl)
        self.panel.show()

    def tearDown(self):
        self.panel.close()
        timers.cancel_timers()

    def test_presets_typed_lengths_and_cancelling(self):
        self.assertFalse(self.panel.empty.isHidden())
        self.panel.preset_buttons[2].click()                                      # 5 min
        self.panel.length.setText("1:30")
        self.panel.name.setText("  tea ")
        self.panel.start_btn.click()
        self.assertEqual(self.panel.list.count(), 2)
        self.assertIn("tea", self.panel.list.item(0).text())                       # 1:30 is sooner
        self.assertIn("5:00 timer", self.panel.list.item(1).text())
        self.assertEqual(self.panel.length.text(), "")
        self.panel.list.setCurrentRow(0)
        self.panel.cancel_btn.click()
        self.assertEqual([t["label"] for t in timers.list_timers()], [""])
        self.panel.cancel_all_btn.click()
        self.assertEqual(timers.list_timers(), [])

    def test_nonsense_and_too_long_are_refused(self):
        self.panel.length.setText("soon")
        self.panel.start_btn.click()
        self.assertIn("isn't a length", self.panel.message.text())
        self.panel.length.setText("25h")
        self.panel.start_btn.click()
        self.assertIn("24 hours", self.panel.message.text())
        self.assertEqual(timers.list_timers(), [])

    def test_the_bar_label_opens_it(self):
        from ui import status_bar as sb
        opened = []
        self.ctl.timers_requested.connect(lambda: opened.append(True))
        bar = sb.StatusBar(self.ctl)
        bar.timer_label.clicked.emit()
        self.assertEqual(opened, [True])
        bar._timer_tick.stop()
        bar.hide()


if __name__ == "__main__":
    unittest.main()
