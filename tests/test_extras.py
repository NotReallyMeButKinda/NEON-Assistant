"""Personas, unit conversions, the fun commands and dictation's text handling.

All pure text in, text out -- nothing here types, speaks or touches the system. The one exception
is a structure-size check for SendInput, which is the failure that would silently make dictation
type nothing at all.
"""

import ctypes
import re
import unittest

import dictation
import fun
import persona
import units

from tests import common  # noqa: F401  (offscreen Qt + a temp config)


class Personas(unittest.TestCase):
    def test_switching(self):
        cases = {"be a pirate": "pirate", "talk like shakespeare": "shakespeare",
                 "switch to noir mode": "noir", "be a hype coach": "coach",
                 "pretend to be a gremlin": "gremlin", "use the butler voice": "butler",
                 "sound like a robot": "robot", "channel your inner scientist": "scientist",
                 "persona: zen": "zen", "give me the haiku mode": "haiku"}
        for text, key in cases.items():
            self.assertEqual(persona.spoken_persona_command(text), ("set", key), text)

    def test_going_back_to_normal(self):
        for text in ("act normal", "be yourself", "drop the act", "knock it off",
                     "stop the silly voice", "back to normal"):
            self.assertEqual(persona.spoken_persona_command(text), ("set", persona.DEFAULT), text)

    def test_listing_and_asking(self):
        self.assertEqual(persona.spoken_persona_command("what personas do you have"), ("list", ""))
        self.assertEqual(persona.spoken_persona_command("which personality are you in"), ("current", ""))

    def test_ordinary_speech_is_not_a_persona_command(self):
        for text in ("stop it", "stop talking", "be quiet", "what is the weather",
                     "play some jazz", "open chrome", "set a timer for five minutes"):
            self.assertIsNone(persona.spoken_persona_command(text), text)

    def test_every_persona_is_complete(self):
        for key in persona.ORDER:
            entry = persona.PERSONAS[key]
            self.assertTrue(entry["label"] and entry["blurb"] and entry["confirm"], key)
            self.assertTrue(0.5 <= entry["rate"] <= 1.5, key)
            self.assertTrue(persona.greeting(key))
        self.assertEqual(persona.PERSONAS[persona.DEFAULT]["prompt"], "")   # the default adds nothing

    def test_unknown_names_fall_back_to_the_default(self):
        self.assertEqual(persona.valid("wizard"), persona.DEFAULT)
        self.assertEqual(persona.valid(None), persona.DEFAULT)
        self.assertEqual(persona.prompt_suffix("wizard"), "")


class Conversions(unittest.TestCase):
    def assertConverts(self, text, contains):
        reply = units.handle_conversion(text)
        self.assertIsNotNone(reply, text)
        self.assertIn(contains, reply, f"{text!r} -> {reply!r}")

    def test_length_mass_and_volume(self):
        self.assertConverts("convert 5 miles to km", "8.05")
        self.assertConverts("5 kg in pounds", "11.02")
        self.assertConverts("1 cup in ml", "236.59")
        self.assertConverts("how many feet in a mile", "5,280")

    def test_temperature_is_not_a_simple_factor(self):
        self.assertConverts("what is 180 f in c", "82.22")
        self.assertConverts("20 c to f", "68")
        self.assertConverts("300 kelvin in celsius", "26.85")

    def test_spoken_numbers(self):
        self.assertConverts("three miles in kilometres", "4.83")

    def test_data_speed_and_energy(self):
        self.assertConverts("2 gb in mb", "2,000")
        self.assertConverts("100 km/h in mph", "62.14")
        self.assertConverts("2000 kcal in kj", "8,368")

    def test_number_bases(self):
        self.assertConverts("convert 255 to hex", "0xFF")
        self.assertConverts("convert 255 to binary", "0b11111111")
        self.assertConverts("what is 0xff in decimal", "255")

    def test_mismatched_units_say_so(self):
        self.assertIn("different things", units.handle_conversion("convert 5 miles to kilograms"))

    def test_big_and_small_numbers_stay_readable(self):
        self.assertEqual(units.pretty(1234.5), "1,234")
        self.assertEqual(units.pretty(8.046), "8.05")
        self.assertEqual(units.pretty(0.5), "0.5")
        self.assertIn("power of", units.pretty(9.4e20))

    def test_ordinary_speech_is_not_a_conversion(self):
        for text in ("how long is a day", "what is the time", "open chrome",
                     "what is 2 plus 2", "set a timer for 5 minutes", "play some music"):
            self.assertIsNone(units.handle_conversion(text), text)

    def test_every_unit_converts_to_its_own_dimension(self):
        for dimension, names in units.dimensions().items():
            base = names[0]
            for name in names:
                value, _s, _p = units.convert(1.0, name, base)
                self.assertGreater(abs(value), 0, f"{name} -> {base} ({dimension})")


