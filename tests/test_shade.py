"""The shade: my reply dropping down from the top of the screen when the status bar can't show it."""

import unittest

from PySide6.QtCore import QObject, Signal

from tests import common
from tests.common import pump
from tests.render_utils import check_screenshot

import assistant as backend  # noqa: E402
from ui import shade as shade_module  # noqa: E402
from ui import theme  # noqa: E402


class FakeController(QObject):
    caption = Signal(str, str)
    audio_level = Signal(float)
    state = Signal(str)
    notification = Signal(dict)
    shade_preview = Signal()


MONITOR = (0, 0, 1600, 900)


class ShadeTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        theme.apply_theme("ember", "", None)
        backend.CONFIG.update(bar_animate=False, show_status_bar=True, shade_enabled=True)
        self.fullscreen = None
        self._real = shade_module.fullscreen_monitor
        shade_module.fullscreen_monitor = lambda: self.fullscreen
        self._monitors = shade_module.list_monitors
        shade_module.list_monitors = lambda: [(*MONITOR, 1.0)]
        self.ctl = FakeController()
        self.shade = shade_module.Shade(self.ctl)

    def tearDown(self):
        self.shade.hide()
        shade_module.fullscreen_monitor = self._real
        shade_module.list_monitors = self._monitors

    def reply(self, text="It's 18 degrees and sunny."):
        self.ctl.caption.emit("Nova", text)
        pump(30)

    def test_not_while_the_bar_can_show_it(self):
        self.reply()
        self.assertFalse(self.shade.isVisible())

    def test_drops_down_while_the_bar_is_off(self):
        backend.CONFIG["show_status_bar"] = False
        self.reply()
        self.assertTrue(self.shade.isVisible())
        self.assertEqual((self.shade._label, self.shade._text), ("Nova", "It's 18 degrees and sunny."))

    def test_drops_down_over_a_fullscreen_app_on_its_monitor(self):
        self.fullscreen = MONITOR
        self.reply()
        self.assertTrue(self.shade.isVisible())
        self.assertEqual(self.shade._monitor, MONITOR)

    def test_fullscreen_means_the_content_covers_the_monitor(self):
        covers = shade_module.covers_monitor
        self.assertTrue(covers((0, 0, 1600, 900), MONITOR))                  # borderless windowed / fullscreen
        self.assertTrue(covers((-8, -8, 1608, 908), MONITOR))
        self.assertFalse(covers((0, 31, 1600, 860), MONITOR))                # maximized: title bar and taskbar
        self.assertFalse(covers((100, 100, 900, 700), MONITOR))

    def test_switched_off(self):
        backend.CONFIG.update(show_status_bar=False, shade_enabled=False)
        self.reply()
        self.assertFalse(self.shade.isVisible())

    def test_a_streamed_reply_grows_and_a_new_question_clears_it(self):
        backend.CONFIG["show_status_bar"] = False
        self.ctl.state.emit("speaking")
        self.reply("Sure.")
        self.reply("Sure. **Here** it is.")
        self.assertEqual(self.shade._text, "Sure. Here it is.")      # markdown symbols aren't shown
        self.ctl.caption.emit("You", "and tomorrow?")
        pump(30)
        self.assertFalse(self.shade.isVisible())

    def test_long_replies_show_their_latest_part(self):
        backend.CONFIG["show_status_bar"] = False
        self.reply("word " * 200 + "the end.")
        self.assertTrue(self.shade._text.startswith("…") and self.shade._text.endswith("the end."))
        self.assertLessEqual(len(self.shade._text), shade_module.MAX_CHARS + 1)

    def test_it_goes_back_up_after_i_finish(self):
        backend.CONFIG.update(show_status_bar=False, shade_seconds=1.0)
        self.ctl.state.emit("speaking")
        self.reply()
        self.ctl.state.emit("idle")
        pump(200)
        self.assertTrue(self.shade.isVisible())
        common.pump_until(lambda: not self.shade.isVisible(), 3)
        self.assertFalse(self.shade.isVisible())

    def test_notifications_are_optional(self):
        backend.CONFIG["show_status_bar"] = False
        note = {"app": "Discord", "title": "Alex", "body": "are you coming?", "count": 1, "seconds": 5}
        backend.CONFIG["shade_notifications"] = False
        self.ctl.notification.emit(note)
        pump(30)
        self.assertFalse(self.shade.isVisible())
        backend.CONFIG["shade_notifications"] = True
        self.ctl.notification.emit(note)
        pump(30)
        self.assertEqual((self.shade._label, self.shade._text), ("Discord", "Alex: are you coming?"))

    def test_a_notification_doesnt_cover_what_im_saying(self):
        backend.CONFIG["show_status_bar"] = False
        self.ctl.state.emit("speaking")
        self.reply()
        self.ctl.notification.emit({"app": "Discord", "title": "Alex", "body": "hi"})
        pump(30)
        self.assertEqual(self.shade._label, "Nova")

    def test_the_glow_follows_what_is_said(self):
        backend.CONFIG["show_status_bar"] = False
        self.shade.resize(1600, shade_module.WINDOW_PX)
        self.reply("Yes.")
        short = self.shade._glow_target
        self.reply("Here is a much longer answer that goes on for a while and wraps onto more than one line "
                   "of large text, so the glow should be both wider and deeper than before.")
        longer = self.shade._glow_target
        self.assertGreater(longer[0], short[0])
        self.assertGreater(longer[1], short[1])

    def test_preview_and_its_look(self):
        self.ctl.shade_preview.emit()                              # shows even with the bar on
        pump(60)
        self.assertTrue(self.shade.isVisible())
        self.assertEqual(self.shade._text, shade_module.PREVIEW_TEXT)
        self.shade.resize(1600, self.shade.height())
        pump(60)
        self.assertIsNone(check_screenshot("shade", self.shade))


if __name__ == "__main__":
    unittest.main()
