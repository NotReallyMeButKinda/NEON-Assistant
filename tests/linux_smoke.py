"""Run NEON's Linux code paths for its own windows and hotkeys against a fake desktop, on this Windows PC.

Uses tests/linux_sim.py's pretence (sys.platform = "linux", no Windows APIs), then replaces osinfo.RUNNER with a
fake that answers like hyprctl (in each of its three dialects) or KDE's tools, and records every command. The
real status bar, shade, window outline, quick box, hotkeys and command socket are built offscreen, and the
commands they'd send to the compositor are checked.

    python tests/linux_smoke.py hyprland lua|hyprlang|legacy
    python tests/linux_smoke.py kde

Prints "PASS <check>" / "FAIL <check>: why" lines and exits 1 if anything failed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import linux_sim  # noqa: E402

DESKTOP = sys.argv[1] if len(sys.argv) > 1 else "hyprland"
DIALECT = sys.argv[2] if len(sys.argv) > 2 else "lua"
linux_sim.pretend_linux(DESKTOP)
sys.path.insert(0, linux_sim.ROOT)

import osinfo  # noqa: E402

CALLS: list[list[str]] = []
MONITORS = [{"id": 0, "name": "DP-1", "x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1.25,
             "refreshRate": 143.97, "transform": 0, "focused": True},
            {"id": 1, "name": "HDMI-A-1", "x": 2048, "y": 0, "width": 1920, "height": 1080, "scale": 1.0,
             "refreshRate": 60.0, "transform": 0, "focused": False}]
TOOLS = {"hyprland": {"hyprctl", "wtype", "wl-paste", "wl-copy", "busctl", "playerctl", "wpctl"},
         "kde": {"kwriteconfig6", "kreadconfig6", "busctl", "kdotool", "ydotool", "wl-paste", "wl-copy",
                 "playerctl", "wpctl"}}[DESKTOP]


def fake_run(args, timeout, input_text):
    CALLS.append(list(args))
    tool = args[0]
    if tool == "hyprctl":
        if args[1] == "eval":
            if DIALECT == "lua":
                return osinfo.Result(True, "ok")
            return osinfo.Result(True, "eval is only supported with the lua config manager" if DIALECT == "hyprlang"
                                 else "unknown request")
        if args[1:3] == ["-j", "version"]:
            return osinfo.Result(True, json.dumps({"tag": {"lua": "v0.56.2", "hyprlang": "v0.54.1",
                                                           "legacy": "v0.50.1"}[DIALECT]}))
        if args[1:3] == ["-j", "monitors"]:
            return osinfo.Result(True, json.dumps(MONITORS))
        if args[1:3] == ["-j", "activewindow"]:
            return osinfo.Result(True, "{}")
        if args[1:3] == ["-j", "clients"]:
            return osinfo.Result(True, "[]")
        if args[1:3] == ["-j", "getoption"]:
            return osinfo.Result(True, json.dumps({"option": "animations:enabled", "int": 1}))
        if args[1] in ("keyword", "dispatch"):
            if DIALECT == "lua":
                return osinfo.Result(True, "keyword can't work with non-legacy parsers. Use eval.")
            if args[1] == "keyword" and args[2] == "windowrule":
                new_syntax = args[3].startswith("match:")
                return osinfo.Result(True, "ok" if new_syntax == (DIALECT == "hyprlang") else "invalid rule")
            return osinfo.Result(True, "ok")
    if tool in ("kwriteconfig6", "busctl"):
        return osinfo.Result(True, "")
    if tool == "kreadconfig6":
        return osinfo.Result(True, "")
    return osinfo.Result(False, "", f"{tool} not faked", 1)


osinfo.RUNNER.update(run=fake_run, which=lambda name: f"/usr/bin/{name}" if name in TOOLS else None,
                     spawn=lambda args: CALLS.append(["spawn", *args]) or True)

from tests import common  # noqa: E402  (offscreen Qt app, temp config)
from tests.common import backend, pump  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, why: str = "") -> None:
    RESULTS.append((bool(ok), name, why))


def joined(calls) -> list[str]:
    return [" ".join(c) for c in calls]


def calls_since(mark: int) -> list[str]:
    return joined(CALLS[mark:])


def any_call(lines: list[str], *parts: str) -> bool:
    return any(all(p in line for p in parts) for line in lines)


common.use_temp_config()
backend.CONFIG.update(bar_animate=False, show_status_bar=True, shade_enabled=True, bar_height=40)

# ---- the status bar --------------------------------------------------------------------------------------
from PySide6.QtCore import QTimer  # noqa: E402
from ui import status_bar as sb  # noqa: E402

check("monitors come from Qt on Linux (scale 1.0)", all(m[4] == 1.0 for m in sb.list_monitors()), sb.list_monitors())
from controller import Assistant  # noqa: E402

ctl = Assistant()
bar = sb.StatusBar(ctl)
mark = len(CALLS)
bar.prepare_linux()
bar.show()
pump(150)
lines = calls_since(mark)
x, y, w, h = bar.linux_rect()
check("bar is titled for the compositor's rules", bar.windowTitle() == sb.BAR_TITLE, bar.windowTitle())
check("bar geometry is its strip", (bar.x(), bar.y(), bar.width(), bar.height()) == (x, y, w, h),
      ((bar.x(), bar.y(), bar.width(), bar.height()), (x, y, w, h)))
if DESKTOP == "hyprland":
    if DIALECT == "lua":
        check("bar rule (lua)", any_call(lines, "hyprctl eval hl.window_rule(", "NEON status bar", "float = true",
                                         "pin = true", "no_focus = true"), lines)
        check("bar placed (lua)", any_call(lines, "hl.dsp.window.move(", f"x = {x}, y = {y}"), lines)
        check("bar space reserved (lua)", any_call(lines, "hl.monitor(", "reserved_area = { top = 40",
                                                   'mode = "2560x1440@143.97"', 'scale = "1.25"'), lines)
    elif DIALECT == "hyprlang":
        check("bar rule (0.53 syntax)", any_call(lines, "hyprctl keyword windowrule match:title ^(NEON status bar)$",
                                                 "float on", "pin on", "no_focus on"), lines)
        check("bar placed", any_call(lines, "hyprctl dispatch movewindowpixel exact", "NEON status bar"), lines)
        check("bar space reserved", any_call(lines, "hyprctl keyword monitor", "addreserved,40,0,0,0"), lines)
    else:
        check("bar rules (old syntax)", any_call(lines, "hyprctl keyword windowrule float, title:^(NEON status bar)$")
              and any_call(lines, "hyprctl keyword windowrule nofocus, title:"), lines)
        check("bar space reserved", any_call(lines, "hyprctl keyword monitor", "addreserved,40,0,0,0"), lines)
    mark = len(CALLS)
    backend.CONFIG["bar_position"] = "bottom"
    bar.apply_config()
    pump(120)
    lines = calls_since(mark)
    check("bar moved to the bottom reserves the bottom",
          any_call(lines, "reserved_area = { top = 0, bottom = 40") or any_call(lines, "addreserved,0,40,0,0"), lines)
    mark = len(CALLS)
    bar.unregister_appbar()
    lines = calls_since(mark)
    check("quitting gives the space back",
          any_call(lines, "reserved_area = { top = 0, bottom = 0") or any_call(lines, "addreserved,0,0,0,0"), lines)
else:
    check("bar rule written for KWin", any_call(lines, "kwriteconfig6 --file kwinrulesrc --group "
                                                "neon-assistant-neon-status-bar --key position", f"{x},{y}")
          and any_call(lines, "--key acceptfocus false") and any_call(lines, "--key aboverule 2"), lines)
    check("KWin asked to reload", any_call(lines, "busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure"),
          lines)
    check("no hyprctl on KDE", not any_call(lines, "hyprctl"), lines)
bar.hide()
for timer in bar.findChildren(QTimer):
    timer.stop()

# ---- the shade -------------------------------------------------------------------------------------------
from ui import shade as shade_module  # noqa: E402

backend.CONFIG.update(show_status_bar=False, bar_position="top")
shade = shade_module.Shade(ctl)
mark = len(CALLS)
shade.preview()
pump(150)
lines = calls_since(mark)
check("shade shown", shade.isVisible())
check("shade titled", shade.windowTitle() == shade_module.SHADE_TITLE)
if DESKTOP == "hyprland":
    check("shade floats and is placed", any_call(lines, "NEON shade") and
          (any_call(lines, "hl.dsp.window.move(") or any_call(lines, "movewindowpixel")), lines)
else:
    check("shade KWin rule", any_call(lines, "--group neon-assistant-neon-shade --key size"), lines)
shade.hide()

# ---- the "close this?" outline ---------------------------------------------------------------------------
from ui import window_highlight  # noqa: E402

hl = window_highlight.WindowHighlight(ctl)
mark = len(CALLS)
hl.show_windows(["0x55aa"], 5)
lines = calls_since(mark)
if DESKTOP == "hyprland":
    check("outline recolours the border", hl.active and (any_call(lines, "set_prop(", "address:0x55aa", "rgb(")
                                                        or any_call(lines, "setprop", "address:0x55aa", "rgb(")), lines)
    mark = len(CALLS)
    hl.clear()
    lines = calls_since(mark)
    check("outline cleared", not hl.active and (any_call(lines, "unset") or any_call(lines, "-1")), lines)
else:
    check("no outline on KDE, and no error", not hl.active)

# ---- the quick box ---------------------------------------------------------------------------------------
from ui.quick_input import QuickInput  # noqa: E402

box = QuickInput(ctl)
mark = len(CALLS)
box.open_box()
pump(400)
lines = calls_since(mark)
check("quick box opens", box.isVisible())
if DESKTOP == "hyprland":
    check("quick box floats and is placed", any_call(lines, "NEON quick box"), lines)
else:
    check("quick box KWin rule", any_call(lines, "--group neon-assistant-neon-quick-box --key position"), lines)
box.hide()

# ---- hotkeys ---------------------------------------------------------------------------------------------
import hotkeys  # noqa: E402

manager = hotkeys.HotkeyManager()
mark = len(CALLS)
errors = manager.apply([("Talk", "Ctrl+Alt+N", lambda: None, "talk"), ("Quick box", "Meta+Space", lambda: None, "quick")])
hold = hotkeys.HoldKeyHook(lambda: None, lambda: None)
hold_error = hold.set_combo("Ctrl+Alt+H")
lines = calls_since(mark)
if DESKTOP == "hyprland":
    check("hotkeys bound without errors", not errors and not hold_error, (errors, hold_error))
    if DIALECT == "lua":
        check("talk bind (lua)", any_call(lines, 'hl.bind("CTRL + ALT + N", hl.dsp.exec_cmd(', "--command talk"), lines)
        check("hold release bind (lua)", any_call(lines, 'hl.bind("CTRL + ALT + H"', "hold-stop", "release = true"),
              lines)
    else:
        check("talk bind", any_call(lines, "hyprctl keyword bind CTRL ALT, N, exec,", "--command talk"), lines)
        check("hold release bind", any_call(lines, "hyprctl keyword bindr CTRL ALT, H, exec,", "hold-stop"), lines)
    mark = len(CALLS)
    manager.unregister()
    lines = calls_since(mark)
    check("hotkeys unbound", any_call(lines, "unbind"), lines)
else:
    from linuxdesk import portal_keys
    if portal_keys.available():
        check("KDE hotkeys go to the portal without errors", not errors and not hold_error, (errors, hold_error))
    else:
        check("KDE without jeepney explains what's missing", errors and "jeepney" in errors[0], errors)

# ---- spoken commands, through the real router and the Linux backends -----------------------------------
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402


class Agent:
    def run(self, text):
        return {"results": [{"status": f"TOOL:{text}"}]}

    def reset(self):
        pass


def say(text: str) -> str:
    return str(backend.handle_utterance(Agent(), text))


work = Path(tempfile.mkdtemp())
(work / "share" / "applications").mkdir(parents=True)
(work / "share" / "applications" / "firefox.desktop").write_text(
    "[Desktop Entry]\nType=Application\nName=Firefox\nGenericName=Web Browser\nExec=firefox %u\n", encoding="utf-8")
os.environ.update(XDG_DATA_HOME=str(work / "share"), XDG_DATA_DIRS=str(work / "nothing"))
resume = work / "docs" / "resume 2026.pdf"
resume.parent.mkdir()
resume.write_text("x")
TOOLS.update({"wpctl", "plocate", "grim", "playerctl", "gio", "kdotool"} if DESKTOP == "kde" else {"wpctl", "plocate", "grim", "playerctl", "gio"})
_previous_run = osinfo.RUNNER["run"]


def commands_run(args, timeout, input_text):
    if args[0] == "wpctl":
        CALLS.append(list(args))
        return osinfo.Result(True, "Volume: 0.25\n" if args[1] == "get-volume" else "")
    if args[0] == "plocate":
        CALLS.append(list(args))
        return osinfo.Result(True, f"{resume}\n")
    if args[0] == "grim":
        CALLS.append(list(args))
        Path(args[-1]).write_bytes(b"\x89PNG fake")
        return osinfo.Result(True, "")
    if args[0] == "playerctl":
        CALLS.append(list(args))
        return osinfo.Result(True, "spotify\tPlaying\tDaft Punk\tAround the World\t1000000\t420000000\n")
    if args[:3] == ["hyprctl", "-j", "clients"]:
        CALLS.append(list(args))
        return osinfo.Result(True, json.dumps([{"address": "0xf1", "pid": 4242, "class": "firefox", "mapped": True,
                                                "title": "News - Mozilla Firefox", "workspace": {"name": "1"}}]))
    if args[0] == "kdotool":
        CALLS.append(list(args))
        answers = {"search": "{f1}\n", "getwindowname": "News — Mozilla Firefox\n",
                   "getwindowclassname": "firefox\n", "getwindowpid": "4242\n", "windowclose": ""}
        return osinfo.Result(args[1] in answers, answers.get(args[1], ""))
    return _previous_run(args, timeout, input_text)


osinfo.RUNNER["run"] = commands_run
backend.APP_CACHE_PATH = work / "app.cache"
backend.build_app_catalogue(background=False)
backend.CONFIG.update(system_control_enabled=True, files_enabled=True, confirm_window_close=True)
mark = len(CALLS)
reply = say("set the volume to 40 percent")
check("\"set the volume to 40 percent\"", "40" in reply and ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.40"]
      in CALLS[mark:], (reply, calls_since(mark)))
spawned = len(CALLS)
reply = backend.launch_app("firefox")                  # what the tool-picker calls for "open firefox"
check("\"open firefox\" launches its .desktop file", any(c[:3] == ["spawn", "gio", "launch"] and
                                                         c[3].endswith("firefox.desktop") for c in CALLS[spawned:]),
      (reply, calls_since(spawned)))
reply = say("find the file called resume")
check("\"find the file called resume\" uses plocate", "resume 2026" in reply, reply)
import media  # noqa: E402

backend.MEDIA = media.MediaWatcher()
backend.CONFIG["media_any_player"] = True
reply = str(backend.handle_music_command("what's playing"))
backend.MEDIA.stop()
check("\"what's playing\" asks MPRIS", "Around the World" in reply, reply)
shots = work / "Pictures"
with __import__("unittest.mock").mock.patch("linuxdesk.system.screenshots_dir", return_value=shots):
    reply = say("take a screenshot")
check("\"take a screenshot\" uses grim", any(c[0] == "grim" for c in CALLS) and list(shots.glob("*.png")), reply)
window_id = "0xf1" if DESKTOP == "hyprland" else "{f1}"
mark = len(CALLS)
reply = say("close firefox")
check("\"close firefox\" asks first", "?" in reply and not any("close" in line and window_id in line
                                                                for line in calls_since(mark)), reply)
reply = say("yes")
check("...and closes it after a yes", any(window_id in line and any(w in line for w in ("closewindow", "window.close",
                                                                                         "windowclose"))
                                          for line in calls_since(mark)), (reply, calls_since(mark)))
osinfo.RUNNER["run"] = _previous_run

# ---- Settings and the welcome tour: every page builds with the Linux wording ------------------------------
from ui.onboarding import OnboardingDialog  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

try:
    dialog = SettingsDialog(common.StubController())
    pump(100)
    texts = " ".join(w.text() for w in dialog.findChildren(__import__("PySide6.QtWidgets").QtWidgets.QCheckBox))
    check("Settings builds", True)
    check("Settings offers the Plasma panel widget", "Feed the Plasma panel widget" in texts, texts[:300])
    check("Settings speaks Linux", "sign in to Windows" not in texts and "Start automatically when I sign in" in texts,
          texts[:300])
    dialog.close()
    tour = OnboardingDialog(common.StubController())
    pump(50)
    check("welcome tour builds", True)
    tour.close()
except Exception as exc:  # noqa: BLE001
    import traceback
    check("Settings and the tour build", False, traceback.format_exc()[-1500:])

# ---- the command socket ----------------------------------------------------------------------------------
from linuxdesk import single  # noqa: E402

got = []
server = single.CommandServer(got.append)
check("command server listens", server.listen())
check("a second NEON sees the first", not single.CommandServer(lambda c: None).listen())
got.clear()
import subprocess  # noqa: E402

# The way a compositor keybind runs it: main.py --command talk, in its own process.
sender = subprocess.Popen([sys.executable, os.path.join(linux_sim.ROOT, "main.py"), "--command", "talk"])
for _ in range(100):
    pump(100)
    if sender.poll() is not None:
        break
pump(100)
check("`main.py --command talk` reaches the running NEON", got == ["talk"] and sender.returncode == 0,
      (got, sender.returncode))
server.close()

failed = [r for r in RESULTS if not r[0]]
for ok, name, why in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {str(why)[:600]}"))
print(f"{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed ({DESKTOP}{'' if DESKTOP == 'kde' else ' ' + DIALECT})")
sys.stdout.flush()
os._exit(1 if failed else 0)