class Fun(unittest.TestCase):
    def setUp(self):
        fun.pending_effect()            # start each test with no effect queued

    def test_coin_and_dice(self):
        self.assertIn(fun.handle_fun_command("flip a coin"), ("Heads.", "Tails."))
        for _ in range(20):
            reply = fun.handle_fun_command("roll a d6")
            self.assertRegex(reply, r"^[1-6]\.$")
        self.assertRegex(fun.handle_fun_command("roll 3 dice"), r"^\d+, \d+, \d+ -- \d+ in total\.$")

    def test_absurd_dice_are_refused_politely(self):
        self.assertIn("reasonable", fun.handle_fun_command("roll 900 dice"))

    def test_random_number_respects_its_range(self):
        for _ in range(20):
            value = int(fun.handle_fun_command("pick a random number between 5 and 7").strip("."))
            self.assertIn(value, (5, 6, 7))

    def test_choosing_between_options(self):
        for _ in range(10):
            self.assertIn(fun.handle_fun_command("pick pizza or curry"), ("Pizza.", "Curry."))

    def test_jokes_facts_and_friends(self):
        for text in ("tell me a joke", "tell me a fact", "say something nice", "read my fortune",
                     "magic 8 ball will it rain", "what is the meaning of life",
                     "open the pod bay doors", "are you human", "sing me a song"):
            self.assertTrue(fun.handle_fun_command(text), text)

    def test_effects_are_queued_for_the_ui(self):
        for text, effect in (("do a barrel roll", "barrel_roll"), ("party mode", "rainbow"),
                             ("the matrix", "matrix"), ("confetti", "confetti"),
                             ("shake it off", "shake")):
            self.assertTrue(fun.handle_fun_command(text), text)
            self.assertEqual(fun.pending_effect(), effect, text)
            self.assertEqual(fun.pending_effect(), "")     # read once, then gone

    def test_every_effect_name_is_one_the_ui_knows(self):
        for text in ("do a barrel roll", "party mode", "the matrix", "confetti", "shake it off",
                     "self destruct"):
            fun.handle_fun_command(text)
            self.assertIn(fun.pending_effect(), fun.EFFECTS, text)

    def test_ordinary_speech_is_left_alone(self):
        for text in ("open chrome", "what is the weather", "pick up the phone", "pick one",
                     "play some music", "how many miles to the moon"):
            self.assertIsNone(fun.handle_fun_command(text), text)


class Dictation(unittest.TestCase):
    def test_the_input_structure_is_the_size_windows_expects(self):
        # Wrong by even 8 bytes and SendInput silently types nothing at all.
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(dictation._INPUT), expected)

    def test_starting_and_stopping(self):
        for text in ("take dictation", "start dictation mode", "type what i say", "dictation on"):
            self.assertTrue(dictation.START.match(text), text)
        for text in ("stop dictation", "that's enough", "that is enough", "dictation off"):
            self.assertTrue(dictation.STOP.match(text), text)

    def test_spoken_punctuation_becomes_real_punctuation(self):
        self.assertEqual(dictation.transform("hello there comma how are you question mark"),
                         " Hello there, how are you?")
        self.assertEqual(dictation.transform("new line"), "\n")
        self.assertEqual(dictation.transform("new paragraph"), "\n\n")
        self.assertEqual(dictation.transform("full stop"), ".")

    def test_brackets_do_not_gain_a_gap(self):
        self.assertEqual(dictation.transform("open bracket a note close bracket"), " (a note)")

    def test_sentences_are_capitalised(self):
        self.assertEqual(dictation.transform("i said hello period then she left"),
                         " I said hello. Then she left")

    def test_the_first_phrase_has_no_leading_space(self):
        self.assertEqual(dictation.transform("hello", leading_space=False), "Hello")

    def test_one_shot_typing(self):
        match = dictation.ONE_SHOT.match("type this: hello world")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("what"), "hello world")

    def test_key_events_come_in_down_up_pairs(self):
        events = dictation._key_events("Hi\n")
        self.assertEqual(len(events), 6)
        self.assertEqual(events[0].u.ki.dwFlags & dictation._KEYEVENTF_KEYUP, 0)
        self.assertTrue(events[1].u.ki.dwFlags & dictation._KEYEVENTF_KEYUP)

    def test_characters_outside_the_basic_plane_are_split_into_surrogates(self):
        self.assertEqual(len(dictation._utf16_units("\U0001f642")), 2)
        self.assertEqual(len(dictation._utf16_units("a")), 1)


class HelpText(unittest.TestCase):
    def test_help_mentions_the_new_abilities(self):
        from tests.common import backend
        for word in ("remember", "convert", "routine", "dictation", "screenshot"):
            self.assertIn(word, backend.HELP_TEXT.lower(), word)

    def test_help_is_one_speakable_paragraph(self):
        from tests.common import backend
        self.assertNotIn("\n", backend.HELP_TEXT)
        self.assertFalse(re.search(r"[*_`#]", backend.HELP_TEXT))


if __name__ == "__main__":
    unittest.main()
