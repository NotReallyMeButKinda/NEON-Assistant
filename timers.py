"""
timers.py -- timers and reminders: "set a timer for 10 minutes", "remind me in an hour to call Sam".
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from mathcalc import _words_to_digits


def _spoken_duration(seconds: float) -> str:
    if seconds < 90:
        n = max(1, round(seconds))
        return f"{n} second{'s' if n != 1 else ''}"
    if seconds >= 3600:
        hours = round(seconds / 3600, 1)
        hours = int(hours) if hours == int(hours) else hours
        return "an hour" if hours == 1 else f"{hours} hours"
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


# ---------------------------------------------------------------------------
# Timers and reminders: "set a timer for 10 minutes", "remind me in an hour to call Sam"
# ---------------------------------------------------------------------------

_TIMERS: list[dict] = []                    # {"id", "label", "seconds", "ends" (monotonic), "timer"}
_LOCK = threading.RLock()                   # _TIMERS is touched from the UI thread and from timer threads
# Set by the controller: "fire" when one goes off, "warn" shortly before it does.
TIMER_HOOKS = {"fire": None, "warn": None}
WARN_SECONDS = 60.0                         # how long before the end the heads-up comes
WARN_MINIMUM = 150.0                        # ...and the shortest timer that gets one at all
_timer_ids = iter(range(1, 10**9))
_STORE = {"path": None}                    # where running timers are kept across restarts (None: not persisted)
_DURATION = re.compile(r"(\d+(?:\.\d+)?|an?|half an?)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)")
_TIMER_SET = re.compile(
    r"(?:set|start|create|begin|make)(?: me)?(?: an?| another)?(?: (?P<label>[a-z ]{1,30}?))? timer"
    r"(?: for| of)? (?P<d>.+)|timer (?:for|of) (?P<d2>.+)")
_TIMER_SET2 = re.compile(
    r"(?:set|start|create|make)(?: me)?(?: an?| another)? (?P<d>(?:\d+(?:\.\d+)?|an?|half an?)[ -]?"
    r"(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)(?: and (?:a half|\d+ (?:minutes?|seconds?)))?) (?:timer|alarm)")
_REMIND = re.compile(r"remind me (?:in|after) (?P<d>.+?) (?:to|that) (?P<what>.+)")
_TIMER_STATUS = re.compile(r"(?:how (?:much time|long)(?: is)? (?:left|remaining)(?: on (?:the|my) timers?)?"
                           r"|(?:check|how(?:'s| is)) (?:the |my )?timers?|what(?:'s| is) left on (?:the|my) timers?)")
_TIMER_CANCEL = re.compile(r"(?:cancel|stop|clear|delete|turn off)(?: all)?(?: of)?(?: the| my)? (?:timers?|reminders?)")


def duration_seconds(spoken: str) -> float:
    """'ten minutes' / '1 hour 30 minutes' / 'half an hour' / '45 seconds' -> seconds (0 if none found)."""
    d = _words_to_digits(re.sub(r"\s+", " ", spoken.lower().replace("-", " ")).strip())
    if "half an hour" in d or "half hour" in d:
        return 1800.0
    total, last_unit = 0.0, 0
    for amount, unit in _DURATION.findall(d):
        n = 1.0 if amount in ("a", "an") else (0.5 if amount.startswith("half") else float(amount))
        last_unit = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        total += n * last_unit
    if re.search(r"\band a half\b", d):           # "an hour and a half", "two minutes and a half"
        total += 0.5 * last_unit
    return total


def spoken_timer_command(text: str) -> tuple[str, object] | None:
    """('set', (seconds, label, is_reminder)) | ('status', None) | ('cancel', None) | None.
    Whole utterances only, so a sentence that merely mentions a timer isn't hijacked."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    t = re.sub(r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)+", "", t)
    t = re.sub(r"\s+please$", "", t)
    if _TIMER_CANCEL.fullmatch(t):
        return "cancel", None
    if _TIMER_STATUS.fullmatch(t):
        return "status", None
    m = _REMIND.fullmatch(t)
    if m:
        seconds = duration_seconds(m.group("d"))
        return ("set", (seconds, m.group("what").strip(), True)) if seconds > 0 else None
    m = _TIMER_SET2.fullmatch(t.replace("-", " "))
    if m:
        seconds = duration_seconds(m.group("d"))
        return ("set", (seconds, "", False)) if seconds > 0 else None
    m = _TIMER_SET.fullmatch(t)
    if m:
        seconds = duration_seconds(m.group("d") or m.group("d2") or "")
        return ("set", (seconds, (m.group("label") or "").strip(), False)) if seconds > 0 else None
    return None


