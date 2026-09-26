"""The September 25 evening pass: due dates with times on the board ("make a card for dinner with Mom at
4pm tomorrow"), the faster model for simple questions, Bitwarden without a master password when bw is
already unlocked, and notifications that the listener misses while a game or video is fullscreen (read
from Windows' notification database instead).

Nothing here reaches outside the process: Ollama, bw.exe and Windows' notification database are stubbed
(the database is a throwaway SQLite file with the same tables)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from tests import common
from tests.common import backend

import bitwarden
import board
import notifications as n
import system_control
from tests.test_finish_pass import Patch

FRI = date(2026, 9, 25)                  # a Friday


def col(name):
    return next(c for c in board.columns() if c["name"] == name)


class DueTimeParsingTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_times_come_out_of_the_title(self):
        morning = datetime.combine(FRI, dtime(8, 0))
        cases = {
            "dinner with mom at 4pm tomorrow": ("dinner with mom", "2026-09-26T16:00"),
            "dinner with mom tomorrow at 4 p.m.": ("dinner with mom", "2026-09-26T16:00"),
            "dentist at 9:30 am on monday": ("dentist", "2026-09-28T09:30"),
            "standup at 09:15 tomorrow": ("standup", "2026-09-26T09:15"),
            "lunch with Sam at noon tomorrow": ("lunch with Sam", "2026-09-26T12:00"),
            "call bob at 3 tomorrow": ("call bob", "2026-09-26T15:00"),            # 1 to 7: afternoon
            "gym at 7 in the morning tomorrow": ("gym", "2026-09-26T07:00"),
            "dinner tonight at 8": ("dinner", "2026-09-25T20:00"),
            "pick up kids at five pm on the 30th": ("pick up kids", "2026-09-30T17:00"),
            "call the bank at 2pm": ("call the bank", "2026-09-25T14:00"),         # a time alone: today...
            "pay rent friday": ("pay rent", "2026-10-02"),                          # no time: a plain date
        }
        for text, want in cases.items():
            self.assertEqual(board.split_due(text, FRI, morning), want, text)
        evening = datetime.combine(FRI, dtime(15, 0))
        self.assertEqual(board.split_due("call the bank at 2pm", FRI, evening),
                         ("call the bank", "2026-09-26T14:00"))                     # ...or tomorrow once it's passed
        for text in ("read 1984", "meet at 10 downing street", "buy 4 apples", "at 4pm"):
            self.assertEqual(board.split_due(text, FRI, morning), (text, None), text)

    def test_stored_times_are_read_back_and_described(self):
        self.assertEqual(board._parse_iso("2026-09-26T16:00"), date(2026, 9, 26))
        self.assertIsNone(board._parse_iso("2026-09-26T25:00"))
        self.assertEqual(board.due_time("2026-09-26T16:30"), dtime(16, 30))
        self.assertIsNone(board.due_time("2026-09-26"))
        now = datetime.combine(FRI, dtime(15, 0))
        self.assertEqual(board.describe_due("2026-09-26T16:00", FRI, now), "tomorrow at 4 PM")
        self.assertEqual(board.describe_due("2026-09-25T16:30", FRI, now), "today at 4:30 PM")
        self.assertEqual(board.describe_due("2026-09-25T14:00", FRI, now), "overdue: 2 PM")
        self.assertEqual(board.due_state("2026-09-25T14:00", FRI, now), "overdue")
        self.assertEqual(board.due_state("2026-09-25T16:00", FRI, now), "today")
        self.assertEqual(board.describe_due("2026-09-25", FRI, now), "today")    # a plain date is never overdue today
        backend.CONFIG["time_format"] = "24h"
        self.assertEqual(board.describe_due("2026-09-26T16:00", FRI, now), "tomorrow at 16:00")

    def test_times_survive_a_reload(self):
        card = board.add_card(col("To do")["id"], "Dinner", "2026-09-26T16:00")
        board.enable_persistence(backend.CONFIG_PATH.parent / "board.json")
        self.assertEqual(col("To do")["cards"][0]["due"], "2026-09-26T16:00")
        board.update_card(card["id"], due="2026-09-27T18:45")
        self.assertEqual(col("To do")["cards"][0]["due"], "2026-09-27T18:45")


class DueTimeVoiceTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        Patch(self)(system_control, "perform", lambda *a, **k: self.fail("a board command reached the PC"))

    def test_make_a_card_for_dinner_with_mom_at_4pm_tomorrow(self):
        tomorrow = date.today() + timedelta(days=1)
        reply = backend.handle_utterance(None, "Make a card for dinner with Mom at 4pm tomorrow.")
        self.assertEqual(reply, "Added Dinner with Mom to To do, due tomorrow at 4 PM.")
        cards = col("To do")["cards"]
        self.assertEqual([(c["title"], c["due"]) for c in cards], [("Dinner with Mom", f"{tomorrow}T16:00")])

    def test_other_ways_of_saying_it(self):
        self.assertEqual(board.handle_board_command("add dinner with mom tomorrow at 4pm to my board", FRI),
                         "Added Dinner with mom to To do, due tomorrow at 4 PM.")
        self.assertEqual(board.handle_board_command("add gym at 7 in the morning to doing on monday", FRI),
                         "Added Gym to Doing, due on Monday at 7 AM.")
        self.assertEqual(board.handle_board_command("gym is due tuesday at 6:30 pm", FRI),
                         "Gym is due on Tuesday at 6:30 PM.")
        self.assertEqual(col("Doing")["cards"][0]["due"], "2026-09-29T18:30")
        self.assertEqual(board.handle_board_command("add a card read 1984", FRI), "Added Read 1984 to To do.")


class DueTimeReminderTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.todo = col("To do")["id"]

    def test_a_card_with_a_time_is_announced_shortly_before_it(self):
        board.add_card(self.todo, "Dinner with Mom", "2026-09-25T16:00")
        at = lambda h, m=0: datetime.combine(FRI, dtime(h, m))
        self.assertEqual([c["title"] for c, _ in board.take_due_reminders(at(9))], ["Dinner with Mom"])  # the morning round
        self.assertEqual(board.take_due_reminders(at(15, 30)), [])
        due = board.take_due_reminders(at(15, 51))                                     # ten minutes before
        self.assertEqual([c["title"] for c, _ in due], ["Dinner with Mom"])
        self.assertEqual(board.reminder_text(due, FRI), "Dinner with Mom is due today at 4 PM.")
        self.assertEqual(board.take_due_reminders(at(15, 55)), [])                    # only once

    def test_a_time_long_gone_is_left_to_the_morning_round(self):
        board.add_card(self.todo, "Old call", "2026-09-24T10:00")
        early = datetime.combine(FRI, dtime(7, 0))
        self.assertEqual(board.take_due_reminders(early), [])
        self.assertEqual(len(board.take_due_reminders(datetime.combine(FRI, dtime(9, 0)))), 1)


class CardDialogTimeTests(unittest.TestCase):
    def test_the_editor_sets_and_clears_a_time(self):
        common.use_temp_config()
        from ui.board import CardDialog
        card = board.add_card(col("To do")["id"], "Dinner", "2026-09-26T16:00")
        dialog = CardDialog(card)
        self.assertTrue(dialog.has_time.isChecked())
        self.assertEqual(dialog.due_value(), "2026-09-26T16:00")
        dialog.has_time.setChecked(False)
        self.assertEqual(dialog.due_value(), "2026-09-26")
        dialog.has_due.setChecked(False)
        self.assertFalse(dialog.has_time.isEnabled())
        self.assertIsNone(dialog.due_value())
        dialog.deleteLater()


class LightModelTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG.update(ollama_light_enabled=True, ollama_light_model="llama3.2", ollama_model="qwen3:8b")
        self.calls = []
        self.judge, self.light_reply = "chat", "Doing well, thanks."

        def generate(prompt, system=None, timeout=None, options=None, schema=None, model=None):
            self.calls.append(("judge" if schema else "answer", model or "main"))
            if schema:
                return json.dumps({"needs": self.judge})
            return self.light_reply if model else "The main model's answer."
        Patch(self)(backend, "ollama_generate", generate)

    def test_chat_stays_on_the_light_model(self):
        self.assertEqual(backend.ollama_chit_chat("how are you"), "Doing well, thanks.")
        self.assertEqual(self.calls, [("judge", "llama3.2"), ("answer", "llama3.2")])

    def test_it_hands_questions_that_need_knowledge_to_the_main_model(self):
        self.judge = "expert"
        self.assertEqual(backend.ollama_chit_chat("who won the 1987 tour de france"), "The main model's answer.")
        self.assertEqual(self.calls, [("judge", "llama3.2"), ("answer", "main")])

    def test_a_hand_over_or_an_i_dont_know_goes_to_the_main_model(self):
        for reply in ("HAND_OVER", "I don't know that one.", "I'm not sure."):
            self.light_reply = reply
            self.assertEqual(backend.ollama_chit_chat("tell me something"), "The main model's answer.", reply)

    def test_heavy_requests_skip_it_and_it_can_be_off(self):
        backend.ollama_chit_chat("write me a poem about rain")
        self.assertEqual(self.calls, [("answer", "main")])
        self.calls.clear()
        backend.CONFIG["ollama_light_enabled"] = False
        backend.ollama_chit_chat("how are you")
        self.assertEqual(self.calls, [("answer", "main")])
        backend.CONFIG.update(ollama_light_enabled=True, ollama_light_model="qwen3:8b")   # the same model: pointless
        self.assertEqual(backend.light_model(), "")

    def test_notification_summaries_use_it_without_asking(self):
        backend.CONFIG["notify_offer_card"] = False
        self.light_reply = "Alex asked if you're free."
        text = backend.summarize_notifications([{"app": "Discord", "title": "Alex", "body": "are you free?"}])
        self.assertEqual(text, "Alex asked if you're free.")
        self.assertEqual(self.calls, [("answer", "llama3.2")])


class BitwardenWithoutPasswordTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.patch = Patch(self)
        self.patch(bitwarden, "find_cli", lambda: "bw.exe")
        self.patch(bitwarden, "touch", lambda: None)
        self.addCleanup(lambda: bitwarden._STATE.update(session="", items=[], choice=None, pending=""))
        backend.CONFIG["bitwarden_enabled"] = True
        self.runs = []
        self.unlocked = True
        items = [{"id": "1", "type": 1, "name": "GitHub", "login": {"username": "octo", "uris": []}}]

        def run(args, env_extra=None, timeout=0, ambient=False):
            self.runs.append((args, ambient))
            if args == ["list", "items"]:
                if not self.unlocked:
                    raise bitwarden.BitwardenError("Vault is locked.")
                return json.dumps(items)
            return ""
        self.patch(bitwarden, "_run", run)

    def test_an_unlocked_bw_is_used_without_asking(self):
        asked = []
        self.patch(bitwarden, "HOOKS", {"unlock": lambda: asked.append(True)})
        self.assertEqual(backend.handle_bitwarden("what's my github username"), "Your GitHub username is octo.")
        self.assertEqual(asked, [])
        self.assertEqual(self.runs[0], (["list", "items"], True))              # the environment's own session
        bitwarden.lock()
        self.assertNotIn((["lock"], False), self.runs)                          # the user's session: theirs to lock
        self.assertFalse(bitwarden.is_unlocked())

    def test_a_locked_bw_still_asks_for_the_password(self):
        self.unlocked = False
        asked = []
        self.patch(bitwarden, "HOOKS", {"unlock": lambda: asked.append(True)})
        self.patch(bitwarden, "status", lambda: "locked")
        self.assertIn("vault is locked", backend.handle_bitwarden("copy my github password"))
        self.assertEqual(asked, [True])

    def test_the_session_bw_is_given(self):
        bitwarden._STATE["session"] = "s" * 40
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, env=None, **k):
            seen.update(env)
            return Done()
        import subprocess
        self.patch(subprocess, "run", fake_run)                        # the real _run, over a fake bw
        self.patch(bitwarden.os, "environ", {"BW_SESSION": "from-the-user", "PATH": ""})
        BitwardenWithoutPasswordTests._real_run(["status"])
        self.assertEqual(seen["BW_SESSION"], "s" * 40)                             # ours wins
        bitwarden._STATE["session"] = bitwarden.AMBIENT
        BitwardenWithoutPasswordTests._real_run(["status"])
        self.assertEqual(seen["BW_SESSION"], "from-the-user")                      # the user's is kept

    _real_run = staticmethod(bitwarden._run)


def make_wpn_db(path: Path) -> sqlite3.Connection:
    """A stand-in for Windows' wpndatabase.db: the two tables and columns DatabaseWatch reads."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT)")
    con.execute("CREATE TABLE Notification (Id INTEGER, HandlerId INTEGER, Type TEXT, Payload BLOB, ArrivalTime INTEGER)")
    con.execute("INSERT INTO NotificationHandler VALUES (1, 'com.squirrel.Discord.Discord')")
    con.commit()
    return con


