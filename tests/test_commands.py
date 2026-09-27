"""What the assistant understands: spoken math, media / music / window / website / summary /
notification commands. Pure text in, decisions out."""

import time
import unittest

import tts

from tests import common
from tests.common import backend as a


class SpokenSymbolTests(unittest.TestCase):
    """Emoji and styled letters used to be read out as their code points."""

    def say(self, text):
        return tts.clean_for_speech(text)

    def test_emoji_are_named_once_per_run(self):
        self.assertEqual(self.say("lol \U0001f602\U0001f602\U0001f602 see you there!"), "lol laughing see you there!")
        self.assertEqual(self.say("good job \U0001f44d\U0001f3fd"), "good job thumbs up")         # skin tone dropped
        self.assertEqual(self.say("I \u2764\ufe0f you"), "I heart you")
        self.assertEqual(self.say("a \U0001f984 appears"), "a unicorn appears")                  # from Unicode's names
        self.assertEqual(self.say("\U0001f469\u200d\U0001f4bb coding"), "woman coding")
        self.assertEqual(self.say("\u26a0\ufe0f Low battery"), "warning Low battery")

    def test_styled_text_becomes_plain_letters(self):
        self.assertEqual(self.say("\U0001d4d7\U0001d4ee\U0001d4f5\U0001d4f5\U0001d4f8 \U0001d42d\U0001d421\U0001d41e\U0001d42b\U0001d41e"), "Hello there")
        self.assertEqual(self.say("\uff46\uff55\uff4c\uff4c"), "full")
        self.assertEqual(self.say("\ua731\u1d0d\u1d00\u029f\u029f"), "small")
        self.assertEqual(self.say("z\u0336a\u0336p\u0336"), "zap")

    def test_real_letters_and_bullets(self):
        self.assertEqual(self.say("Caf\u00e9 na\u00efve, \u041f\u0440\u0438\u0432\u0435\u0442"), "Caf\u00e9 na\u00efve, \u041f\u0440\u0438\u0432\u0435\u0442")
        self.assertEqual(self.say("\u2022 one \u2022 two"), "one, two")
        self.assertEqual(self.say("72\u00b0F"), "seventy-two degrees fahrenheit")


class SpokenMathTests(unittest.TestCase):
    def say(self, text):
        return tts.clean_for_speech(text)

    def test_operators_are_spoken(self):
        self.assertEqual(self.say("12 * (3 + 4) = 84"), "twelve times open bracket three plus four close bracket equals eighty-four")
        self.assertEqual(self.say("10 / 4 = 2.5"), "ten divided by four equals two point five")
        self.assertEqual(self.say("2 ** 10 = 1024"), "two to the power of ten equals one thousand twenty-four")
        self.assertEqual(self.say("sqrt(16) = 4"), "square root of sixteen equals four")
        self.assertEqual(self.say("-5 + 3 = -2"), "negative five plus three equals negative two")
        self.assertEqual(self.say("It's 20% off"), "It's twenty percent off")

    def test_prose_is_left_alone(self):
        for text in ("wind 12 km/h", "call 555-1234", "version 1.2.3", "costs $3.50", "see (2019) note"):
            self.assertNotIn("divided", self.say(text))
            self.assertNotIn("minus", self.say(text))
        self.assertEqual(self.say("call 555-1234"), "call five five five, one two three four")
        self.assertEqual(self.say("version 1.2.3"), "version one point two point three")

    def test_long_decimals_are_shortened(self):
        self.assertEqual(self.say("2.6457513110645907"), "two point six four five eight")


class MediaAndMusicTests(unittest.TestCase):
    def test_bare_media_commands(self):
        self.assertEqual(a.spoken_media_action("pause the music"), "pause")
        self.assertEqual(a.spoken_media_action("next song"), "next")
        self.assertIsNone(a.spoken_media_action("play some jazz"))

    def test_music_commands(self):
        cases = {"what's playing": ("now_playing", None), "what's next": ("up_next", None),
                 "skip ahead 30 seconds": ("seek", 30), "go back 10 seconds": ("seek", -10),
                 "rewind two minutes": ("seek", -120), "set volume to fifty percent": ("set_volume", 50),
                 "play glass beach on youtube music": ("search_and_play", "glass beach"),
                 "play music by toto": ("search_and_play", "toto"),
                 "play the album nurture by porter robinson": ("search_and_play", "the album nurture by porter robinson"),
                 "play worlds album": ("search_and_play", "worlds album"),
                 "play the tummy ache album by stomach book": ("search_and_play", "the tummy ache album by stomach book"),
                 "play my chill playlist": ("search_and_play", "my chill playlist")}
        for text, (method, arg) in cases.items():
            got = a.spoken_music_command(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got[0], got[1]), (method, arg), text)
        for text in ("play", "play minecraft", "go 30 seconds", "shuffle my thoughts", "what's the weather next week"):
            self.assertIsNone(a.spoken_music_command(text), text)


