"""File search through Everything (filesearch.py) and reusing recent lookups (lookup_cache.py).

Everything is never asked for real here (except in the live test): the IPC reply is built by hand in the
format everything_ipc.h describes, and opening / revealing files is stubbed."""

from __future__ import annotations

import json
import struct
import time
import unittest
from datetime import date, datetime
from pathlib import Path

from tests import common
from tests.common import backend, live_only
from tests.test_finish_pass import Patch

import filesearch as fs
import lookup_cache
import system_control
import websearch


def list2(items, flags=fs.REQUEST_NAME | fs.REQUEST_PATH | fs.REQUEST_SIZE | fs.REQUEST_DATE_MODIFIED):
    """An EVERYTHING_IPC_LIST2 reply: header, item table, then each item's data in the documented order."""
    header_size, table = 20, 8 * len(items)
    data, table_bytes = b"", b""
    for name, folder, is_folder, size, filetime in items:
        table_bytes += struct.pack("<2I", fs.ITEM_FOLDER if is_folder else 0, header_size + table + len(data))
        for text in (name, folder):
            data += struct.pack("<I", len(text)) + text.encode("utf-16-le") + b"\0\0"
        data += struct.pack("<q", size) + struct.pack("<Q", filetime)
    return struct.pack("<5I", len(items), len(items), 0, flags, fs.SORT_DATE_MODIFIED_DESCENDING) + table_bytes + data


FT_2026_09_20 = 134343792000000000        # a FILETIME on 20 September 2026


class ProtocolTests(unittest.TestCase):
    def test_the_query_is_laid_out_as_everything_expects(self):
        raw = fs.query_bytes("resume", 0x1234, 60, fs.SORT_DATE_MODIFIED_DESCENDING, fs.REQUEST_NAME)
        self.assertEqual(struct.unpack_from("<7I", raw), (0x1234, fs._REPLY_ID, 0, 0, 60, fs.REQUEST_NAME, 14))
        self.assertEqual(raw[28:], "resume".encode("utf-16-le") + b"\0\0")

    def test_replies_are_read_back(self):
        hits = fs.parse_list2(list2([("Resume.pdf", r"C:\Users\me\Documents", False, 5120, FT_2026_09_20),
                                     ("Taxes", r"D:\Paperwork", True, -1, 0)]))
        self.assertEqual([(h.name, h.path, h.is_folder, h.size) for h in hits],
                         [("Resume.pdf", r"C:\Users\me\Documents\Resume.pdf", False, 5120),
                          ("Taxes", r"D:\Paperwork\Taxes", True, -1)])
        self.assertEqual(hits[0].modified.date(), date(2026, 9, 20))
        self.assertIsNone(hits[1].modified)
        self.assertEqual(fs.parse_list2(b""), [])

    def test_the_search_string(self):
        s = fs.search_string("tax return", "pdf")
        self.assertTrue(s.startswith("tax return ext:pdf "))
        self.assertIn("!\\AppData\\", s)
        self.assertEqual(fs.search_string('bad"|name', "", system=True), "bad name")


