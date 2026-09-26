"""System control, routines and plugins.

Safety: nothing in here locks, sleeps, shuts down, signs out, empties the recycle bin or changes
the volume. The parsers and the confirmation flow are tested with `system_control.perform` replaced
by a recorder; only the genuinely read-only calls (uptime, free space, the PNG encoder) run for
real. Routines run against stub callbacks, and plugins load from a throwaway folder.
"""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import plugins
import routines
import system_control as system

from tests import common
from tests.common import backend


class SystemParsing(unittest.TestCase):
    def test_volume(self):
        self.assertEqual(system.spoken_system_command("set the volume to 40 percent"), ("set_volume", 40))
        self.assertEqual(system.spoken_system_command("set the system volume to 0"), ("set_volume", 0))
        self.assertEqual(system.spoken_system_command("what is the volume"), ("get_volume", None))

    def test_muting_the_whole_pc(self):
        self.assertEqual(system.spoken_system_command("mute the system"), ("mute", True))
        self.assertEqual(system.spoken_system_command("unmute the pc"), ("mute", False))
        self.assertEqual(system.spoken_system_command("turn the sound off"), ("mute", True))
        self.assertEqual(system.spoken_system_command("turn the sound on"), ("mute", False))

    def test_power_and_session(self):
        cases = {"lock the pc": "lock", "put the computer to sleep": "sleep",
                 "shut down the computer": "shutdown", "restart the pc": "restart",
                 "cancel the shutdown": "cancel_shutdown", "sign me out": "sign_out",
                 "empty the recycle bin": "recycle_bin", "show the desktop": "desktop"}
        for text, action in cases.items():
            self.assertEqual(system.spoken_system_command(text), (action, None), text)

    def test_information_and_screenshots(self):
        self.assertEqual(system.spoken_system_command("take a screenshot"), ("screenshot", None))
        self.assertEqual(system.spoken_system_command("what is the uptime"), ("uptime", None))
        self.assertEqual(system.spoken_system_command("how much disk space is left"), ("disk", "C"))
        self.assertEqual(system.spoken_system_command("set brightness to 60"), ("brightness", 60))

    def test_ordinary_speech_is_not_a_system_command(self):
        for text in ("open chrome", "play some music", "pause the music", "what's the weather",
                     "mute", "lock in", "turn it up"):
            self.assertIsNone(system.spoken_system_command(text), text)

    def test_every_confirmable_action_has_a_question(self):
        for action in system.NEEDS_CONFIRMATION:
            self.assertIn(action, system.CONFIRMATIONS)
            self.assertTrue(system.CONFIRMATIONS[action].endswith("?")
                            or "?" in system.CONFIRMATIONS[action])


class ReadOnlySystemCalls(unittest.TestCase):
    """Only the calls that observe the machine rather than changing it."""

    def test_uptime_reads_as_a_sentence(self):
        text = system.uptime()
        self.assertTrue(text.startswith("This PC has been up for"))
        self.assertTrue(text.endswith("."))

    def test_free_space(self):
        self.assertRegex(system.disk_free("C"), r"Drive C has \d+ gigabytes free")

    def test_an_unknown_drive_is_reported_not_raised(self):
        self.assertIn("couldn't read", system.disk_free("Q"))


class ScreenshotEncoder(unittest.TestCase):
    """The hand-written PNG writer: a wrong channel order or stride is invisible until you look."""

    def test_it_writes_a_valid_png_with_the_right_pixels(self):
        width, height = 3, 2
        # Bottom-up BGRA, as GDI hands it over: row 0 is the *bottom* row of the image.
        bottom = bytes([255, 0, 0, 255] * width)         # blue
        top = bytes([0, 255, 0, 255] * width)            # green
        path = Path(tempfile.mkdtemp()) / "shot.png"
        system._write_png(path, width, height, bottom + top)

        data = path.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (width, height))
        self.assertEqual(data[24:29], bytes([8, 2, 0, 0, 0]))     # 8-bit truecolour, no interlace

        # Pull the pixels back out and check the top row really is the one GDI gave us last.
        idat = self._chunk(data, b"IDAT")
        raw = zlib.decompress(idat)
        stride = width * 3 + 1
        self.assertEqual(len(raw), stride * height)
        self.assertEqual(raw[0], 0)                                # filter byte
        self.assertEqual(tuple(raw[1:4]), (0, 255, 0))             # first row is green (RGB)
        self.assertEqual(tuple(raw[stride + 1:stride + 4]), (0, 0, 255))   # second row is blue

    @staticmethod
    def _chunk(data: bytes, kind: bytes) -> bytes:
        at = 8
        while at < len(data):
            length = int.from_bytes(data[at:at + 4], "big")
            if data[at + 4:at + 8] == kind:
                return data[at + 8:at + 8 + length]
            at += 12 + length
        raise AssertionError(f"no {kind!r} chunk")


