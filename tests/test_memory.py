"""Long-term memory (memory_store.py) and the clipboard history (clipboard.py).

Nothing here touches the real clipboard: entries are pushed in directly, which is what the watcher
thread would do anyway.
"""

import tempfile
import unittest
from pathlib import Path

import clipboard
import memory_store as memory

from tests import common  # noqa: F401  (offscreen Qt + a temp config)


class MemoryParsing(unittest.TestCase):
    def setUp(self):
        memory.enable_persistence(Path(tempfile.mkdtemp()) / "memories.json")
        memory.reset()

    def test_keyed_facts(self):
        self.assertEqual(memory.spoken_memory_command("remember that my wifi password is hunter2"),
                         ("remember", "wifi password", "hunter2"))
        self.assertEqual(memory.spoken_memory_command("remember my gate code is 4821"),
                         ("remember", "gate code", "4821"))

    def test_free_form_notes(self):
        self.assertEqual(memory.spoken_memory_command("remember I parked on level 3"),
                         ("remember", "", "I parked on level 3"))
        self.assertEqual(memory.spoken_memory_command("note that the meeting moved to Thursday"),
                         ("remember", "", "the meeting moved to Thursday"))

    def test_recall_shapes(self):
        for text, key in (("what's my wifi password", "wifi password"),
                          ("where did I park", "park"),
                          ("do you remember my gate code", "gate code"),
                          ("what do you remember about the meeting", "meeting"),
                          ("what did I tell you about the car", "car")):
            self.assertEqual(memory.spoken_memory_command(text), ("recall", key, ""), text)

    def test_list_and_forget(self):
        self.assertEqual(memory.spoken_memory_command("what do you remember"), ("list", "", ""))
        self.assertEqual(memory.spoken_memory_command("forget everything"), ("forget_all", "", ""))
        self.assertEqual(memory.spoken_memory_command("forget my wifi password"),
                         ("forget", "wifi password", ""))

    def test_other_features_are_left_alone(self):
        for text in ("remind me in an hour to call Sam", "remember to call mum in an hour",
                     "what is the weather", "what time is it", "play some jazz", "open chrome"):
            self.assertIsNone(memory.spoken_memory_command(text), text)

    def test_original_capitalisation_is_kept(self):
        memory.handle_memory_command("remember that my router is a TP-Link Archer")
        self.assertIn("TP-Link Archer", memory.handle_memory_command("what's my router"))


class MemoryStorage(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "memories.json"
        memory.enable_persistence(self.path)
        memory.reset()

    def test_store_and_recall(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        self.assertEqual(memory.handle_memory_command("what's my gate code"), "Your gate code is 4821.")

    def test_recall_matches_stems(self):
        memory.handle_memory_command("remember I parked on level 3 near the lift")
        self.assertIn("level 3", memory.handle_memory_command("where did I park"))

    def test_unknown_recall_falls_through(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        self.assertIsNone(memory.handle_memory_command("what's my shoe size"))

    def test_overwriting_a_key_replaces_it(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        memory.handle_memory_command("remember that my gate code is 9999")
        self.assertEqual(memory.count(), 1)
        self.assertIn("9999", memory.handle_memory_command("what's my gate code"))

    def test_forget_one_and_all(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        memory.handle_memory_command("remember that my wifi password is hunter2")
        self.assertIn("gate code", memory.handle_memory_command("forget my gate code"))
        self.assertEqual(memory.count(), 1)
        memory.handle_memory_command("forget everything")
        self.assertEqual(memory.count(), 0)

    def test_forgetting_nothing_falls_through(self):
        self.assertIsNone(memory.handle_memory_command("forget my gate code"))

    def test_it_survives_a_restart(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        self.assertTrue(self.path.exists())
        memory.enable_persistence(self.path)      # as a fresh run would
        self.assertEqual(memory.count(), 1)
        self.assertIn("4821", memory.handle_memory_command("what's my gate code"))

    def test_traits_reach_every_prompt(self):
        memory.handle_memory_command("remember I am vegetarian")
        memory.handle_memory_command("remember that my gate code is 4821")
        context = memory.context_for("what should I cook for dinner")
        self.assertIn("vegetarian", context)
        self.assertNotIn("4821", context)         # not relevant, so not offered

    def test_context_is_empty_without_a_match(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        self.assertEqual(memory.context_for("what's the capital of France"), "")

    def test_the_list_is_readable(self):
        memory.handle_memory_command("remember that my gate code is 4821")
        reply = memory.handle_memory_command("what do you remember")
        self.assertIn("1 thing", reply)
        self.assertIn("4821", reply)


class ClipboardHistory(unittest.TestCase):
    def setUp(self):
        clipboard.clear()
        clipboard.note_reply("")

    def tearDown(self):
        clipboard.clear()

    def test_history_order_and_recall(self):
        for text in ("first thing", "second thing", "third thing"):
            clipboard.record(text)
        self.assertIn("third thing", clipboard.handle_clipboard_command("what did I copy"))
        self.assertIn("second thing", clipboard.handle_clipboard_command("what did I copy before that"))
        self.assertIn("first thing",
                      clipboard.handle_clipboard_command("what did I copy before that before that"))

    def test_repeats_move_to_the_front_instead_of_piling_up(self):
        clipboard.record("a")
        clipboard.record("b")
        clipboard.record("a")
        self.assertEqual([e["text"] for e in clipboard.entries()], ["b", "a"])

    def test_secrets_are_kept_but_never_read_out(self):
        clipboard.record("Tr0ub4dor&3xkcd")
        spoken = clipboard.handle_clipboard_command("what did I copy")
        self.assertNotIn("Tr0ub4dor", spoken)
        self.assertIn("password", spoken)
        self.assertEqual(clipboard.entries()[-1]["text"], "Tr0ub4dor&3xkcd")   # still there to paste

    def test_long_entries_are_trimmed_for_speech(self):
        clipboard.record("word " * 400)
        self.assertLess(len(clipboard.handle_clipboard_command("what did I copy")), 400)

    def test_empty_history_says_so(self):
        self.assertIn("haven't seen", clipboard.handle_clipboard_command("what did I copy"))

    def test_copy_that_needs_something_to_copy(self):
        self.assertIn("worth copying", clipboard.handle_clipboard_command("copy that"))

    def test_not_a_clipboard_command(self):
        for text in ("open chrome", "what's the weather", "set a timer"):
            self.assertIsNone(clipboard.handle_clipboard_command(text), text)


if __name__ == "__main__":
    unittest.main()
