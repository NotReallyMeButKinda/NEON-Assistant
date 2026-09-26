"""The September 25 pass: natural speech (intent.py), grounded facts and fact-checking, closing windows by
name with a confirmation, the browser bridge, Bitwarden, and the small safety pieces around them.

Nothing here reaches outside the process: Ollama, the web, the real clipboard, bw.exe, SendInput and
WM_CLOSE are all stubbed, and the bridge only ever talks to a fake extension on a free local port."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests import common
from tests.common import backend

import bitwarden
import board
import browser_bridge
import browser_commands
import clipboard
import filesearch
import fun
import intent
import memory_store
import routines
import settings_schema
import system_control
import timers
import units
import websearch
from commands import spoken_calendar_query, spoken_help_query, spoken_media_action, spoken_music_command
from windows import spoken_close_command, spoken_window_command


class Patch:
    """Set attributes for one test and put them back afterwards."""

    def __init__(self, case: unittest.TestCase):
        self._saved: list = []
        case.addCleanup(self.undo)

    def __call__(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
        return value

    def undo(self):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        self._saved.clear()


# ---------------------------------------------------------------------------
# Natural speech
# ---------------------------------------------------------------------------

class NormalizeTests(unittest.TestCase):
    def test_fillers_politeness_and_the_name_go(self):
        n = intent.normalize_utterance
        names = ("nova", "hey nova")
        self.assertEqual(n("um, nova, could you please pause the music for me", names), "pause the music")
        self.assertEqual(n("uh can you set a timer for ten minutes thanks", names), "set a timer for ten minutes")
        self.assertEqual(n("I'd like you to open spotify please", names), "open spotify")
        self.assertEqual(n("okay so, lock the pc", names), "lock the pc")
        self.assertEqual(n("nova what's the weather", names), "what's the weather")

    def test_questions_and_unchanged_text_are_left_alone(self):
        n = intent.normalize_utterance
        text = "can you hear me"
        self.assertIs(n(text, ("nova",)), text)                  # not a command verb after "can you"
        plain = "pause the music"
        self.assertIs(n(plain, ("nova",)), plain)
        # an assistant called Max must not eat "max" out of a sentence
        self.assertEqual(n("max volume", ("max",)), "max volume")
        self.assertEqual(n("max, pause", ("max",)), "pause")


class FactQuestionTests(unittest.TestCase):
    def test_world_facts_are_recognised(self):
        for q in ("when did hollow knight silksong come out", "When does GTA 6 release?",
                  "who directed dune part two", "who developed celeste", "how old is keanu reeves",
                  "what year did minecraft come out", "what's the release date of the switch 2",
                  "is elden ring nightreign out yet", "how many copies did minecraft sell",
                  "what is the population of canada", "which studio made hades"):
            self.assertTrue(intent.looks_like_fact_question(q), q)

    def test_questions_about_us_or_conversation_are_not(self):
        for q in ("when did i set that timer", "who are you", "how are you", "what should i name my cat",
                  "when is my dentist appointment", "how old are you", "tell me a joke"):
            self.assertFalse(intent.looks_like_fact_question(q), q)


SAMPLE_SLOTS = {"window": "discord", "query": "daft punk", "seconds": "30", "percent": "40",
                "duration": "10 minutes", "what": "call mom", "fact": "the gate code is 4821", "topic": "gate code",
                "card": "buy milk", "column": "to do", "when": "today", "amount": "5", "from_unit": "miles",
                "to_unit": "kilometers", "name": "work mode", "text": "refund policy", "tab": "github",
                "account": "proton mail"}


class IntentCatalogueTests(unittest.TestCase):
    """Every template must render a sentence its own parser accepts, or the AI's understanding would
    silently fall on the floor."""

    def setUp(self):
        common.use_temp_config()
        backend.CONFIG["routines"] = [{"name": "work mode", "steps": ["say: hi"]}]

    @staticmethod
    def _parsers():
        sys_cmd = system_control.spoken_system_command
        return {
            "close_window": spoken_close_command, "close_current_window": spoken_close_command,
            "minimize_window": spoken_window_command, "maximize_window": spoken_window_command,
            "restore_window": spoken_window_command,
            **{k: spoken_media_action for k in ("media_pause", "media_play", "media_next", "media_previous",
                                                 "media_louder", "media_quieter")},
            **{k: spoken_music_command for k in ("play_music", "now_playing", "like_song", "seek_forward",
                                                  "seek_back")},
            **{k: sys_cmd for k in ("set_volume", "get_volume", "mute_pc", "unmute_pc", "lock_pc", "sleep_pc",
                                    "shutdown_pc", "restart_pc", "sign_out", "empty_bin", "screenshot",
                                    "show_desktop", "uptime", "disk_space", "brightness")},
            **{k: timers.spoken_timer_command for k in ("timer_set", "reminder", "timer_status", "timer_cancel")},
            **{k: memory_store.spoken_memory_command for k in ("remember", "recall", "forget")},
            "clipboard_last": clipboard.handle_clipboard_command, "copy_reply": clipboard.handle_clipboard_command,
            **{k: board.handle_board_command for k in ("board_add", "board_move", "board_done", "board_show",
                                                        "board_list", "board_due")},
            **{k: backend.spoken_notification_command for k in ("notifications_read", "notifications_summary",
                                                                 "notifications_clear")},
            "notifications_pause": lambda t: backend._PAUSE_RE.fullmatch(t),
            "calendar": spoken_calendar_query, "convert_units": units.spoken_conversion,
            "run_routine": lambda t: routines.spoken_routine(t, ["work mode"]),
            "summarize_selection": backend.spoken_selection_action, "explain_selection": backend.spoken_selection_action,
            "joke": fun.handle_fun_command, "coin": fun.handle_fun_command, "dice": fun.handle_fun_command,
            "help": spoken_help_query,
            **{k: browser_commands.spoken_browser_command for k in ("page_summary", "page_read", "page_find",
                                                                     "tabs_list", "tab_switch", "tab_close",
                                                                     "tab_close_named")},
            "find_file": filesearch.spoken_file_command, "open_file": filesearch.spoken_file_command,
            **{k: bitwarden.spoken_command for k in ("bw_username", "bw_password_copy", "bw_password_type",
                                                      "bw_totp", "bw_lock", "bw_unlock")},
        }

    def test_every_template_parses(self):
        board.add_card("To do", "buy milk")
        parsers = self._parsers()
        for item in intent.CATALOGUE:
            if item.call:
                continue
            with self.subTest(intent=item.name):
                self.assertIn(item.name, parsers, "a new template intent needs its parser listed here")
                sentence = intent.render(item.name, SAMPLE_SLOTS)
                self.assertTrue(sentence, "didn't render")
                self.assertTrue(parsers[item.name](sentence), f"{sentence!r} isn't accepted by its parser")

    def test_classify_turns_the_models_answer_into_a_command(self):
        asked = {}

        def ask(prompt, schema, system):
            asked.update(prompt=prompt, schema=schema)
            return {"intent": "timer_set", "slots": {"duration": " 10 minutes. "}}
        got = intent.classify("give me ten on the clock", ask)
        self.assertEqual(got.command, "set a timer for 10 minutes")
        self.assertIn("timer_set", asked["schema"]["properties"]["intent"]["enum"])
        # unknown intents, missing slots, a dead model: nothing
        self.assertIsNone(intent.classify("x", lambda *a: {"intent": "nope", "slots": {}}))
        self.assertIsNone(intent.classify("x", lambda *a: {"intent": "timer_set", "slots": {}}))
        self.assertIsNone(intent.classify("x", lambda *a: None))
        # an excluded intent is neither offered nor accepted
        self.assertIsNone(intent.classify("x", lambda *a: {"intent": "page_summary", "slots": {}},
                                          exclude={"page_summary"}))

    def test_the_router_runs_what_the_model_understood(self):
        common.use_temp_config()
        patch = Patch(self)
        backend.CONFIG["smart_commands"] = True
        pressed = []
        patch(backend, "media_control", lambda action: pressed.append(action) or f"did {action}")
        patch(backend, "ollama_json", lambda *a, **k: {"intent": "media_pause", "slots": {}})
        agent = unittest.mock.Mock()
        reply = backend.handle_utterance(agent, "shut the music up for a sec")
        self.assertEqual(pressed, ["pause"])
        self.assertEqual(reply, "did pause")
        agent.run.assert_not_called()

    def test_calls_and_a_dead_model(self):
        common.use_temp_config()
        patch = Patch(self)
        backend.CONFIG["smart_commands"] = True
        patch(backend, "launch_app", lambda app: f"Opening {app}.")
        patch(backend, "ollama_json", lambda *a, **k: {"intent": "open_app", "slots": {"app": "discord"}})
        self.assertEqual(backend.handle_utterance(unittest.mock.Mock(), "fire up discord would you"),
                         "Opening discord.")
        # Ollama down: the tool-picker still gets it
        patch(backend, "ollama_json", lambda *a, **k: None)
        agent = unittest.mock.Mock()
        agent.run.return_value = {"results": [{"status": "picked by needle"}]}
        self.assertEqual(backend.handle_utterance(agent, "fire up discord would you"), "picked by needle")


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def _findings(text="Geometry Dash was released on 13 August 2013."):
    article = websearch.Article(title="Geometry Dash", extract=text,
                                facts=["publication date: 13 August 2013"]) if text else None
    return websearch.Findings("q", article, [])


class FactTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.patch = Patch(self)

    def test_fact_questions_skip_the_models_memory(self):
        backend.CONFIG["ground_facts"] = True
        looked = []
        self.patch(backend, "web_search_and_answer", lambda q: looked.append(q) or "13 August 2013.")
        self.patch(backend, "ollama_chit_chat", lambda t: self.fail("answered from memory"))
        reply = backend.handle_utterance(unittest.mock.Mock(), "when did geometry dash come out")
        self.assertEqual(reply, "13 August 2013.")
        self.assertEqual(looked, ["when did geometry dash come out"])

    def test_a_contradicted_answer_is_corrected(self):
        self.patch(backend, "ollama_json", lambda *a, **k: {"verdict": "contradicted",
                                                              "corrected_answer": "It came out on 13 August 2013."})
        self.assertEqual(backend.fact_check("when did geometry dash come out", "It came out in 2015.", _findings()),
                         "It came out on 13 August 2013.")

    def test_supported_unverifiable_and_offline(self):
        self.patch(backend, "ollama_json", lambda *a, **k: {"verdict": "supported", "corrected_answer": "x"})
        self.assertEqual(backend.fact_check("q", "13 August 2013.", _findings()), "13 August 2013.")
        self.patch(backend, "ollama_json", lambda *a, **k: {"verdict": "unverifiable", "corrected_answer": ""})
        self.assertTrue(backend.fact_check("q", "In 2013.", _findings()).endswith("I couldn't confirm that, though."))
        self.assertTrue(backend.fact_check("q", "In 2013.", _findings(text="")).endswith("check that online, though."))

    def test_chat_answers_are_checked_only_when_worth_it(self):
        self.assertTrue(backend.worth_checking("when was the eiffel tower built", "It was finished in 1889."))
        self.assertFalse(backend.worth_checking("tell me a story", "Once upon a time in 1889..."))
        self.assertFalse(backend.worth_checking("how are you", "I'm doing well, thanks."))
        backend.CONFIG["fact_check"] = True
        checked = []
        self.patch(backend, "ollama_answer", lambda *a, **k: "It was finished in 1887.")
        self.patch(backend, "fact_check", lambda q, a, f=None: checked.append(a) or "It was finished in 1889.")
        self.assertEqual(backend.ollama_chit_chat("when was the eiffel tower built"), "It was finished in 1889.")
        self.assertEqual(checked, ["It was finished in 1887."])

    def test_nothing_streams_while_fact_checking(self):
        backend.CONFIG["fact_check"] = True
        pieces = []
        self.patch(backend, "_STREAM", {"sink": pieces.append})
        self.patch(backend, "ollama_generate", lambda *a, **k: "whole answer")
        self.assertEqual(backend.ollama_answer("q"), "whole answer")
        self.assertEqual(pieces, [])


# ---------------------------------------------------------------------------
# Closing windows by name
# ---------------------------------------------------------------------------

class CloseWindowTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.patch = Patch(self)
        self.closed, self.outlined, self.cleared = [], [], []
        self.patch(backend, "find_windows", lambda name: [(111, 999, "Discord")] if name == "discord" else [])
        self.patch(backend, "close_windows", lambda targets: self.closed.append(targets) or "Closing Discord.")
        backend.LOCAL_CONFIRMED["close_window"] = backend.close_windows
        self.addCleanup(lambda: backend.LOCAL_CONFIRMED.__setitem__("close_window", self._real_close))
        self.patch(backend, "fuzzy_resolve_app", lambda *a, **k: None)
        backend.WINDOW_HOOKS.update(highlight=lambda hwnds, s: self.outlined.append(hwnds),
                                    clear=lambda: self.cleared.append(True))
        self.agent = unittest.mock.Mock()

    import windows as _w
    _real_close = _w.close_windows

    def say(self, text):
        return backend.handle_utterance(self.agent, text)

    def test_the_parser(self):
        self.assertEqual(spoken_close_command("close discord"), ("discord", False, False))
        self.assertEqual(spoken_close_command("quit the spotify app"), ("spotify", False, True))
        self.assertEqual(spoken_close_command("close all chrome windows"), ("chrome", True, True))
        self.assertEqual(spoken_close_command("close this app"), ("", False, True))
        for text in ("shut down the pc", "close this tab", "close the github tab", "close notifications",
                     "turn off the computer", "close it all"):
            self.assertIsNone(spoken_close_command(text), text)

    def test_asks_outlines_and_closes_on_yes(self):
        self.assertEqual(self.say("close discord"), "Close Discord?")
        self.assertEqual(self.outlined, [[111]])
        self.assertEqual(self.closed, [])
        self.assertEqual(self.say("yes close it"), "Closing Discord.")
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(self.cleared, [True])

    def test_no_leaves_it_and_anything_else_drops_the_question(self):
        self.say("close discord")
        self.assertEqual(self.say("no"), "Okay, I'll leave it.")
        self.assertEqual(self.closed, [])
        self.say("close discord")
        self.patch(backend, "media_control", lambda a: "paused")
        self.assertEqual(self.say("pause the music"), "paused")          # routed normally, question gone
        self.assertEqual(backend.pending_confirmation(), "")
        self.assertEqual(len(self.cleared), 2)

    def test_without_confirmation_and_unknown_names(self):
        backend.CONFIG["confirm_window_close"] = False
        self.assertEqual(self.say("close discord"), "Closing Discord.")
        self.assertEqual(self.outlined, [])
        self.assertEqual(backend.handle_close_command("close the deal"), None)       # not a window: falls through
        self.assertEqual(backend.handle_close_command("close the zoom app"), "Zoom isn't open.")


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeExtension(threading.Thread):
    """Long-polls the bridge like background.js and answers from `pages`."""

    def __init__(self, port, token, answers):
        super().__init__(daemon=True)
        self.port, self.token, self.answers = port, token, answers
        self.stop = threading.Event()

    def _req(self, path, body=None, origin="moz-extension://abc"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Neon-Token": self.token, "Origin": origin,
                                              "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()

    def run(self):
        while not self.stop.is_set():
            try:
                status, raw = self._req("/poll")
            except (urllib.error.URLError, OSError):
                return
            if status == 200:
                req = json.loads(raw)
                answer = self.answers.get(req["op"])
                body = {"id": req["id"], "ok": answer is not None, "data": answer, "error": "nope"}
                self._req("/reply", body)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.patch = Patch(self)
        self.patch(browser_bridge, "token", lambda create=True: "secret-token-123")
        self.port = _free_port()
        self.assertTrue(browser_bridge.start(self.port))
        self.addCleanup(browser_bridge.stop)

    def test_a_request_round_trip(self):
        ext = FakeExtension(self.port, "secret-token-123", {"page": {"title": "Hi", "text": "Body"}})
        ext.start()
        self.addCleanup(ext.stop.set)
        self.assertTrue(common.pump_until(browser_bridge.connected, 5))
        self.assertEqual(browser_bridge.request("page"), {"title": "Hi", "text": "Body"})
        with self.assertRaises(browser_bridge.BrowserError):
            browser_bridge.request("tabs")

    def test_strangers_are_refused(self):
        ext = FakeExtension(self.port, "wrong", {})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            ext._req("/hello")
        self.assertEqual(caught.exception.code, 403)
        good = FakeExtension(self.port, "secret-token-123", {})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            good._req("/hello", origin="https://evil.example")        # a web page, even with the token
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(good._req("/hello")[0], 200)
        self.assertEqual(good._req("/hello", origin="chrome-extension://abcdef")[0], 200)   # Chrome, Edge, Brave

    def test_both_extension_packages_build(self):
        import tempfile
        from pathlib import Path
        import build_extension
        folder = Path(tempfile.mkdtemp())
        xpi = build_extension.build(folder / "neon-bridge.xpi")
        unpacked, archive = build_extension.build_chrome(folder / "neon-bridge-chrome")
        import zipfile
        firefox = json.loads(zipfile.ZipFile(xpi).read("manifest.json"))
        chrome = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((firefox["manifest_version"], chrome["manifest_version"]), (2, 3))
        self.assertIn("scripting", firefox["permissions"])
        self.assertEqual(chrome["background"], {"service_worker": "background.js"})
        self.assertIn("<all_urls>", chrome["host_permissions"])
        self.assertNotIn("<all_urls>", chrome["permissions"])
        self.assertIn("action", chrome)
        self.assertEqual(json.loads(zipfile.ZipFile(archive).read("manifest.json")), chrome)
        self.assertTrue((unpacked / "background.js").is_file() and (unpacked / "icons").is_dir())

    def test_nobody_connected(self):
        with self.assertRaises(browser_bridge.BrowserUnavailable):
            browser_bridge.request("page", timeout=0.5)


class BrowserCommandTests(unittest.TestCase):
    def test_parsing(self):
        p = browser_commands.spoken_browser_command
        self.assertEqual(p("summarize this page"), ("summarize", ""))
        self.assertEqual(p("What's this article about?"), ("summarize", ""))
        self.assertEqual(p("read this article to me"), ("read", ""))
        self.assertEqual(p("find refund policy on this page"), ("find", "refund policy"))
        self.assertEqual(p("what's the price on this page")[0], "ask")
        self.assertEqual(p("what does this page say about shipping")[0], "ask")
        self.assertEqual(p("switch to the github tab"), ("switch", "github"))
        self.assertEqual(p("close this tab"), ("close_tab", ""))
        self.assertEqual(p("what tabs do i have open"), ("tabs", ""))
        self.assertIsNone(p("what's the weather"))

    def test_the_relevant_part_of_a_long_page(self):
        filler = "Lorem ipsum dolor sit amet. " * 40
        text = "\n\n".join([f"Intro about the product. {filler}"] + [filler] * 20
                           + ["Shipping takes 3 to 5 business days to Canada."] + [filler] * 20)
        excerpt = browser_commands.relevant_text("how long does shipping to canada take", text, budget=3000)
        self.assertIn("Shipping takes 3 to 5 business days", excerpt)
        self.assertIn("Intro about the product", excerpt)
        self.assertLessEqual(len(excerpt), 3200)

    def test_tabs_by_name(self):
        tabs = [{"id": 1, "title": "Inbox - Proton Mail", "url": "https://mail.proton.me"},
                {"id": 2, "title": "anthropics/claude-code: issues", "url": "https://github.com/x"}]
        self.assertEqual(browser_commands.best_tab(tabs, "github")["id"], 2)
        self.assertEqual(browser_commands.best_tab(tabs, "proton mail")["id"], 1)
        self.assertIsNone(browser_commands.best_tab(tabs, "reddit"))

    def test_page_questions_through_the_router(self):
        common.use_temp_config()
        patch = Patch(self)
        backend.CONFIG["browser_enabled"] = True
        page = {"title": "Widget", "url": "https://shop.example/w", "text": "A fine widget.",
                "structured": [{"type": "Product", "name": "Widget", "price": "19.99", "currency": "USD"}]}
        patch(browser_bridge, "request", lambda op, timeout=6.0, **a: page)
        prompts = []
        patch(backend, "ollama_answer", lambda prompt, system=None, **k: prompts.append(prompt) or "It's $19.99.")
        self.assertEqual(backend.handle_utterance(unittest.mock.Mock(), "what's the price on this page"), "It's $19.99.")
        self.assertIn("price: 19.99", prompts[0])

        def gone(*a, **k):
            raise browser_bridge.BrowserUnavailable("x")
        patch(browser_bridge, "request", gone)
        self.assertIn("can't reach your browser", backend.handle_utterance(unittest.mock.Mock(), "summarize this page"))


# ---------------------------------------------------------------------------
# Bitwarden
# ---------------------------------------------------------------------------

ITEMS = [bitwarden.Item("1", "Proton Mail", "me@proton.me", ["https://account.proton.me"], has_totp=True),
         bitwarden.Item("2", "GitHub", "octo", ["https://github.com"]),
         bitwarden.Item("3", "Proton Pass", "me2@proton.me", ["https://pass.proton.me"])]


class BitwardenTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.patch = Patch(self)
        backend.CONFIG["bitwarden_enabled"] = True
        bitwarden._STATE.update(session="s" * 40, items=list(ITEMS))
        self.addCleanup(lambda: bitwarden._STATE.update(session="", items=[], choice=None, pending=""))
        self.patch(bitwarden, "touch", lambda: None)
        self.fetched = []
        self.patch(bitwarden, "_run", lambda args, **k: self.fetched.append(args) or
                   {"password": "hunter2!", "totp": "123456"}.get(args[1], ""))
        self.copied = []
        self.patch(clipboard, "copy_secret", lambda value, seconds=30: self.copied.append(value) or True)
        self.agent = unittest.mock.Mock()

    def say(self, text):
        return backend.handle_utterance(self.agent, text)

    def test_parsing(self):
        c = bitwarden.spoken_command
        self.assertEqual(c("Pull my username for my Proton Mail account from Bitwarden"),
                         ("username", "proton mail", True))
        self.assertEqual(c("what's my github username")[:2], ("username", "github"))
        self.assertEqual(c("copy my github password")[:2], ("password", "github"))
        self.assertEqual(c("type my proton mail password")[:2], ("type", "proton mail"))
        self.assertEqual(c("what's my github 2fa code")[:2], ("totp", "github"))
        self.assertEqual(c("lock bitwarden")[0], "lock")
        self.assertIsNone(c("what's my wifi password"))           # a remembered fact, not the vault

    def test_the_username_is_said(self):
        self.assertEqual(self.say("Pull my username for my Proton Mail account from Bitwarden"),
                         "Your Proton Mail username is me@proton.me.")

    def test_passwords_are_copied_never_said(self):
        reply = self.say("copy my github password")
        self.assertNotIn("hunter2", reply)
        self.assertEqual(self.copied, ["hunter2!"])
        self.assertIn("Copied your GitHub password", reply)

    def test_two_matches_ask_which(self):
        self.assertEqual(self.say("copy my proton password"), "Which one: Proton Mail or Proton Pass?")
        self.assertIn("Proton Pass", self.say("proton pass"))
        self.assertEqual(self.copied, ["hunter2!"])

    def test_typing_asks_first_and_checks_the_window(self):
        typed = []
        self.patch(backend, "_foreground_window", lambda: (555, 1234, "Proton Mail - Zen"))
        self.patch(backend.dictation, "type_text", lambda text: typed.append(text) or True)
        self.assertEqual(self.say("type my proton mail password"),
                         "Type your Proton Mail password into Proton Mail - Zen?")
        self.assertEqual(typed, [])
        self.assertEqual(self.say("yes"), "Done.")
        self.assertEqual(typed, ["hunter2!"])
        # the focus moved in between: nothing is typed
        self.say("type my proton mail password")
        self.patch(backend, "_foreground_window", lambda: (777, 1234, "Discord"))
        self.assertEqual(self.say("yes"), "The window changed, so I didn't type it.")
        self.assertEqual(typed, ["hunter2!"])

    def test_the_2fa_code_is_spelled(self):
        self.assertEqual(self.say("what's my proton mail 2fa code"),
                         "Your Proton Mail code is 1 2 3, 4 5 6. It's on the clipboard too.")

    def test_locked_and_missing(self):
        bitwarden._STATE.update(session="", items=[])
        asked = []
        self.patch(bitwarden, "HOOKS", {"unlock": lambda: asked.append(True)})
        self.patch(bitwarden, "status", lambda: "locked")
        self.assertIn("vault is locked", self.say("copy my github password"))
        self.assertEqual(asked, [True])
        self.assertEqual(bitwarden.take_pending(), "copy my github password")
        self.patch(bitwarden, "status", lambda: "missing")
        self.assertIsNone(backend.handle_bitwarden("what's my github username"))     # memory may know it
        self.assertIn("isn't installed", backend.handle_bitwarden("what's my github username from bitwarden"))

    def test_unlock_passes_the_password_in_the_environment(self):
        seen = {}

        def run(args, env_extra=None, **k):
            seen.update(args=args, env=env_extra)
            return "S" * 40
        self.patch(bitwarden, "_run", run)
        self.patch(bitwarden, "refresh_items", lambda: 3)
        bitwarden._STATE["session"] = ""
        ok, message = bitwarden.unlock("correct horse")
        self.assertTrue(ok)
        self.assertNotIn("correct horse", " ".join(seen["args"]))
        self.assertEqual(seen["env"], {"NEON_BW_PW": "correct horse"})
        self.assertEqual(message, "3 items")


class SmallPieces(unittest.TestCase):
    def test_a_copied_secret_stays_out_of_the_history(self):
        common.use_temp_config()
        patch = Patch(self)
        board_ = {"text": ""}
        patch(clipboard.selection, "set_text", lambda t: board_.update(text=t) or True)
        patch(clipboard.selection, "read_text", lambda: board_["text"])
        self.assertTrue(clipboard.copy_secret("hunter2!", clear_after=1.0))
        clipboard.record("hunter2!")
        self.assertEqual(clipboard.entries(), [])
        self.assertTrue(common.pump_until(lambda: board_["text"] == "", 3))

    def test_the_old_model_is_upgraded_but_a_chosen_one_is_kept(self):
        self.assertEqual(settings_schema.migrate({"config_version": 3, "ollama_model": "llama3.2"})["ollama_model"],
                         "qwen3:8b")
        self.assertEqual(settings_schema.migrate({"config_version": 3, "ollama_model": "mistral"})["ollama_model"],
                         "mistral")


import unittest.mock  # noqa: E402  (used as unittest.mock above)

if __name__ == "__main__":
    unittest.main()
