"""
linuxdesk/notify.py -- reading other apps' notifications on Linux, and do-not-disturb.

Every app sends notifications to the desktop's notification daemon (mako, swaync or dunst on Hyprland;
Plasma's own on KDE) as a `Notify` call on the session D-Bus. `busctl --user monitor` (systemd, always on
Arch) shows those calls as JSON, so NOTIFY_WATCHER reads them as they pass: the app, the title, the text,
and the `desktop-entry` hint that says which app to open. The daemon's reply carries the notification's
id, which "dismiss" uses to close it again.

Same interface as notifications.NotificationWatcher on Windows (start / stop / remove / expect / ready).
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import threading
import time

import osinfo

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
_TAGS = re.compile(r"<[^>]+>")


def clean_markup(text: str) -> str:
    """Notification bodies may use a little HTML (<b>, <a href>, &amp;): plain text for reading aloud."""
    return " ".join(html.unescape(_TAGS.sub(" ", str(text or ""))).split())


def parse_call(message: dict) -> dict | None:
    """One Notify method call from busctl's JSON -> {"cookie", "app", "title", "body", "entry", "replaces"}."""
    if message.get("type") != "method_call" or message.get("member") != "Notify":
        return None
    data = ((message.get("payload") or {}).get("data")) or []
    if len(data) < 5:
        return None
    hints = data[6] if len(data) > 6 and isinstance(data[6], dict) else {}
    entry = hints.get("desktop-entry") or {}
    entry = entry.get("data", "") if isinstance(entry, dict) else str(entry)
    return {"cookie": message.get("cookie"), "app": str(data[0] or "").strip(), "replaces": int(data[1] or 0),
            "title": clean_markup(data[3]), "body": clean_markup(data[4]), "entry": str(entry or "").strip()}


def parse_reply(message: dict) -> tuple[int, int] | None:
    """(reply_cookie, notification id) for the daemon's answer to a Notify call."""
    if message.get("type") != "method_return":
        return None
    data = ((message.get("payload") or {}).get("data")) or []
    if not data or not isinstance(data[0], int):
        return None
    return message.get("reply_cookie"), int(data[0])


