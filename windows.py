"""
windows.py -- "close this app", "minimize everything": acting on the windows on screen.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import neon_log

log = neon_log.get("windows")


# ---------------------------------------------------------------------------
# "Close this app" -- gracefully close whatever window currently has focus.
# ---------------------------------------------------------------------------

_CLOSE_APP = re.compile(
    r"^(?:(?:please|hey|can you|could you)\s+)*(?:close|quit|kill|exit|terminate|shut down|end)\s+"
    r"(?:out of\s+)?(?:"
    r"(?:this|that|the current|the active|the focused|the foreground|the open|current|my current|my active)"
    r"\s+(?:app|application|program|window|software)"
    r"|this|that|it"
    r"|(?:the\s+)?(?:app|application|program|window)"
    r")(?:\s+(?:please|now))?$")
_PROTECTED_WINDOW_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                             "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}


def spoken_close_app(text: str) -> bool:
    """True for 'close this app' / 'quit that program' / 'close the window' (whole utterance).
    A bare 'exit' is deliberately NOT here: that one closes this assistant, and only that."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    return bool(_CLOSE_APP.match(t))


_CLOSE_NAMED = re.compile(
    r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)*"
    r"(?:close|quit|kill|exit|terminate|shut|end|get rid of)\s+(?:out of\s+|down\s+)?"
    r"(?P<all>all\s+(?:of\s+)?(?:the\s+|my\s+)?)?(?:the\s+|my\s+)?(?P<target>.+?)"
    r"(?P<noun>\s+(?:app|application|program|window|windows|software|apps))?(?:\s+(?:please|now))?$")
# Things "close X" can mean that aren't a window: the PC (that's shutdown), browser tabs, and the
# assistant's own panels, which have their own commands.
_NOT_A_WINDOW = re.compile(
    r"^(?:(?:the\s+|this\s+|that\s+|my\s+|current\s+)?(?:pc|computer|machine|laptop|system|windows|"
    r"tabs?|browser tab|notifications?|board|timers?|reminders?|panel|everything|all|it all|yourself|"
    r"it|this|that|them|down)|.*\btabs?)$")


def spoken_close_command(text: str) -> tuple[str, bool, bool] | None:
    """(target, every_window, named_as_window) for 'close discord' / 'quit the spotify app' /
    'close all chrome windows'; target '' means the focused window ('close this app'). None when it
    isn't a close command. `named_as_window` says the user called it an app / window, which decides
    whether an unknown name is reported ("I couldn't find ...") or left for the rest of the router."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    if _CLOSE_APP.match(t):
        return "", False, True
    m = _CLOSE_NAMED.match(t)
    if not m:
        return None
    target = m.group("target").strip()
    if not target or _NOT_A_WINDOW.match(target) or len(target.split()) > 4:
        return None
    return target, bool(m.group("all")), bool(m.group("noun"))


def _process_image_stem(pid: int) -> str:
    """'chrome' for a process id (empty string if it can't be read)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        return Path(buf.value).stem if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def _window_info(hwnd) -> tuple[int, str, str, str]:
    """(pid, class name, title, friendly label) of a window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    label = title.value.split(" - ")[-1].strip()[:40] or _process_image_stem(pid.value)
    return pid.value, cls.value, title.value, label


def _foreground_window():
    """The focused window as (hwnd, pid, label), or a ready-to-speak refusal string when
    it's something we must never act on (the desktop / taskbar) or nothing is focused."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "I can't tell which app is focused right now."
    pid, cls, _title, label = _window_info(hwnd)
    if cls in _PROTECTED_WINDOW_CLASSES:
        return "I won't touch the desktop or the taskbar."
    return hwnd, pid, label or "the current window"


def close_focused_app() -> str:
    """Asks the foreground window to close (WM_CLOSE), like clicking its X, so apps can still
    prompt to save. Refuses to touch this assistant, the desktop, or the taskbar."""
    import ctypes
    target = _foreground_window()
    if isinstance(target, str):
        return target.replace("touch", "close")
    hwnd, pid, label = target
    if pid == os.getpid():
        return "That's me. Say exit if you want to close me."
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return f"Closing {label}."


# ---------------------------------------------------------------------------
# Minimize / maximize / restore windows (the focused one, a named one, or everything)
# ---------------------------------------------------------------------------

_SC = {"minimize": 0xF020, "maximize": 0xF030, "restore": 0xF120}   # SC_MINIMIZE / MAXIMIZE / RESTORE
_ALL_WINDOWS = {"all", "everything", "all windows", "all apps", "all of them", "every window",
                "all my windows", "all the windows", "all my apps", "the desktop", "desktop"}
_WINDOW_CMD = re.compile(
    r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)*"
    r"(?P<verb>minimi[sz]e|maximi[sz]e|restore|un-?maximi[sz]e|un-?minimi[sz]e|enlarge)"
    r"(?:\s+(?P<obj>.+?))?(?:\s+(?:please|now))?$")
