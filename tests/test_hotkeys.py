import threading
import time
import unittest

import ctypes

import hotkeys

from tests.common import live_only, pump


class ParseTests(unittest.TestCase):
    def test_valid_and_invalid_combinations(self):
        self.assertEqual(hotkeys.parse_hotkey("Ctrl+Alt+V"), (hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, ord("V")))
        self.assertEqual(hotkeys.parse_hotkey("Ctrl+Shift+F2")[1], 0x71)
        self.assertEqual(hotkeys.parse_hotkey("F13"), (0, 0x7C))               # function keys may stand alone
        for bad in ("", "Bogus+Q", "Ctrl+Alt", "V", "Ctrl+Nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                hotkeys.parse_hotkey(bad)

    def test_manager_reports_errors_and_skips_unmapped(self):
        manager = hotkeys.HotkeyManager()
        errors = manager.apply([("Talk", "", lambda: None), ("Wake", "Bogus+Q", lambda: None)])
        self.assertEqual(len(errors), 1)
        self.assertIn("Wake", errors[0])
        manager.unregister()

    def test_hold_hook_rejects_a_bad_combo_and_can_be_switched_off(self):
        hook = hotkeys.HoldKeyHook(lambda: None, lambda: None)
        self.assertIn("Hold to talk", hook.set_combo("Bogus+Q"))
        self.assertIsNone(hook.set_combo(""))
        self.assertFalse(hook.installed)


@live_only
class LiveHookTests(unittest.TestCase):
    """Real low-level hooks with a harmless key (F13), driven by synthetic key events."""

    def keys(self, *sequence):
        user32 = ctypes.windll.user32
        for vk, up, pause in sequence:
            user32.keybd_event(vk, 0, 2 if up else 0, 0)
            time.sleep(pause)

    def test_copilot_hook_fires_once_and_swallows_repeats(self):
        fired = []
        hook = hotkeys.CopilotKeyHook(lambda: fired.append(1), keys=(0x7C,))
        self.assertTrue(hook.install())
        try:
            threading.Thread(target=lambda: self.keys((0x5B, False, 0), (0x7C, False, 0), (0x7C, False, 0),
                                                       (0x7C, True, 0), (0x5B, True, 0.2)), daemon=True).start()
            pump(900)
            self.assertEqual(len(fired), 1)
        finally:
            hook.uninstall()

    def test_hold_hook_press_and_release(self):
        events = []
        hook = hotkeys.HoldKeyHook(lambda: events.append("press"), lambda: events.append("release"))
        self.assertIsNone(hook.set_combo("Ctrl+Alt+F13"))
        try:
            threading.Thread(target=lambda: self.keys(
                (0x7C, False, 0.1), (0x7C, True, 0.1),                          # F13 alone: ignored
                (0x11, False, 0), (0x12, False, 0), (0x7C, False, 0.4), (0x7C, False, 0.05), (0x7C, True, 0.1),
                (0x12, True, 0), (0x11, True, 0.1)), daemon=True).start()
            pump(1500)
            self.assertEqual(events, ["press", "release"])
        finally:
            hook.uninstall()


if __name__ == "__main__":
    unittest.main()