class WebsiteTests(unittest.TestCase):
    def test_websites_are_recognized(self):
        cases = {"open github.com": "github.com", "go to youtube dot com": "youtube.com",
                 "open youtube com": "youtube.com", "visit docs.python.org slash 3": "docs.python.org/3",
                 "can you open google.co.uk please": "google.co.uk", "open the website wikipedia.org": "wikipedia.org"}
        for text, expected in cases.items():
            self.assertEqual(a.spoken_website(text), expected, text)

    def test_files_and_apps_are_not_websites(self):
        for text in ("open chrome", "open notes.txt", "open main.py", "open readme.md", "open node.js",
                     "open the gmail app", "open script.sh", "open 192.168.1.1", "go to sleep"):
            self.assertIsNone(a.spoken_website(text), text)


class SummarizeSelectionTests(unittest.TestCase):
    def test_phrases(self):
        for text in ("summarize this", "Summarize the selected text.", "please summarise what I highlighted",
                     "tldr this", "sum up this"):
            self.assertTrue(a.spoken_summarize_selection(text), text)
        for text in ("summarize that", "summarize", "summarize the news", "explain this"):
            self.assertFalse(a.spoken_summarize_selection(text), text)


class NotificationCommandTests(unittest.TestCase):
    def setUp(self):
        a._NOTIFS.clear()
        a._NOTIF_QUESTION["until"] = 0.0
        a.resume_notifications()

    def test_yes_and_no_only_count_while_a_question_is_open(self):
        self.assertIsNone(a.spoken_notification_command("yes"))
        a.open_notification_question(30)
        self.assertEqual(a.spoken_notification_command("yes please"), "summarize")
        self.assertEqual(a.spoken_notification_command("no thanks"), "dismiss")
        self.assertIsNone(a.spoken_notification_command("yes I love pizza"))

    def test_explicit_commands(self):
        self.assertEqual(a.spoken_notification_command("summarize that notification"), "summarize")
        self.assertEqual(a.spoken_notification_command("read my notifications"), "read")
        self.assertEqual(a.spoken_notification_command("clear my notifications"), "clear")
        self.assertIsNone(a.spoken_notification_command("summarize the news"))

    def test_what_did_an_app_say(self):
        a.note_notification({"id": 1, "app": "Discord", "title": "Alex", "body": "are you coming?"})
        a.note_notification({"id": 2, "app": "Steam", "title": "Sale", "body": "50% off"})
        for text in ("what did discord say", "What did Discord send me?", "any notifications from steam"):
            self.assertEqual(a.spoken_notification_command(text), "from_app", text)
        for text in ("what did he say", "what did outlook say", "what did you say"):
            self.assertIsNone(a.spoken_notification_command(text), text)     # carries on to the chat
        self.assertEqual(a.handle_notification_command("what did discord say"), "Discord says: Alex. are you coming?")

    def test_remind_me_about_that(self):
        self.assertIsNone(a.spoken_notification_command("remind me about that in an hour"))   # nothing to remind of
        a.note_notification({"id": 3, "app": "Mail", "title": "Invoice due", "body": "", "received": time.time()})
        self.assertEqual(a.spoken_notification_command("remind me about that in an hour"), "remind")
        common.use_temp_config()
        try:
            reply = a.handle_notification_command("remind me about that notification in 10 minutes")
            self.assertEqual(reply, "Okay, I'll remind you in 10 minutes to check Mail (Invoice due).")
        finally:
            a.cancel_timers()
        a._NOTIFS[-1]["received"] = time.time() - 3600                     # an old one: "that" is something else
        self.assertIsNone(a.spoken_notification_command("remind me about that in an hour"))
        self.assertEqual(a.spoken_notification_command("remind me about the notification in an hour"), "remind")

    def test_history(self):
        a.note_notification({"id": 4, "app": "A", "title": "one", "body": ""})
        a.note_notification({"id": 5, "app": "B", "title": "two", "body": ""})
        self.assertEqual([n["id"] for n in a.recent_notifications()], [5, 4])       # newest first
        a.mark_notification_handled(4)
        self.assertTrue(a._NOTIFS[0]["handled"])
        a.clear_notification_history()
        self.assertEqual(a.recent_notifications(), [])

    def test_pause_and_resume(self):
        cases = {"pause notifications": 3600, "snooze my notifications for an hour": 3600,
                 "don't read my notifications for 30 minutes": 1800, "pause notifications for two hours": 7200,
                 "pause notifications for half an hour": 1800, "pause notifications until tomorrow": 8 * 3600}
        for text, seconds in cases.items():
            a.resume_notifications()
            self.assertIn("quiet", a.handle_notification_command(text))
            self.assertAlmostEqual(a._NOTIF_PAUSE["until"] - time.time(), seconds, delta=5)
            self.assertTrue(a.notifications_paused())
        self.assertEqual(a.handle_notification_command("resume notifications"), "Notifications are back on.")
        self.assertFalse(a.notifications_paused())
        self.assertIsNone(a.spoken_notification_command("pause the music"))

    def test_read_and_dismiss_use_the_latest_notification(self):
        a.note_notification({"id": 1, "app": "Slack", "title": "Build failed", "body": "pipeline 42"}, 30)
        self.assertEqual(a.handle_notification_command("read it"), "Slack says: Build failed. pipeline 42")
        a.note_notification({"id": 2, "app": "Mail", "title": "Invoice", "body": ""}, 30)
        self.assertEqual(a.handle_notification_command("no"), "Okay.")
        self.assertTrue(a._NOTIFS[-1]["handled"])


