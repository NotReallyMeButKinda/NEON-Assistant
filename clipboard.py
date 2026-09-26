"""
clipboard.py -- a short memory of what you have copied, and "copy that" for replies.

A background thread watches Windows' clipboard sequence number (a counter the OS bumps on every
change, so this is a single cheap call and not a full clipboard read) and keeps the last few pieces
of text. Nothing is written to disk: the history dies with the process, which is the right default
for something that routinely holds passwords.

    "what did I copy"                 the most recent entry
    "what did I copy before that"     the one before it
    "read my clipboard history"       the last few, numbered
    "copy that"                       puts the assistant's last reply on the clipboard
    "clear my clipboard history"      forget them all

Entries the history refuses to keep: anything longer than MAX_CHARS (a copied file, a whole page)
is truncated, and `PRIVATE` patterns (anything that looks like a password manager's payload) are
kept as a placeholder so "read my clipboard history" can't say them out loud.
"""

from __future__ import annotations

import re
import threading
import time

import neon_log
import selection

log = neon_log.get("clipboard")

MAX_ENTRIES = 25
MAX_CHARS = 2000
POLL_SECONDS = 0.8
_SPEAK_MAX = 300            # how much of one entry is read aloud

# Kept, but never spoken: the history is a convenience, not a way to shout your secrets.
PRIVATE = re.compile(
    r"^(?:[A-Za-z0-9+/=_\-]{24,}|(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s])\S{10,})$")

_LOCK = threading.RLock()
_ENTRIES: list[dict] = []           # {"text", "at"}; newest last
_REPLY = {"text": ""}               # the assistant's most recent reply, for "copy that"


class Watcher:
    """Polls the clipboard and records text changes. Start it once; stop it on shutdown."""

    def __init__(self, poll: float = POLL_SECONDS):
        self._poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sequence = -1

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="Nova-Clipboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _loop(self) -> None:
        self._last_sequence = selection.sequence_number()
        while not self._stop.wait(self._poll):
            try:
                sequence = selection.sequence_number()
                # Windows returns 0 when this process can't read the clipboard's counter (a locked
                # workstation, a restricted window station). Reading the text itself still works,
                # so fall back to that rather than going quietly deaf.
                if sequence and sequence == self._last_sequence:
                    continue
                self._last_sequence = sequence
                text = selection.read_text()
                if text.strip():
                    record(text)
            except Exception as exc:  # noqa: BLE001 -- a clipboard hiccup must not kill the watcher
                log.warning("clipboard poll failed: %s", exc)


# ---------------------------------------------------------------------------
# The history
# ---------------------------------------------------------------------------

_SECRETS: set[str] = set()          # text put on the clipboard by us that must never enter the history


def copy_secret(text: str, clear_after: float = 30.0) -> bool:
    """Put a secret (a password from Bitwarden) on the clipboard without it ever entering the history,
    and take it off again after `clear_after` seconds -- if it's still what's there."""
    value = str(text)
    with _LOCK:
        _SECRETS.add(value.strip())
    if not selection.set_text(value):
        return False

    def wipe() -> None:
        try:
            if selection.read_text() == value:
                selection.set_text("")
        finally:
            with _LOCK:
                _SECRETS.discard(value.strip())
    timer = threading.Timer(max(1.0, clear_after), wipe)
    timer.daemon = True
    timer.start()
    return True


def record(text: str) -> None:
    """Add one clipboard entry (skipping an exact repeat of the newest one, and our own secrets)."""
    text = str(text)[:MAX_CHARS].strip()
    if not text:
        return
    with _LOCK:
        if text in _SECRETS:
            return
        if _ENTRIES and _ENTRIES[-1]["text"] == text:
            return
        _ENTRIES[:] = [e for e in _ENTRIES if e["text"] != text]
        _ENTRIES.append({"text": text, "at": time.time()})
        del _ENTRIES[:-MAX_ENTRIES]


def entries() -> list[dict]:
    with _LOCK:
        return [dict(e) for e in _ENTRIES]


