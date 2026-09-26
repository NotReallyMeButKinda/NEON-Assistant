"""
browser_bridge.py -- the link between NEON and its browser extension (browser_extension/, for Zen and
Firefox).

The extension can't be spoken to directly, so it calls us: it keeps one long-poll open on a tiny HTTP
server bound to 127.0.0.1 ("anything for me?"), and NEON answers that poll with a request ("send me the
page you're on"). The extension runs it and posts the result back. `request()` hides all of this behind
one blocking call.

Who may talk to the server: every request must carry the token (generated once, kept in Windows
Credential Manager, pasted into the extension's options page once), and a request from a web page is
refused outright (web pages send an http(s) Origin; the extension sends moz-extension:// in Firefox / Zen,
chrome-extension:// in Chrome, Edge and Brave). Nothing is
reachable from other machines. Page text lives in memory only for as long as it takes to answer.
"""

from __future__ import annotations

import hmac
import itertools
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import neon_log
import secrets_store

log = neon_log.get("browser")

TOKEN_SECRET = "browser_token"
POLL_SECONDS = 20.0            # how long one long-poll is held open with nothing to send
CONNECTED_GRACE = POLL_SECONDS + 8.0     # no poll for this long = the extension is gone
MAX_BODY = 2_000_000           # a page's text is capped by the extension well below this


class BrowserUnavailable(Exception):
    """The extension isn't connected (browser closed, extension not installed, or the bridge is off)."""


class BrowserError(Exception):
    """The extension answered, but couldn't do it (e.g. about: pages can't be read)."""


def token(create: bool = True) -> str:
    """The shared secret the extension must send. Created on first use."""
    value = secrets_store.get_secret(TOKEN_SECRET) or ""
    if not value and create:
        value = secrets.token_urlsafe(24)
        secrets_store.set_secret(TOKEN_SECRET, value)
    return value


def new_token() -> str:
    """Replace the token (the extension will need the new one pasted in)."""
    value = secrets.token_urlsafe(24)
    secrets_store.set_secret(TOKEN_SECRET, value)
    if _STATE["server"] is not None:
        _STATE["token"] = value
    return value


_STATE = {"server": None, "thread": None, "token": "", "last_poll": 0.0, "browser": ""}
_OUTBOX: "queue.Queue[dict]" = queue.Queue()
_WAITING: dict[int, dict] = {}          # request id -> {"event", "reply"}
_LOCK = threading.Lock()
_IDS = itertools.count(1)


def connected() -> bool:
    return _STATE["server"] is not None and time.monotonic() - _STATE["last_poll"] < CONNECTED_GRACE


def browser_name() -> str:
    return _STATE["browser"] or "your browser"


EXTENSION_ORIGINS = ("moz-extension://", "chrome-extension://")     # Firefox / Zen; Chrome, Edge, Brave


def _is_extension(origin: str) -> bool:
    return str(origin).startswith(EXTENSION_ORIGINS)


def request(op: str, timeout: float = 6.0, **args):
    """Ask the extension to do `op` and wait for its answer (the `data` it sends back).
    Raises BrowserUnavailable if it isn't connected or doesn't answer in time, BrowserError if it
    answered with an error."""
    if not connected():
        raise BrowserUnavailable("the browser extension isn't connected")
    rid = next(_IDS)
    slot = {"event": threading.Event(), "reply": None}
    with _LOCK:
        _WAITING[rid] = slot
    _OUTBOX.put({"id": rid, "op": op, "args": args, "at": time.monotonic()})
    try:
        if not slot["event"].wait(timeout):
            raise BrowserUnavailable("the browser didn't answer")
    finally:
        with _LOCK:
            _WAITING.pop(rid, None)
    reply = slot["reply"] or {}
    if not reply.get("ok"):
        raise BrowserError(str(reply.get("error") or "the browser couldn't do that"))
    return reply.get("data")


def _deliver(reply: dict) -> bool:
    try:
        rid = int(reply.get("id"))
    except (TypeError, ValueError):
        return False
    with _LOCK:
        slot = _WAITING.get(rid)
    if slot is None:
        return False                   # answered too late; the asker has given up
    slot["reply"] = reply
    slot["event"].set()
    return True


class _Handler(BaseHTTPRequestHandler):
    server_version = "NeonBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:       # the default writes every request to stderr
        pass

    # ---- who's asking ----------------------------------------------------------------------
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        return not origin or _is_extension(origin) or origin == "null"

    def _authorised(self) -> bool:
        sent = self.headers.get("X-Neon-Token", "")
        expected = _STATE["token"]
        return bool(expected) and self._origin_ok() and hmac.compare_digest(sent.encode(), expected.encode())

    def _send(self, status: int, body: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if _is_extension(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_OPTIONS(self) -> None:       # CORS preflight: only ever granted to the extension
        if not _is_extension(self.headers.get("Origin", "")):
            self._send(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Neon-Token, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorised():
            self._send(403, {"error": "wrong token"})
            return
        _STATE["last_poll"] = time.monotonic()
        _STATE["browser"] = self.headers.get("X-Neon-Browser", "")[:40]
        if self.path.startswith("/hello"):
            self._send(200, {"ok": True, "app": "NEON"})
            return
        if not self.path.startswith("/poll"):
            self._send(404, {"error": "no such path"})
            return
        deadline = time.monotonic() + POLL_SECONDS
        while True:
            try:
                item = _OUTBOX.get(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            except queue.Empty:
                _STATE["last_poll"] = time.monotonic()
                if time.monotonic() >= deadline:
                    self._send(204)
                    return
                continue
            if time.monotonic() - item["at"] > 10:          # nobody is waiting for this any more
                continue
            _STATE["last_poll"] = time.monotonic()
            self._send(200, {"id": item["id"], "op": item["op"], "args": item["args"]})
            return

    def do_POST(self) -> None:
        if not self._authorised():
            self._send(403, {"error": "wrong token"})
            return
        _STATE["last_poll"] = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(413 if length > MAX_BODY else 400, {"error": "bad body"})
            return
        try:
            reply = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "not json"})
            return
        if self.path.startswith("/reply") and isinstance(reply, dict):
            self._send(200 if _deliver(reply) else 410, {"ok": True})
            return
        self._send(404, {"error": "no such path"})


def start(port: int) -> bool:
    """Start listening on 127.0.0.1:`port` (idempotent). False if the port is taken."""
    if _STATE["server"] is not None:
        return True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), _Handler)
    except OSError as exc:
        log.warning("browser bridge couldn't listen on port %s: %s", port, exc)
        return False
    server.daemon_threads = True
    _STATE["token"] = token()
    _STATE["server"] = server
    thread = threading.Thread(target=server.serve_forever, name="browser-bridge", daemon=True)
    _STATE["thread"] = thread
    thread.start()
    log.info("browser bridge listening on 127.0.0.1:%s", port)
    return True


def stop() -> None:
    server = _STATE["server"]
    if server is None:
        return
    _STATE["server"] = None
    _STATE["last_poll"] = 0.0
    try:
        server.shutdown()
        server.server_close()
    except OSError:
        pass
