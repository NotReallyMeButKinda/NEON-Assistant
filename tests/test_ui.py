"""The windows: status bar, settings, onboarding, quick box. Offscreen, with a throwaway config."""

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog

from controller import Assistant
from tests import common
from tests.common import StubController, backend, pump, pump_until
from ui import status_bar as sb
from ui import theme
from ui.onboarding import OnboardingDialog
from ui.quick_input import QuickInput
from ui.settings_dialog import SettingsDialog

sb.StatusBar._register_appbar = lambda self: None          # never reserve real screen space in a test


class StatusBarTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG.update(bar_caption_seconds=0.4, bar_height=44)
        self.ctl = Assistant()
        self.bar = sb.StatusBar(self.ctl)
        self.bar.resize(1500, 44)
        self.bar.show()
        pump(80)

    def tearDown(self):
        self.bar.hide()
        # Hiding a bar doesn't stop its pollers, and a dozen leftover clock / weather / music
        # timers make everything after this test slower and less predictable.
        for timer in self.bar.findChildren(QTimer):
            timer.stop()

    def test_caption_rolls_in_out_and_streams_without_reanimating(self):
        ticker = self.bar.area.ticker
        self.ctl.caption.emit("You", "what's the weather like")
        pump(400)
        self.assertEqual((ticker._live.pos().y(), ticker._live.fx.opacity()), (0, 1.0))
        self.ctl.caption.emit("Dan", "It's 78 degrees.")
        pump(30)
        self.assertTrue(ticker._ghost.isVisible())                            # the user's line is sliding out
        pump(400)
        self.assertFalse(ticker._ghost.isVisible())
        self.assertEqual(ticker.text, "Dan: It's 78 degrees.")
        self.ctl.caption.emit("Dan", "It's 78 degrees. Wind is light.")       # same speaker: no new animation
        pump(30)
        self.assertEqual((ticker._live.pos().y(), ticker._live.fx.opacity()), (0, 1.0))
        self.ctl.state.emit("idle")
        self.assertTrue(pump_until(lambda: ticker.text == "", 4))

    def test_animation_can_be_switched_off(self):
        backend.CONFIG["bar_animate"] = False
        self.ctl.caption.emit("You", "instant")
        self.assertIsNone(self.bar.area.ticker._group)

    def test_notification_card(self):
        strip, answers = self.bar.strip, []
        self.ctl.answer_notification = answers.append
        self.ctl.notification.emit({"id": 5, "app": "Discord", "title": "Alex", "body": "are you free?", "count": 3,
                                    "seconds": 30, "ask": "speech"})
        pump(500)
        self.assertTrue(strip.isVisible())
        self.assertEqual((strip.app.text(), strip.more.text()), ("Discord", "+2 more"))
        self.assertGreater(strip.width(), 600)                                # spans the bar, whatever else is on it
        strip.summarize_btn.click()
        strip.dismiss_btn.click()
        self.assertEqual(answers, [True, False])
        self.assertTrue(strip.open_btn.isHidden())                            # no "can_open": nothing to open
        self.ctl.notification_settled.emit()
        self.assertTrue(pump_until(lambda: not strip.isVisible(), 3))
        self.ctl.notification.emit({"id": 6, "app": "Mail", "title": "Invoice", "body": "", "count": 1, "seconds": 2, "ask": "none"})
        pump(400)
        self.assertTrue(strip.summarize_btn.isHidden() and not strip.dismiss_btn.isHidden())   # the X always shows

    def test_notification_card_open_button(self):
        strip, opened = self.bar.strip, []
        self.ctl.open_notification_app = opened.append
        for ask in ("speech", "card", "none"):                                # every mode gets it
            self.ctl.notification.emit({"id": 5, "app": "Discord", "title": "Alex", "body": "hi", "count": 1,
                                        "seconds": 30, "ask": ask, "aumid": "d!app", "can_open": True})
            pump(100)
            self.assertFalse(strip.open_btn.isHidden(), ask)
        strip.open_btn.click()
        self.assertEqual(opened[0]["aumid"], "d!app")

    def test_elements_clock_and_stopwatch(self):
        bar = self.bar
        backend.CONFIG.update(bar_show_talk=False, bar_show_orb=False, bar_show_clock=True, bar_clock_seconds=False,
                              bar_show_stopwatch=True)
        bar.apply_config()
        pump(100)
        self.assertTrue(bar._buttons["bar_show_talk"].isHidden())
        self.assertTrue(bar.orb.isHidden())
        backend.CONFIG.update(bar_clock_format="24h", bar_clock_date=True)
        bar._tick_clock()
        self.assertRegex(bar.clock_label.text(), r"^\w{3} \w{3} \d+\s+\d\d:\d\d$")
        backend.CONFIG.update(bar_clock_format="12h", bar_clock_date=False)
        bar._tick_clock()
        self.assertRegex(bar.clock_label.text(), r"^\d{1,2}:\d\d [AP]M$")
        bar._stopwatch_toggle()
        pump(1600)                                      # the tick just before 1.0 s still reads 0:00
        self.assertNotEqual(bar.stopwatch.text(), "\u23f1 0:00")
        bar._stopwatch_toggle()
        frozen = bar.stopwatch.text()
        pump(600)
        self.assertEqual(bar.stopwatch.text(), frozen)
        bar._stopwatch_reset()
        self.assertEqual(bar.stopwatch.text(), "\u23f1 0:00")

    def test_calendar_and_system_meters(self):
        ics = Path(self.id().replace(".", "_") + ".ics")
        ics = Path(__import__("tempfile").mkdtemp()) / "c.ics"
        soon = (datetime.now() + timedelta(minutes=40)).strftime("%Y%m%dT%H%M%S")
        ics.write_text(f"BEGIN:VEVENT\nSUMMARY:Design review\nDTSTART:{soon}\nEND:VEVENT\n")
        backend.CONFIG.update(bar_show_calendar=True, calendar_ics=str(ics), bar_show_ram=True)
        self.bar.apply_config()
        self.assertTrue(pump_until(lambda: not self.bar.calendar_label.isHidden(), 8))
        self.assertIn("Design review", self.bar.calendar_label.text())
        self.assertRegex(self.bar.sys_label.text(), r"RAM \d+%")
        backend.CONFIG["calendar_ics"] = ""
        self.bar.apply_config()
        self.assertTrue(self.bar.calendar_label.isHidden())

    def test_timer_countdown_appears_only_while_a_timer_runs(self):
        import timers
        backend.CONFIG["bar_show_timer"] = True
        self.bar.apply_config()
        pump(50)
        self.assertTrue(self.bar.timer_label.isHidden())
        timers.start_timer(120, "pasta")
        try:
            self.bar._tick_timer()
            self.assertFalse(self.bar.timer_label.isHidden())
            self.assertIn("pasta", self.bar.timer_label.text())
            self.assertRegex(self.bar.timer_label.text(), r"\d+:\d\d")
        finally:
            timers.cancel_timers()
        self.bar._tick_timer()
        self.assertTrue(self.bar.timer_label.isHidden())

    def test_the_spectrum_follows_the_voice(self):
        backend.CONFIG["bar_show_spectrum"] = True
        self.bar.apply_config()
        pump(50)
        self.assertFalse(self.bar.spectrum.isHidden())
        self.ctl.spectrum.emit([0.9] * 9)
        pump(20)
        self.assertGreater(max(self.bar.spectrum._levels), 0.5)
        self.ctl.state.emit("idle")
        pump(20)
        self.assertEqual(max(self.bar.spectrum._levels), 0.0)

    def test_the_persona_badge_shows_only_a_non_default_persona(self):
        backend.CONFIG.update(bar_show_persona=True, persona="default")
        self.bar.apply_config()
        pump(30)
        self.assertTrue(self.bar.persona_label.isHidden())
        backend.CONFIG["persona"] = "pirate"
        self.bar.apply_config()
        pump(30)
        self.assertFalse(self.bar.persona_label.isHidden())
        self.assertIn("Pirate", self.bar.persona_label.text())

    def test_an_effect_plays_and_then_gets_out_of_the_way(self):
        self.ctl.effect.emit("barrel_roll")
        pump(30)
        self.assertTrue(self.bar.effects.running)
        self.assertFalse(self.bar.effects.isHidden())
        self.assertTrue(pump_until(lambda: not self.bar.effects.running, 4))
        self.assertTrue(self.bar.effects.isHidden())

    def test_an_unknown_effect_is_ignored(self):
        self.bar.play_effect("nonsense")
        pump(20)
        self.assertFalse(self.bar.effects.running)

    def test_party_mode_restores_the_users_theme(self):
        backend.CONFIG.update(theme="ember", theme_accent="")
        theme.apply_theme("ember", "", None)
        before = theme.COLORS["accent"]
        sb.RAINBOW_SECONDS, saved = 0.5, sb.RAINBOW_SECONDS   # don't sit through the real seven
        try:
            self.ctl.effect.emit("rainbow")
            pump(150)
            self.assertNotEqual(theme.COLORS["accent"], before)
            self.assertTrue(pump_until(lambda: theme.COLORS["accent"] == before, 10))
        finally:
            sb.RAINBOW_SECONDS = saved

    def test_placement_geometry(self):
        monitor = (0, 0, 2560, 1440, 1.0)
        self.assertEqual(sb.compute_rect(sb.ABE_TOP, monitor, 44), (0, 0, 2560, 44))
        self.assertEqual(sb.compute_rect(sb.ABE_BOTTOM, monitor, 44), (0, 1396, 2560, 1440))
        backend.CONFIG.update(bar_position="bottom", bar_monitor=99)          # a monitor that isn't there
        self.assertEqual(self.bar._edge(), sb.ABE_BOTTOM)
        self.assertEqual(self.bar._target_monitor(), sb.list_monitors()[0])


class NotificationHistoryTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend._NOTIFS.clear()
        self.ctl, self.calls = Assistant(), []
        for name in ("open_notification_app", "summarize_one_notification", "remind_about_notification"):
            setattr(self.ctl, name, lambda n, *rest, name=name: self.calls.append((name, n["id"])))
        from ui.notification_history import NotificationHistory
        self.panel = NotificationHistory(self.ctl)

    def tearDown(self):
        self.panel.close()
        backend._NOTIFS.clear()

    def test_empty_then_listed_newest_first(self):
        self.panel.show()
        pump(50)
        self.assertEqual(self.panel.list.count(), 0)
        self.assertFalse(self.panel.empty.isHidden())
        self.assertFalse(self.panel.open_btn.isEnabled())
        backend.note_notification({"id": 1, "app": "Discord", "title": "Alex", "body": "hi", "received": 0})
        backend.note_notification({"id": 2, "app": "Steam", "title": "Sale", "body": "", "received": 0})
        self.panel.reload()
        self.assertEqual(self.panel.list.count(), 2)
        self.assertTrue(self.panel.list.item(0).text().startswith("Steam"))
        self.assertTrue(self.panel.empty.isHidden())

    def test_buttons_act_on_the_selected_one(self):
        backend.note_notification({"id": 1, "app": "Discord", "title": "Alex", "body": "hi", "aumid": "d!a"})
        backend.note_notification({"id": 2, "app": "Steam", "title": "Sale", "body": ""})
        self.panel.show()
        pump(50)
        self.panel.list.setCurrentRow(1)                                     # Discord
        self.panel.open_btn.click()
        self.panel.summarize_btn.click()
        self.panel._remind(3600)
        self.assertEqual(self.calls, [("open_notification_app", 1), ("summarize_one_notification", 1),
                                 ("remind_about_notification", 1)])
        self.panel.clear_btn.click()
        self.assertEqual(self.panel.list.count(), 0)

    def test_ago(self):
        from ui.notification_history import ago
        self.assertEqual((ago(10), ago(300), ago(7200)), ("just now", "5 min ago", "2 hours ago"))


