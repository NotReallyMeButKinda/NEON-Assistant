"""
linuxdesk/portal_keys.py -- global hotkeys through the XDG desktop portal (org.freedesktop.portal.GlobalShortcuts).

KDE Plasma (and GNOME 48+) let an app ask for system-wide shortcuts this way: NEON names each action and the
keys it would like; the first time, Plasma shows a dialog to confirm them, and afterwards they're listed under
System Settings > Keyboard > Shortcuts > NEON Assistant, where they can be changed. The portal reports both the
press and the release, so hold-to-talk works too.

The portal ties the shortcuts to NEON's own D-Bus connection, so this keeps one open (with jeepney, a small
pure-Python D-Bus library) in a background thread for as long as the shortcuts should work.
"""

from __future__ import annotations

import os
import secrets
import threading

APP_ID = "neon-assistant"
DESKTOP = "/org/freedesktop/portal/desktop"
PORTAL = "org.freedesktop.portal.Desktop"
IFACE = "org.freedesktop.portal.GlobalShortcuts"

_MODS = {"ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT", "meta": "LOGO", "win": "LOGO",
         "super": "LOGO"}
_KEYS = {"space": "space", "return": "Return", "enter": "Return", "esc": "Escape", "escape": "Escape",
         "tab": "Tab", "backspace": "BackSpace", "del": "Delete", "delete": "Delete", "ins": "Insert",
         "insert": "Insert", "home": "Home", "end": "End", "pgup": "Page_Up", "pgdown": "Page_Down", "up": "Up",
         "down": "Down", "left": "Left", "right": "Right", "print": "Print", "pause": "Pause", "`": "grave",
         "-": "minus", "=": "equal", "[": "bracketleft", "]": "bracketright", ";": "semicolon",
         "'": "apostrophe", ",": "comma", ".": "period", "/": "slash", "\\": "backslash", "+": "plus"}


def trigger(qt_spec: str) -> str:
    """A Qt key sequence ('Ctrl+Alt+N', 'Meta+Space') -> the portal's trigger format ('CTRL+ALT+n',
    'LOGO+space'); '' if it can't be expressed."""
    spec = str(qt_spec or "").replace(" ", "")
    if not spec:
        return ""
    parts = spec.split("+")
    if spec.endswith("++"):
        parts = parts[:-2] + ["+"]
    *mods, key = parts
    names = []
    for mod in mods:
        name = _MODS.get(mod.lower())
        if name is None:
            return ""
        names.append(name)
    lowered = key.lower()
    if lowered in _KEYS:
        key = _KEYS[lowered]
    elif len(key) == 1:
        key = key.lower()
    elif lowered.startswith("f") and lowered[1:].isdigit():
        key = key.upper()
    else:
        return ""
    return "+".join(names + [key])


def available() -> bool:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return True


MISSING = ("For hotkeys on this desktop I need the jeepney Python package (Arch: python-jeepney, or "
           "pip install jeepney).")


def sender_token(unique_name: str) -> str:
    """':1.42' -> '1_42', the part of a portal Request path that names the caller."""
    return unique_name.lstrip(":").replace(".", "_")


def _value(variant):
    """jeepney gives a{sv} values as (signature, value) pairs."""
    return variant[1] if isinstance(variant, tuple) and len(variant) == 2 else variant


