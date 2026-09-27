"""
linuxdesk/keys.py -- NEON's hotkeys on Linux.

Wayland apps can't grab keys for themselves; the compositor owns the keyboard. So:

  * Hyprland: NEON adds keybinds itself (hyprctl) that run NEON's command (`main.py --command talk`, or the
    packaged `neon-assistant --command talk`), which reaches the running NEON over its local socket
    (linuxdesk/single.py). Hold-to-talk is a bind for the press and one for the release.
  * KDE Plasma (and GNOME, and others with xdg-desktop-portal): the GlobalShortcuts portal
    (linuxdesk/portal_keys.py). Plasma asks once to confirm; after that they're in System Settings.
  * Neither available: `setup_hint()` says which command to bind in the desktop's own keyboard settings.

Same classes as hotkeys.py on Windows, so main.py drives both the same way.
"""

from __future__ import annotations

import shlex

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal

import app_paths
from linuxdesk import hypr, portal_keys


def command_line(action: str) -> str:
    return " ".join(shlex.quote(a) for a in app_paths.command_args(action))


def setup_hint(actions: list[tuple[str, str, str]]) -> str:
    """What to add as custom shortcuts on a desktop NEON can't bind keys on itself."""
    lines = [f"{label} ({spec}): {command_line(name)}" for label, spec, name in actions if spec]
    if not lines:
        return ""
    return ("Hotkeys on this desktop are set in its own keyboard settings (KDE: System Settings, Keyboard, "
            "Shortcuts, Add New, Command). Add: " + "; ".join(lines))


class _Portal(QObject):
    """The one portal session for every NEON hotkey (the regular ones and hold-to-talk): each owner sets its
    group, and the whole set is sent together a moment later (settings apply both groups back to back)."""

    pressed = Signal(str)
    released = Signal(str)
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self.groups: dict[str, list[tuple[str, str, str]]] = {}
        self.on_press: dict[str, callable] = {}
        self.on_release: dict[str, callable] = {}
        self.client = portal_keys.PortalShortcuts(self.pressed.emit, self.released.emit, self.status.emit)
        self.pressed.connect(lambda sid: self.on_press.get(sid, lambda: None)())       # queued: the UI thread
        self.released.connect(lambda sid: self.on_release.get(sid, lambda: None)())
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._send)
        self._sent: list | None = None

    def set_group(self, owner: str, shortcuts: list[tuple[str, str, str]]) -> None:
        self.groups[owner] = shortcuts
        self._timer.start()

    def _send(self) -> None:
        wanted = [s for group in self.groups.values() for s in group]
        if wanted == self._sent:
            return
        self._sent = wanted
        self.client.bind(wanted)

    def close(self) -> None:
        self._timer.stop()
        self._sent = None
        self.client.close()


_PORTAL: dict = {"portal": None}


def portal() -> _Portal:
    if _PORTAL["portal"] is None:
        _PORTAL["portal"] = _Portal()
    return _PORTAL["portal"]


class HotkeyManager(QAbstractNativeEventFilter):
    """binds: (label, spec, callback, action name). On Hyprland they become keybinds running NEON's command
    (main.py routes those commands to the callbacks); elsewhere they go to the GlobalShortcuts portal."""

    def __init__(self):
        super().__init__()
        self._binds = hypr.Binds()
        self.callbacks: dict[str, callable] = {}

    def apply(self, binds: list[tuple]) -> list[str]:
        self.unregister()
        wanted = []
        for bind in binds:
            label, spec, callback = bind[:3]
            name = bind[3] if len(bind) > 3 else label.lower()
            spec = str(spec or "").strip()
            self.callbacks[name] = callback
            if spec:
                wanted.append((label, spec, name))
        if hypr.available():
            if not wanted:
                return []
            failed = self._binds.apply(wanted, app_paths.command_args("")[:-1])     # "... --command" + name
            return [f"{label}: Hyprland didn't accept {spec}." for label, spec, _n in wanted if label in failed]
        if portal_keys.available():
            shared = portal()
            shared.on_press.update({name: self.callbacks[name] for _label, _spec, name in wanted})
            shared.set_group("hotkeys", [(name, label, spec) for label, spec, name in wanted])
            return []
        if not wanted:
            return []
        return [portal_keys.MISSING + " " + setup_hint(wanted)]

    def unregister(self) -> None:
        self._binds.clear()
        if _PORTAL["portal"] is not None:
            _PORTAL["portal"].set_group("hotkeys", [])

    def nativeEventFilter(self, event_type, message):
        return False, 0


class HoldKeyHook:
    """Hold-to-talk: on Hyprland a press bind (hold-start) and a release bind (hold-stop); through the portal,
    its Activated and Deactivated signals."""

    def __init__(self, on_press, on_release):
        self.on_press, self.on_release = on_press, on_release
        self._binds = hypr.Binds()
        self._spec = ""

    @property
    def installed(self) -> bool:
        return bool(self._spec)

    def set_combo(self, spec: str) -> str | None:
        self.uninstall()
        spec = str(spec or "").strip()
        if not spec:
            return None
        if hypr.available():
            if self._binds.apply([("Hold to talk", spec, "hold:hold")], app_paths.command_args("")[:-1]):
                return f"Hold to talk: Hyprland didn't accept {spec}."
        elif portal_keys.available():
            shared = portal()
            shared.on_press["hold"] = self.on_press
            shared.on_release["hold"] = self.on_release
            shared.set_group("hold", [("hold", "Hold to talk", spec)])
        else:
            return (f"Hold to talk ({spec}): on this desktop, bind the key's press to "
                    f"{command_line('hold-start')} and its release to {command_line('hold-stop')}.")
        self._spec = spec
        return None

    def uninstall(self) -> None:
        self._binds.clear()
        if self._spec and _PORTAL["portal"] is not None:
            _PORTAL["portal"].set_group("hold", [])
        self._spec = ""


class CopilotKeyHook:
    """The Copilot key is a Windows thing; on Linux any key can simply be bound to `--command talk`."""

    def __init__(self, callback):
        self.callback = callback

    @property
    def installed(self) -> bool:
        return False

    def install(self) -> bool:
        return False

    def uninstall(self) -> None:
        pass


class KeyCapture:
    """Wayland has no global keyboard hook: the quick box takes focus instead, even over fullscreen apps."""

    def __init__(self, on_key):
        self.on_key = on_key

    @property
    def installed(self) -> bool:
        return False

    def install(self) -> bool:
        return False

    def uninstall(self) -> None:
        pass
