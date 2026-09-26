"""How one utterance finds its way to the right handler.

The point of these is *order*: every feature added to `_route_utterance` is another chance for
"what's my gate code" to be answered by a web search, or for "run work mode" to open an app called
"work". The agent is a stub, so anything reaching the tool-picker is visible as "TOOL:...".
"""

import tempfile
import unittest
from pathlib import Path

import fun
import memory_store
import routines
import system_control

from tests import common
from tests.common import backend


class FakeAgent:
    """Stands in for Needle: whatever reaches it comes back marked."""

    def __init__(self):
        self.seen = []

    def run(self, text):
        self.seen.append(text)
        return {"results": [{"status": f"TOOL:{text}"}]}

    def reset(self):
        pass


class Routing(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        memory_store.enable_persistence(Path(tempfile.mkdtemp()) / "memories.json")
        memory_store.reset()
        backend.clear_history()
        backend._PENDING.update(action="", argument=None, until=0.0)
        backend.CONFIG["routines"] = list(routines.EXAMPLES)
        self.agent = FakeAgent()
        self.performed = []
        self._real_perform = system_control.perform
        system_control.perform = lambda action, argument=None: (
            self.performed.append((action, argument)), f"did {action}")[1]

    def tearDown(self):
        system_control.perform = self._real_perform
        backend.CONFIG["routines"] = []

    def say(self, text):
        return backend.handle_utterance(self.agent, text)

    # ---- each feature is reachable -------------------------------------------------------
    def test_persona(self):
        self.assertIn("Arr", self.say("be a pirate"))
        self.assertEqual(backend.current_persona(), "pirate")
        self.say("act normal")
        self.assertEqual(backend.current_persona(), "default")

    def test_memory(self):
        self.assertIn("4821", self.say("remember that my gate code is 4821"))
        self.assertEqual(self.say("what's my gate code"), "Your gate code is 4821.")

    def test_conversion(self):
        self.assertIn("8.05", self.say("convert 5 miles to km"))

    def test_fun(self):
        self.assertIn(self.say("flip a coin"), ("Heads.", "Tails."))
        self.say("do a barrel roll")
        self.assertEqual(fun.pending_effect(), "barrel_roll")

    def test_system_control(self):
        self.assertEqual(self.say("set the volume to 40 percent"), "did set_volume")
        self.assertEqual(self.performed, [("set_volume", 40)])

    def test_routines(self):
        said = []
        backend.ROUTINE_HOOKS["say"] = said.append
        try:
            self.assertIn("enough for today", self.say("run wind down"))
        finally:
            backend.ROUTINE_HOOKS["say"] = None
        self.assertEqual(said, ["That's enough for today."])
        # ...and its "set the volume" step went through system control, not straight to the mixer.
        self.assertIn(("set_volume", 10), self.performed)

    def test_routine_listing(self):
        self.assertIn("work mode", self.say("what routines do i have"))

    def test_help(self):
        self.assertIn("remember", self.say("what can you do").lower())

    # ---- and nothing shadows anything else -----------------------------------------------
    def test_ordinary_questions_still_reach_the_tool_picker(self):
        for text in ("who was Ada Lovelace", "open chrome", "what's the weather in Paris"):
            self.assertTrue(self.say(text).startswith("TOOL:"), text)

    def test_a_failed_memory_recall_falls_through_instead_of_answering_wrongly(self):
        self.say("remember that my gate code is 4821")
        self.assertTrue(self.say("what's my shoe size").startswith("TOOL:"))

    def test_timers_beat_memory_for_reminders(self):
        reply = self.say("remind me in 10 minutes to call Sam")
        self.assertIn("remind you", reply)
        backend.cancel_timers()

    def test_math_is_not_hijacked_by_the_converter(self):
        self.assertIn("4", self.say("what is two plus two"))

    def test_a_disabled_feature_falls_through(self):
        # With the feature off, the utterance carries on down the router (to the tool-picker, or to
        # the chat lane when it reads like conversation) instead of being answered here.
        backend.CONFIG["fun_enabled"] = False
        saved = backend.ollama_generate, backend.ollama_generate_stream, backend.ollama_answer
        # A running Ollama may well answer "Heads." itself: keep it out of this.
        backend.ollama_generate = backend.ollama_answer = lambda *a, **k: "LLM"
        backend.ollama_generate_stream = lambda *a, **k: iter(["LLM"])
        try:
            self.assertNotIn(self.say("flip a coin"), ("Heads.", "Tails."))
        finally:
            backend.ollama_generate, backend.ollama_generate_stream, backend.ollama_answer = saved
        backend.CONFIG["memory_enabled"] = False
        self.assertTrue(self.say("remember that my gate code is 4821").startswith("TOOL:"))
        backend.CONFIG["system_control_enabled"] = False
        self.say("lock the pc")
        self.assertEqual(self.performed, [])

    def test_direct_commands_are_not_remembered_as_conversation(self):
        self.say("flip a coin")
        self.say("convert 5 miles to km")
        self.assertEqual(backend._HISTORY, [])      # follow-ups shouldn't refer to a dice roll

    def test_a_pending_confirmation_captures_the_next_yes(self):
        self.say("shut down the computer")
        self.assertEqual(backend.pending_confirmation(), "shutdown")
        self.assertEqual(self.say("yes"), "did shutdown")
        self.assertEqual(self.performed, [("shutdown", None)])

    def test_a_pending_confirmation_does_not_swallow_a_real_command(self):
        self.say("restart the pc")
        self.assertTrue(self.say("what's the weather").startswith("TOOL:"))
        self.assertEqual(self.performed, [])


class PromptBuilding(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        memory_store.enable_persistence(Path(tempfile.mkdtemp()) / "memories.json")
        memory_store.reset()

    def test_the_persona_is_added_to_the_system_prompt(self):
        plain = backend.get_ollama_system_prompt("hello")
        backend.CONFIG["persona"] = "pirate"
        with_persona = backend.get_ollama_system_prompt("hello")
        self.assertIn("pirate", with_persona.lower())
        self.assertGreater(len(with_persona), len(plain))

    def test_relevant_memories_are_offered_to_the_model(self):
        memory_store.remember("gate code", "4821")
        self.assertIn("4821", backend.get_ollama_system_prompt("what is my gate code again"))
        self.assertNotIn("4821", backend.get_ollama_system_prompt("what is the capital of France"))

    def test_memories_can_be_kept_out_of_the_prompt(self):
        memory_store.remember("gate code", "4821")
        backend.CONFIG["memory_in_prompt"] = False
        self.assertNotIn("4821", backend.get_ollama_system_prompt("what is my gate code"))

    def test_the_name_always_reaches_the_model(self):
        backend.CONFIG["assistant_name"] = "Jarvis"
        backend.CONFIG["ollama_system_prompt"] = "You are a helpful assistant."
        self.assertIn("Jarvis", backend.get_ollama_system_prompt(""))


if __name__ == "__main__":
    unittest.main()