class SpeechTests(unittest.TestCase):
    def test_what_counts_as_a_file_command(self):
        c = fs.spoken_file_command
        cases = {
            "find the file called resume": ("find", "resume", "file"),
            "where did I save my budget spreadsheet": ("find", "budget", "spreadsheet"),
            "find my screenshots folder": ("find", "screenshots", "folder"),
            "search my computer for invoice": ("find", "invoice", ""),
            "find resume dot pdf": ("find", "resume.pdf", ""),
            "can you find the pdf called tax return on my pc": ("find", "tax return", "pdf"),
            "open the file called budget": ("open", "budget", "file"),
            "open my resume pdf": ("open", "resume", "pdf"),
            "open budget.xlsx": ("open", "budget.xlsx", ""),
            "show resume in explorer": ("reveal", "resume", ""),
            "open the folder that has my resume": ("reveal", "resume", ""),
        }
        for text, want in cases.items():
            got = c(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.action, got.words, got.kind), want, text)
        self.assertEqual((c("open the second one").index, c("open the second one").pick_action), (1, "open"))
        self.assertEqual(c("show it in the folder").pick_action, "reveal")
        for text in ("find my phone", "find my keys", "open spotify", "where is the nearest pizza place",
                     "find a restaurant near me", "open the song called smile", "what's the weather"):
            self.assertIsNone(c(text), text)

    def test_ranking_and_places(self):
        hits = [fs.Hit("resume old notes.txt", "C:\\x"), fs.Hit("Resume.pdf", "C:\\y"), fs.Hit("resume2.docx", "C:\\z")]
        self.assertEqual([h.name for h in fs.rank(hits, "resume")], ["Resume.pdf", "resume old notes.txt", "resume2.docx"])   # ties keep Everything's (newest-first) order
        home = Path.home()
        self.assertEqual(fs.friendly_folder(str(home / "Downloads")), "Downloads")
        self.assertEqual(fs.friendly_folder(str(home / "Documents" / "Work")), "the Work folder in Documents")
        self.assertEqual(fs.friendly_folder("D:\\"), "the root of drive D")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG["files_enabled"] = True
        patch = Patch(self)
        patch(system_control, "perform", lambda *a, **k: self.fail("a file command reached the PC"))
        self.opened, self.listed, self.searched = [], [], []
        patch(fs, "ensure_running", lambda: None)
        self.hits = [fs.Hit("Resume 2026.pdf", str(Path.home() / "Documents"), modified=datetime(2026, 9, 24, 10)),
                     fs.Hit("Resume.docx", str(Path.home() / "Desktop"), modified=datetime(2026, 1, 3)),
                     fs.Hit("resume-builder.exe", "C:\\Tools")]
        patch(fs, "_raw_search", self.fake_search)
        fs.ACTIONS.update(open=lambda p: self.opened.append(("open", p)), reveal=lambda p: self.opened.append(("reveal", p)))
        fs.HOOKS["listing"] = self.listed.append

    def fake_search(self, search, max_results=60):
        """Everything's side: the kind filter is applied, newest first."""
        self.searched.append(search)
        return [h for h in self.hits if "ext:pdf" not in search or h.name.endswith(".pdf")]

    def say(self, text):
        return backend.handle_utterance(None, text)

    def test_find_then_open_by_number(self):
        reply = self.say("find the file called resume")
        self.assertIn("I found 3 files with \"resume\" in the name.", reply)
        self.assertIn("The best match is Resume.docx in Desktop, changed on January 3.", reply)   # the exact name
        self.assertIn("file:", self.searched[0])
        self.assertIn("1. " + str(Path.home() / "Desktop" / "Resume.docx"), self.listed[0])
        self.assertEqual(self.say("open the second one"), "Opening Resume 2026.pdf.")
        self.assertEqual(self.say("show it in the folder"), "Here's Resume.docx, in Desktop.")
        self.assertEqual(self.opened, [("open", str(Path.home() / "Documents" / "Resume 2026.pdf")),
                                       ("reveal", str(Path.home() / "Desktop" / "Resume.docx"))])

    def test_programs_are_never_run_from_a_search(self):
        self.say("find the file called resume")
        reply = self.say("open the third one")
        self.assertIn("won't run it", reply)
        self.assertEqual(self.opened, [("reveal", "C:\\Tools\\resume-builder.exe")])

    def test_open_goes_straight_to_the_best_match(self):
        self.assertEqual(self.say("open my resume pdf"), "Opening Resume 2026.pdf.")
        self.assertIn("ext:pdf", self.searched[0])

    def test_nothing_found_and_switched_off(self):
        self.hits = []
        self.assertEqual(self.say("find the pdf called zzz"), "I couldn't find any pdfs with \"zzz\" in the name.")
        self.assertIsNone(fs.handle_file_command("open it"))           # nothing found lately: not ours
        backend.CONFIG["files_enabled"] = False
        self.assertIsNone(backend.handle_files("find the file called resume"))

    def test_everything_missing_says_how_to_get_it(self):
        def missing():
            raise fs.EverythingError("Finding files needs Everything, the free file search from voidtools.")
        Patch(self)(fs, "ensure_running", missing)
        self.assertIn("needs Everything", self.say("find the file called resume"))


