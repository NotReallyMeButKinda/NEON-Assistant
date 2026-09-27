"""The Linux backends (linuxdesk/), run on any OS against a fake command runner: what each would run, and how it
reads the answers. Nothing here touches the real system -- osinfo.RUNNER is swapped for a recorder."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import osinfo
from linuxdesk import apps, files, hypr, input as keys_in, kwin, media, notify, portal_keys, single, system, wm


class FakeRunner:
    """Answers commands from a table of (prefix tuple -> Result or callable), and records every call."""

    def __init__(self, answers=None, tools=()):
        self.answers = dict(answers or {})
        self.tools = set(tools)
        self.calls: list[list[str]] = []
        self.spawned: list[list[str]] = []

    def run(self, args, timeout, input_text):
        self.calls.append(list(args))
        best = None
        for prefix, answer in self.answers.items():
            if tuple(args[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, answer)
        if best is None:
            return osinfo.Result(False, "", "not faked", 1)
        answer = best[1]
        return answer(args, input_text) if callable(answer) else answer

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def spawn(self, args):
        self.spawned.append(list(args))
        return True

    def lines(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def ok(out=""):
    return osinfo.Result(True, out)


class LinuxTestCase(unittest.TestCase):
    desktop = "hyprland"

    def setUp(self):
        self.fake = FakeRunner()
        self._saved = dict(osinfo.RUNNER)
        osinfo.RUNNER.update(run=self.fake.run, which=self.fake.which, spawn=self.fake.spawn)
        patches = [mock.patch("osinfo.desktop", return_value=self.desktop),
                   mock.patch("osinfo.wayland", return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        hypr.forget_dialect()

    def tearDown(self):
        osinfo.RUNNER.clear()
        osinfo.RUNNER.update(self._saved)
        hypr.forget_dialect()


# ---------------------------------------------------------------------------
# Hyprland
# ---------------------------------------------------------------------------

class HyprDialectTests(LinuxTestCase):
    def use(self, dialect):
        self.fake.tools = {"hyprctl"}
        tag = {"lua": "v0.56.2", "hyprlang": "v0.54.0", "legacy": "v0.49.0"}[dialect]
        self.fake.answers = {
            ("hyprctl", "eval"): ok("ok") if dialect == "lua" else ok("eval is only supported with the lua config manager"),
            ("hyprctl", "-j", "version"): ok(json.dumps({"tag": tag})),
            ("hyprctl", "keyword"): ok("ok"), ("hyprctl", "dispatch"): ok("ok"),
            ("hyprctl", "-j", "monitors"): ok(json.dumps([{"id": 0, "name": "DP-1", "x": 0, "y": 0, "width": 2560,
                                                           "height": 1440, "scale": 1.25, "refreshRate": 143.97,
                                                           "transform": 0},
                                                          {"id": 1, "name": "HDMI-A-1", "x": 2048, "y": 0,
                                                           "width": 1080, "height": 1920, "scale": 1.0,
                                                           "refreshRate": 60.0, "transform": 0}])),
        }

    def test_dialect_is_detected_once(self):
        for dialect in ("lua", "hyprlang", "legacy"):
            hypr.forget_dialect()
            self.use(dialect)
            self.assertEqual(hypr.dialect(), dialect)
            before = len(self.fake.calls)
            hypr.dialect()
            self.assertEqual(len(self.fake.calls), before)          # remembered

    def test_window_rules_in_each_dialect(self):
        self.use("lua")
        self.assertTrue(hypr.window_rules("NEON status bar"))
        self.assertIn('hl.window_rule({ name = "neon-neon-status-bar", match = { title = "^(NEON status bar)$" }, '
                      'float = true, pin = true', self.fake.lines()[-1])
        self.assertIn("no_initial_focus = true", self.fake.lines()[-1])
        hypr.forget_dialect()
        self.use("hyprlang")
        hypr.window_rules("NEON quick box", focus=True)
        self.assertEqual(self.fake.calls[-1][:3], ["hyprctl", "keyword", "windowrule"])
        self.assertTrue(self.fake.calls[-1][3].startswith("match:title ^(NEON quick box)$, float on, pin on"))
        self.assertNotIn("no_focus", self.fake.calls[-1][3])
        hypr.forget_dialect()
        self.use("legacy")
        hypr.window_rules("NEON shade")
        self.assertIn("hyprctl keyword windowrule nofocus, title:^(NEON shade)$", self.fake.lines())

    def test_place_and_actions_in_lua(self):
        self.use("lua")
        hypr.place("NEON shade", 10, 20, 300, 40)
        self.assertIn('hl.dsp.window.move({ window = "title:^(NEON shade)$", x = 10, y = 20 })', self.fake.lines()[-1])
        hypr.close_window("0xabc")
        self.assertIn('hl.dsp.window.close({ window = "address:0xabc" })', self.fake.lines()[-1])
        hypr.move_to_workspace("0xabc", "special:minimized")
        self.assertIn('workspace = "special:minimized", follow = false', self.fake.lines()[-1])

    def test_place_and_actions_in_hyprlang(self):
        self.use("hyprlang")
        hypr.place("NEON shade", 10, 20, 300, 40)
        self.assertIn(["hyprctl", "dispatch", "movewindowpixel", "exact 10 20,title:^(NEON shade)$"], self.fake.calls)
        hypr.close_window("0xabc")
        self.assertEqual(self.fake.calls[-1], ["hyprctl", "dispatch", "closewindow", "address:0xabc"])
        hypr.set_border("0xabc", "#FF2FB4")
        self.assertIn(["hyprctl", "dispatch", "setprop", "address:0xabc active_border_color rgb(FF2FB4)"],
                      self.fake.calls)

    def test_reserve_keeps_the_monitor_as_it_is_in_lua(self):
        self.use("lua")
        hypr.reserve("DP-1", top=40)
        line = self.fake.lines()[-1]
        for part in ('output = "DP-1"', "reserved_area = { top = 40, bottom = 0", 'mode = "2560x1440@143.97"',
                     'position = "0x0"', 'scale = "1.25"', "transform = 0"):
            self.assertIn(part, line)
        hypr.forget_dialect()
        self.use("hyprlang")
        hypr.reserve("DP-1", bottom=36)
        self.assertEqual(self.fake.calls[-1], ["hyprctl", "keyword", "monitor", "DP-1,addreserved,0,36,0,0"])

    def test_monitor_at_uses_layout_size(self):
        self.use("hyprlang")
        self.assertEqual(hypr.monitor_at(100, 100), "DP-1")
        self.assertEqual(hypr.monitor_at(2047, 1000), "DP-1")        # 2560 / 1.25 = 2048 wide
        self.assertEqual(hypr.monitor_at(2100, 1500), "HDMI-A-1")    # portrait: 1080 x 1920
        self.assertEqual(hypr.monitor_at(9000, 0), "")

    def test_binds_and_unbinds(self):
        self.use("lua")
        binds = hypr.Binds()
        failed = binds.apply([("Talk", "Ctrl+Alt+N", "talk"), ("Hold", "Meta+Space", "hold:hold"),
                              ("Bad", "Hyper+Q", "quick")], ["/usr/bin/neon-assistant", "--command"])
        self.assertEqual(failed, ["Bad"])
        lines = self.fake.lines()
        self.assertTrue(any('hl.bind("CTRL + ALT + N", hl.dsp.exec_cmd("/usr/bin/neon-assistant --command talk"))'
                            in line for line in lines), lines)
        self.assertTrue(any('hl.bind("SUPER + SPACE"' in line and "hold-stop" in line and "release = true" in line
                            for line in lines), lines)
        binds.clear()
        self.assertTrue(any('hl.unbind("CTRL + ALT + N")' in line for line in self.fake.lines()))
        hypr.forget_dialect()
        self.use("hyprlang")
        binds.apply([("Talk", "Ctrl+Alt+N", "talk")], ["neon", "--command"])
        self.assertEqual(self.fake.calls[-1], ["hyprctl", "keyword", "bind", "CTRL ALT, N, exec, neon --command talk"])

    def test_bind_spec(self):
        self.assertEqual(hypr.bind_spec("Ctrl+Alt+N"), ("CTRL ALT", "N"))
        self.assertEqual(hypr.bind_spec("Meta+Space"), ("SUPER", "SPACE"))
        self.assertEqual(hypr.bind_spec("Ctrl+F5"), ("CTRL", "F5"))
        self.assertEqual(hypr.bind_spec("Ctrl+/"), ("CTRL", "slash"))
        self.assertIsNone(hypr.bind_spec(""))
        self.assertEqual(hypr.lua_keys("CTRL ALT", "N"), "CTRL + ALT + N")

    def test_a_config_reload_is_noticed_and_binds_are_forgotten_not_unbound(self):
        import socket
        import threading
        self.use("hyprlang")
        binds = hypr.Binds()
        binds.apply([("Talk", "Ctrl+Alt+N", "talk")], ["neon", "--command"])
        self.assertEqual(len(binds._active), 1)
        reloaded = threading.Event()
        watcher = hypr.ReloadWatcher(reloaded.set)
        ours, theirs = socket.socketpair()
        watcher._sock = ours
        thread = threading.Thread(target=watcher._run, args=(ours,), daemon=True)
        thread.start()
        theirs.sendall(b"workspace>>2\nactivewindow>>kitty,~\n")
        self.assertFalse(reloaded.wait(0.3))
        theirs.sendall(b"configrel")
        theirs.sendall(b"oaded>>\n")                                     # split across reads
        self.assertTrue(reloaded.wait(3))
        self.assertEqual(binds._active, [])
        before = len(self.fake.calls)
        binds.clear()
        self.assertFalse(any(c[:3] == ["hyprctl", "keyword", "unbind"] for c in self.fake.calls[before:]))
        watcher.stop()
        theirs.close()
        thread.join(3)
        self.assertTrue(hypr.is_reload("configreloaded>>"))
        self.assertFalse(hypr.is_reload("configreloadedx>>"))

    def test_lua_strings_are_escaped(self):
        self.assertEqual(hypr.lua_string('say "hi"\\'), '"say \\"hi\\"\\\\"')


class HyprWindowTests(LinuxTestCase):
    def setUp(self):
        super().setUp()
        self.fake.tools = {"hyprctl"}
        clients = [{"address": "0x1", "pid": 111, "class": "firefox", "title": "News - Mozilla Firefox",
                    "mapped": True, "workspace": {"name": "1"}},
                   {"address": "0x2", "pid": 222, "class": "org.kde.dolphin", "title": "Home — Dolphin",
                    "mapped": True, "workspace": {"name": "special:minimized"}},
                   {"address": "0x3", "pid": os.getpid(), "class": "neon-assistant", "title": "NEON",
                    "mapped": True, "workspace": {"name": "1"}}]
        self.fake.answers = {("hyprctl", "-j", "clients"): ok(json.dumps(clients)),
                             ("hyprctl", "eval"): ok("eval is only supported with the lua config manager"),
                             ("hyprctl", "-j", "version"): ok('{"tag": "v0.54.2"}'),
                             ("hyprctl", "dispatch"): ok("ok"),
                             ("hyprctl", "-j", "activewindow"): ok(json.dumps({"address": "0x1", "pid": 111,
                                                                               "class": "firefox", "fullscreen": 2,
                                                                               "monitor": 1, "at": [1920, 0],
                                                                               "size": [1920, 1080]})),
                             ("hyprctl", "-j", "monitors"): ok(json.dumps([
                                 {"id": 0, "name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1},
                                 {"id": 1, "name": "DP-2", "x": 1920, "y": 0, "width": 3840, "height": 2160,
                                  "scale": 2}]))}

    def test_find_never_returns_neon_itself_and_prefers_the_app_name(self):
        self.assertEqual(wm.find("firefox"), [("0x1", 111, "Mozilla Firefox")])
        self.assertEqual(wm.find("dolphin"), [("0x2", 222, "Dolphin")])
        self.assertEqual(wm.find("neon"), [])

    def test_minimized_windows_are_found_and_restored(self):
        minimized = [w for w in wm.windows() if w["minimized"]]
        self.assertEqual([w["id"] for w in minimized], ["0x2"])
        self.assertEqual(wm.restore_all(), "Restoring your windows.")

    def test_close_goes_through_hyprctl(self):
        self.assertTrue(wm.close("0x1"))
        self.assertEqual(self.fake.calls[-1], ["hyprctl", "dispatch", "closewindow", "address:0x1"])

    def test_fullscreen_monitor_matches_by_id(self):
        self.assertEqual(wm.fullscreen_monitor(), (1920, 0, 3840, 1080))     # DP-2 in layout pixels


# ---------------------------------------------------------------------------
# KDE
# ---------------------------------------------------------------------------

class KdeTests(LinuxTestCase):
    desktop = "kde"

    def setUp(self):
        super().setUp()
        self.fake.tools = {"kwriteconfig6", "kreadconfig6", "busctl", "kdotool"}
        self.config: dict[tuple[str, str], str] = {}

        def write(args, _input):
            group, key, value = args[4], args[6], args[7]
            self.config[(group, key)] = value
            return ok()

        def read(args, _input):
            return ok(self.config.get((args[4], args[6]), "") + "\n")
        self.fake.answers = {("kwriteconfig6",): write, ("kreadconfig6",): read, ("busctl",): ok(),
                             ("kdotool", "windowactivate"): ok()}

    def test_window_rule_forces_place_and_keeps_it_out_of_the_way(self):
        self.assertTrue(kwin.window_rule("NEON status bar", 0, 0, 1920, 40, focus=False))
        group = "neon-assistant-neon-status-bar"
        c = self.config
        self.assertEqual((c[(group, "title")], c[(group, "titlematch")]), ("NEON status bar", "1"))
        self.assertEqual((c[(group, "position")], c[(group, "positionrule")]), ("0,0", "2"))
        self.assertEqual((c[(group, "size")], c[(group, "sizerule")]), ("1920,40", "2"))
        self.assertEqual((c[(group, "acceptfocus")], c[(group, "above")], c[(group, "skiptaskbar")]),
                         ("false", "true", "true"))
        self.assertEqual(c[("General", "Order")], group)
        self.assertNotIn(("General", "rules"), c)            # never start the legacy list: new KWin would purge
        self.assertTrue(kwin.reconfigure())
        self.assertEqual(self.fake.calls[-1][-3:], ["/KWin", "org.kde.KWin", "reconfigure"])

    def test_legacy_rule_list_is_extended_when_in_use(self):
        self.config[("General", "rules")] = "abc123"
        kwin.window_rule("NEON shade", 0, 0, 800, 280)
        self.assertEqual(self.config[("General", "rules")], "abc123,neon-assistant-neon-shade")
        kwin.window_rule("NEON shade", 0, 0, 800, 280)
        self.assertEqual(self.config[("General", "rules")], "abc123,neon-assistant-neon-shade")    # once

    def test_popups_only_force_the_position(self):
        kwin.window_rule("NEON quick box", 500, 300, focus=True)
        group = "neon-assistant-neon-quick-box"
        self.assertNotIn((group, "size"), self.config)
        self.assertNotIn((group, "acceptfocus"), self.config)

    def test_kwin_shortcuts_use_busctl(self):
        self.assertEqual(wm.minimize_all(), "Minimizing all windows.")
        self.assertEqual(self.fake.calls[-1], ["busctl", "--user", "call", "org.kde.kglobalaccel", "/component/kwin",
                                               "org.kde.kglobalaccel.Component", "invokeShortcut", "s",
                                               "Show Desktop"])
        self.assertTrue(wm.maximize("{abc}"))
        self.assertEqual(self.fake.calls[-1][-1], "Window Maximize")

    def test_no_hyprland_calls(self):
        self.assertFalse(hypr.available())
        kwin.window_rule("NEON shade", 0, 0, 10, 10)
        self.assertFalse(any(c[0] == "hyprctl" for c in self.fake.calls))


class PortalTests(unittest.TestCase):
    def test_triggers(self):
        self.assertEqual(portal_keys.trigger("Ctrl+Alt+N"), "CTRL+ALT+n")
        self.assertEqual(portal_keys.trigger("Meta+Space"), "LOGO+space")
        self.assertEqual(portal_keys.trigger("Shift+F9"), "SHIFT+F9")
        self.assertEqual(portal_keys.trigger("Ctrl++"), "CTRL+plus")
        self.assertEqual(portal_keys.trigger("Hyper+Q"), "")
        self.assertEqual(portal_keys.sender_token(":1.42"), "1_42")

    def test_session_flow_with_a_fake_bus(self):
        try:
            import jeepney  # noqa: F401
        except ImportError:
            self.skipTest("jeepney isn't installed here")
        from jeepney.low_level import HeaderFields, MessageType

        class Header:
            def __init__(self, kind, member=""):
                self.message_type = kind
                self.fields = {HeaderFields.member: member}

        class Msg:
            def __init__(self, body, kind=MessageType.method_return, member=""):
                self.body, self.header = body, Header(kind, member)

        presses, releases = [], []

        class FakeConn:
            unique_name = ":1.7"

            def __init__(self):
                self.sent = []
                self.queue = []
                self.closed = False

            def send_and_get_reply(self, msg, timeout=None):
                member = msg.header.fields.get(HeaderFields.member)
                self.sent.append((member, msg.body))
                if member == "CreateSession":
                    self.queue.append(Msg((0, {"session_handle": ("o", "/s/1")}), MessageType.signal, "Response"))
                elif member == "BindShortcuts":
                    self.queue.append(Msg((0, {"shortcuts": ("a(sa{sv})", [
                        ("talk", {"trigger_description": ("s", "Ctrl+Alt+N")})])}), MessageType.signal, "Response"))
                    self.queue.append(Msg(("/s/1", "talk", 1, {}), MessageType.signal, "Activated"))
                    self.queue.append(Msg(("/s/other", "talk", 2, {}), MessageType.signal, "Activated"))
                    self.queue.append(Msg(("/s/1", "hold", 3, {}), MessageType.signal, "Deactivated"))
                return Msg(())

            def filter(self, rule, bufsize=1):
                conn = self

                class Handle:
                    def __enter__(self):
                        return conn.queue

                    def __exit__(self, *exc):
                        return False
                return Handle()

            def recv_until_filtered(self, queue, timeout=None):
                if queue:
                    return queue.pop(0)
                raise OSError("closed")

            def close(self):
                self.closed = True

        conn = FakeConn()
        client = portal_keys.PortalShortcuts(presses.append, releases.append)
        with mock.patch("jeepney.io.blocking.open_dbus_connection", return_value=conn):
            client.bind([("talk", "Talk", "Ctrl+Alt+N"), ("hold", "Hold to talk", "Ctrl+Alt+H")])
            client._thread.join(5)
        self.assertEqual(presses, ["talk"])                          # the other session's press is ignored
        self.assertEqual(releases, ["hold"])
        self.assertEqual(client.assigned, {"talk": "Ctrl+Alt+N"})
        bind_call = next(body for member, body in conn.sent if member == "BindShortcuts")
        self.assertEqual(bind_call[0], "/s/1")
        self.assertEqual(bind_call[1][0], ("talk", {"description": ("s", "Talk"),
                                                    "preferred_trigger": ("s", "CTRL+ALT+n")}))
        self.assertTrue(conn.closed)


# ---------------------------------------------------------------------------
# The rest of the desktop
# ---------------------------------------------------------------------------

class SystemTests(LinuxTestCase):
    def test_volume_through_wpctl(self):
        self.fake.tools = {"wpctl"}
        self.fake.answers = {("wpctl", "get-volume"): ok("Volume: 0.35 [MUTED]\n"), ("wpctl", "set-volume"): ok(),
                             ("wpctl", "set-mute"): ok()}
        self.assertEqual(system.get_volume(), 35)
        self.assertEqual(system.set_volume(62.4), "Volume set to 62 percent.")
        self.assertIn(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.62"], self.fake.calls)
        self.assertEqual(self.fake.calls[-1], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        self.assertEqual(system.set_mute(None), "Sound muted.")

    def test_missing_tools_say_what_to_install(self):
        self.assertIn("wireplumber", system.set_volume(50))

    def test_sign_out_on_hyprland_and_kde(self):
        self.fake.tools = {"hyprctl"}
        self.fake.answers = {("hyprctl", "eval"): ok("eval is only supported with the lua config manager"),
                             ("hyprctl", "-j", "version"): ok('{"tag": "v0.54.0"}'), ("hyprctl", "dispatch"): ok("ok")}
        self.assertEqual(system.sign_out(), "Signing you out.")
        self.assertEqual(self.fake.calls[-1], ["hyprctl", "dispatch", "exit"])
        with mock.patch("osinfo.desktop", return_value="kde"):
            self.fake.answers[("busctl",)] = ok()
            self.assertEqual(system.sign_out(), "Signing you out.")
            self.assertEqual(self.fake.calls[-1][3:7], ["org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown", "logout"])


class MediaTests(LinuxTestCase):
    def test_playing_beats_paused(self):
        out = ("spotify\tPaused\tArtist A\tSong A\t1000000\t2000000\n"
               "firefox.instance_1\tPlaying\tArtist B\tSong B\t5000000\t300000000\n")
        report = media.parse(out)
        self.assertEqual((report["player"], report["title"], report["position"]), ("firefox.instance_1", "Song B", 5.0))
        song = media.to_song(report)
        self.assertEqual((song["app"], song["isPaused"], song["songDuration"]), ("Firefox", False, 300.0))
        self.assertIsNone(media.parse("spotify\tStopped\t\t\n"))


class NotifyTests(LinuxTestCase):
    def test_notify_calls_are_paired_with_their_ids(self):
        got = []
        watcher = notify.NotificationWatcher(got.append)
        watcher.handle({"type": "method_call", "member": "Notify", "cookie": 5,
                        "payload": {"data": ["discord", 0, "", "<b>Sam</b>", "hi &amp; bye", [], {
                            "desktop-entry": {"type": "s", "data": "discord"}}, -1]}})
        self.assertEqual(got, [])
        watcher.handle({"type": "method_return", "reply_cookie": 5, "payload": {"data": [42]}})
        self.assertEqual(len(got), 1)
        note = got[0]
        self.assertEqual((note.id, note.app, note.title, note.body, note.aumid), (42, "discord", "Sam", "hi & bye",
                                                                                  "discord"))

    def test_a_call_without_a_reply_still_arrives(self):
        got = []
        watcher = notify.NotificationWatcher(got.append)
        watcher.handle({"type": "method_call", "member": "Notify", "cookie": 9,
                        "payload": {"data": ["app", 0, "", "Title", "", [], {}, -1]}})
        watcher._pending[9] = (watcher._pending[9][0], time.monotonic() - 5)
        watcher.handle({"type": "signal"})
        self.assertEqual([n.title for n in got], ["Title"])

    def test_ignored_apps(self):
        got = []
        watcher = notify.NotificationWatcher(got.append, ignore=lambda: ("neon",))
        watcher._deliver({"app": "Neon", "title": "x", "body": "", "entry": "", "cookie": 1}, 3)
        self.assertEqual(got, [])

    def test_dnd_per_daemon(self):
        self.fake.answers = {("swaync-client",): ok(), ("dunstctl",): ok(), ("makoctl",): ok()}
        with mock.patch.object(notify, "daemon", return_value="swaync"):
            self.assertTrue(notify.set_quiet(True))
            self.assertEqual(self.fake.calls[-1], ["swaync-client", "--dnd-on"])
        with mock.patch.object(notify, "daemon", return_value="mako"):
            notify.set_quiet(False)
            self.assertEqual(self.fake.calls[-1], ["makoctl", "mode", "-r", "do-not-disturb"])


class InputTests(LinuxTestCase):
    def test_key_specs(self):
        combo = keys_in.parse("ctrl+shift+esc")[0]
        self.assertEqual(keys_in._combo_args("wtype", combo),
                         ["wtype", "-M", "ctrl", "-M", "shift", "-k", "Escape", "-m", "shift", "-m", "ctrl"])
        self.assertEqual(keys_in._combo_args("ydotool", combo),
                         ["ydotool", "key", "29:1", "42:1", "1:1", "1:0", "42:0", "29:0"])
        self.assertEqual(keys_in._combo_args("xdotool", keys_in.parse("super+d")[0]), ["xdotool", "key", "super+d"])
        self.assertEqual(len(keys_in.parse("ctrl+c, ctrl+v")), 2)
        with self.assertRaises(ValueError):
            keys_in.parse("ctrl+banana")

    def test_kde_types_with_ydotool(self):
        self.fake.tools = {"wtype", "ydotool"}
        self.assertEqual(keys_in.tool(), "wtype")
        with mock.patch("osinfo.desktop", return_value="kde"):
            self.assertEqual(keys_in.tool(), "ydotool")          # KWin has no virtual-keyboard protocol for wtype
        self.fake.answers = {("wtype",): ok()}
        self.assertTrue(keys_in.type_text("héllo"))
        self.assertEqual(self.fake.calls[-1], ["wtype", "-"])


class AppsTests(LinuxTestCase):
    def test_desktop_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "share"
            (data / "applications").mkdir(parents=True)
            (data / "applications" / "firefox.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Firefox\nName[de]=Feuerfuchs\nGenericName=Web Browser\n"
                "Exec=firefox %u\nCategories=Network;\n[Desktop Action new]\nName=New Window\n", encoding="utf-8")
            (data / "applications" / "hidden.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Hidden\nExec=x\nNoDisplay=true\n", encoding="utf-8")
            (data / "applications" / "kdeonly.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=KDE Thing\nExec=y\nOnlyShowIn=KDE;\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data), "XDG_DATA_DIRS": str(Path(tmp) / "none")}):
                found = apps.discover()
            self.assertEqual(sorted(found), ["Firefox"])            # hidden, and KDE-only on Hyprland
            self.assertEqual(found["Firefox"]["description"], "Web Browser")
        self.assertEqual(apps.exec_args("firefox %u --new", ["https://x.org"]), ["firefox", "https://x.org", "--new"])
        self.assertEqual(apps.exec_args('"/opt/My App/run" %F %i'), ["/opt/My App/run"])

    def test_entry_text(self):
        text = apps.entry_text("Neon Assistant", "neon-assistant --minimized", "/x/neon.png", "Voice assistant")
        self.assertIn("StartupWMClass=neon-assistant\n", text)
        self.assertTrue(text.startswith("[Desktop Entry]\nType=Application\nName=Neon Assistant\n"))


class FilesTests(LinuxTestCase):
    def test_plocate_results_are_filtered_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "Resume old.pdf"
            new = Path(tmp) / "resume 2026.pdf"
            folder = Path(tmp) / "Resume stuff"
            hidden = Path(tmp) / ".cache" / "resume.pdf"
            for p in (old, new, hidden):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
            folder.mkdir()
            os.utime(old, (1_600_000_000, 1_600_000_000))
            listing = "\n".join(str(p) for p in (old, new, folder, hidden, Path(tmp) / "resume gone.pdf"))
            self.fake.tools = {"plocate"}
            self.fake.answers = {("plocate",): ok(listing)}
            hits = files.search("resume", "ext:pdf")
            self.assertEqual([h.name for h in hits], ["resume 2026.pdf", "Resume old.pdf"])
            self.assertEqual(self.fake.calls[-1][:6], ["plocate", "--ignore-case", "--basename", "--limit", "3000", "--"])
            self.assertEqual([h.name for h in files.search("resume", "folder:")], ["Resume stuff"])

    def test_usb_drives_are_not_system_folders(self):
        self.assertFalse(files._is_system("/run/media/me/USB/resume.pdf"))
        self.assertTrue(files._is_system("/run/user/1000/x"))
        self.assertTrue(files._is_system("/usr/share/doc/resume.txt"))


class PlasmoidInstallTests(LinuxTestCase):
    desktop = "kde"

    def test_install_fills_in_the_start_command_and_port(self):
        import xml.etree.ElementTree as ET
        from linuxdesk import plasmoid
        seen = {}

        def kpackagetool(args, _input):
            package = Path(args[-1])
            seen["args"] = args
            seen["xml"] = (package / "contents" / "config" / "main.xml").read_text(encoding="utf-8")
            seen["files"] = sorted(str(p.relative_to(package)).replace(os.sep, "/") for p in package.rglob("*")
                                   if p.is_file())
            return ok()
        self.fake.tools = {"kpackagetool6"}
        self.fake.answers = {("kpackagetool6",): kpackagetool}
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}), \
                mock.patch("app_paths.launch_args", return_value=["/home/me/R&D <neon>/.venv/bin/python",
                                                                  "/home/me/R&D <neon>/main.py", "--minimized"]):
            done, text = plasmoid.install(48000)
        self.assertTrue(done, text)
        self.assertEqual(seen["args"][:4], ["kpackagetool6", "--type", "Plasma/Applet", "--install"])
        root = ET.fromstring(seen["xml"])                                 # still valid XML
        ns = {"k": "http://www.kde.org/standards/kcfg/1.0"}
        defaults = {e.get("name"): e.findtext("k:default", "", ns) for e in root.iterfind(".//k:entry", ns)}
        self.assertEqual(defaults["port"], "48000")
        self.assertEqual(defaults["launchCommand"],
                         "'/home/me/R&D <neon>/.venv/bin/python' '/home/me/R&D <neon>/main.py' --minimized")
        self.assertIn("contents/ui/main.qml", seen["files"])
        self.assertIn("metadata.json", seen["files"])
        self.assertTrue(plasmoid.source_dir().joinpath("contents/config/main.xml").read_text(encoding="utf-8")
                        .count("@LAUNCH@"))                                  # the shipped copy is untouched

    def test_without_plasma(self):
        from linuxdesk import plasmoid
        done, text = plasmoid.install(47812)
        self.assertFalse(done)
        self.assertIn("kpackagetool6", text)


class CommandTests(unittest.TestCase):
    def test_every_hotkey_action_is_a_command(self):
        for action in ("talk", "wake", "window", "quick", "mute", "dictation", "hold-start", "hold-stop", "show"):
            self.assertIn(action, single.COMMANDS)

    def test_server_name_is_per_user_and_safe(self):
        with mock.patch("getpass.getuser", return_value="jo smith/x"):
            self.assertEqual(single.server_name(), "neon-assistant-jo_smith_x")


if __name__ == "__main__":
    unittest.main()
