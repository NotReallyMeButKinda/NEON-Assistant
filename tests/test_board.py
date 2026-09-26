"""The Trello-style board: storage, due dates, voice commands, and the window."""

import json
import unittest
from datetime import date, datetime, time as dtime, timedelta

import board

from tests import common
from PySide6.QtCore import Qt

from tests.common import backend, pump


def WHEEL_STEP_CARDS():
    from ui import board as ui_board
    return ui_board.WHEEL_STEP_CARDS


def WHEEL_STEP_SIDEWAYS():
    from ui import board as ui_board
    return ui_board.WHEEL_STEP_SIDEWAYS

THU = date(2026, 9, 24)                  # a Thursday


def col(name):
    return next(c for c in board.columns() if c["name"] == name)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.path = common.use_temp_config().parent / "board.json"

    def test_starts_with_three_columns_and_survives_a_reload(self):
        self.assertEqual([c["name"] for c in board.columns()], ["To do", "Doing", "Done"])
        board.add_card(col("To do")["id"], "  Pay   rent ", "2026-10-01")
        board.enable_persistence(self.path)                                  # read back from disk
        self.assertEqual(col("To do")["cards"][0]["title"], "Pay rent")
        self.assertEqual(col("To do")["cards"][0]["due"], "2026-10-01")

    def test_columns_can_be_added_renamed_moved_and_deleted(self):
        ideas = board.add_column("Ideas")
        board.rename_column(ideas["id"], "Later")
        board.move_column(ideas["id"], 0)
        self.assertEqual([c["name"] for c in board.columns()], ["Later", "To do", "Doing", "Done"])
        self.assertIsNone(board.add_column("   "))
        board.delete_column(ideas["id"])
        self.assertEqual(len(board.columns()), 3)

    def test_cards_move_between_and_within_columns(self):
        todo, doing = col("To do")["id"], col("Doing")["id"]
        a, b, c = (board.add_card(todo, t)["id"] for t in "ABC")
        board.move_card(a, todo, 3)                                          # to the end of its own column
        self.assertEqual([x["title"] for x in col("To do")["cards"]], ["B", "C", "A"])
        board.move_card(b, doing, 0)
        self.assertEqual([x["title"] for x in col("Doing")["cards"]], ["B"])
        board.update_card(c, title="C2", due="2026-12-01")
        board.update_card(c, due=None)
        self.assertEqual(col("To do")["cards"][0], {**col("To do")["cards"][0], "title": "C2", "due": None})
        board.delete_card(c)
        self.assertEqual([x["title"] for x in col("To do")["cards"]], ["A"])
        self.assertFalse(board.update_card("nope", title="x"))

    def test_a_damaged_file_is_kept_aside_not_overwritten(self):
        self.path.write_text("{ not json", encoding="utf-8")
        board.enable_persistence(self.path)
        self.assertEqual(len(board.columns()), 3)
        self.assertTrue(list(self.path.parent.glob("board.json.corrupt-*")))

    def test_bad_entries_are_dropped(self):
        self.path.write_text(json.dumps({"columns": [{"name": "Ok", "cards": [{"title": "t", "due": "garbage"},
                                                                            {"title": ""}]}, {"name": ""}, 5]}))
        board.enable_persistence(self.path)
        self.assertEqual([(c["name"], [(x["title"], x["due"]) for x in c["cards"]]) for c in board.columns()],
                         [("Ok", [("t", None)])])


