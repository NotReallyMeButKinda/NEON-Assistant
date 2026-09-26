"""The controller's own behaviour: dictation mode, the after-reply hooks and conversation mode.

`dictation.type_text` is replaced throughout, so no test ever sends a keystroke to whatever window
happens to have focus. The controller is built without `start()`, so no microphone, speaker, model
or notification watcher is created.
"""

import unittest

import clipboard
import dictation
import fun
from controller import Assistant

from tests import common
from tests.common import backend, pump


class ControllerCase(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.controller = Assistant()
        self.typed: list[str] = []
        self.backspaces: list[int] = []
        self._real_type, self._real_back = dictation.type_text, dictation.press_backspace
        dictation.type_text = lambda text: (self.typed.append(text), True)[1]
        dictation.press_backspace = self.backspaces.append
        self.messages: list[tuple] = []
        self.statuses: list[str] = []
        self.controller.message.connect(lambda who, text: self.messages.append((who, text)))
        self.controller.status.connect(self.statuses.append)

    def tearDown(self):
        dictation.type_text, dictation.press_backspace = self._real_type, self._real_back
        self.controller._dictation.stop()


class DictationMode(ControllerCase):
    def test_it_starts_stops_and_types_in_between(self):
        self.assertFalse(self.controller.dictating)
        self.controller._process("take dictation")
        pump(30)
        self.assertTrue(self.controller.dictating)

        self.controller._process("hello there comma how are you")
        self.assertEqual(self.typed, ["Hello there, how are you"])

        self.controller._process("stop dictation")
        self.assertFalse(self.controller.dictating)

    def test_commands_are_typed_not_obeyed_while_dictating(self):
        self.controller.set_dictation(True)
        self.controller._process("open chrome")
        self.assertEqual(self.typed, ["Open chrome"])

    def test_scratch_that_backspaces_exactly_what_was_typed(self):
        self.controller.set_dictation(True)
        self.controller._process("hello world")
        self.assertEqual(self.typed, ["Hello world"])
        self.controller._process("scratch that")
        self.assertEqual(self.backspaces, [len("Hello world")])

    def test_scratch_with_nothing_typed_says_so(self):
        self.controller.set_dictation(True)
        self.controller._process("scratch that")
        self.assertEqual(self.backspaces, [])
        self.assertTrue(any("nothing to take back" in s for s in self.statuses))

    def test_one_shot_typing_does_not_enter_the_mode(self):
        self.controller._process("type this: hello world")
        self.assertEqual(self.typed, ["Hello world"])
        self.assertFalse(self.controller.dictating)

    def test_the_first_phrase_has_no_leading_space(self):
        self.controller.set_dictation(True)
        self.controller._process("first")
        self.controller._process("second")
        self.assertEqual(self.typed, ["First", " Second"])


class AfterReply(ControllerCase):
    def test_the_reply_is_kept_for_copy_that(self):
        clipboard.clear()
        self.controller._after_reply("The answer is 42.")
        self.assertIn("Copied", clipboard.handle_clipboard_command("copy that") or "")

    def test_an_effect_reaches_the_ui(self):
        seen = []
        self.controller.effect.connect(seen.append)
        fun.handle_fun_command("do a barrel roll")
        self.controller._after_reply("Whee!")
        self.assertEqual(seen, ["barrel_roll"])

    def test_no_effect_means_no_signal(self):
        seen = []
        self.controller.effect.connect(seen.append)
        self.controller._after_reply("Nothing special.")
        self.assertEqual(seen, [])

    def test_a_persona_change_is_announced_once(self):
        seen = []
        self.controller.persona_changed.connect(seen.append)
        backend.CONFIG["persona"] = "noir"
        self.controller._after_reply("It was raining.")
        self.controller._after_reply("Still raining.")
        self.assertEqual(seen, ["noir"])

    def test_conversation_mode_is_off_by_default(self):
        self.controller._after_reply("Done.")
        pump(30)
        self.assertEqual(self.controller._conversation_until, 0.0)

    def test_conversation_mode_opens_a_window_when_switched_on(self):
        backend.CONFIG["conversation_mode"] = True
        self.controller._after_reply("Done.")
        self.assertGreater(self.controller._conversation_until, 0.0)
        pump(60)     # the follow-up thread finds no listener and gives up quietly


class Extras(ControllerCase):
    def test_the_clipboard_watcher_follows_its_setting(self):
        backend.CONFIG["clipboard_history"] = False
        self.controller._apply_clipboard_setting()
        self.assertFalse(self.controller._clipboard.running)
        backend.CONFIG["clipboard_history"] = True
        self.controller._apply_clipboard_setting()
        self.assertTrue(self.controller._clipboard.running)
        self.controller._clipboard.stop()

    def test_a_timer_warning_is_reported_without_a_speaker(self):
        self.controller._on_timer_warning("1 minute left on your timer.")
        self.assertIn(("system", "1 minute left on your timer."), self.messages)


if __name__ == "__main__":
    unittest.main()