class PortalShortcuts:
    """One portal session holding NEON's shortcuts. bind() replaces the whole set (a new session); the
    callbacks run in the background thread, so the caller hands them to the UI thread itself."""

    def __init__(self, on_press, on_release, on_status=None):
        self._on_press, self._on_release = on_press, on_release
        self._on_status = on_status or (lambda text: None)
        self._conn = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.assigned: dict[str, str] = {}          # shortcut id -> the keys the desktop actually gave it

    def bind(self, shortcuts: list[tuple[str, str, str]]) -> None:
        """shortcuts: (id, description, qt_spec). An empty list just closes the session."""
        self.close()
        wanted = [(sid, desc, trigger(spec)) for sid, desc, spec in shortcuts if spec]
        if not wanted:
            return
        thread = threading.Thread(target=self._run, args=(wanted,), name="Nova-PortalKeys", daemon=True)
        self._thread = thread
        thread.start()

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()                            # ends the session; the thread's receive() fails and it exits
            except OSError:
                pass

    # ---- the background thread ------------------------------------------------------
    def _request(self, conn, method: str, signature: str, body: tuple, token: str, timeout: float):
        """Call a portal method that answers through a Request object's Response signal."""
        from jeepney import DBusAddress, MatchRule, new_method_call
        from jeepney.bus_messages import message_bus
        path = f"{DESKTOP}/request/{sender_token(conn.unique_name)}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5)
        with conn.filter(rule) as queue:
            reply = conn.send_and_get_reply(new_method_call(DBusAddress(DESKTOP, bus_name=PORTAL, interface=IFACE),
                                                            method, signature, body), timeout=10)
            if reply.header.message_type.name == "error":
                raise RuntimeError(f"{method}: {reply.body[0] if reply.body else 'refused'}")
            response = conn.recv_until_filtered(queue, timeout=timeout)
        code, results = response.body
        if code != 0:
            raise RuntimeError(f"{method}: {'cancelled' if code == 1 else 'failed'}")
        return results

    def _run(self, wanted: list[tuple[str, str, str]]) -> None:
        try:
            from jeepney import DBusAddress, MatchRule, new_method_call
            from jeepney.bus_messages import message_bus
            from jeepney.io.blocking import open_dbus_connection
            conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # noqa: BLE001 -- no session bus, no jeepney
            self._on_status(f"Hotkeys: I couldn't reach the desktop's shortcut service ({exc}).")
            return
        with self._lock:
            self._conn = conn
        try:
            # Tell the portal who we are (host apps otherwise have no app id). Older portals don't have this.
            try:
                conn.send_and_get_reply(new_method_call(
                    DBusAddress(DESKTOP, bus_name=PORTAL, interface="org.freedesktop.host.portal.Registry"),
                    "Register", "sa{sv}", (APP_ID, {})), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            session_token = "neon" + secrets.token_hex(4)
            token = "neon" + secrets.token_hex(4)       # the Request object's path ends in this handle token
            results = self._request(conn, "CreateSession", "a{sv}",
                                    ({"handle_token": ("s", token), "session_handle_token": ("s", session_token)},),
                                    token, 30)
            session = str(_value(results.get("session_handle", ("s", ""))))
            token = "neon" + secrets.token_hex(4)
            shortcuts = [(sid, {"description": ("s", desc), **({"preferred_trigger": ("s", trig)} if trig else {})})
                         for sid, desc, trig in wanted]
            results = self._request(conn, "BindShortcuts", "oa(sa{sv})sa{sv}",
                                    (session, shortcuts, "", {"handle_token": ("s", token)}), token, 600)
            for sid, props in _value(results.get("shortcuts", ("a(sa{sv})", []))) or []:
                self.assigned[sid] = str(_value(props.get("trigger_description", ("s", ""))))
            for signal in ("Activated", "Deactivated"):
                conn.send_and_get_reply(message_bus.AddMatch(
                    MatchRule(type="signal", interface=IFACE, member=signal, path=DESKTOP)), timeout=5)
            rule = MatchRule(type="signal", interface=IFACE, path=DESKTOP)
            with conn.filter(rule, bufsize=64) as queue:
                while True:
                    msg = conn.recv_until_filtered(queue)
                    body = msg.body
                    if len(body) < 2 or str(body[0]) != session:
                        continue
                    member = _member(msg)
                    if member == "Activated":
                        self._on_press(str(body[1]))
                    elif member == "Deactivated":
                        self._on_release(str(body[1]))
        except Exception as exc:  # noqa: BLE001 -- closed on purpose, or the portal said no
            with self._lock:
                closed = self._conn is not conn
            if not closed:
                self._on_status(f"Hotkeys: the desktop didn't set them up ({exc}).")
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _member(msg) -> str:
    from jeepney.low_level import HeaderFields
    return str(msg.header.fields.get(HeaderFields.member, ""))
