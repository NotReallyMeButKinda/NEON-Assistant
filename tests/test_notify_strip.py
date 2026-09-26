"""Settings > Notifications > "Remove before reading": text taken out of app names, titles and messages before
they're read aloud or handed to the AI, and the small editor (common rules, a live preview, pattern checks)."""

import unittest

import notifications as n

from tests import common
from tests.common import backend, pump
from tests.test_finish_pass import Patch


class StripTests(unittest.TestCase):
    def test_plain_text_and_patterns(self):
        rules = n.strip_rules("(My Server)\n- OUTLOOK\n/\\(\\d+ new\\)/\n\n/[broken/")
        self.assertEqual(len(rules), 3)                                    # the broken pattern is skipped
        cases = {"Alex (my server)": "Alex", "Inbox (3 new) - Outlook": "Inbox", "plain": "plain", "": ""}
        for text, want in cases.items():
            self.assertEqual(n.strip_text(text, rules), want, text)

    def test_the_common_rules(self):
        by_name = dict(n.COMMON_STRIP_RULES)
        cases = {"Anything in (parentheses)": ("Alex (My Server) (2)", "Alex"),
                 "Anything in [square brackets]": ("[Work] Standup moved", "Standup moved"),
                 "Channel names like #general": ("Sam in #general-chat: hi", "Sam in: hi"),
                 "Everything after \" - \"": ("Weekly report - Outlook - Work", "Weekly report"),
                 "Counts like \"3 new messages\"": ("Discord 3 new messages", "Discord"),
                 "Emoji": ("Party \U0001f389 tonight ✨", "Party tonight")}
        self.assertEqual(set(cases), set(by_name))
        for name, (text, want) in cases.items():
            self.assertEqual(n.strip_text(text, n.strip_rules(by_name[name])), want, name)

    def test_problems_are_reported_by_line(self):
        self.assertEqual(n.strip_problems("ok\n/(/"), ["Line 2 isn't a valid pattern (missing ), unterminated "
                                                       "subpattern), so it's skipped."])
        self.assertEqual(n.strip_problems("(not a pattern"), [])


class ReadingTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG["notify_strip"] = "/\\([^)]*\\)/\n- #general"
        self.note = {"app": "Discord", "title": "Alex (My Server) - #general", "body": "are you free? (edited)"}

    def test_read_aloud_and_summaries_use_the_cleaned_text(self):
        self.assertEqual(backend.read_aloud_text([self.note]), "Discord says: Alex. are you free?")
        prompts = []
        Patch(self)(backend, "ollama_answer", lambda prompt, **k: prompts.append(prompt) or "Alex asked if you're free.")
        backend.summarize_notifications([self.note])
        self.assertIn("- Discord: Alex - are you free?", prompts[0])
        self.assertNotIn("My Server", prompts[0])

    def test_the_message_reader_gets_it_cleaned_too(self):
        seen = []
        line = backend.notification_message({**self.note, "message": True},
                                            ai=lambda prompt, system: seen.append(prompt) or "{}")
        self.assertNotIn("My Server", seen[0])
        self.assertEqual(line, "Alex says are you free?")

    def test_nothing_set_changes_nothing(self):
        backend.CONFIG["notify_strip"] = ""
        self.assertIs(n.stripped(self.note, ""), self.note)


class OfferCardTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        import system_control
        patch = Patch(self)
        patch(system_control, "perform", lambda *a, **k: self.fail("a yes reached the PC"))
        patch(backend, "ollama_answer", lambda *a, **k: "Mom asked about dinner tomorrow at 4pm.")
        patch(backend, "ollama_generate", lambda *a, **k: '"Dinner with Mom tomorrow at 4pm."')
        self.note = {"app": "Discord", "title": "Mom", "body": "Dinner tomorrow at 4pm?"}

    def test_yes_makes_a_card_with_its_day_and_time(self):
        import board
        from datetime import date, timedelta
        summary = backend.summarize_notifications([self.note])
        self.assertEqual(summary, "Mom asked about dinner tomorrow at 4pm. Want me to add that to your board?")
        self.assertEqual(backend.pending_confirmation(), "notif_card")
        self.assertEqual(backend.handle_utterance(None, "yes please"),
                         "Added Dinner with Mom to To do, due tomorrow at 4 PM.")
        card = board.columns()[0]["cards"][0]
        self.assertEqual((card["title"], card["due"]), ("Dinner with Mom", f"{date.today() + timedelta(days=1)}T16:00"))

    def test_only_offered_when_a_day_or_time_is_named(self):
        import board
        from datetime import date
        today = date(2026, 9, 26)
        for text in ("Dinner tomorrow at 4pm?", "Meeting moved to Friday", "Your order ships Oct 3",
                     "Call me at 9:30", "party on the 30th"):
            self.assertTrue(board.mentions_when(text, today), text)
        for text in ("lol ok", "see you next week", "sale ends in two weeks", "this weekend?", "Room 12 is free",
                     "Offer valid for 7 days", "3 new messages"):
            self.assertFalse(board.mentions_when(text, today), text)
        plain = {"app": "Discord", "title": "Sam", "body": "lol ok"}
        self.assertEqual(backend.summarize_notifications([plain]), "Mom asked about dinner tomorrow at 4pm.")
        self.assertEqual(backend.pending_confirmation(), "")         # the summary's own words don't count

    def test_no_and_switched_off(self):
        backend.summarize_notifications([self.note])
        self.assertEqual(backend.handle_utterance(None, "no thanks"), "Okay, I'll leave it.")
        backend.CONFIG["notify_offer_card"] = False
        self.assertEqual(backend.summarize_notifications([self.note]), "Mom asked about dinner tomorrow at 4pm.")
        backend.CONFIG.update(notify_offer_card=True, board_enabled=False)
        self.assertEqual(backend.summarize_notifications([self.note]), "Mom asked about dinner tomorrow at 4pm.")


class EditorTests(unittest.TestCase):
    def test_common_rules_preview_and_problems(self):
        common.use_temp_config()
        from ui.settings_widgets import StripRulesEditor
        editor = StripRulesEditor()
        changes = []
        editor.changed.connect(lambda: changes.append(1))
        editor.add_rule(dict(n.COMMON_STRIP_RULES)["Anything in (parentheses)"])
        editor.add_rule(dict(n.COMMON_STRIP_RULES)["Anything in (parentheses)"])     # not twice
        self.assertEqual(editor.value(), "/\\([^)]*\\)/")
        self.assertTrue(changes)
        editor.sample.setText("Alex (My Server): hi")
        pump(20)
        self.assertEqual(editor.result.text(), "Read as: Alex: hi")
        editor.set_value("/(/")
        self.assertTrue(editor.problems.text().startswith("Line 1 isn't a valid pattern"))
        editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
