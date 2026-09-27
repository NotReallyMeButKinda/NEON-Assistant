"""NEON's own floating windows (the status bar, the shade, the quick box, the Wikipedia card) on Linux.

On Wayland the compositor decides where windows go and ignores where an app asks to put them:

  * Hyprland tiles every new window. A window rule matched on the window's title (set once, before it first
    opens) makes it float on every workspace without a border; once it's open it's moved to where Qt has it.
  * KDE Plasma places new windows itself. A KWin window rule, matched on the title, forces the position (and,
    for the bar and the shade, the size), keeps it above other windows, on every desktop and out of the
    taskbar; it's rewritten when the place changes.

Everywhere else (Windows, X11) these do nothing and Qt's own placement stands.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QWidget

import osinfo

_STATE: dict[str, dict] = {}          # title -> {"focus": bool, "ruled": bool, "kde_rect": tuple | None}


def _hypr():
    if osinfo.IS_WINDOWS:
        return None
    from linuxdesk import hypr
    return hypr if hypr.available() else None


def _kwin():
    if osinfo.IS_WINDOWS:
        return None
    from linuxdesk import kwin
    return kwin if kwin.available() else None


def prepare(widget: QWidget, title: str, focus: bool = True) -> None:
    """Give the window its title and, on Hyprland, the rule that floats it. Call before it first opens."""
    widget.setWindowTitle(title)
    state = _STATE.setdefault(title, {"focus": focus, "ruled": False, "kde_rect": None})
    state["focus"] = focus
    hypr = _hypr()
    if hypr is not None and not state["ruled"]:
        state["ruled"] = hypr.window_rules(title, focus=focus)


def settle(widget: QWidget, fixed_size: bool = False) -> None:
    """After a move or resize (or right before / after show()): put the window where Qt has it.
    `fixed_size`: KDE also forces the size (the bar, the shade), not only the position."""
    title = widget.windowTitle()
    state = _STATE.get(title)
    if state is None:
        return
    g = widget.frameGeometry() if widget.isVisible() else widget.geometry()
    hypr = _hypr()
    if hypr is not None:
        def place() -> None:
            if widget.isVisible():
                now = widget.frameGeometry()
                hypr.place(title, now.x(), now.y(), now.width(), now.height())
        QTimer.singleShot(40, place)                # once Hyprland has mapped the window
        return
    kwin = _kwin()
    if kwin is not None:
        rect = (g.x(), g.y(), g.width(), g.height()) if fixed_size else (g.x(), g.y())
        if rect != state["kde_rect"]:
            size = (g.width(), g.height()) if fixed_size else (None, None)
            if kwin.window_rule(title, g.x(), g.y(), *size, focus=state["focus"]):
                kwin.reconfigure()
                state["kde_rect"] = rect


def reapply_rules() -> None:
    """Hyprland reloaded its config, which drops runtime window rules: set them again."""
    hypr = _hypr()
    if hypr is None:
        return
    for title, state in _STATE.items():
        state["ruled"] = hypr.window_rules(title, focus=state["focus"])


class _Relay(QObject):
    fired = Signal()


_WATCH: dict = {}


def watch_reloads(callback) -> bool:
    """On Hyprland, call `callback` on the UI thread after every config reload (after the rules are back)."""
    hypr = _hypr()
    if hypr is None:
        return False
    relay = _Relay()
    relay.fired.connect(reapply_rules)
    relay.fired.connect(callback)
    watcher = hypr.ReloadWatcher(relay.fired.emit)
    _WATCH.update(relay=relay, watcher=watcher)
    return watcher.start()


def stop_watching() -> None:
    watcher = _WATCH.pop("watcher", None)
    if watcher is not None:
        watcher.stop()