class NotificationWatcher:
    """Calls on_notification(Notification) for every new notification (from its own thread)."""

    def __init__(self, on_notification, on_status=None, ignore=lambda: (), on_backlog=None):
        self._on_notification = on_notification
        self._on_status = on_status or (lambda message: None)
        self._ignore = ignore
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: dict = {}                      # cookie -> parsed call, until its id arrives
        self._expect: dict[str, threading.Event] = {}
        self.ready = threading.Event()
        self.access = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if not osinfo.which("busctl"):
            self.access = "Denied"
            self._on_status("I can't read notifications here: busctl (systemd) isn't available.")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Nova-Notifications", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.ready.clear()

    def remove(self, notification_id: int) -> None:
        if int(notification_id) <= 0:
            return
        osinfo.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                    "--object-path", "/org/freedesktop/Notifications",
                    "--method", "org.freedesktop.Notifications.CloseNotification", str(int(notification_id))],
                   timeout=3)

    def expect(self, title: str) -> threading.Event:
        event = threading.Event()
        self._expect[title] = event
        return event

    # ---- internals -------------------------------------------------------------------
    def handle(self, message: dict) -> None:
        """One JSON message from busctl (public so tests can feed them)."""
        call = parse_call(message)
        if call is not None:
            self._pending[call["cookie"]] = (call, time.monotonic())
            return
        reply = parse_reply(message)
        if reply is not None:
            cookie, notification_id = reply
            found = self._pending.pop(cookie, None)
            if found is not None:
                self._deliver(found[0], notification_id)
        # a call whose reply never came (another monitor ate it): deliver it anyway after a moment
        now = time.monotonic()
        for cookie, (call, at) in list(self._pending.items()):
            if now - at > 1.0:
                self._pending.pop(cookie, None)
                self._deliver(call, 0)

    def _deliver(self, call: dict, notification_id: int) -> None:
        from notifications import Notification
        if not (call["title"] or call["body"]):
            return
        title = call["title"] or call["body"]
        body = call["body"] if call["title"] else ""
        note = Notification(id=notification_id, app=call["app"] or "an app", title=title, body=body,
                            created=time.time(), aumid=call["entry"])
        event = self._expect.pop(note.title, None)
        if event is not None:
            event.set()
            self.remove(note.id)
            return
        if any(part and part in note.app.lower() for part in self._ignore()):
            return
        self._on_notification(note)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    ["busctl", "--user", "monitor", "--json=short", "--match", MATCH,
                     "--match", "type='method_return'"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
                self.access = "Allowed"
                self.ready.set()
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        return
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        self.handle(message)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    self._on_status(f"Couldn't start the notification watcher: {exc}")
            finally:
                proc, self._proc = self._proc, None
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if self._stop.is_set():
                return
            failures = failures + 1 if time.monotonic() - started < 20 else 0
            if failures >= 3:
                self._on_status("The notification watcher keeps stopping, so I gave up on it. Reading "
                                "notifications needs permission to monitor the session bus.")
                return
            self._stop.wait(3.0)


# ---------------------------------------------------------------------------
# Do-not-disturb ("hide the pop-ups while NEON reads them")
# ---------------------------------------------------------------------------

def daemon() -> str:
    """'swaync' | 'dunst' | 'mako' | 'kde' | '' -- which notification daemon is running."""
    if osinfo.desktop() == "kde":
        return "kde"
    names = set()
    try:
        import linuxdesk.system as system
        names = system.process_names()
    except OSError:
        pass
    for name in ("swaync", "dunst", "mako"):
        if name in names:
            return name
    return ""


_KDE_INHIBIT: dict = {"conn": None, "cookie": 0}


def _kde_quiet(on: bool) -> bool:
    """Plasma's own "do not disturb while presenting": org.freedesktop.Notifications.Inhibit. It lasts as long
    as the D-Bus connection that asked, so the connection is kept open until the pop-ups come back."""
    conn = _KDE_INHIBIT["conn"]
    if not on:
        if conn is None:
            return True
        _KDE_INHIBIT["conn"] = None
        try:
            conn.close()                               # Plasma lifts the inhibition when its caller leaves
        except OSError:
            pass
        return True
    if conn is not None:
        return True
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        conn = open_dbus_connection(bus="SESSION")
        reply = conn.send_and_get_reply(new_method_call(
            DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                        interface="org.freedesktop.Notifications"),
            "Inhibit", "ssa{sv}", ("neon-assistant", "NEON is reading notifications aloud", {})), timeout=5)
    except Exception:  # noqa: BLE001 -- no jeepney, no session bus, or Plasma said no
        return False
    if reply.header.message_type.name == "error":
        conn.close()
        return False
    _KDE_INHIBIT.update(conn=conn, cookie=reply.body[0] if reply.body else 0)
    return True


def set_quiet(on: bool) -> bool:
    """Pop-ups off (still delivered, so NEON still reads them) or back on. False if this daemon can't."""
    kind = daemon()
    if kind == "kde":
        return _kde_quiet(on)
    if kind == "swaync":
        return osinfo.run(["swaync-client", "--dnd-on" if on else "--dnd-off"], timeout=3).ok
    if kind == "dunst":
        return osinfo.run(["dunstctl", "set-paused", "true" if on else "false"], timeout=3).ok
    if kind == "mako":
        return osinfo.run(["makoctl", "mode", "-a" if on else "-r", "do-not-disturb"], timeout=3).ok
    return False


def send_test(title: str, body: str = "") -> bool:
    return osinfo.which("notify-send") is not None and \
        osinfo.run(["notify-send", "--app-name=Neon", title, body], timeout=5).ok