def _timer_fired(timer_id: int) -> None:
    with _LOCK:
        item = next((x for x in _TIMERS if x["id"] == timer_id), None)
        if item is None:
            return
        _TIMERS.remove(item)
        _save()
    hook = TIMER_HOOKS["fire"]
    if hook is not None:
        hook(item["label"], item["seconds"], item["reminder"])


def _timer_warning(timer_id: int) -> None:
    """A heads-up shortly before a long timer ends, so it isn't a surprise."""
    with _LOCK:
        item = next((x for x in _TIMERS if x["id"] == timer_id), None)
    hook = TIMER_HOOKS["warn"]
    if item is None or hook is None:
        return
    left = max(1.0, item["ends"] - time.monotonic())
    what = f"{item['label']} " if item["label"] and not item["reminder"] else ""
    hook(f"{_spoken_duration(left)} left on your {what}timer.".replace("your  ", "your "))


def timer_announcement(label: str, seconds: float, reminder: bool) -> str:
    """What is said when a timer / reminder goes off."""
    if reminder:
        return f"Reminder: {label}."
    return f"Your {label} timer is done." if label else f"Your timer for {_spoken_duration(seconds)} is done."


def _schedule(seconds: float, total: float, label: str, reminder: bool) -> None:
    timer_id = next(_timer_ids)
    timer = threading.Timer(seconds, _timer_fired, args=(timer_id,))
    timer.daemon = True
    warning = None
    if seconds >= WARN_MINIMUM:
        warning = threading.Timer(seconds - WARN_SECONDS, _timer_warning, args=(timer_id,))
        warning.daemon = True
    with _LOCK:
        _TIMERS.append({"id": timer_id, "label": label, "seconds": total, "reminder": reminder,
                        "ends": time.monotonic() + seconds, "due": time.time() + seconds,
                        "timer": timer, "warning": warning})
    timer.start()
    if warning is not None:
        warning.start()


def next_timer() -> dict | None:
    """The timer ending soonest, as {"label", "remaining", "reminder"}, or None. Used by the
    status bar's countdown, so it is cheap and never raises."""
    with _LOCK:
        if not _TIMERS:
            return None
        item = min(_TIMERS, key=lambda x: x["ends"])
        return {"label": item["label"], "reminder": item["reminder"],
                "remaining": max(0.0, item["ends"] - time.monotonic())}


def list_timers() -> list[dict]:
    """Every running timer, soonest first: {"id", "label", "reminder", "seconds", "remaining"} (the
    Timers window's list)."""
    with _LOCK:
        running = sorted(_TIMERS, key=lambda x: x["ends"])
        now = time.monotonic()
        return [{"id": x["id"], "label": x["label"], "reminder": x["reminder"], "seconds": x["seconds"],
                 "remaining": max(0.0, x["ends"] - now)} for x in running]


def cancel_timer(timer_id: int) -> bool:
    """Cancel one timer; False if it had already gone off (or never existed)."""
    with _LOCK:
        item = next((x for x in _TIMERS if x["id"] == timer_id), None)
        if item is None:
            return False
        item["timer"].cancel()
        if item.get("warning") is not None:
            item["warning"].cancel()
        _TIMERS.remove(item)
        _save()
    return True


