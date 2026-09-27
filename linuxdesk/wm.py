"""
linuxdesk/wm.py -- the windows on screen, on Linux: list them, find one by name, focus, close, minimize,
maximize, and tell whether something is fullscreen.

Wayland doesn't let one app see or touch another's windows, so this goes through the compositor:
  * Hyprland: hyprctl (clients / activewindow / dispatch), which can do all of it;
  * KDE Plasma: kdotool (AUR: kdotool), which drives KWin's scripting;
  * anything else: nothing (the commands say so).

A window is the same tuple windows.py uses on Windows, (id, pid, label), with the id being Hyprland's
address ('0x55d4...') or KDE's window UUID.
"""

from __future__ import annotations

import os
import re

import osinfo
from linuxdesk import hypr


def backend() -> str:
    """'hyprland' | 'kde' | '' (no way to manage windows here)."""
    desktop = osinfo.desktop()
    if desktop == "hyprland" and osinfo.which("hyprctl"):
        return "hyprland"
    if desktop == "kde" and osinfo.which("kdotool"):
        return "kde"
    return ""


def kde_shortcut(name: str) -> bool:
    """Run one of KWin's own shortcuts by name ('Show Desktop', 'Window Maximize') over D-Bus. busctl comes with
    systemd, so this works whether Plasma's qdbus is installed as qdbus6, qdbus-qt6 or not at all."""
    return osinfo.run(["busctl", "--user", "call", "org.kde.kglobalaccel", "/component/kwin",
                       "org.kde.kglobalaccel.Component", "invokeShortcut", "s", name], timeout=3).ok


UNSUPPORTED = ("I can't manage windows on this desktop. On KDE, install kdotool (it's in the AUR); on "
               "Hyprland it works out of the box.")


def _label(title: str, app: str) -> str:
    """What a person calls a window: the app part of 'Page - Site - Firefox', else the app name."""
    parts = [p.strip() for p in re.split(r" [-—–] ", title) if p.strip()]
    if len(parts) > 1:
        return parts[-1][:40]
    return (app or title or "a window")[:40]


def _app_name(cls: str) -> str:
    """'org.kde.dolphin' -> 'dolphin', 'firefox' -> 'firefox'."""
    return (cls or "").split(".")[-1].lower()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def windows() -> list[dict]:
    """Every normal window: {"id", "pid", "app", "title", "fullscreen", "minimized", "monitor", "at", "size"}."""
    kind = backend()
    if kind == "hyprland":
        clients = hypr.query("clients") or []
        out = []
        for c in clients:
            if not c.get("mapped", True) or c.get("hidden") or not c.get("address"):
                continue
            workspace = (c.get("workspace") or {}).get("name", "")
            out.append({"id": c["address"], "pid": int(c.get("pid") or 0), "app": _app_name(c.get("class", "")),
                        "title": c.get("title", ""), "fullscreen": bool(c.get("fullscreen")),
                        "minimized": str(workspace).startswith("special:minimized"),
                        "monitor": c.get("monitor"), "at": c.get("at"), "size": c.get("size")})
        return out
    if kind == "kde":
        done = osinfo.run(["kdotool", "search", "--class", "."], timeout=5)
        out = []
        for wid in done.out.split() if done.ok else []:
            title = osinfo.run(["kdotool", "getwindowname", wid], timeout=3).out.strip()
            cls = osinfo.run(["kdotool", "getwindowclassname", wid], timeout=3).out.strip()
            pid = osinfo.run(["kdotool", "getwindowpid", wid], timeout=3).out.strip()
            out.append({"id": wid, "pid": int(pid) if pid.isdigit() else 0, "app": _app_name(cls), "title": title,
                        "fullscreen": False, "minimized": False, "monitor": None, "at": None, "size": None})
        return out
    return []


def active() -> dict | None:
    kind = backend()
    if kind == "hyprland":
        c = hypr.query("activewindow") or {}
        if not c.get("address"):
            return None
        return {"id": c["address"], "pid": int(c.get("pid") or 0), "app": _app_name(c.get("class", "")),
                "title": c.get("title", ""), "fullscreen": bool(c.get("fullscreen")), "monitor": c.get("monitor"),
                "at": c.get("at"), "size": c.get("size")}
    if kind == "kde":
        wid = osinfo.run(["kdotool", "getactivewindow"], timeout=3).out.strip()
        if not wid:
            return None
        return next((w for w in windows() if w["id"] == wid), None)
    return None