_WINDOW_FILLER = re.compile(r"\b(?:the|this|that|my|current|active|focused|foreground|open|currently)\b")
_WINDOW_NOUNS = re.compile(r"\b(?:windows?|apps?|applications?|programs?|software)\b")


def spoken_window_command(text: str) -> tuple[str, str] | None:
    """'minimize this window' -> ('minimize', ''); 'maximize chrome' -> ('maximize', 'chrome');
    'minimize everything' -> ('minimize', 'all'). Whole-utterance matches only. Target '' means
    the focused window. 'restore' needs an object so a stray 'restore' doesn't fire."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    m = _WINDOW_CMD.match(t)
    if not m:
        return None
    verb = m.group("verb")
    action = ("minimize" if verb.startswith("minimi")
              else "restore" if verb.startswith(("restore", "unm", "un-m")) else "maximize")
    obj = (m.group("obj") or "").strip()
    if obj in _ALL_WINDOWS:
        return action, "all"
    core = " ".join(_WINDOW_NOUNS.sub(" ", _WINDOW_FILLER.sub(" ", obj)).split())
    if obj and not core and not _WINDOW_NOUNS.search(obj) and obj not in ("it", "that", "this"):
        return None
    if core in ("", "it"):
        return (action, "") if (obj or action != "restore") else None
    if len(core.split()) > 3:
        return None
    return action, core


def find_window(name: str):
    """(hwnd, pid, label) of the best visible top-level window for `name` (matches the program
    name first, then the title), skipping tool windows and our own; None if there isn't one."""
    found = find_windows(name)
    return found[0] if found else None


def find_windows(name: str) -> list[tuple]:
    """Every visible top-level window for `name` as (hwnd, pid, label), best match first."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    name = name.lower().strip()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80 and not user32.GetWindowLongW(hwnd, -20) & 0x40000:
            return True  # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW: not a real app window
        pid, cls, title, label = _window_info(hwnd)
        if cls in _PROTECTED_WINDOW_CLASSES or pid == os.getpid():
            return True
        stem = _process_image_stem(pid).lower()
        if name == stem:
            score = 0
        elif stem.startswith(name) or name in stem:
            score = 1
        elif name in title.lower():
            score = 2
        else:
            return True
        found.append((score, hwnd, pid, label or stem))
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return []
    best = min(f[0] for f in found)
    # Only the best tier: "close code" must not also close a browser tab titled "...code...".
    return [(hwnd, pid, label) for score, hwnd, pid, label in sorted(found, key=lambda f: f[0]) if score == best]


def close_windows(targets: list[tuple]) -> str:
    """Ask each window to close (WM_CLOSE, like clicking its X, so apps can still offer to save).
    Windows that have gone away in the meantime are skipped."""
    import ctypes
    user32 = ctypes.windll.user32
    closed = [label for hwnd, pid, label in targets
              if pid != os.getpid() and user32.IsWindow(hwnd) and user32.PostMessageW(hwnd, 0x0010, 0, 0)]
    if not closed:
        return "That window has already gone."
    if len(closed) == 1:
        return f"Closing {closed[0]}."
    return f"Closing {len(closed)} {closed[0]} windows."


def spoken_window_list(targets: list[tuple]) -> str:
    """'Discord' / '3 Chrome windows' for a confirmation question."""
    if len(targets) == 1:
        return targets[0][2]
    return f"all {len(targets)} {targets[0][2]} windows"


def _press_keys(*vks: int) -> None:
    """Chord like Win+M: hold every key down in order, then release in reverse."""
    import ctypes
    user32 = ctypes.windll.user32
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 2, 0)


def window_command(action: str, target: str = "", found=None) -> str:
    """Minimize / maximize / restore the focused window, a named one, or every window. `found` is an
    already-resolved find_window() result (saves a second enumeration of every window)."""
    import ctypes
    user32 = ctypes.windll.user32
    if target == "all":
        if action == "minimize":
            _press_keys(0x5B, 0x4D)                # Win+M
            return "Minimizing all windows."
        if action == "restore":
            _press_keys(0x5B, 0x10, 0x4D)          # Win+Shift+M
            return "Restoring your windows."
        return "I can only minimize or restore all windows at once."

    if target:
        found = found or find_window(target)
        if found is None:
            return f"I couldn't find a window for {target}."
    else:
        found = _foreground_window()
        if isinstance(found, str):
            return found
    hwnd, _pid, label = found

    minimized, maximized = bool(user32.IsIconic(hwnd)), bool(user32.IsZoomed(hwnd))
    if action == "minimize" and minimized:
        return f"{label} is already minimized."
    if action == "maximize" and maximized:
        return f"{label} is already maximized."
    if action == "restore" and not (minimized or maximized):
        return f"{label} is already at its normal size."
    if action == "maximize" and minimized:
        user32.PostMessageW(hwnd, 0x0112, _SC["restore"], 0)   # WM_SYSCOMMAND; un-minimize first
    user32.PostMessageW(hwnd, 0x0112, _SC[action], 0)
    return {"minimize": f"Minimizing {label}.", "maximize": f"Maximizing {label}.",
            "restore": f"Restoring {label}."}[action]