class TimerTests(unittest.TestCase):
    def setUp(self):
        a.cancel_timers()
        self.fired = []
        a.TIMER_HOOKS["fire"] = lambda *args: self.fired.append(args)

    def tearDown(self):
        a.cancel_timers()
        a.TIMER_HOOKS["fire"] = None

    def test_durations(self):
        cases = {"ten minutes": 600, "1 hour 30 minutes": 5400, "half an hour": 1800, "an hour and a half": 5400,
                 "two minutes and a half": 150, "45 seconds": 45, "an hour": 3600, "banana": 0}
        for spoken, seconds in cases.items():
            self.assertEqual(a.duration_seconds(spoken), seconds, spoken)

    def test_phrases(self):
        cases = {"set a timer for 10 minutes": (600, "", False), "set a 5 minute timer": (300, "", False),
                 "set a 5-minute timer": (300, "", False), "start a pasta timer for 12 minutes": (720, "pasta", False),
                 "timer for 30 seconds": (30, "", False), "remind me in an hour to call sam": (3600, "call sam", True)}
        for text, expected in cases.items():
            self.assertEqual(a.spoken_timer_command(text), ("set", expected), text)
        self.assertEqual(a.spoken_timer_command("how much time is left"), ("status", None))
        self.assertEqual(a.spoken_timer_command("cancel all timers"), ("cancel", None))
        for text in ("set a timer", "timer", "stop", "what's the weather", "set an alarm"):
            self.assertIsNone(a.spoken_timer_command(text), text)

    def test_sentences_use_the_right_units(self):
        self.assertEqual(a.handle_timer_command("set a timer for 1 second"), "Okay, timer set for 1 second.")
        self.assertEqual(a.handle_timer_command("set a 5 minute timer"), "Okay, timer set for 5 minutes.")
        self.assertEqual(a.handle_timer_command("remind me in 1 second to stretch"),
                         "Okay, I'll remind you in 1 second to stretch.")
        self.assertIn("longer than a day", a.handle_timer_command("set a timer for 30 hours"))
        self.assertEqual(a.timer_announcement("", 300, False), "Your timer for 5 minutes is done.")
        self.assertEqual(a.timer_announcement("pasta", 300, False), "Your pasta timer is done.")
        self.assertEqual(a.timer_announcement("stretch", 60, True), "Reminder: stretch.")

    def test_timers_fire_report_and_cancel(self):
        a.handle_timer_command("set a timer for 1 second")
        a.handle_timer_command("remind me in 1 second to stretch")
        self.assertIn("left", a.handle_timer_command("how much time is left"))
        self.assertTrue(common.pump_until(lambda: len(self.fired) == 2, 5))
        self.assertEqual({f[2] for f in self.fired}, {True, False})
        self.assertEqual(a.handle_timer_command("check my timer"), "You don't have any timers running.")
        a.handle_timer_command("set a timer for 20 minutes")
        self.assertEqual(a.handle_timer_command("cancel the timer"), "Cancelled your timers.")


class CalendarHelpSelectionTests(unittest.TestCase):
    def test_calendar_and_help_questions(self):
        for text in ("what's my next meeting", "what's on my calendar", "do I have any meetings today"):
            self.assertTrue(a.spoken_calendar_query(text), text)
        self.assertFalse(a.spoken_calendar_query("what's the weather"))
        self.assertTrue(a.spoken_help_query("what can you do"))
        self.assertFalse(a.spoken_help_query("helpful tips"))
        saved = a.CONFIG.get("calendar_ics", "")
        a.CONFIG["calendar_ics"] = ""
        try:
            self.assertIn("haven't added a calendar", a.answer_calendar())
        finally:
            a.CONFIG["calendar_ics"] = saved
        self.assertIn("timers", a._route_utterance(None, "what can you do", None))

    def test_selection_actions(self):
        cases = {"summarize this": "summarize", "read this aloud": "read", "read the selected text": "read",
                 "please read what I highlighted out loud": "read", "explain the selected text": "explain",
                 "read that": None, "explain this": None, "summarize that": None}
        for text, expected in cases.items():
            self.assertEqual(a.spoken_selection_action(text), expected, text)


if __name__ == "__main__":
    unittest.main()
