"""Your own name ("call me Alex"), the beep maker (sounds you make from a few notes), and the Unsure persona.
Nothing is played: the preview is stubbed."""

import unittest

import numpy as np

import persona
import sounds

from tests import common
from tests.common import backend, pump
from tests.test_finish_pass import Patch


class UserNameTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_set_ask_and_forget(self):
        say = backend.handle_user_name
        self.assertIn("don't know your name", say("what's my name"))
        self.assertEqual(say("call me alex"), "Nice to meet you, Alex.")
        self.assertEqual(backend.CONFIG["user_name"], "Alex")
        self.assertEqual(say("who am I?"), "You're Alex.")
        self.assertEqual(say("please call me Jo from now on"), "Nice to meet you, Jo.")
        self.assertEqual(say("my name is sam taylor"), "Nice to meet you, Sam Taylor.")
        self.assertEqual(say("forget my name"), "Okay, I won't use your name.")
        self.assertEqual(backend.CONFIG["user_name"], "")

    def test_things_that_arent_a_name(self):
        for text in ("call me later", "call me a taxi", "call me when you're done", "call yourself jarvis",
                     "call me back tomorrow"):
            self.assertIsNone(backend.handle_user_name(text), text)
        self.assertIn("That's my name", backend.handle_user_name("call me nova"))

    def test_the_ai_is_told_and_it_is_routed(self):
        backend.CONFIG["user_name"] = "Alex"
        self.assertIn("The user's name is Alex.", backend.get_ollama_system_prompt())
        backend.CONFIG["user_name"] = ""
        self.assertNotIn("The user's name", backend.get_ollama_system_prompt())
        self.assertEqual(backend.handle_utterance(None, "Call me Riley."), "Nice to meet you, Riley.")


class BeepTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.played = []
        Patch(self)(sounds, "play_samples", lambda samples, device=None: self.played.append(samples) or True)

    def test_notes_and_recipes(self):
        self.assertAlmostEqual(sounds.note_freq("A4"), 440.0)
        self.assertAlmostEqual(sounds.note_freq("C5"), 523.25, places=1)
        self.assertEqual((sounds.NOTE_NAMES[0], sounds.NOTE_NAMES[-1]), ("C3", "C7"))
        clean = sounds.clean_recipe({"tone": "?", "notes": [["Z9", 9999]] * 9, "gap_ms": -5, "echo": 1})
        self.assertEqual(clean, {"tone": "bell", "notes": [["A4", 800]] * 6, "gap_ms": 0, "echo": True})
        self.assertEqual(sounds.clean_recipe(None)["notes"], [["A5", 200]])
        for _label, recipe in sounds.PRESETS:
            samples = sounds.render_recipe(recipe, 0.5)
            self.assertTrue(0.1 < len(samples) / sounds.SAMPLE_RATE < 2.5)
            self.assertLessEqual(int(np.abs(samples.astype(np.int32)).max()), int(0.5 * 0.9 * 32767) + 1)

    def test_a_saved_sound_plays_by_name_and_falls_back_when_deleted(self):
        backend.CONFIG["custom_beeps"] = {"Ping pong": sounds.PRESETS[2][1]}
        self.assertIn("beep:Ping pong", sounds.custom_sounds())
        self.assertEqual(len(sounds.render("beep:Ping pong")), len(sounds.render_recipe(sounds.PRESETS[2][1])))
        self.assertEqual(len(sounds.render("beep:gone")), len(sounds.render(sounds.DEFAULT_SOUND)))

    def test_the_editor_makes_saves_and_lists_a_sound(self):
        from ui.beep_maker import BeepMaker
        from ui.sound_picker import SoundPicker
        maker = BeepMaker(None, "", 0.5)
        maker.load({"tone": "pluck", "notes": [["C5", 100]], "gap_ms": 10, "echo": False})
        maker.add_note.click()
        self.assertEqual(maker.recipe()["notes"], [["C5", 100], ["E5", 100]])     # a step up, same length
        for _ in range(8):
            maker.add_note.click()
        self.assertEqual(len(maker._rows), sounds.MAX_NOTES)
        self.assertFalse(maker.add_note.isEnabled())
        maker.play_btn.click()
        self.assertEqual(len(self.played), 1)
        maker.name.setText("")
        maker._save()
        self.assertTrue(maker.message.isVisibleTo(maker))                       # needs a name
        maker.name.setText("Blip blop")
        maker._save()
        self.assertEqual(maker.saved_key, "beep:Blip blop")
        self.assertEqual(backend.CONFIG["custom_beeps"]["Blip blop"]["tone"], "pluck")
        picker = SoundPicker(allow_off=True)
        self.assertGreaterEqual(picker.combo.findData("beep:Blip blop"), 0)
        picker.set_name("beep:Blip blop")
        self.assertEqual(picker.make_btn.text(), "Edit sound...")
        maker.deleteLater()
        picker.deleteLater()
        pump(10)


class UnsurePersonaTests(unittest.TestCase):
    def test_it_exists_and_is_reachable_by_voice(self):
        self.assertIn("unsure", persona.ORDER)
        self.assertIn("aren't sure you're really unsure", persona.prompt_suffix("unsure"))
        for text in ("be unsure", "switch to nervous mode", "be indecisive", "talk like you are not sure"):
            self.assertEqual(persona.spoken_persona_command(text), ("set", "unsure"), text)
        self.assertIn("Unsure", persona.catalogue())


if __name__ == "__main__":
    unittest.main()