def find(name: str) -> list[tuple]:
    """(id, pid, label) for every window of `name`, best tier only (the program name first, then the
    title), never NEON's own -- the same rules as windows.find_windows on Windows."""
    name = name.lower().strip()
    found = []
    for w in windows():
        if w["pid"] == os.getpid():
            continue
        app = w["app"]
        if name == app:
            score = 0
        elif app.startswith(name) or name in app:
            score = 1
        elif name in w["title"].lower():
            score = 2
        else:
            continue
        found.append((score, w["id"], w["pid"], _label(w["title"], app)))
    if not found:
        return []
    best = min(f[0] for f in found)
    return [(wid, pid, label) for score, wid, pid, label in sorted(found, key=lambda f: f[0]) if score == best]


# ---------------------------------------------------------------------------
# Acting on them
# ---------------------------------------------------------------------------

def close(wid: str) -> bool:
    """Ask the window to close, like clicking its X (it can still offer to save)."""
    kind = backend()
    if kind == "hyprland":
        return hypr.close_window(wid)
    if kind == "kde":
        return osinfo.run(["kdotool", "windowclose", wid], timeout=3).ok
    return False


def focus(wid: str) -> bool:
    kind = backend()
    if kind == "hyprland":
        return hypr.focus_window(wid)
    if kind == "kde":
        return osinfo.run(["kdotool", "windowactivate", wid], timeout=3).ok
    return False


def minimize(wid: str) -> bool:
    """Hyprland has no minimizing: the window moves to a hidden special workspace NEON restores it from."""
    kind = backend()
    if kind == "hyprland":
        return hypr.move_to_workspace(wid, "special:minimized")
    if kind == "kde":
        return osinfo.run(["kdotool", "windowminimize", wid], timeout=3).ok
    return False


def restore(wid: str) -> bool:
    kind = backend()
    if kind == "hyprland":
        workspace = (hypr.query("activeworkspace") or {}).get("id", 1)
        return hypr.move_to_workspace(wid, str(workspace), follow=True)
    if kind == "kde":
        return osinfo.run(["kdotool", "windowactivate", wid], timeout=3).ok
    return False


def maximize(wid: str) -> bool:
    kind = backend()
    if kind == "hyprland":
        return hypr.maximize(wid)
    if kind == "kde":
        # kdotool has no maximize; the KWin shortcut acts on the active window
        return osinfo.run(["kdotool", "windowactivate", wid], timeout=3).ok and kde_shortcut("Window Maximize")
    return False


def minimize_all() -> str:
    kind = backend()
    if kind == "hyprland":
        count = sum(minimize(w["id"]) for w in windows() if not w["minimized"] and w["pid"] != os.getpid())
        return "Minimizing all windows." if count else "There's nothing to minimize."
    if kind == "kde":
        ok = kde_shortcut("Show Desktop")
        return "Minimizing all windows." if ok else "I couldn't minimize the windows."
    return UNSUPPORTED


def restore_all() -> str:
    kind = backend()
    if kind == "hyprland":
        count = sum(restore(w["id"]) for w in windows() if w["minimized"])
        return "Restoring your windows." if count else "Nothing was minimized."
    if kind == "kde":
        ok = kde_shortcut("Show Desktop")
        return "Restoring your windows." if ok else "I couldn't restore the windows."
    return UNSUPPORTED


# ---------------------------------------------------------------------------
# Fullscreen (the shade and the quick box care)
# ---------------------------------------------------------------------------

def monitors() -> list[dict]:
    """[{"id", "name", "x", "y", "width", "height", "scale", "focused"}] in layout pixels."""
    if backend() != "hyprland":
        return []
    out = []
    for m in hypr.query("monitors") or []:
        scale = float(m.get("scale") or 1.0)
        out.append({"id": m.get("id"), "name": m.get("name", ""), "x": int(m.get("x", 0)), "y": int(m.get("y", 0)),
                    "width": int(round(int(m.get("width", 0)) / scale)),
                    "height": int(round(int(m.get("height", 0)) / scale)), "scale": scale,
                    "focused": bool(m.get("focused"))})
    return out


def fullscreen_monitor() -> tuple | None:
    """(left, top, right, bottom) of the monitor a fullscreen window fills, or None."""
    window = active()
    if not window or not window.get("fullscreen") or window["pid"] == os.getpid():
        return None
    for m in monitors():
        if window.get("monitor") in (m["id"], m["name"]):     # activewindow gives the monitor's id
            return m["x"], m["y"], m["x"] + m["width"], m["y"] + m["height"]
    at, size = window.get("at") or [0, 0], window.get("size") or [0, 0]
    return at[0], at[1], at[0] + size[0], at[1] + size[1]