@live_only
class EverythingLiveTests(unittest.TestCase):
    def test_a_real_query(self):
        if not fs.everything_window():
            self.skipTest("Everything isn't running")
        started = time.monotonic()
        hits = fs.search("assistant", "file")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(all(isinstance(h.path, str) and "assistant" in h.name.lower() for h in hits))


class LookupCacheTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_newest_fifty_per_kind_and_ages(self):
        for i in range(55):
            lookup_cache.put("answer", f"q{i}", i)
        self.assertIsNone(lookup_cache.get("answer", "q0", 60))              # the oldest went
        self.assertEqual(lookup_cache.get("answer", "q54", 60), 54)
        lookup_cache.get("answer", "q5", 60)                                 # used: it stays
        lookup_cache.put("answer", "new", 1)
        self.assertEqual(lookup_cache.get("answer", "q5", 60), 5)
        self.assertIsNone(lookup_cache.get("answer", "q6", 60))
        self.assertEqual(lookup_cache.count(), 50)
        self.assertIsNone(lookup_cache.get("answer", "q54", -1))             # too old for this reader

    def test_it_survives_a_restart_and_can_be_cleared(self):
        lookup_cache.put("place", "paris", [48.8, 2.3, "Paris", "FR", "Europe/Paris"])
        lookup_cache.enable_persistence(backend.CONFIG_PATH.parent / "lookup_cache.json")
        self.assertEqual(lookup_cache.get("place", "paris", 60)[2], "Paris")
        lookup_cache.clear()
        self.assertEqual(lookup_cache.count(), 0)
        self.assertEqual(lookup_cache.normalize("  When did Silksong come out?? "), "when did silksong come out")

    def test_a_fact_is_looked_up_once(self):
        patch = Patch(self)
        finds, asks, shown = [], [], []
        article = websearch.Article("Hollow Knight: Silksong", "A game.", facts=["Publication date: 4 September 2025"])
        patch(websearch, "find", lambda q, *a: finds.append(q) or websearch.Findings(q, article, [("t", "s", "u")]))
        patch(backend, "ollama_answer", lambda *a, **k: asks.append(1) or "It came out on 4 September 2025.")
        backend.CONFIG["wiki_popup"] = True
        backend.SEARCH_HOOKS["article"] = shown.append
        self.addCleanup(lambda: backend.SEARCH_HOOKS.update(article=None))
        first = backend.web_search_and_answer("when did silksong come out")
        again = backend.web_search_and_answer("When did Silksong come out?")
        self.assertEqual(first, again)
        self.assertEqual((len(finds), len(asks)), (1, 1))
        self.assertEqual(len(shown), 2)                                       # the pop-up shows both times
        backend.CONFIG["lookup_cache"] = False
        backend.web_search_and_answer("when did silksong come out")
        self.assertEqual(len(finds), 2)

    def test_the_fact_checker_reuses_the_search(self):
        finds = []
        Patch(self)(websearch, "find", lambda q, *a: finds.append(q) or websearch.Findings(q, None, [("t", "s", "u")]))
        backend._find("who directed dune")
        backend._find("Who directed Dune?")
        self.assertEqual(len(finds), 1)

    def test_places_are_looked_up_once_and_failures_not_kept(self):
        calls = []
        Patch(self)(backend, "_get_json", lambda url, timeout=6.0: calls.append(url) or (
            {"results": [{"latitude": 1.0, "longitude": 2.0, "name": "Paris", "country_code": "FR",
                          "timezone": "Europe/Paris"}]} if "paris" in url.lower() else None))
        self.assertEqual(backend.geocode("Paris"), (1.0, 2.0, "Paris", "FR", "Europe/Paris"))
        self.assertEqual(backend.geocode("paris"), (1.0, 2.0, "Paris", "FR", "Europe/Paris"))
        self.assertEqual(len(calls), 1)
        backend.geocode("Nowhereville")
        backend.geocode("Nowhereville")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