def add_toast(con, nid, title, body=""):
    xml = (f'<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text>'
           f'<text placement="attribution">via Discord</text></binding></visual></toast>')
    con.execute("INSERT INTO Notification VALUES (?, 1, 'toast', ?, ?)", (nid, xml.encode(), 134031000000000000))
    con.commit()


class DatabaseFallbackTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.path = Path(tempfile.mkdtemp()) / "wpndatabase.db"
        self.con = make_wpn_db(self.path)
        self.addCleanup(self.con.close)

    def test_it_delivers_what_the_listener_missed_after_a_grace_period(self):
        add_toast(self.con, 1, "Old", "already there")
        watch = n.DatabaseWatch(self.path, grace=3.0)
        self.assertEqual(watch.poll(now=0), [])                                   # the start: only noted
        add_toast(self.con, 2, "Alex", "are you free?")
        add_toast(self.con, 3, "Sam", "gg")
        watch.seen(3)                                                             # the listener reported this one
        self.assertEqual(watch.poll(now=1), [])                                   # the listener still has time
        got = watch.poll(now=4.5)
        self.assertEqual([(g["id"], g["texts"], g["aumid"]) for g in got],
                         [(2, ["Alex", "are you free?"], "com.squirrel.Discord.Discord")])
        self.assertEqual(watch.poll(now=9), [])                                   # once
        self.assertFalse(watch.seen(2))                                           # the listener's late copy is dropped

    def test_the_watcher_hands_them_on_once(self):
        got = []
        watcher = n.NotificationWatcher(got.append)
        watcher._db_watch = n.DatabaseWatch(self.path, grace=0)
        watcher._db_watch.poll()
        watcher.ready.set()
        watcher._app_names["com.squirrel.Discord.Discord"] = "Discord"
        add_toast(self.con, 7, "Alex", "are you free?")
        for obj in watcher._db_watch.poll():                                      # what _watch_database does
            obj["app"] = watcher._app_names.get(obj["aumid"]) or n.app_from_aumid(obj["aumid"])
            watcher._deliver(n.parse_toast(obj), from_db=True)
        watcher._handle_line(json.dumps({"type": "toast", "id": 7, "app": "Discord",
                                         "texts": ["Alex", "are you free?"]}).encode())
        self.assertEqual([(g.app, g.title, g.body) for g in got], [("Discord", "Alex", "are you free?")])

    def test_app_names_from_ids(self):
        cases = {"com.squirrel.Discord.Discord": "Discord", "Microsoft.ScreenSketch_8wekyb3d8bbwe!App": "ScreenSketch",
                 "Mozilla.Firefox.6F193CCC56814779": "Firefox", "308046B0AF4A39CB": "an app", "": "an app"}
        for aumid, name in cases.items():
            self.assertEqual(n.app_from_aumid(aumid), name, aumid)


if __name__ == "__main__":
    unittest.main()
