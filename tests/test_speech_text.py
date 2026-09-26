"""Numbers and symbols are spelled out before text reaches a voice (speech_text.py)."""

from __future__ import annotations

import unittest

from tests import common  # noqa: F401  (offscreen Qt, import paths)

import speech_text
import tts

say = tts.clean_for_speech


class NumberTests(unittest.TestCase):
    def test_numbers(self):
        n = speech_text.number_words
        self.assertEqual(n(0), "zero")
        self.assertEqual(n(42), "forty-two")
        self.assertEqual(n(115), "one hundred fifteen")
        self.assertEqual(n(1250000), "one million two hundred fifty thousand")

    def test_years(self):
        y = speech_text.year_words
        self.assertEqual(y(2008), "two thousand eight")
        self.assertEqual(y(2000), "two thousand")
        self.assertEqual(y(2025), "twenty twenty-five")
        self.assertEqual(y(1999), "nineteen ninety-nine")
        self.assertEqual(y(1905), "nineteen oh five")
        self.assertEqual(y(1900), "nineteen hundred")

    def test_ordinals(self):
        o = speech_text.ordinal_words
        self.assertEqual([o(1), o(2), o(3), o(12), o(20), o(21), o(103)],
                         ["first", "second", "third", "twelfth", "twentieth", "twenty-first", "one hundred third"])


class SentenceTests(unittest.TestCase):
    def test_years_versus_amounts(self):
        self.assertEqual(say("Grand Theft Auto IV came out in 2008."), "Grand Theft Auto IV came out in two thousand eight.")
        self.assertEqual(say("The game sold 2000 copies in 2000."),
                         "The game sold two thousand copies in two thousand.")
        self.assertIn("two thousand eight to twenty twelve", say("from 2008-2012"))

    def test_money_times_dates(self):
        self.assertEqual(say("It costs $19.99."), "It costs nineteen dollars and ninety-nine cents.")
        self.assertEqual(say("It costs $1.5 billion."), "It costs one point five billion dollars.")
        self.assertEqual(say("Meet me at 3:30 pm or 10am."), "Meet me at three thirty p m or ten a m.")
        self.assertEqual(say("It's at 7:05."), "It's at seven oh five.")
        self.assertIn("September fourth, twenty twenty-five", say("released September 4, 2025"))
        self.assertIn("the third of May nineteen ninety", say("Born on the 3rd of May 1990."))
        self.assertIn("September twenty-fifth, twenty twenty-six", say("Today is 2026-09-25."))

    def test_symbols_become_words_and_brackets_go(self):
        self.assertEqual(say("Silksong (the sequel) is out."), "Silksong the sequel is out.")
        self.assertEqual(say("Rated #1 & loved"), "Rated number one and loved")
        self.assertEqual(say("It rose 40%"), "It rose forty percent")
        self.assertEqual(say("It is 21°C."), "It is twenty-one degrees celsius.")
        self.assertEqual(say("Email me@example.com"), "Email me at example dot com")
        self.assertEqual(say("Dune: Part Two [2024]"), "Dune, Part Two twenty twenty-four")
        self.assertEqual(say("Call 007"), "Call zero zero seven")

    def test_math_still_reads_as_math(self):
        self.assertEqual(say("What is 12 * (3 + 4)?"), "What is twelve times open bracket three plus four close bracket?")
        self.assertEqual(say("Pi is 3.14159"), "Pi is three point one four one six")

    def test_the_windows_voice_gets_the_same(self):
        from tests.common import backend
        self.assertEqual(backend.SapiSpeaker._clean("It came out in 2008 (in Japan)."),
                         "It came out in two thousand eight in Japan.")


if __name__ == "__main__":
    unittest.main()