def clear() -> str:
    with _LOCK:
        count = len(_ENTRIES)
        _ENTRIES.clear()
    return f"Cleared {count} clipboard entr{'ies' if count != 1 else 'y'}." if count \
        else "There was nothing in the clipboard history."


def note_reply(text: str) -> None:
    """The controller calls this after every answer, so "copy that" has something to copy."""
    _REPLY["text"] = str(text or "").strip()


def spoken(text: str) -> str:
    """One entry, safe and short enough to read aloud."""
    flat = " ".join(str(text).split())
    if PRIVATE.match(flat):
        return f"something {len(flat)} characters long that looks like a password or a token"
    return flat if len(flat) <= _SPEAK_MAX else flat[:_SPEAK_MAX].rsplit(" ", 1)[0] + "..."


# ---------------------------------------------------------------------------
# Understanding what was said
# ---------------------------------------------------------------------------

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you)\s+)*"
_ORDINAL = {"last": 1, "latest": 1, "first": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}

_WHAT = re.compile(
    rf"^{_LEAD}what(?:'s| is| was| did i)?\s*(?:i\s+)?(?:copy|copied|cut|on (?:the|my) clipboard|"
    r"in (?:the|my) clipboard)(?:\s+(?:just now|last|recently))?"
    r"(?P<back>(?:\s+(?:before|prior to)\s+(?:that|this|it))+)?[?.!]?$")
_NTH = re.compile(
    rf"^{_LEAD}what(?:'s| is| was)?\s+the\s+(?P<which>{'|'.join(_ORDINAL)})"
    r"(?:[\s-]to[\s-]last)?\s+thing\s+i\s+copied[?.!]?$")
_HISTORY = re.compile(
    rf"^{_LEAD}(?:read|show|list|what(?:'s| is) in)\s+(?:me\s+)?(?:my|the)?\s*clipboard"
    r"(?:\s+history)?[?.!]?$")
_CLEAR = re.compile(
    rf"^{_LEAD}(?:clear|wipe|forget|empty|delete)\s+(?:my|the)?\s*clipboard(?:\s+history)?[?.!]?$")
_COPY_THAT = re.compile(
    rf"^{_LEAD}copy\s+(?:that|this|it|the (?:answer|reply|last (?:answer|reply)))"
    r"(?:\s+to\s+(?:my|the)\s+clipboard)?[?.!]?$"
    rf"|^{_LEAD}put\s+(?:that|this|it)\s+(?:on|in)\s+(?:my|the)\s+clipboard[?.!]?$")


def handle_clipboard_command(text: str) -> str | None:
    """The reply for a clipboard command, or None if `text` wasn't one."""
    t = " ".join(str(text).lower().split())
    if not t:
        return None

    if _CLEAR.match(t):
        return clear()

    if _COPY_THAT.match(t):
        reply = _REPLY["text"]
        if not reply:
            return "I haven't said anything worth copying yet."
        if selection.set_text(reply):
            record(reply)
            return "Copied to your clipboard."
        return "I couldn't get to the clipboard just now."

    if _HISTORY.match(t):
        items = entries()
        if not items:
            return "I haven't seen you copy anything yet."
        lines = [f"{i}. {spoken(e['text'])}" for i, e in enumerate(reversed(items[-5:]), start=1)]
        return "Most recent first: " + " ".join(lines)

    m = _NTH.match(t)
    if m:
        return _nth(_ORDINAL[m.group("which")])

    m = _WHAT.match(t)
    if m:
        steps = len(re.findall(r"before|prior to", m.group("back") or "")) + 1
        return _nth(steps)
    return None


def _nth(position: int) -> str:
    """`position` counts back from the newest (1 = the last thing copied)."""
    items = entries()
    if not items:
        return "I haven't seen you copy anything yet."
    if position > len(items):
        return f"I've only kept the last {len(items)} thing{'s' if len(items) != 1 else ''} you copied."
    entry = items[-position]
    when = _ago(time.time() - entry["at"])
    lead = "You copied" if position == 1 else f"The one {position - 1} before that was"
    return f"{lead}: {spoken(entry['text'])} ({when})."


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(seconds // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} ago"