class Confirmation(unittest.TestCase):
    """Anything irreversible asks first, and only a clear yes goes ahead."""

    def setUp(self):
        common.use_temp_config()
        backend._PENDING.update(action="", argument=None, until=0.0)
        self.done = []
        self._real = system.perform
        system.perform = lambda action, argument=None: (self.done.append(action), "done")[1]

    def tearDown(self):
        system.perform = self._real
        backend._PENDING.update(action="", argument=None, until=0.0)

    def test_it_asks_before_shutting_down(self):
        reply = backend.handle_system_command("shut down the computer")
        self.assertIn("Shut the PC down?", reply)
        self.assertEqual(self.done, [])
        self.assertEqual(backend.pending_confirmation(), "shutdown")

    def test_yes_goes_ahead(self):
        backend.handle_system_command("shut down the computer")
        self.assertEqual(backend._answer_confirmation("yes"), "done")
        self.assertEqual(self.done, ["shutdown"])
        self.assertEqual(backend.pending_confirmation(), "")

    def test_no_cancels(self):
        backend.handle_system_command("empty the recycle bin")
        self.assertEqual(backend._answer_confirmation("never mind"), "Cancelled.")
        self.assertEqual(self.done, [])

    def test_anything_else_drops_the_question_and_routes_normally(self):
        backend.handle_system_command("restart the pc")
        self.assertIsNone(backend._answer_confirmation("what's the weather"))

    def test_harmless_actions_are_not_gated(self):
        backend.handle_system_command("what is the volume")
        self.assertEqual(self.done, ["get_volume"])

    def test_turning_confirmation_off_runs_it_straight_away(self):
        backend.CONFIG["confirm_destructive"] = False
        backend.handle_system_command("shut down the computer")
        self.assertEqual(self.done, ["shutdown"])

    def test_the_feature_can_be_switched_off_entirely(self):
        backend.CONFIG["system_control_enabled"] = False
        self.assertIsNone(backend.handle_system_command("lock the pc"))
        self.assertEqual(self.done, [])


