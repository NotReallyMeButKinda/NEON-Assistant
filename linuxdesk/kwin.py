"""
linuxdesk/kwin.py -- KDE Plasma: KWin window rules for NEON's own bar and pop-ups.

On Wayland KWin decides where every window goes, so an app can't put its status bar along the top of the
screen by itself. A KWin window rule can: it matches the window by its exact title and forces its position,
size, "keep above", no title bar, all virtual desktops, and no taskbar / pager / Alt+Tab entry. NEON writes
one rule group per window into ~/.config/kwinrulesrc (the same file System Settings > Window Management >
Window Rules edits; each shows up there as "NEON Assistant: ...") and asks KWin to reload them.

Uses kwriteconfig6 / kreadconfig6 (Plasma's own tools) and busctl (systemd).
"""

from __future__ import annotations

import re

import osinfo

FILE = "kwinrulesrc"
FORCE = 2            # KWin's Rules::Force
EXACT = 1            # KWin's Rules::ExactMatch


def _tool(kind: str) -> str:
    for name in (f"k{kind}config6", f"k{kind}config5"):
        if osinfo.which(name):
            return name
    return ""


def available() -> bool:
    return osinfo.desktop() == "kde" and bool(_tool("write")) and bool(osinfo.which("busctl"))


def group_name(title: str) -> str:
    return "neon-assistant-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _write(group: str, key: str, value: str) -> bool:
    return osinfo.run([_tool("write"), "--file", FILE, "--group", group, "--key", key, value], timeout=3).ok


def _read(group: str, key: str) -> str:
    tool = _tool("read")
    if not tool:
        return ""
    done = osinfo.run([tool, "--file", FILE, "--group", group, "--key", key], timeout=3)
    return done.out.strip() if done.ok else ""


def _list_add(key: str, item: str, only_if_present: bool = False) -> None:
    current = [x for x in _read("General", key).split(",") if x]
    if item in current or (only_if_present and not current):
        return
    _write("General", key, ",".join(current + [item]))


def window_rule(title: str, x: int | None = None, y: int | None = None, width: int | None = None,
                height: int | None = None, focus: bool = False) -> bool:
    """Write (or rewrite) the rule for NEON's window with this exact title. Returns False if KWin's config
    tools aren't there. Call reconfigure() afterwards for KWin to pick it up."""
    if not available():
        return False
    group = group_name(title)
    values = {"Description": f"NEON Assistant: {title}", "Enabled": "true", "title": title, "titlematch": str(EXACT),
              "above": "true", "aboverule": str(FORCE), "noborder": "true", "noborderrule": str(FORCE),
              "skiptaskbar": "true", "skiptaskbarrule": str(FORCE), "skippager": "true",
              "skippagerrule": str(FORCE), "skipswitcher": "true", "skipswitcherrule": str(FORCE),
              "desktops": "", "desktopsrule": str(FORCE)}           # an empty list, forced: every desktop
    if not focus:
        values.update({"acceptfocus": "false", "acceptfocusrule": str(FORCE)})
    if x is not None and y is not None:
        values.update({"position": f"{int(x)},{int(y)}", "positionrule": str(FORCE)})
    if width is not None and height is not None:
        values.update({"size": f"{int(width)},{int(height)}", "sizerule": str(FORCE)})
    ok = all([_write(group, key, value) for key, value in values.items()])
    # Current Plasma loads every rule group and adds it to "Order" itself; older Plasma 6 only loads the
    # groups listed under "rules" -- extend that list only if it's in use (writing it otherwise would make
    # new KWin drop every group not in it).
    _list_add("Order", group)
    _list_add("rules", group, only_if_present=True)
    return ok


def remove_rule(title: str) -> bool:
    if not available():
        return False
    return osinfo.run([_tool("write"), "--file", FILE, "--group", group_name(title), "--key", "Enabled", "false"],
                      timeout=3).ok


def reconfigure() -> bool:
    """Ask KWin to reload its settings, window rules included (forced rules then apply to open windows)."""
    return osinfo.run(["busctl", "--user", "call", "org.kde.KWin", "/KWin", "org.kde.KWin", "reconfigure"],
                      timeout=5).ok
