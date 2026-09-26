"""
calendar_feed.py -- the next event from an .ics calendar feed, for the status bar, plus heads-up alerts.

Works with any iCalendar source: a Google Calendar "secret address in iCal format", an Outlook /
Microsoft 365 published-calendar link, or a local .ics file. Understands UTC ("...Z"), time-zoned
(TZID=...), floating and all-day events, and the common recurrence rules (DAILY / WEEKLY with BYDAY /
MONTHLY / YEARLY, INTERVAL, COUNT, UNTIL, EXDATE). Fancier rules (BYSETPOS and friends) are not
expanded: those events just show up on their first date.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
HORIZON = timedelta(days=35)        # how far ahead recurring events are expanded
MAX_OCCURRENCES = 400


@dataclass
class Event:
    title: str
    start: datetime          # timezone-aware, local time
    end: datetime
    all_day: bool = False


def fetch(source: str, timeout: float = 10.0) -> str:
    """The raw .ics text from an http(s) link or a file path."""
    source = source.strip()
    if source.lower().startswith(("http://", "https://", "webcal://")):
        url = "https://" + source[len("webcal://"):] if source.lower().startswith("webcal://") else source
        req = urllib.request.Request(url, headers={"User-Agent": "neon-assistant/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(text: str) -> str:
    return text.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_prop(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), {k.upper(): v for k, _, v in (p.partition("=") for p in params)}, value


def _to_local(value: str, params: dict) -> tuple[datetime, bool]:
    """(local aware datetime, is_all_day) for a DTSTART / DTEND / EXDATE value."""
    local_tz = datetime.now().astimezone().tzinfo
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=local_tz), True
    stamp = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return stamp.replace(tzinfo=timezone.utc).astimezone(local_tz), False
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            return stamp.replace(tzinfo=ZoneInfo(tzid)).astimezone(local_tz), False
        except Exception:  # noqa: BLE001 -- unknown / Windows-style zone name: treat as local time
            pass
    return stamp.replace(tzinfo=local_tz), False


def _add_months(dt: datetime, months: int) -> datetime | None:
    month0 = dt.month - 1 + months
    year, month = dt.year + month0 // 12, month0 % 12 + 1
    try:
        return dt.replace(year=year, month=month)
    except ValueError:                                    # the 31st in a 30-day month: skip that occurrence
        return None


def _occurrences(start: datetime, rrule: str, now: datetime, exdates: set[datetime]) -> list[datetime]:
    rule = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until, _ = _to_local(rule["UNTIL"], {})
        except ValueError:
            until = None
    byday = [_DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in _DAYS]
    limit = now + HORIZON
    out: list[datetime] = []
    produced = 0

    def emit(dt: datetime | None) -> bool:
        nonlocal produced
        if dt is None or dt < start:
            return True
        if until is not None and dt > until:
            return False
        produced += 1
        if count is not None and produced > count:
            return False
        if dt >= now - timedelta(days=1) and dt not in exdates:
            out.append(dt)
        return dt <= limit and len(out) < MAX_OCCURRENCES

    if freq == "DAILY":
        k = 0
        while emit(start + timedelta(days=k * interval)):
            k += 1
    elif freq == "WEEKLY":
        days = byday or [start.weekday()]
        week0 = start - timedelta(days=start.weekday())
        k = 0
        keep_going = True
        while keep_going and k < 5000:
            for wd in sorted(days):
                if not emit(week0 + timedelta(weeks=k * interval, days=wd)):
                    keep_going = False
                    break
            k += 1
    elif freq in ("MONTHLY", "YEARLY"):
        step = interval * (12 if freq == "YEARLY" else 1)
        k = 0
        while k < 5000:
            dt = _add_months(start, k * step)
            if dt is not None and not emit(dt):
                break
            if dt is None and start + timedelta(days=31 * k * step) > limit:
                break
            k += 1
    else:
        emit(start)
    return out


def parse_events(text: str, now: datetime | None = None) -> list[Event]:
    """Every event that is happening now or starts within the look-ahead window, soonest first."""
    now = now or datetime.now().astimezone()
    events: list[Event] = []
    block: list[str] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            block = []
        elif line == "END:VEVENT" and block is not None:
            events.extend(_event_from_block(block, now))
            block = None
        elif block is not None:
            block.append(line)
    horizon = now + timedelta(days=2)
    upcoming = [e for e in events if e.end >= now and e.start <= horizon]
    return sorted(upcoming, key=lambda e: e.start)


def _event_from_block(block: list[str], now: datetime) -> list[Event]:
    props: dict[str, tuple[dict, str]] = {}
    exdates: set[datetime] = set()
    for line in block:
        name, params, value = _parse_prop(line)
        if name == "EXDATE":
            for part in value.split(","):
                try:
                    exdates.add(_to_local(part, params)[0])
                except ValueError:
                    pass
        else:
            props.setdefault(name, (params, value))
    if "DTSTART" not in props or props.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
        return []
    try:
        start, all_day = _to_local(props["DTSTART"][1], props["DTSTART"][0])
        if "DTEND" in props:
            end, _ = _to_local(props["DTEND"][1], props["DTEND"][0])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    except ValueError:
        return []
    title = _unescape(props.get("SUMMARY", ({}, "(no title)"))[1]).strip() or "(no title)"
    duration = end - start
    if "RRULE" in props:
        starts = _occurrences(start, props["RRULE"][1], now, exdates)
    else:
        starts = [start]
    return [Event(title, s, s + duration, all_day) for s in starts]


def next_event(source: str, now: datetime | None = None) -> tuple[Event | None, list[Event]]:
    """(the event to show, the next few) from a feed; (None, []) when nothing is coming up.
    Events already running count as 'current'; all-day events are shown only if nothing timed is left today."""
    now = now or datetime.now().astimezone()
    events = parse_events(fetch(source), now)
    timed = [e for e in events if not e.all_day]
    pool = timed or events
    return (pool[0] if pool else None), events[:5]


def describe(event: Event, now: datetime | None = None, use_24h: bool = False) -> str:
    """'Standup in 25 min', 'Standup (now)', 'Dentist 3:00 PM', 'Holiday (all day)'."""
    now = now or datetime.now().astimezone()
    if event.all_day:
        return f"{event.title} (all day)"
    if event.start <= now <= event.end:
        return f"{event.title} (now)"
    minutes = round((event.start - now).total_seconds() / 60)
    if minutes < 60:
        return f"{event.title} in {max(minutes, 1)} min"
    clock = event.start.strftime("%H:%M" if use_24h else "%I:%M %p").lstrip("0")
    day = "" if event.start.date() == now.date() else ("tomorrow " if event.start.date() == (now + timedelta(days=1)).date() else event.start.strftime("%a "))
    return f"{event.title} {day}{clock}"


# ---- heads-up alerts ---------------------------------------------------------------------------------

REFETCH_SECONDS = 300               # the feed is downloaded at most this often; the check itself is cheap
ALERT_GRACE = timedelta(seconds=45)  # an event that started this recently still gets its alert (a slow poll)


def due_alerts(events: list[Event], now: datetime, lead_minutes: float, seen: set) -> list[Event]:
    """Timed events that start within `lead_minutes` (or just started) and haven't been announced.
    They are added to `seen`, so each is announced once; entries for events long past are dropped."""
    horizon = now + timedelta(minutes=lead_minutes)
    due = []
    for event in events:
        if event.all_day or not (now - ALERT_GRACE <= event.start <= horizon):
            continue
        key = (event.title, event.start.isoformat())
        if key not in seen:
            seen.add(key)
            due.append(event)
    stale = now - timedelta(days=1)
    seen.difference_update({k for k in seen if datetime.fromisoformat(k[1]) < stale})
    return due


def alert_text(event: Event, now: datetime) -> str:
    """'Standup in 5 minutes.' / 'Standup is starting now.'"""
    seconds = (event.start - now).total_seconds()
    if seconds < 45:
        return f"{event.title} is starting now."
    if seconds < 90:
        return f"{event.title} in a minute."
    return f"{event.title} in {round(seconds / 60)} minutes."


class CalendarAlerts:
    """Polls the configured calendar and calls `on_alert(event, text)` shortly before each event.

    Settings are read on every pass (`get_config`), so changing the calendar link or the lead time
    needs no restart. A feed that can't be read is retried on the next pass, quietly."""

    def __init__(self, get_config, on_alert, fetch_events=None, clock=None, poll_seconds: float = 30.0):
        self._config = get_config
        self._on_alert = on_alert
        self._fetch_events = fetch_events or (lambda source, now: parse_events(fetch(source), now))
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._poll = poll_seconds
        self._seen: set = set()
        self._events: list[Event] = []
        self._source = ""
        self._fetched_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> list[Event]:
        """One pass. Returns the events announced (handy for tests)."""
        cfg = self._config()
        source = str(cfg.get("calendar_ics", "")).strip()
        if not source or not cfg.get("calendar_alerts", True):
            return []
        now = self._clock()
        mono = time.monotonic()
        if source != self._source or mono - self._fetched_at >= REFETCH_SECONDS:
            try:
                self._events = self._fetch_events(source, now)
            except Exception:  # noqa: BLE001 -- offline, bad link, unreadable file: try again next time
                self._events = [] if source != self._source else self._events
            self._source, self._fetched_at = source, mono
        try:
            lead = float(cfg.get("calendar_alert_minutes", 5))
        except (TypeError, ValueError):
            lead = 5.0
        due = due_alerts(self._events, now, lead, self._seen)
        for event in due:
            self._on_alert(event, alert_text(event, now))
        return due

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.check()
                except Exception:  # noqa: BLE001 -- a bad pass must never end the watcher
                    pass
                self._stop.wait(self._poll)

        self._thread = threading.Thread(target=loop, name="Nova-CalendarAlerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