class Routines(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG["routines"] = list(routines.EXAMPLES)

    def test_names_are_recognised_in_several_shapes(self):
        known = routines.names(backend.CONFIG)
        for text in ("run work mode", "work mode", "do work mode please", "start the work mode routine"):
            self.assertEqual(routines.spoken_routine(text, known), "work mode", text)
        self.assertEqual(routines.spoken_routine("activate focus mode", known), "focus")

    def test_unknown_phrases_are_not_routines(self):
        known = routines.names(backend.CONFIG)
        for text in ("open chrome", "run away", "what's the weather", "start a timer"):
            self.assertIsNone(routines.spoken_routine(text, known), text)

    def test_listing(self):
        self.assertTrue(routines.spoken_routine_list("what routines do i have"))
        self.assertTrue(routines.spoken_routine_list("list my routines"))
        self.assertFalse(routines.spoken_routine_list("run work mode"))
        self.assertIn("work mode", routines.catalogue(backend.CONFIG))

    def test_no_routines_is_explained(self):
        backend.CONFIG["routines"] = []
        self.assertIn("Settings", routines.catalogue(backend.CONFIG))

    def test_step_prefixes(self):
        self.assertEqual(routines.parse_step("say: hello"), ("say", "hello"))
        self.assertEqual(routines.parse_step("wait: 3"), ("wait", "3"))
        self.assertEqual(routines.parse_step("open: Code"), ("open", "Code"))
        self.assertEqual(routines.parse_step("url: https://x.com"), ("url", "https://x.com"))
        self.assertEqual(routines.parse_step("run: notepad"), ("run", "notepad"))
        self.assertEqual(routines.parse_step("pause the music"), ("command", "pause the music"))

    def test_running_dispatches_each_step(self):
        seen = []
        reply = routines.run({"name": "test", "steps": ["pause the music", "open: Code",
                                                        "url: https://x.com", "say: done"]},
                             run_command=lambda t: seen.append(("cmd", t)),
                             say=lambda t: seen.append(("say", t)),
                             open_app=lambda t: seen.append(("app", t)),
                             open_url=lambda t: seen.append(("url", t)))
        self.assertEqual(seen, [("cmd", "pause the music"), ("app", "Code"),
                                ("url", "https://x.com"), ("say", "done")])
        self.assertEqual(reply, "done")

    def test_one_failing_step_does_not_abandon_the_rest(self):
        seen = []

        def flaky(text):
            if text == "boom":
                raise RuntimeError("nope")
            seen.append(text)
        reply = routines.run({"name": "test", "steps": ["one", "boom", "two"]}, run_command=flaky)
        self.assertEqual(seen, ["one", "two"])
        self.assertIn("2 steps done", reply)

    def test_key_steps_are_pressed_never_routed(self):
        self.assertEqual(routines.parse_step("keys: ctrl+shift+esc"), ("keys", "ctrl+shift+esc"))
        self.assertEqual(routines.parse_step("press: win+d"), ("keys", "win+d"))
        pressed, routed = [], []
        routines.run({"name": "t", "steps": ["keys: win+d", "keys: alt+tab"]}, run_command=routed.append,
                     press_keys=pressed.append)
        self.assertEqual((pressed, routed), (["win+d", "alt+tab"], []))
        reply = routines.run({"name": "t", "steps": ["keys: win+d", "say: ok"]}, run_command=routed.append)
        self.assertEqual((reply, routed), ("ok", []))           # no key sender: skipped, not sent to the router

    def test_key_combinations_are_parsed_and_checked(self):
        import dictation
        self.assertEqual(dictation.parse_keys("ctrl+shift+esc, win+d"), [[0x11, 0x10, 0x1B], [0x5B, 0x44]])
        self.assertEqual(dictation.parse_keys("Alt + F4"), [[0x12, 0x73]])
        for bad in ("ctrl+alt+del", "ctrl+alt+delete", "win+l", "ctlr+c", ""):
            with self.assertRaises(ValueError, msg=bad):
                dictation.parse_keys(bad)
        self.assertIn("Ctrl+Alt+Del", routines.check_step("keys: ctrl+alt+del"))
        self.assertIsNone(routines.check_step("keys: win+d"))
        self.assertIsNone(routines.check_step("pause the music"))

    def test_junk_entries_are_dropped(self):
        cleaned = routines.normalize([{"name": "ok", "steps": ["a"]}, "not a dict",
                                      {"name": "", "steps": ["a"]}, {"name": "empty", "steps": []},
                                      {"name": "ok", "steps": ["duplicate name"]}])
        self.assertEqual([r["name"] for r in cleaned], ["ok"])


class Plugins(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "plugins"
        plugins.set_folder(self.folder)
        plugins.set_api(config={}, data_dir=self.folder)
        self._timeout = plugins.HANDLER_TIMEOUT

    def tearDown(self):
        plugins.HANDLER_TIMEOUT = self._timeout

    def test_the_example_is_created_and_works(self):
        self.assertTrue((plugins.folder() / "example.py").exists())
        count, problems = plugins.load()
        self.assertEqual((count, problems), (1, []))
        self.assertIn("plugin", plugins.handle("hello plugin").lower())

    def test_a_broken_plugin_is_reported_and_skipped(self):
        plugins.folder()
        (self.folder / "broken.py").write_text("import a_module_that_does_not_exist_xyz")
        count, problems = plugins.load()
        self.assertEqual(count, 1)                      # the example still loaded
        self.assertEqual(len(problems), 1)
        self.assertIn("broken.py", problems[0])

    def test_a_handler_that_raises_is_treated_as_no_answer(self):
        plugins.folder()
        (self.folder / "angry.py").write_text(
            "COMMANDS = [(r'^make me angry$', lambda m, t: 1 / 0)]\n")
        plugins.load()
        self.assertIsNone(plugins.handle("make me angry"))

    def test_a_handler_that_hangs_is_abandoned(self):
        plugins.folder()
        (self.folder / "slow.py").write_text(
            "import time\nCOMMANDS = [(r'^hang now$', lambda m, t: (time.sleep(30), 'never')[1])]\n")
        plugins.load()
        plugins.HANDLER_TIMEOUT = 0.4
        self.assertIsNone(plugins.handle("hang now"))

    def test_plugins_only_answer_what_they_match(self):
        plugins.load()
        self.assertIsNone(plugins.handle("open chrome"))

    def test_the_spoken_commands_about_plugins(self):
        plugins.load()
        self.assertIn("Example", plugins.handle_plugin_command("what plugins do i have"))
        self.assertIn("Reloaded", plugins.handle_plugin_command("reload plugins"))
        self.assertIsNone(plugins.handle_plugin_command("open chrome"))

    def test_files_starting_with_an_underscore_are_ignored(self):
        plugins.folder()
        (self.folder / "_helper.py").write_text("raise RuntimeError('should never be imported')\n")
        count, problems = plugins.load()
        self.assertEqual((count, problems), (1, []))


if __name__ == "__main__":
    unittest.main()
