"""
panel_feed.py -- what the status bar shows, for a desktop panel widget (the KDE Plasma widget in plasmoid/).

A small HTTP server on 127.0.0.1 that a panel widget can follow and drive:

  GET  /status?since=N   the current state as JSON: listening / thinking / speaking, the latest caption and a
                         few lines before it, mute / wake word / dictation, the assistant's name and the theme's
                         colours. With `since`, the answer waits (up to 20 s) until something newer than N
                         happens, so the widget updates at once without polling hard.
  POST /command          body `talk` (or quick, mute, wake, window, dictation, stop, show, settings, board...):
                         the same commands as `main.py --command`.

Only this PC can reach it. Web pages in a browser can't drive it either: commands need an `X-Neon-Widget`
header, which a page can't send to another origin without a CORS preflight this server never approves, and
the Host header must name 127.0.0.1 / localhost (against DNS rebinding).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from linuxdesk.single import COMMANDS

HISTORY = 8                 # caption lines kept for the widget's pop-up
WAIT_SECONDS = 20.0         # a long poll's longest wait
LEVEL_STEP = 0.1            # the voice level is sent in steps this big...
LEVEL_INTERVAL = 0.08       # ...at most this often (it changes ~30 times a second while speaking)


class Feed:
    """The shared state, and a sequence number that goes up on every change (thread-safe)."""

    def __init__(self):
        self._cond = threading.Condition()
        self.seq = 0
        self.data: dict = {"name": "Nova", "wake_phrase": "hey nova", "state": "idle", "sender": "", "text": "", "history": [], "level": 0.0,
                           "muted": False, "wake": False, "dictating": False, "colors": {}}
        self._level_at = 0.0
        self.closing = False                          # the server is stopping: waiting polls answer now

    def update(self, **fields) -> None:
        with self._cond:
            changed = {k: v for k, v in fields.items() if self.data.get(k) != v}
            if not changed:
                return
            self.data.update(changed)
            self.seq += 1
            self._cond.notify_all()

    def caption(self, sender: str, text: str) -> None:
        """A caption line; a reply streaming in grows its own line instead of adding one per piece."""
        sender, text = str(sender), " ".join(str(text).split())
        with self._cond:
            history = list(self.data["history"])
            last = history[-1] if history else None
            if last and last["sender"] == sender and (text.startswith(last["text"]) or last["text"].startswith(text)):
                history[-1] = {"sender": sender, "text": text}
            else:
                history.append({"sender": sender, "text": text})
            self.data.update(sender=sender, text=text, history=history[-HISTORY:])
            self.seq += 1
            self._cond.notify_all()

    def level(self, value: float) -> None:
        stepped = round(max(0.0, min(1.0, float(value))) / LEVEL_STEP) * LEVEL_STEP
        now = time.monotonic()
        if stepped != self.data["level"] and (now - self._level_at >= LEVEL_INTERVAL or stepped == 0.0):
            self._level_at = now
            self.update(level=round(stepped, 2))

    def snapshot(self, since: int | None = None, wait: float = WAIT_SECONDS) -> dict:
        with self._cond:
            if since is not None and since >= self.seq:
                self._cond.wait_for(lambda: self.seq > since or self.closing, timeout=wait)
            return dict(self.data, seq=self.seq)

    def release(self, closing: bool = True) -> None:
        """Answer every waiting poll now (the server is stopping), or let polls wait again."""
        with self._cond:
            self.closing = closing
            self._cond.notify_all()


class _Handler(BaseHTTPRequestHandler):
    server_version = "NeonPanel/1"
    feed: Feed
    on_command = staticmethod(lambda name: None)
    port = 0

    def log_message(self, *_args) -> None:           # no console noise per request
        pass

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body or {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 -- the http.server naming
        url = urlparse(self.path)
        if not self._host_ok() or url.path != "/status":
            self._send(404, {"error": "not here"})
            return
        since = parse_qs(url.query).get("since", [""])[0]
        self._send(200, self.feed.snapshot(int(since) if since.lstrip("-").isdigit() else None))

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or urlparse(self.path).path != "/command" or not self.headers.get("X-Neon-Widget"):
            self._send(403, {"error": "not allowed"})
            return
        length = min(int(self.headers.get("Content-Length") or 0), 200)
        name = self.rfile.read(length).decode("utf-8", "replace").strip().lower()
        if name not in COMMANDS:
            self._send(400, {"error": f"unknown command {name!r}"})
            return
        self.on_command(name)
        self._send(200, {"ok": True})

    def do_OPTIONS(self) -> None:  # noqa: N802 -- a browser's preflight: never approved
        self._send(403, {"error": "not allowed"})


class PanelServer:
    """The HTTP server in a background thread. on_command(name) is called from that thread."""

    def __init__(self, feed: Feed, on_command):
        self.feed = feed
        self._on_command = on_command
        self._server: ThreadingHTTPServer | None = None
        self.on_stop = lambda: None                   # connect() unhooks the controller's signals here

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def start(self, port: int) -> bool:
        handler = type("Handler", (_Handler,), {"feed": self.feed, "on_command": staticmethod(self._on_command),
                                                "port": int(port)})
        try:
            server = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
        except OSError:
            return False
        server.daemon_threads = True
        self.feed.release(False)
        handler.port = server.server_address[1]
        self._server = server
        threading.Thread(target=server.serve_forever, name="Nova-PanelFeed", daemon=True).start()
        return True

    def stop(self) -> None:
        self.on_stop()
        server, self._server = self._server, None
        if server is not None:
            self.feed.release()
            server.shutdown()
            server.server_close()


def connect(controller, on_command, port: int):
    """Follow the controller's signals and serve them on `port`. Returns the (started) PanelServer, or None if
    the port is taken. on_command(name) runs on the UI thread."""
    from PySide6.QtCore import QObject, Signal

    import assistant as backend
    from ui import theme
    from ui.theme import signals as theme_signals

    class _Relay(QObject):
        command = Signal(str)

    feed = Feed()
    relay = _Relay()
    relay.command.connect(on_command)                 # queued: the server thread hands it to the UI thread

    def colors() -> None:
        c = dict(theme.STATE_COLORS)
        c.update(accent=theme.COLORS.get("accent", ""), text=theme.COLORS.get("text", ""))
        feed.update(colors=c)

    def names() -> None:
        feed.update(name=backend.assistant_name(), wake_phrase=str(backend.CONFIG.get("wake_word") or "hey nova"))

    names()
    feed.update(muted=bool(controller.muted), wake=bool(controller.wake_on), dictating=bool(controller.dictating))
    colors()
    links = [(controller.caption, feed.caption),
             (controller.state, lambda state: feed.update(state=str(state))),
             (controller.audio_level, feed.level),
             (controller.muted_changed, lambda on: feed.update(muted=bool(on))),
             (controller.wake_changed, lambda on: feed.update(wake=bool(on))),
             (controller.dictation_changed, lambda on: feed.update(dictating=bool(on))),
             (controller.name_changed, lambda _name: names()),
             (controller.settings_saved, names),
             (theme_signals.changed, colors)]
    for signal, slot in links:
        signal.connect(slot)

    def detach() -> None:
        for signal, slot in links:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    server = PanelServer(feed, relay.command.emit)
    server.relay = relay                              # keep it alive with the server
    server.on_stop = detach
    if server.start(port):
        return server
    detach()
    return None