class FakeKeyCapture:
    """Stands in for hotkeys.KeyCapture: "installs" without touching the real keyboard."""

    def __init__(self, on_key):
        self.on_key = on_key
        self.installed = False

    def install(self):
        self.installed = True
        return True

    def uninstall(self):
        self.installed = False


class KeyCaptureTests(unittest.TestCase):
    """The hook's decisions, without installing it."""

    def setUp(self):
        import hotkeys
        self.hotkeys = hotkeys
        self.held = set()
        self._real_held = hotkeys._held
        hotkeys._held = lambda vk: vk in self.held
        self.got = []
        self.capture = hotkeys.KeyCapture(lambda vk, text, ctrl: self.got.append((vk, text, ctrl)))

    def tearDown(self):
        self.hotkeys._held = self._real_held

    def test_letters_are_taken_and_modifiers_pass_through(self):
        take = self.capture._take
        self.assertTrue(take(0x41, 0x1E, True))                 # A down: taken
        self.assertTrue(take(0x41, 0x1E, False))                # ...and its release
        self.assertFalse(take(0x10, 0x2A, True))                # Shift reaches the game
        self.assertFalse(take(0x42, 0x30, False))               # a release we never took passes
        pump(20)
        self.assertEqual([(vk, text.lower(), ctrl) for vk, text, ctrl in self.got], [(0x41, "a", False)])

    def test_system_shortcuts_are_left_alone(self):
        take = self.capture._take
        self.held = {0x12}                                      # Alt: Alt+Tab
        self.assertFalse(take(0x09, 0x0F, True))
        self.held = {0x5B}                                      # Win+D
        self.assertFalse(take(0x44, 0x20, True))
        self.held = {0x11}                                      # Ctrl+S is the game's; Ctrl+V is ours
        self.assertFalse(take(0x53, 0x1F, True))
        self.assertTrue(take(0x56, 0x2F, True))
        pump(20)
        self.assertEqual(self.got, [(0x56, "", True)])


class QuickBoxTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG["quick_reply_seconds"] = 1.0
        self.ctl = Assistant()
        self.sent = []
        self.ctl.submit = lambda text, echo=True: self.sent.append(text)
        self.given_back = []
        self._real_give_back = QuickInput.__dict__["_give_focus_back"]       # the staticmethod itself
        QuickInput._give_focus_back = staticmethod(self.given_back.append)    # never touch the real windows
        import hotkeys
        import ui.quick_input as quick_module
        self.quick_module, self.hotkeys = quick_module, hotkeys
        self._real_capture, self._real_fullscreen = hotkeys.KeyCapture, quick_module.fullscreen_screen
        hotkeys.KeyCapture = FakeKeyCapture          # never a real keyboard hook: it would take the user's typing
        self.fullscreen = None
        quick_module.fullscreen_screen = lambda: self.fullscreen
        self.box = QuickInput(self.ctl)
        self.box.open_box()
        pump(300)

    def tearDown(self):
        self.box.hide()
        QuickInput._give_focus_back = self._real_give_back
        self.hotkeys.KeyCapture = self._real_capture
        self.quick_module.fullscreen_screen = self._real_fullscreen

    def test_over_a_fullscreen_app_it_is_typed_into_without_taking_focus(self):
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        self.fullscreen = screen
        self.box.hide()
        pump(30)
        self.box.open_box()
        pump(300)
        self.given_back.clear()                               # (from closing the ordinary box opened in setUp)
        area = screen.geometry()                              # the whole screen: a fullscreen app hides the taskbar
        self.assertEqual(self.box.x(), area.x() + (area.width() - self.box.width()) // 2)
        self.assertTrue(self.box.capturing)
        self.assertTrue(self.box.testAttribute(Qt.WA_ShowWithoutActivating))
        self.assertEqual(self.box._return_to, 0)               # the game never lost focus: nothing to give back
        key = self.box._on_captured_key
        for char in "hi there":
            key(0x20 if char == " " else ord(char.upper()), char, False)
        key(0x08, "", False)                                   # Backspace
        key(0x25, "", False)                                   # Left
        key(0x41, "", True)                                    # Ctrl+A selects all...
        key(ord("X"), "x", False)                              # ...and typing replaces it
        self.assertEqual(self.box.edit.text(), "x")
        key(0x0D, "", False)                                   # Enter sends
        self.assertEqual(self.sent, ["x"])
        key(0x1B, "", False)                                   # Esc closes, and the keyboard is released
        self.assertFalse(self.box.isVisible())
        self.assertFalse(self.box.capturing)
        self.assertEqual(self.given_back, [])

    def test_without_a_fullscreen_app_it_takes_focus_as_usual(self):
        self.assertFalse(self.box.capturing)
        self.assertFalse(self.box.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_left_alone_over_a_game_it_closes_itself(self):
        from PySide6.QtGui import QGuiApplication
        self.fullscreen = QGuiApplication.primaryScreen()
        self.box.hide()
        self.box.open_box()
        self.box._capture_idle.start(50)
        pump(200)
        self.assertFalse(self.box.isVisible())
        self.assertFalse(self.box.capturing)

    def type_and_send(self, text):
        QTest.keyClicks(self.box.edit, text)
        QTest.keyClick(self.box.edit, Qt.Key_Return)
        pump(30)

    def test_height_follows_the_reply_and_resets_on_close(self):
        pump(300)
        empty = self.box.height()
        self.type_and_send("question")
        self.box._show_reply("word " * 60)
        tall = self.box.height()
        self.assertGreater(tall, empty + 40)
        self.box._show_reply("Short.")
        self.assertLess(self.box.height(), tall)                              # shrinks while open
        self.box._show_reply("")
        self.assertEqual(self.box.height(), empty)
        self.box._show_reply("word " * 60)
        self.box.hide()
        self.box.open_box()
        pump(300)
        self.assertEqual(self.box.height(), empty)                            # reopens at one line
        self.type_and_send("again")
        self.box._show_reply("word " * 60)
        self.box._hide_timer.start(10)                                        # the auto-hide after a reply
        pump(100)
        self.box.open_box()
        pump(300)
        self.assertEqual(self.box.height(), empty)

    def test_reply_appears_under_the_field_and_hides_after_the_delay(self):
        self.assertTrue(self.box.isVisible())
        self.type_and_send("what time is it")
        self.assertEqual(self.sent, ["what time is it"])
        self.assertTrue(self.box.isVisible())
        self.assertEqual((self.box.edit.text(), self.box.reply.text()), ("", "Thinking..."))
        self.ctl.caption.emit("You", "what time is it")                        # your own words aren't the reply
        pump(20)
        self.assertEqual(self.box.reply.text(), "Thinking...")
        self.ctl.caption.emit("Dan", "It's 4:24 PM.")
        pump(20)
        self.assertEqual(self.box.reply.text(), "It's 4:24 PM.")
        self.ctl.state.emit("idle")
        pump(300)
        self.assertTrue(self.box.isVisible())
        self.assertTrue(pump_until(lambda: not self.box.isVisible(), 3))

    def test_reply_streams_in_word_by_word_with_a_caret_until_it_is_finished(self):
        self.type_and_send("why is the sky blue")
        self.ctl.state.emit("speaking")                                        # the model starts writing
        for text in ("Because", "Because sunlight", "Because sunlight scatters."):
            self.ctl.caption.emit("Dan", text)
            pump(20)
            self.assertEqual(self.box.reply.text(), text + " ▍")
        self.ctl.state.emit("idle")                                            # finished: no caret
        self.box._show_reply("Because sunlight scatters.")
        self.assertEqual(self.box.reply.text(), "Because sunlight scatters.")

    def test_long_reply_is_shortened_and_fully_visible(self):
        self.type_and_send("tell me a story")
        self.ctl.caption.emit("Dan", "A long reply. " * 60)
        pump(30)
        self.assertLessEqual(len(self.box.reply.text()), 324)
        self.assertGreaterEqual(self.box.reply.height(), self.box.reply.heightForWidth(self.box.reply.width()))

    def test_escape_history_and_unrelated_replies(self):
        self.type_and_send("first")
        QTest.keyClick(self.box.edit, Qt.Key_Up)
        self.assertEqual(self.box.edit.text(), "first")
        QTest.keyClick(self.box.edit, Qt.Key_Escape)
        self.assertFalse(self.box.isVisible())
        self.box.open_box()
        self.ctl.caption.emit("Dan", "reply to a voice command")
        pump(20)
        self.assertTrue(self.box.reply.isHidden())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.path = common.use_temp_config()
        self.ctl = StubController()
        theme.apply_theme("ember", "", None)
        self.dlg = SettingsDialog(self.ctl)
        self.dlg.show()
        pump(200)

    def tearDown(self):
        self.dlg.reject()

    def on_disk(self):
        return json.loads(self.path.read_text())

    def test_pages_and_instant_pages(self):
        titles = [p.title for p in self.dlg._pages]
        self.assertEqual(titles[:4], ["General", "Appearance", "Status bar", "Voice & sounds"])
        self.assertEqual([p.title for p in self.dlg._pages if p.live],
                         ["Appearance", "Status bar", "Voice & sounds", "Persona"])

    def test_search_filters_pages_and_rows(self):
        d = self.dlg
        d.search.setText("quiet hours")
        pump(50)
        self.assertEqual([p.title for i, p in enumerate(d._pages) if not d.nav.item(i).isHidden()], ["Notifications"])
        d.search.setText("zzzzqq")
        pump(50)
        self.assertFalse(d.empty.isHidden())
        d.search.clear()
        pump(50)
        self.assertTrue(all(not r.isHidden() for p in d._pages for r in p._rows
                            if not getattr(r, "suppressed", False)))    # rows that only show when they apply

    def test_keyboard_shortcuts_focus_search_and_step_through_pages(self):
        d = self.dlg
        QTest.keyClick(d, Qt.Key_F, Qt.ControlModifier)
        pump(30)
        self.assertTrue(d.search.hasFocus())
        first = d.nav.currentRow()
        QTest.keyClick(d, Qt.Key_Tab, Qt.ControlModifier)
        self.assertEqual(d.nav.currentRow(), first + 1)
        QTest.keyClick(d, Qt.Key_Tab, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertEqual(d.nav.currentRow(), first)
        QTest.keyClick(d, Qt.Key_Tab, Qt.ControlModifier | Qt.ShiftModifier)     # wraps around to the last page
        self.assertEqual(d.nav.currentRow(), d.nav.count() - 1)

    def test_stepping_pages_skips_ones_hidden_by_the_search(self):
        d = self.dlg
        d.search.setText("quiet hours")                                          # only Notifications matches
        pump(50)
        here = d.nav.currentRow()
        d._step_page(1)
        self.assertEqual(d.nav.currentRow(), here)

    def test_a_reset_button_appears_only_when_a_row_differs_from_its_default(self):
        d = self.dlg
        row, default = d._rows_by_key["tts_speed"], backend.DEFAULT_CONFIG["tts_speed"]
        d._set["tts_speed"](default)
        self.assertTrue(row.reset.isHidden())
        d._set["tts_speed"](default + 0.5)
        self.assertFalse(row.reset.isHidden())
        row.reset.click()
        self.assertEqual(d._get["tts_speed"](), default)
        self.assertTrue(row.reset.isHidden())

    def test_text_choice_and_checkbox_rows_reset_too(self):
        d = self.dlg
        d._set["wake_engine"]("stt")                  # the wake word row is greyed out for a trained phrase
        d._set["name_is_wake_word"](False)
        d._set["notify_enabled"](True)                # ...and the notification rows while notifications are off
        for key, other in (("wake_word", "computer"), ("notify_silence", not backend.DEFAULT_CONFIG["notify_silence"]),
                           ("time_format", "24h" if backend.DEFAULT_CONFIG["time_format"] == "12h" else "12h")):
            row = d._rows_by_key[key]
            d._set[key](other)
            self.assertFalse(row.reset.isHidden(), key)
            row.reset.click()
            self.assertTrue(d._is_default(key), key)
            self.assertTrue(row.reset.isHidden(), key)

    def test_instant_page_applies_now_and_saves_after_the_debounce(self):
        d = self.dlg
        d._set["tts_speed"](1.4)
        pump(50)
        self.assertEqual(backend.CONFIG["tts_speed"], 1.4)                    # applied at once
        self.assertNotEqual(self.on_disk()["tts_speed"], 1.4)                 # ...saved a moment later
        pump(700)
        self.assertEqual(self.on_disk()["tts_speed"], 1.4)
        self.assertEqual(self.ctl.called("apply_live")[-1][1], (["tts_speed"],))

    def test_theme_changes_instantly(self):
        d = self.dlg
        d._set["theme"]("matrix")
        d._live_changed("theme")
        theme.apply_theme("matrix", "", None)
        pump(700)
        self.assertEqual(self.on_disk()["theme"], "matrix")

    def test_invalid_voice_is_not_applied(self):
        d = self.dlg
        d._set["tts_engine"]("piper")
        before = backend.CONFIG["tts_voice"]
        d._set["tts_voice"]("totally not a voice")
        d._live_changed("tts_voice")
        self.assertEqual(backend.CONFIG["tts_voice"], before)
        self.assertIn("isn't a Piper voice name", d.error.text())

    def test_other_pages_wait_for_save_and_save_keeps_instant_changes(self):
        d = self.dlg
        d._set["tts_volume"](0.7)
        pump(700)
        d._set["wake_word"]("computer")
        d._set["notify_rules"]([{"app": "Steam", "mode": "ignore"}])
        pump(700)
        self.assertNotEqual(self.on_disk()["wake_word"], "computer")          # not saved yet
        d._save()
        disk = self.on_disk()
        self.assertEqual((disk["wake_word"], disk["tts_volume"]), ("computer", 0.7))
        self.assertEqual(disk["notify_rules"], [{"app": "Steam", "mode": "ignore"}])
        self.assertTrue(self.ctl.called("settings_saved.emit"))

    def test_cancel_keeps_instant_changes_but_drops_unsaved_ones(self):
        d = self.dlg
        d._set["tts_volume"](0.55)
        d._set["wake_word"]("nothing saved")
        d.reject()
        disk = self.on_disk()
        self.assertEqual(disk["tts_volume"], 0.55)
        self.assertNotEqual(disk["wake_word"], "nothing saved")

    def test_hotkey_validation(self):
        d = self.dlg
        d._set["hotkey_talk"]("Ctrl+Alt+V")
        d._set["hotkey_wake"]("Ctrl+Alt+V")
        d._save()
        self.assertIn("can't share", d.error.text())
        self.assertEqual(d._pages[d.stack.currentIndex()].key, "hotkeys")

    def test_export_and_import(self):
        d = self.dlg
        target = self.path.parent / "export.json"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))
        d._export_settings()
        exported = json.loads(target.read_text())
        exported.update(tts_speed=99, wake_word="hello there", bar_height="12", bogus_key=1)
        target.write_text(json.dumps(exported))
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(target), ""))
        d._import_settings()
        self.assertEqual(d._get["wake_word"](), "hello there")
        self.assertEqual(d._get["tts_speed"](), 2.0)                           # clamped, not 99
        self.assertEqual(d._get["bar_height"](), 30)                           # "12" coerced, then clamped
        self.assertNotEqual(backend.CONFIG["wake_word"], "hello there")        # importing doesn't save by itself

    def test_restore_page_defaults_on_an_instant_page(self):
        d = self.dlg
        d.nav.setCurrentRow(3)
        d._set["tts_speed"](1.9)
        d._live_changed("tts_speed")
        d._restore_page_defaults()
        pump(700)
        self.assertEqual(self.on_disk()["tts_speed"], backend.DEFAULT_CONFIG["tts_speed"])

    def test_rows_that_dont_apply_are_greyed_out(self):
        d = self.dlg
        row = d._row_of
        d._set["stt_engine"]("google")
        self.assertFalse(row(d._rows_by_key["whisper_model"]).isEnabled())
        d._set["stt_engine"]("whisper")
        self.assertTrue(row(d._rows_by_key["whisper_model"]).isEnabled())
        d._set["tts_engine"]("sapi")
        self.assertFalse(d._rows_by_key["tts_speed"].isEnabled())
        self.assertTrue(d._rows_by_key["sapi_rate"].isEnabled())
        d._set["tts_engine"]("piper")
        self.assertTrue(d._rows_by_key["tts_speed"].isEnabled())
        self.assertFalse(d._rows_by_key["sapi_rate"].isEnabled())
        d._set["show_status_bar"](False)
        self.assertFalse(d._rows_by_key["bar_height"].isEnabled())
        self.assertFalse(d.bar_spectrum.isEnabled())

    def test_old_ignore_list_becomes_rules(self):
        d = self.dlg
        d._set["notify_rules"]([{"app": "Discord", "mode": "ask"}])
        d._set["notify_ignore"]("Steam, discord")
        self.assertEqual(d._get["notify_rules"](), [{"app": "Steam", "mode": "ignore"}, {"app": "Discord", "mode": "ask"}])
        d._save()
        disk = self.on_disk()
        self.assertEqual((disk["notify_ignore"], disk["notify_rules"][0]), ("", {"app": "Steam", "mode": "ignore"}))

    def test_routine_editor_flags_keys_windows_wont_press(self):
        from ui.settings_widgets import RoutinesEditor
        editor = RoutinesEditor()
        editor.add_routine("test", ["keys: win+d"])
        self.assertTrue(editor.step_note.isHidden())
        editor.steps.setPlainText("keys: win+d\nkeys: ctrl+alt+del")
        self.assertIn("Line 2", editor.step_note.text())
        self.assertIn("Ctrl+Alt+Del", editor.step_note.text())
        editor.key_record.setKeySequence(QKeySequence("Ctrl+Shift+Esc"))
        editor._add_key_step()
        self.assertEqual(editor.value()[0]["steps"][-1], "keys: ctrl+shift+esc")

    def test_disabled_controls_look_disabled(self):
        self.assertIn("disabled", theme.build_stylesheet())


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.path = common.use_temp_config()
        backend.CONFIG.update(onboarding_done=False, assistant_name="Dan", tts_engine="sapi", theme="ember")
        theme.apply_theme("ember", "", None)
        self.ctl = StubController()
        self.wiz = OnboardingDialog(self.ctl)
        self.wiz.show()
        pump(200)

    def tearDown(self):
        if self.wiz.isVisible():
            self.wiz.reject()

    def go_to(self, name):
        for _ in range(12):
            if self.wiz._pages[self.wiz.pages.currentIndex()][0] == name:
                return
            self.wiz._go(1)
        self.fail(f"never reached {name}")

    @staticmethod
    def pick(group, value):
        next(b for b in group.buttons() if b.property("value") == value).click()

    def test_the_steps(self):
        self.assertEqual([t for t, _ in self.wiz._pages],
                         ["Welcome", "Hearing you", "My voice", "My brain", "Look", "Connections", "Shortcuts",
                          "All set"])

    def test_finish_saves_choices_and_marks_onboarding_done(self):
        w = self.wiz
        w.name.setText("Jarvis")
        self.go_to("Hearing you")
        self.pick(w.wake_group, "name")
        self.go_to("My voice")
        self.pick(w.voice_group, "en_US-ryan-high")
        w.listen_sound.setChecked(False)
        self.go_to("Look")
        self.pick(w.theme_group, "matrix")
        w.titlebar.setChecked(False)
        self.go_to("Connections")
        w.notify.setChecked(True)
        w.browser.setChecked(False)
        self.go_to("Shortcuts")
        w._seqs["hotkey_quick"].setKeySequence(QKeySequence("Ctrl+Alt+Q"))
        self.pick(w.copilot_group, "quick")
        self.go_to("All set")
        summary = w.summary.text()
        for part in ("Jarvis", "\"jarvis\"", "Copilot key: quick command box", "notifications"):
            self.assertIn(part, summary)
        w._go(1)
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved["onboarding_done"])
        self.assertEqual((saved["assistant_name"], saved["wake_word"], saved["wake_engine"]), ("Jarvis", "jarvis", "stt"))
        self.assertEqual((saved["tts_engine"], saved["tts_voice"], saved["listen_sound"]),
                         ("sapi", "en_US-ryan-high", "off"))               # the setUp config uses the Windows voice
        self.assertEqual((saved["theme"], saved["custom_titlebar"], saved["notify_enabled"]), ("matrix", False, True))
        self.assertEqual((saved["hotkey_quick"], saved["copilot_key_action"], saved["copilot_key_enabled"]),
                         ("Ctrl+Alt+Q", "quick", True))
        self.assertTrue(self.ctl.called("apply_settings"))

    def test_the_voice_step(self):
        w = self.wiz
        self.go_to("My voice")
        w.use_windows.setChecked(False)
        self.pick(w.voice_group, "en_US-lessac-high")
        self.assertEqual(w._chosen_voice(), "en_US-lessac-high")
        w.more_voice.set_value("en_US-kathleen-low")                   # the full list replaces the featured pick
        self.assertIsNone(w.voice_group.checkedButton())
        self.assertEqual(w._chosen_voice(), "en_US-kathleen-low")
        w.more_voice.set_value("nonsense")
        w._go(1)
        self.assertEqual(w._pages[w.pages.currentIndex()][0], "My voice")
        self.assertIn("isn't a Piper voice", w.error.text())
        self.pick(w.voice_group, "en_US-amy-medium")                    # clicking a card clears the list's choice
        self.assertEqual(w.more_voice.value(), "")
        w._go(1)
        self.assertEqual(w._pages[w.pages.currentIndex()][0], "My brain")

    def test_shortcut_collision_with_settings_blocks_next(self):
        backend.CONFIG["hotkey_mute"] = "Ctrl+Alt+M"
        self.go_to("Shortcuts")
        self.wiz._seqs["hotkey_talk"].setKeySequence(QKeySequence("Ctrl+Alt+M"))
        self.wiz._go(1)
        self.assertEqual(self.wiz._pages[self.wiz.pages.currentIndex()][0], "Shortcuts")
        self.assertIn("already used", self.wiz.error.text())

    def test_skip_undoes_the_theme_preview_but_remembers_it_was_shown(self):
        self.go_to("Look")
        self.pick(self.wiz.theme_group, "mono")
        self.assertEqual(theme.current_theme(), "mono")
        self.wiz.reject()
        self.assertEqual(theme.current_theme(), "ember")
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["onboarding_done"], saved["theme"]), (True, "ember"))

    def test_the_microphone_only_opens_while_testing(self):
        self.go_to("Hearing you")
        self.assertIsNone(self.wiz._mic_stream)                         # showing the page opens nothing