class DueDateTests(unittest.TestCase):
    def test_spoken_and_typed_dates(self):
        cases = {"today": THU, "tomorrow": THU + timedelta(1), "friday": THU + timedelta(1),
                 "next friday": THU + timedelta(1), "thursday": THU + timedelta(7), "in 3 days": THU + timedelta(3),
                 "in two weeks": THU + timedelta(14), "end of the week": THU + timedelta(1),
                 "next week": date(2026, 9, 28), "september 30": date(2026, 9, 30),
                 "30th of september": date(2026, 9, 30), "sep 1": date(2027, 9, 1), "2026-10-01": date(2026, 10, 1),
                 "10/1": date(2026, 10, 1), "by friday": THU + timedelta(1), "blah": None, "": None}
        for text, want in cases.items():
            self.assertEqual(board.parse_due(text, THU), want, text)

    def test_a_date_in_a_card_title_becomes_its_due_date(self):
        cases = {"Complete project on september 30th": ("Complete project", date(2026, 9, 30)),
                 "Finish essay by the 30th of october": ("Finish essay", date(2026, 10, 30)),
                 "Renew passport before 10/15": ("Renew passport", date(2026, 10, 15)),
                 "Party on sept 22 2027": ("Party", date(2027, 9, 22)),
                 "Submit taxes in 2 weeks": ("Submit taxes", THU + timedelta(14)),
                 "pay rent due friday": ("pay rent", THU + timedelta(1)),
                 "pay rent next monday": ("pay rent", date(2026, 9, 28)),
                 "on friday, dentist": ("dentist", THU + timedelta(1)),
                 "tomorrow: call the bank": ("call the bank", THU + timedelta(1))}
        for text, (title, due) in cases.items():
            self.assertEqual(board._split_due(text, THU), (title, due, True), text)
        for text in ("Buy milk", "Read 1984", "Friday night plans", "Plan trip for may", "Watch Friday the 13th",
                     "tomorrow"):                        # a lone date is kept as the title
            self.assertEqual(board._split_due(text, THU), (text, None, False), text)

    def test_a_day_of_the_month_is_this_month_or_the_next(self):
        self.assertEqual(board.parse_due("the 29th", THU), date(2026, 9, 29))
        self.assertEqual(board.parse_due("on the 24th", THU), THU)                    # today counts
        self.assertEqual(board.parse_due("the 23rd", THU), date(2026, 10, 23))        # passed: next month
        self.assertEqual(board.parse_due("the 31st", THU), date(2026, 10, 31))        # September has 30 days
        self.assertEqual(board.parse_due("the 30th", date(2026, 1, 31)), date(2026, 3, 30))   # skips February
        self.assertEqual(board.parse_due("the 5th", date(2026, 12, 20)), date(2027, 1, 5))
        self.assertIsNone(board.parse_due("the 40th", THU))
        self.assertEqual(board._split_due("Pay rent on the 29th", THU), ("Pay rent", date(2026, 9, 29), True))
        self.assertEqual(board.parse_due("tuesday the 29th", THU), date(2026, 9, 29))  # the weekday agrees
        self.assertIsNone(board.parse_due("friday the 29th", THU))                     # it doesn't
        self.assertEqual(board._split_due("Meeting tuesday the 29th", THU), ("Meeting", date(2026, 9, 29), True))

    def test_describing_them(self):
        d = lambda n: (THU + timedelta(n)).isoformat()
        got = [board.describe_due(d(n), THU) for n in (-2, 0, 1, 3, 20)]
        self.assertEqual(got, ["overdue: Sep 22", "today", "tomorrow", "Sunday", "Oct 14"])
        self.assertEqual([board.due_state(d(n), THU) for n in (-1, 0, 2, 5)], ["overdue", "today", "soon", "later"])
        self.assertEqual(board.describe_due(None), "")


class VoiceTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def say(self, text):
        return board.handle_board_command(text, THU)

    def test_a_date_before_to_my_board(self):
        self.assertEqual(self.say("add dinner with mom on september 24th to my board"),
                         "Added Dinner with mom to To do, due today.")
        self.assertEqual(self.say("add pay rent on the 29th to my board"), "Added Pay rent to To do, due on Tuesday.")
        self.assertEqual(self.say("add work on essay to doing"), "Added Work on essay to Doing.")

    def test_adding_cards(self):
        self.assertEqual(self.say("add pay rent to to do due friday"), "Added Pay rent to To do, due tomorrow.")
        self.assertEqual(self.say("add a card call the bank tomorrow"), "Added Call the bank to To do, due tomorrow.")
        self.assertEqual(self.say("add dentist to doing on monday"), "Added Dentist to Doing, due on Monday.")
        self.assertEqual(self.say("add taxes to my board by october 15"), "Added Taxes to To do, due on October 15.")
        self.assertEqual(self.say("add a task walk the dog"), "Added Walk the dog to To do.")
        self.assertEqual(col("To do")["cards"][0]["due"], "2026-09-25")

    def test_moving_finishing_dating_deleting(self):
        self.say("add pay rent to to do")
        self.assertEqual(self.say("move pay rent to doing"), "Moved Pay rent to Doing.")
        self.assertEqual(self.say("pay rent is due next monday"), "Pay rent is due on Monday.")
        self.assertEqual(self.say("remove the due date from pay rent"), "Pay rent has no due date now.")
        self.assertEqual(self.say("mark pay rent as done"), "Nice. Moved Pay rent to Done.")
        self.assertEqual(self.say("mark pay rent as done"), "Pay rent is already in Done.")
        self.assertEqual(self.say("delete the card pay rent"), "Deleted Pay rent.")
        self.assertEqual(self.say("delete the card pay rent"), "I couldn't find that card on your board.")

    def test_asking_whats_in_a_column(self):
        self.say("add pay rent to to do")
        self.say("add call the bank to to do")
        want = "In To do: Pay rent and Call the bank."
        for text in ("what's on the to do list", "What's on my to-do list?", "what is in to do", "read me my to do list",
                     "what do I have to do", "what's left to do", "list my to do cards", "what's in the to do column"):
            self.assertEqual(self.say(text), want, text)
        self.assertEqual(self.say("anything in doing"), "Doing is empty.")
        for text in ("what's on my calendar", "what's on tv", "read me a story", "what do i have today"):
            self.assertIsNone(self.say(text), text)

    def test_moving_cards_between_columns(self):
        self.say("add pay rent to to do")
        self.say("add call the bank to to do")
        self.assertEqual(self.say("move pay rent from to do to doing"), "Moved Pay rent to Doing.")
        self.assertEqual(self.say("put call the bank in doing"), "Moved Call the bank to Doing.")   # not a new card
        self.assertEqual(self.say("send pay rent back to to do"), "Moved Pay rent to To do.")
        self.assertEqual(self.say("move the pay rent card to the done column"), "Moved Pay rent to Done.")
        self.assertEqual(self.say("move pay rent to done"), "Pay rent is already in Done.")
        self.assertEqual(self.say("move everything from doing to done"), "Moved 1 card from Doing to Done.")
        self.assertEqual(self.say("move all cards from done to to do"), "Moved 2 cards from Done to To do.")
        self.assertEqual(self.say("move the banana to done"), "I couldn't find a card called banana on your board.")
        self.assertEqual(sum(len(c["cards"]) for c in board.columns()), 2)
        for text in ("put the kettle on", "move this window to the left", "send an email to bob"):
            self.assertIsNone(self.say(text), text)

    def test_columns_by_voice(self):
        self.assertEqual(self.say("add a column called ideas"), "Added a column called Ideas.")
        self.say("add paint the fence to ideas")
        self.assertEqual(self.say("rename the column ideas to later"), "Renamed Ideas to Later.")
        self.assertEqual(self.say("what's in later"), "In Later: Paint the fence.")
        self.assertEqual(self.say("delete the column later"), "Deleted the Later column and its 1 card.")

    def test_questions(self):
        self.assertIn("empty", self.say("what's on my board"))
        self.say("add pay rent to to do due tomorrow")
        self.say("add old bill to to do due 9/20")          # next year's: not overdue
        board.update_card(col("To do")["cards"][1]["id"], due="2026-09-20")
        self.say("add shipped to done due today")
        self.assertEqual(self.say("what's due this week"), "Old bill, overdue: Sep 20; Pay rent, tomorrow.")
        self.assertEqual(self.say("anything overdue"), "Old bill, overdue: Sep 20.")
        self.assertEqual(self.say("what's on my board"), "To do: Pay rent and Old bill. Done: Shipped.")

    def test_ordinary_sentences_are_left_alone(self):
        for text in ("add milk to my shopping list", "move this window to the left", "what did he say",
                     "delete my memories", "remind me in 5 minutes to call sam", "what is due diligence"):
            self.assertIsNone(self.say(text), text)

    def test_it_is_routed_and_can_be_switched_off(self):
        self.assertEqual(backend.handle_utterance(None, "add a card water the plants"), "Added Water the plants to To do.")
        backend.CONFIG["board_enabled"] = False
        self.assertIsNone(backend.handle_board("add a card water the plants"))


class ReminderTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.todo = col("To do")["id"]

    def at(self, hour, day=THU):
        return datetime.combine(day, dtime(hour, 0))

    def test_once_a_day_from_the_set_time(self):
        board.add_card(self.todo, "Pay rent", THU.isoformat())
        board.add_card(self.todo, "Old bill", (THU - timedelta(2)).isoformat())
        board.add_card(self.todo, "Later", (THU + timedelta(1)).isoformat())
        board.add_card(col("Done")["id"], "Finished", THU.isoformat())                 # done: never
        self.assertEqual(board.take_due_reminders(self.at(8)), [])                        # before 9:00
        due = board.take_due_reminders(self.at(9))
        self.assertEqual([c["title"] for c, _col in due], ["Old bill", "Pay rent"])
        self.assertEqual(board.take_due_reminders(self.at(15)), [])                       # once a day
        board.enable_persistence(common.backend.CONFIG_PATH.parent / "board.json")        # ...even after a restart
        self.assertEqual(board.take_due_reminders(self.at(16)), [])
        tomorrow = board.take_due_reminders(self.at(9, THU + timedelta(1)))
        self.assertEqual([c["title"] for c, _col in tomorrow], ["Old bill", "Pay rent", "Later"])   # overdue: again

    def test_a_new_due_date_gets_a_new_reminder(self):
        card = board.add_card(self.todo, "Pay rent", THU.isoformat())
        board.take_due_reminders(self.at(10))
        board.update_card(card["id"], due=(THU - timedelta(1)).isoformat())
        self.assertEqual(len(board.take_due_reminders(self.at(11))), 1)
        board.update_card(card["id"], title="Pay the rent")                               # a title change: no
        self.assertEqual(board.take_due_reminders(self.at(12)), [])

    def test_what_is_said(self):
        card = lambda title, n: ({"title": title, "due": (THU - timedelta(n)).isoformat()}, {})
        self.assertEqual(board.reminder_text([card("Pay rent", 0)], THU), "Pay rent is due today.")
        self.assertEqual(board.reminder_text([card("Old bill", 1)], THU), "Old bill was due yesterday.")
        self.assertEqual(board.reminder_text([card("Old bill", 3)], THU), "Old bill was due on Monday.")
        self.assertEqual(board.reminder_text([card("A", 2), card("B", 0)], THU),
                         "2 cards are due: A (overdue) and B (today).")

    def test_the_assistant_announces_them_and_open_shows_the_board(self):
        from controller import Assistant
        ctl = Assistant()
        said, cards, shown = [], [], []
        ctl._announce = lambda app, what, open_board=False: said.append((app, what, open_board))
        board.add_card(self.todo, "Pay rent", THU.isoformat())
        ctl.check_board_reminders(self.at(7))
        self.assertEqual(said, [])
        ctl.check_board_reminders(self.at(9))
        self.assertEqual(said, [("Board", "Pay rent is due today.", True)])
        common.backend.CONFIG["board_reminders"] = False
        board.add_card(self.todo, "Another", THU.isoformat())
        ctl.check_board_reminders(self.at(10))
        self.assertEqual(len(said), 1)
        ctl.board_requested.connect(lambda: shown.append(True))
        ctl.notification.connect(cards.append)
        ctl.open_notification_app({"app": "Board", "open_board": True})
        self.assertEqual(shown, [True])


class WindowTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        from ui.board import BoardWindow
        self.win = BoardWindow(None)
        self.win.show()
        pump(50)

    def tearDown(self):
        self.win.close()

    def test_adding_cards_and_columns_from_the_window(self):
        todo = self.win.columns[0]
        todo.add_btn.click()
        self.assertTrue(todo.adder.isVisible())
        todo.new_title.setText("Pay rent tomorrow")
        todo._add_card()
        pump(50)
        todo = self.win.columns[0]                                           # redrawn
        self.assertTrue(todo.adder.isVisible())                              # stays open for the next card
        card = col("To do")["cards"][0]
        self.assertEqual((card["title"], card["due"]), ("Pay rent", (date.today() + timedelta(1)).isoformat()))
        self.win.add_column_btn.click()
        self.win.column_name.setText("Ideas")
        self.win._add_column()
        self.assertEqual([c.column["name"] for c in self.win.columns], ["To do", "Doing", "Done", "Ideas"])

    def test_voice_changes_redraw_it(self):
        from controller import Assistant
        from ui.board import BoardWindow
        ctl = Assistant()
        board.HOOKS["changed"] = ctl.board_changed.emit
        win = BoardWindow(ctl)
        try:
            board.handle_board_command("add a card buy milk")
            pump(100)
            self.assertEqual(win.columns[0].cards.count(), 1)
        finally:
            board.HOOKS["changed"] = None
            win.close()

    def test_rename_and_card_actions(self):
        board.add_card(col("To do")["id"], "Card")
        self.win.reload()
        todo = self.win.columns[0]
        todo.start_rename()
        todo.name_edit.setText("Backlog")
        todo._finish_rename()
        pump(200)
        self.assertEqual(self.win.columns[0].column["name"], "Backlog")
        card = col("Backlog")["cards"][0]
        self.win.columns[0]._set_due(card, "2026-10-01")
        self.win.columns[0]._move_card(card, col("Done")["id"])
        pump(200)
        self.assertEqual(col("Done")["cards"][0]["due"], "2026-10-01")
        self.assertEqual(self.win.columns[2].cards.count(), 1)

    def test_dragging_a_card_onto_another_column(self):
        from PySide6.QtCore import QPointF
        a = board.add_card(col("To do")["id"], "A")
        board.add_card(col("Done")["id"], "B")
        self.win.reload()
        pump(50)
        source, target = self.win.columns[0].cards, self.win.columns[2].cards
        source.setCurrentRow(0)
        first = target.visualItemRect(target.item(0))

        class Drop:                                   # what Qt hands dropEvent, minus the drag machinery
            def __init__(self, y):
                self.y, self.accepted = y, False

            def source(self):
                return source

            def position(self):
                return QPointF(10, self.y)

            def setDropAction(self, _action):
                pass

            def accept(self):
                self.accepted = True

            def ignore(self):
                pass

        drop = Drop(first.top() + 2)                   # the upper half of B: before it
        target.dropEvent(drop)
        self.assertTrue(drop.accepted)
        self.assertEqual([c["title"] for c in col("Done")["cards"]], ["A", "B"])
        self.assertEqual(col("To do")["cards"], [])
        self.assertEqual(a["id"], col("Done")["cards"][0]["id"])

    def test_one_selected_card_on_the_whole_board(self):
        for name in ("To do", "Doing"):
            board.add_card(col(name)["id"], name + " card")
        self.win.reload()
        pump(50)
        first, second = self.win.columns[0].cards, self.win.columns[1].cards
        first.setCurrentRow(0)
        second.setCurrentRow(0)
        self.assertEqual((len(first.selectedItems()), len(second.selectedItems())), (0, 1))
        first.setCurrentRow(0)
        self.assertEqual((len(first.selectedItems()), len(second.selectedItems())), (1, 0))

    def test_the_drag_image_is_the_whole_card(self):
        board.add_card(col("To do")["id"], "A card with a long enough title to wrap onto a second line", "2026-10-01")
        self.win.reload()
        pump(100)
        cards = self.win.columns[0].cards
        rect = cards.visualItemRect(cards.item(0))
        pixmap = cards.card_pixmap(cards.item(0))
        self.assertEqual((round(pixmap.width() / pixmap.devicePixelRatio()), round(pixmap.height() / pixmap.devicePixelRatio())),
                         (rect.width(), rect.height()))
        self.assertGreaterEqual(rect.width(), cards.viewport().width() - 2 * cards.spacing())    # as wide as the list
        self.assertFalse(cards.verticalScrollBar().isVisible())                                  # and nothing is cut off

    def test_delete_removes_the_selected_card(self):
        from PySide6.QtTest import QTest
        board.add_card(col("To do")["id"], "Keep")
        board.add_card(col("To do")["id"], "Bin")
        self.win.reload()
        pump(50)
        cards = self.win.columns[0].cards
        QTest.keyClick(cards, Qt.Key_Delete)                                  # nothing selected: nothing goes
        self.assertEqual(len(col("To do")["cards"]), 2)
        cards.setCurrentRow(1)
        QTest.keyClick(cards, Qt.Key_Delete)
        pump(100)
        self.assertEqual([c["title"] for c in col("To do")["cards"]], ["Keep"])
        self.assertEqual(self.win.columns[0].cards.count(), 1)

    def _wheel(self, target, notches=-1, shift=False, pixels=None):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        event = QWheelEvent(QPointF(20, 20), QPointF(target.mapToGlobal(QPoint(20, 20))),
                            QPoint(0, pixels or 0), QPoint(0, int(notches * 120)), Qt.NoButton,
                            Qt.ShiftModifier if shift else Qt.NoModifier, Qt.NoScrollPhase, False)
        return self.win.eventFilter(target, event)

    def _full_board(self):
        for i in range(8):
            board.add_column(f"Column {i}")
        for i in range(30):
            board.add_card(col("To do")["id"], f"Card {i}")
        self.win.resize(900, 420)
        self.win.reload()
        pump(150)
        return self.win.scroll.horizontalScrollBar(), self.win.columns[0].cards.verticalScrollBar()

    def test_the_wheel_scrolls_smoothly_down_a_column_or_across_the_board(self):
        across, down = self._full_board()
        self.assertGreater(across.maximum(), 0)
        self.assertGreater(down.maximum(), 0)
        self.assertTrue(self._wheel(self.win.columns[0].cards.viewport()))    # over a column: its cards
        pump(20)
        self.assertLess(down.value(), WHEEL_STEP_CARDS())                      # still gliding, not a jump
        pump(300)
        self.assertEqual((down.value(), across.value()), (WHEEL_STEP_CARDS(), 0))
        self._wheel(self.win.columns[0].cards.viewport(), -2)                 # notches mid-glide add up
        self._wheel(self.win.columns[0].cards.viewport(), -1)
        pump(300)
        self.assertEqual(down.value(), 4 * WHEEL_STEP_CARDS())
        self._wheel(self.win.columns[0].cards.viewport(), shift=True)         # Shift: across
        self._wheel(self.win.scroll.viewport())                               # the empty board: across
        pump(300)
        self.assertEqual(across.value(), 2 * WHEEL_STEP_SIDEWAYS())

    def test_scroll_sideways_option(self):
        across, down = self._full_board()
        self.win.sideways.setChecked(True)
        self.assertIs(common.backend.CONFIG["board_scroll_sideways"], True)
        self._wheel(self.win.columns[0].cards.viewport())
        pump(300)
        self.assertEqual((across.value(), down.value()), (WHEEL_STEP_SIDEWAYS(), 0))
        self._wheel(self.win.columns[0].cards.viewport(), shift=True)         # Shift: down the column
        pump(300)
        self.assertEqual(down.value(), WHEEL_STEP_CARDS())
        self._wheel(self.win.columns[0].cards.viewport(), notches=0, pixels=-37)   # a touchpad: exact, at once
        self.assertEqual(across.value(), WHEEL_STEP_SIDEWAYS() + 37)

    def test_card_dialog(self):
        from ui.board import CardDialog
        card = board.add_card(col("To do")["id"], "Old", "2026-10-01")
        dialog = CardDialog(card, self.win)
        dialog.title.setText("New")
        dialog.has_due.setChecked(False)
        dialog._save()
        self.assertEqual((col("To do")["cards"][0]["title"], col("To do")["cards"][0]["due"]), ("New", None))
        CardDialog(col("To do")["cards"][0], self.win)._delete()
        self.assertEqual(col("To do")["cards"], [])


if __name__ == "__main__":
    unittest.main()