def parse_duration_text(text: str) -> float:
    """What someone types into the Timers window -> seconds (0 if it isn't a duration):
    '25' (minutes), '1:30' (minutes:seconds), '1:05:00', '10m', '90s', '1h30m', '1h 30', or anything
    spoken, like 'half an hour'."""
    t = str(text or "").strip().lower()
    if not t:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return float(t) * 60
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        return float(a * 3600 + b * 60 + int(c)) if c is not None else float(a * 60 + b)
    short = re.sub(r"(?<=\d)\s*(?:hours?|hrs?)\b", "h", t)
    short = re.sub(r"(?<=\d)\s*(?:minutes?|mins?)\b", "m", short)
    short = re.sub(r"(?<=\d)\s*(?:seconds?|secs?)\b", "s", short)
    if re.fullmatch(r"(?:\d+(?:\.\d+)?\s*[hms]?\s*)+", short):
        unit_seconds = {"h": 3600, "m": 60, "s": 1, "": 60}           # a bare number is minutes ("1h 30")
        return sum(float(n) * unit_seconds[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([hms]?)", short))
    return duration_seconds(t)


def missed_announcement(item: dict) -> str:
    """What is said about a timer that went off while the assistant wasn't running."""
    what = timer_announcement(item["label"], item["seconds"], item["reminder"])
    late = item["late"]
    ago = "just now" if late < 90 else f"{_spoken_duration(late)} ago"
    return f"While I was closed: {what[0].lower() + what[1:]} (it went off {ago})"


def start_timer(seconds: float, label: str = "", reminder: bool = False) -> str:
    _schedule(seconds, seconds, label, reminder)
    with _LOCK:
        _save()
    if reminder:
        return f"Okay, I'll remind you in {_spoken_duration(seconds)} to {label}."
    return f"Okay, {label + ' ' if label else ''}timer set for {_spoken_duration(seconds)}."


# ---- surviving a restart -----------------------------------------------------------------------

def enable_persistence(path: Path) -> None:
    """Keep running timers in `path`, so closing the assistant doesn't lose them."""
    _STORE["path"] = Path(path)


def _save() -> None:
    """Write the running timers out. Call with _LOCK held (every caller here does)."""
    path = _STORE["path"]
    if path is None:
        return
    rows = [{"label": x["label"], "seconds": x["seconds"], "reminder": x["reminder"], "due": x["due"]}
            for x in _TIMERS]
    try:
        if rows:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass                                    # a timer that isn't remembered still fires this session


def restore_timers(now: float | None = None) -> list[dict]:
    """Start again the timers saved by an earlier run. Returns the ones that went off while the
    assistant was closed ({"label", "seconds", "reminder", "late" seconds}) so they can be reported."""
    path = _STORE["path"]
    if path is None or not path.exists():
        return []
    now = time.time() if now is None else now
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    missed = []
    for row in rows if isinstance(rows, list) else []:
        try:
            label, total, reminder, due = str(row["label"]), float(row["seconds"]), bool(row["reminder"]), float(row["due"])
        except (KeyError, TypeError, ValueError):
            continue
        if due > now:
            _schedule(due - now, total, label, reminder)
        else:
            missed.append({"label": label, "seconds": total, "reminder": reminder, "late": now - due})
    with _LOCK:
        _save()
    return missed


def timers_status() -> str:
    with _LOCK:
        running = sorted(_TIMERS, key=lambda x: x["ends"])
    if not running:
        return "You don't have any timers running."
    parts = []
    for item in running:
        left = max(0.0, item["ends"] - time.monotonic())
        parts.append(f"{_spoken_duration(left)} left" + (f" to {item['label']}" if item["label"] else ""))
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] + "."          # not .capitalize(): that lowercases the labels


def cancel_timers() -> str:
    with _LOCK:
        count = len(_TIMERS)
        for item in _TIMERS:
            item["timer"].cancel()
            if item.get("warning") is not None:
                item["warning"].cancel()
        _TIMERS.clear()
        _save()
    return "Cancelled your timers." if count else "You don't have any timers running."


def handle_timer_command(text: str) -> str | None:
    cmd = spoken_timer_command(text)
    if cmd is None:
        return None
    kind, arg = cmd
    if kind == "set":
        seconds, label, reminder = arg
        if seconds > 24 * 3600:
            return "That's longer than a day; I only keep timers up to 24 hours."
        return start_timer(seconds, label, reminder)
    return timers_status() if kind == "status" else cancel_timers()
