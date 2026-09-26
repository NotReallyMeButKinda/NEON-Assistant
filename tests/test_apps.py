"""Matching a spoken phrase to an installed app.

The catalogue here is made up, so the results don't depend on what happens to be installed on the
machine running the tests -- and the awkward cases (one shared generic word, half a name, a
misheard spelling) are all present on purpose.
"""

import unittest

from tests import common
from tests.common import backend

CATALOGUE = {
    "Notepad": ("Microsoft", "A plain text editor included with Windows."),
    "Calculator": ("Microsoft", "A calculator app for arithmetic and unit conversion."),
    "Registry Editor": ("Microsoft", "A tool for viewing and editing the Windows registry."),
    "Microsoft Edge": ("Microsoft", "A web browser based on Chromium."),
    "Microsoft News": ("Microsoft", "A news aggregator. Content also appears in Microsoft Edge."),
    "Roblox Studio": ("Roblox", "An editor for creating Roblox experiences with Lua code."),
    "Visual Studio Code": ("Microsoft", "A source code editor with debugging and extensions."),
    "Paint.NET": ("dotPDN", "An image and photo editing program for Windows."),
    "Zen": ("Zen Team", "A privacy-focused web browser built on Firefox."),
    "Media Player Legacy": ("Microsoft", "Plays video and audio files."),
    "Spotify": ("Spotify AB", "A music streaming service."),
    "Character Map": ("Microsoft", "Shows the characters available in a font."),
}


def build(names=None) -> None:
    backend._APP_CATALOGUE = {
        name: backend._catalogue_entry(
            name, f"C:\\fake\\{name}.lnk",
            {"publisher": publisher, "description": description})
        for name, (publisher, description) in CATALOGUE.items()
        if names is None or name in names}


class AppMatching(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self._saved = backend._APP_CATALOGUE
        build()

    def tearDown(self):
        backend._APP_CATALOGUE = self._saved

    def resolve(self, query, **kwargs):
        return backend.fuzzy_resolve_app(query, **kwargs)

    def test_exact_and_contained_names(self):
        self.assertEqual(self.resolve("notepad"), "Notepad")
        self.assertEqual(self.resolve("Visual Studio Code"), "Visual Studio Code")
        self.assertEqual(self.resolve("open zen please"), "Zen")
        self.assertEqual(self.resolve("media player"), "Media Player Legacy")

    def test_a_misheard_name_still_lands(self):
        self.assertEqual(self.resolve("spotfy"), "Spotify")
        self.assertEqual(self.resolve("calculater"), "Calculator")

    def test_one_shared_generic_word_is_not_a_match(self):
        build(names=set(CATALOGUE) - {"Visual Studio Code"})
        self.assertIsNone(self.resolve("visual studio code"))       # not Roblox Studio

    def test_half_a_name_is_not_a_match(self):
        build(names=set(CATALOGUE) - {"Microsoft Edge"})
        self.assertIsNone(self.resolve("microsoft edge"))           # not Microsoft News

    def test_a_description_can_earn_a_match_when_no_name_was_spoken(self):
        self.assertEqual(self.resolve("the photo editing program"), "Paint.NET")

    def test_weak_character_similarity_alone_never_wins(self):
        build(names={"Character Map"})
        self.assertIsNone(self.resolve("chrome"))

    def test_nothing_installed_means_no_answer(self):
        build(names=set())
        self.assertIsNone(self.resolve("notepad"))
        self.assertIsNone(self.resolve(""))

    def test_the_avoid_list_is_respected(self):
        backend.CONFIG["avoid_apps"] = ["Microsoft Edge"]
        self.assertEqual(self.resolve("microsoft edge"), "Microsoft Edge")     # asked for by name
        self.assertIsNone(self.resolve("microsoft edge", skip_avoided=True))   # ...but not vaguely

    def test_a_stricter_threshold_only_removes_matches(self):
        loose = {q: self.resolve(q) for q in ("notepad", "spotfy", "the photo editing program")}
        strict = {q: self.resolve(q, min_score=0.95) for q in loose}
        for query, answer in strict.items():
            self.assertIn(answer, (None, loose[query]), query)

    def test_the_entry_keeps_name_words_apart_from_the_description(self):
        entry = backend._APP_CATALOGUE["Paint.NET"]
        self.assertIn("paint", entry["name_words"])
        self.assertNotIn("photo", entry["name_words"])
        self.assertIn("photo", entry["corpus"])


if __name__ == "__main__":
    unittest.main()