class TitleBarTests(unittest.TestCase):
    def test_the_custom_bar_can_be_switched_off_and_on(self):
        from ui import frame
        common.use_temp_config()
        dlg = SettingsDialog(StubController())
        dlg.show()
        pump(150)
        self.assertIsNotNone(dlg._neon_titlebar)
        self.assertEqual([b.kind for b in dlg._neon_titlebar.buttons], ["min", "max", "close"])
        self.assertEqual(dlg.contentsMargins().top(), frame.BAR_HEIGHT)
        self.assertTrue(dlg.windowFlags() & Qt.FramelessWindowHint)
        backend.CONFIG["custom_titlebar"] = False
        frame.refresh_all()
        pump(100)
        self.assertIsNone(dlg._neon_titlebar)
        self.assertEqual(dlg.contentsMargins().top(), 0)
        self.assertFalse(dlg.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(dlg.isVisible())                                 # switching doesn't close anything
        backend.CONFIG["custom_titlebar"] = True
        frame.refresh_all()
        pump(100)
        self.assertIsNotNone(dlg._neon_titlebar)
        dlg.reject()

    def test_native_from_the_start(self):
        common.use_temp_config()
        backend.CONFIG["custom_titlebar"] = False
        dlg = SettingsDialog(StubController())
        self.assertIsNone(getattr(dlg, "_neon_titlebar", None))
        dlg.reject()


class ModelPickerTests(unittest.TestCase):
    def test_installed_first_then_suggestions_and_any_name(self):
        from ui import model_picker
        common.use_temp_config()
        original = model_picker.installed_models
        model_picker.installed_models = lambda: {"qwen3:8b": "5.2 GB", "llama3.2:latest": "2.0 GB"}
        self.addCleanup(setattr, model_picker, "installed_models", original)
        picker = model_picker.ModelPicker("llama3.2")
        items = [picker.combo.itemText(i) for i in range(picker.combo.count())]
        self.assertEqual(items[:3], ["On this PC", "llama3.2:latest", "qwen3:8b"])
        self.assertIn("Download", items)
        self.assertNotIn("qwen3:8b", items[items.index("Download"):])     # installed ones aren't offered again
        self.assertEqual(picker.value(), "llama3.2:latest")               # "llama3.2" is the same model
        self.assertFalse(picker.download.isVisibleTo(picker))
        picker.set_value("gemma3:12b")
        self.assertIn("Not downloaded yet", picker.info.text())
        self.assertTrue(picker.download.isVisibleTo(picker))
        picker.set_value("someone/custom-model:7b")
        self.assertEqual(picker.value(), "someone/custom-model:7b")


class NoClippingTests(unittest.TestCase):
    """The regression test for 'text is cut off': wrapped labels get the height they need."""

    def test_wizard_pages_show_all_their_text(self):
        common.use_temp_config()
        wiz = OnboardingDialog(StubController())
        wiz.show()
        pump(200)
        from PySide6.QtWidgets import QLabel
        for i in range(len(wiz._pages)):
            wiz._show_page(i)
            pump(200)
            page = wiz.pages.currentWidget().widget()
            for label in page.findChildren(QLabel):
                if label.wordWrap() and not label.isHidden() and label.text():
                    self.assertGreaterEqual(label.height() + 2, label.heightForWidth(label.width()),
                                            f"{wiz._pages[i][0]}: {label.text()[:40]!r} is clipped")
        wiz.reject()

    def test_chat_system_lines_show_all_their_text(self):
        from ui.widgets import ChatView
        view = ChatView()
        view.resize(700, 500)
        view.show()
        pump(100)
        view.add_message("system", "Notification from Discord: someone - hey are you coming tonight? we're "
                                   "meeting at the usual place around eight, bring snacks please and the thing")
        for width in (700, 520):                       # and still after a resize
            view.resize(width, 500)
            pump(100)
            line = view._system_lines[0]
            self.assertGreater(line.heightForWidth(line.width()), line.fontMetrics().height() * 2)
            self.assertGreaterEqual(line.height(), line.heightForWidth(line.width()))
        view.close()


class DialogModalityTests(unittest.TestCase):
    """An application-modal dialog disables the status bar (clicks on it only played the error sound)."""

    def test_settings_blocks_nothing_without_a_parent_and_only_its_parent_with_one(self):
        from PySide6.QtWidgets import QWidget
        common.use_temp_config()
        dlg = SettingsDialog(StubController())
        self.assertEqual(dlg.windowModality(), Qt.NonModal)
        dlg.close()
        parent = QWidget()
        dlg = SettingsDialog(StubController(), parent)
        self.assertEqual(dlg.windowModality(), Qt.WindowModal)
        dlg.close()
        parent.deleteLater()

    def test_a_second_open_raises_the_first(self):
        common.use_temp_config()
        self.assertFalse(SettingsDialog.raise_open())
        dlg = SettingsDialog(StubController())
        dlg.show()
        pump(100)
        self.assertTrue(SettingsDialog.raise_open())
        dlg.reject()
        pump(100)
        self.assertFalse(SettingsDialog.raise_open())

    def test_onboarding_without_a_parent_is_not_modal(self):
        common.use_temp_config()
        wiz = OnboardingDialog(StubController())
        self.assertEqual(wiz.windowModality(), Qt.NonModal)
        wiz.close()


if __name__ == "__main__":
    unittest.main()
