"""
board.py -- a small Trello-style board: columns you name yourself, cards in them, and due dates
(optionally with a time: "dinner with Mom at 4pm tomorrow"). A card's "due" is "YYYY-MM-DD" or
"YYYY-MM-DDTHH:MM".

The data lives in one JSON file (`enable_persistence`), written atomically after every change. The
Board window (ui/board.py) and voice commands both work on it; `HOOKS["changed"]` tells the window
to redraw when a voice command changed something.

Voice (whole utterances only, and only when a column or card actually matches, so ordinary
sentences are never hijacked):

    add pay rent to to do due friday        add a card call the bank          open my board
    make a card for dinner with mom at 4pm tomorrow                            dentist is due friday at 9:30
    move pay rent to done                   mark pay rent as done             what's on my board
    pay rent is due next monday             remove the due date from pay rent what's in doing
    delete the card pay rent                add a column called ideas         what's due this week
    rename the column ideas to later        delete the column later           anything overdue
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import threading
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from mathcalc import _words_to_digits

DEFAULT_COLUMNS = ("To do", "Doing", "Done")
TITLE_MAX = 200
COLUMN_MAX = 40

_LOCK = threading.RLock()
_STATE: dict = {"columns": None}
_STORE = {"path": None}
HOOKS = {"changed": None, "show": None}      # set by the controller
CLOCK = {"24h": lambda: False}               # set by assistant.py: Settings' 12 / 24-hour clock
AT_TIME_LEAD = timedelta(minutes=10)         # a card with a time is announced this long before it


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _default() -> list[dict]:
    return [{"id": _new_id(), "name": name, "cards": []} for name in DEFAULT_COLUMNS]


def enable_persistence(path) -> None:
    """Keep the board in `path` (loaded now)."""
    with _LOCK:
        _STORE["path"] = Path(path)
        _STATE["columns"] = None
        _cols()


def reset() -> None:
    """Back to the three empty default columns (tests)."""
    with _LOCK:
        _STATE["columns"] = _default()
        _save()


def _clean(data) -> list[dict] | None:
    """A loaded file -> columns, dropping anything malformed rather than failing."""
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
        return None
    columns = []
    for col in data["columns"]:
        if not isinstance(col, dict) or not str(col.get("name", "")).strip():
            continue
        cards = []
        for card in col.get("cards") or []:
            if isinstance(card, dict) and str(card.get("title", "")).strip():
                due = card.get("due")
                reminded, reminded_at = card.get("reminded"), card.get("reminded_at")
                cards.append({"id": str(card.get("id") or _new_id()), "title": str(card["title"])[:TITLE_MAX],
                              "due": due if isinstance(due, str) and _parse_iso(due) else None,
                              "created": float(card.get("created") or 0),
                              "reminded": reminded if isinstance(reminded, str) and _parse_iso(reminded) else None,
                              "reminded_at": reminded_at if isinstance(reminded_at, str) and _parse_iso(reminded_at)
                              else None})
        columns.append({"id": str(col.get("id") or _new_id()), "name": str(col["name"])[:COLUMN_MAX], "cards": cards})
    return columns


def _cols() -> list[dict]:
    """The live columns (call with _LOCK held)."""
    if _STATE["columns"] is None:
        loaded = None
        path = _STORE["path"]
        if path is not None and path.exists():
            try:
                loaded = _clean(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                loaded = None
                try:                                    # keep an unreadable file aside, never overwrite it
                    path.replace(path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"))
                except OSError:
                    pass
        _STATE["columns"] = loaded if loaded is not None else _default()
    return _STATE["columns"]


def _save() -> None:
    path = _STORE["path"]
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "columns": _STATE["columns"]}, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    hook = HOOKS["changed"]
    if hook is not None:
        try:
            hook()
        except Exception:  # noqa: BLE001 -- a closed window must never break a change
            pass


# ---------------------------------------------------------------------------
# Reading and changing it
# ---------------------------------------------------------------------------

def columns() -> list[dict]:
    """A copy of the whole board: [{"id", "name", "cards": [{"id", "title", "due", "created"}]}]."""
    with _LOCK:
        return copy.deepcopy(_cols())


def _column(column_id: str) -> dict | None:
    return next((c for c in _cols() if c["id"] == column_id), None)


def _locate(card_id: str) -> tuple[dict, dict] | tuple[None, None]:
    for col in _cols():
        for card in col["cards"]:
            if card["id"] == card_id:
                return card, col
    return None, None


def add_column(name: str, index: int | None = None) -> dict | None:
    name = " ".join(str(name).split())[:COLUMN_MAX]
    if not name:
        return None
    with _LOCK:
        col = {"id": _new_id(), "name": name, "cards": []}
        cols = _cols()
        cols.insert(len(cols) if index is None else max(0, min(index, len(cols))), col)
        _save()
        return copy.deepcopy(col)


def rename_column(column_id: str, name: str) -> bool:
    name = " ".join(str(name).split())[:COLUMN_MAX]
    with _LOCK:
        col = _column(column_id)
        if col is None or not name:
            return False
        col["name"] = name
        _save()
        return True


def delete_column(column_id: str) -> bool:
    with _LOCK:
        col = _column(column_id)
        if col is None:
            return False
        _cols().remove(col)
        _save()
        return True


def move_column(column_id: str, index: int) -> bool:
    with _LOCK:
        col = _column(column_id)
        if col is None:
            return False
        cols = _cols()
        cols.remove(col)
        cols.insert(max(0, min(index, len(cols))), col)
        _save()
        return True


def add_card(column_id: str, title: str, due: str | None = None) -> dict | None:
    title = " ".join(str(title).split())[:TITLE_MAX]
    with _LOCK:
        col = _column(column_id)
        if col is None or not title:
            return None
        card = {"id": _new_id(), "title": title, "due": due if due and _parse_iso(due) else None,
                "created": time.time(), "reminded": None, "reminded_at": None}
        col["cards"].append(card)
        _save()
        return copy.deepcopy(card)


_KEEP = object()


def update_card(card_id: str, title: str | None = None, due=_KEEP) -> bool:
    """Change a card's title and / or due date ("YYYY-MM-DD", "YYYY-MM-DDTHH:MM", or None to clear it)."""
    with _LOCK:
        card, _col = _locate(card_id)
        if card is None:
            return False
        if title is not None:
            title = " ".join(str(title).split())[:TITLE_MAX]
            if not title:
                return False
            card["title"] = title
        if due is not _KEEP:
            new = due if due and _parse_iso(due) else None
            if new != card["due"]:
                card["reminded"] = card["reminded_at"] = None     # a new date gets its own reminders
            card["due"] = new
        _save()
        return True


def delete_card(card_id: str) -> bool:
    with _LOCK:
        card, col = _locate(card_id)
        if card is None:
            return False
        col["cards"].remove(card)
        _save()
        return True


def move_card(card_id: str, column_id: str, index: int | None = None) -> bool:
    """Into `column_id` at `index` (the end if None). Moving within one column reorders it."""
    with _LOCK:
        card, col = _locate(card_id)
        target = _column(column_id)
        if card is None or target is None:
            return False
        old = col["cards"].index(card)
        col["cards"].remove(card)
        if index is None:
            index = len(target["cards"])
        elif target is col and index > old:
            index -= 1                                  # the card's own slot is gone now
        target["cards"].insert(max(0, min(index, len(target["cards"]))), card)
        _save()
        return True


# ---------------------------------------------------------------------------
# Due dates
# ---------------------------------------------------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")


_DUE_FORMAT = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T([01]\d|2[0-3]):([0-5]\d))?")


def _parse_iso(text: str) -> date | None:
    """The day of a stored due date ("2026-09-26" or "2026-09-26T16:00"), None if it isn't one."""
    m = _DUE_FORMAT.fullmatch(str(text or "").strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def due_time(due: str | None) -> dtime | None:
    """The time of a due date that has one ("2026-09-26T16:00" -> 16:00), else None."""
    m = _DUE_FORMAT.fullmatch(str(due or "").strip())
    return dtime(int(m.group(2)), int(m.group(3))) if m and m.group(2) and _parse_iso(due) else None


def due_at(due: str | None) -> datetime | None:
    """The moment a due date with a time falls due (None for a plain date)."""
    d, t = _parse_iso(due) if due else None, due_time(due)
    return datetime.combine(d, t) if d and t else None


def make_due(day: date, at: dtime | None = None) -> str:
    """The stored form: "2026-09-26", or "2026-09-26T16:00" with a time."""
    return f"{day.isoformat()}T{at:%H:%M}" if at is not None else day.isoformat()


def clock(t: dtime) -> str:
    """'4 PM' / '4:30 PM' / '16:30', per Settings' clock."""
    if CLOCK["24h"]():
        return f"{t:%H:%M}"
    return f"{t.hour % 12 or 12}{'' if t.minute == 0 else f':{t.minute:02d}'} {'AM' if t.hour < 12 else 'PM'}"


def _month(word: str) -> int | None:
    word = word.lower().rstrip(".")
    for i, name in enumerate(_MONTHS, 1):
        if len(word) >= 3 and name.startswith(word):
            return i
    return None


def parse_due(text: str, today: date | None = None) -> date | None:
    """'today' / 'tomorrow' / 'friday' / 'next monday' / 'in 3 days' / 'in two weeks' / 'end of the week' /
    'september 30' / '30th of september' / '2026-10-01' / '10/1' -> a date (None if it isn't one).
    A date without a year that has already passed means next year's."""
    today = today or date.today()
    t = _words_to_digits(re.sub(r"[,.!?]", " ", str(text or "").lower()))
    t = re.sub(r"\b(?:on|by|the|this coming|this)\b", " ", t)
    t = " ".join(t.split())
    if not t:
        return None
    if t in ("today", "tonight"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "day after tomorrow":
        return today + timedelta(days=2)
    if t == "end of week":                                  # Friday ("the" was dropped above)
        return today + timedelta(days=(4 - today.weekday()) % 7)
    if t == "weekend":
        return today + timedelta(days=(5 - today.weekday()) % 7)
    if t == "next week":                                    # the coming Monday
        return today + timedelta(days=7 - today.weekday())
    m = re.fullmatch(r"in (\d+|a|an|one) (day|week|month)s?", t)
    if m:
        n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        return today + timedelta(days=n * {"day": 1, "week": 7, "month": 30}[m.group(2)])
    m = re.fullmatch(r"(?:next )?(" + "|".join(_WEEKDAYS) + r")", t)
    if m:                                                   # the next one after today ("friday" on a Friday: in a week)
        return today + timedelta(days=(_WEEKDAYS.index(m.group(1)) - today.weekday() - 1) % 7 + 1)
    iso = _parse_iso(t)
    if iso:
        return iso
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)       # month/day, as Windows shows dates here
    if m:
        return _future(today, int(m.group(1)), int(m.group(2)), m.group(3))
    m = re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", t)
    if m and _month(m.group(1)):
        return _future(today, _month(m.group(1)), int(m.group(2)), m.group(3))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?(?: of)? ([a-z]+)(?: (\d{4}))?", t)
    if m and _month(m.group(2)):
        return _future(today, _month(m.group(2)), int(m.group(1)), m.group(3))
    m = re.fullmatch(r"(?:(" + "|".join(_WEEKDAYS) + r") )?(\d{1,2})(?:st|nd|rd|th)", t)
    if m:                                                   # "the 29th": this month, or next if it has passed
        when = _next_day_of_month(today, int(m.group(2)))
        if m.group(1) and when is not None and when.weekday() != _WEEKDAYS.index(m.group(1)):
            return None                                     # "friday the 13th" that isn't a Friday: a name
        return when
    return None


def _next_day_of_month(today: date, day: int) -> date | None:
    """The next date that falls on `day` of a month, today included: the 29th on the 24th is this
    month's, on the 30th it's next month's. A month too short for it (the 31st in November) is skipped."""
    if not 1 <= day <= 31:
        return None
    year, month = today.year, today.month
    for _ in range(3):                                      # never more than two months ahead
        try:
            candidate = date(year, month, day)
            if candidate >= today:
                return candidate
        except ValueError:
            pass
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _future(today: date, month: int, day: int, year: str | None) -> date | None:
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def _now_for(today: date | None, now: datetime | None) -> datetime | None:
    """The moment to compare times with: `now`, or the clock when no day was given (a fixed `today`
    without `now` compares dates only)."""
    return now if now is not None else (datetime.now() if today is None else None)


def describe_due(due: str | None, today: date | None = None, now: datetime | None = None) -> str:
    """'' | 'today' | 'tomorrow' | 'Friday' (within a week) | 'Sep 30' | 'overdue: Sep 22', each with
    ' at 4 PM' when the card has a time; a time that has passed today reads 'overdue: 4 PM'."""
    d = _parse_iso(due) if due else None
    if d is None:
        return ""
    now = _now_for(today, now)
    today = today or (now.date() if now else date.today())
    t = due_time(due)
    at = f" at {clock(t)}" if t else ""
    days = (d - today).days
    if days < 0:
        return f"overdue: {d:%b} {d.day}"
    if days == 0:
        if t and now is not None and now >= datetime.combine(d, t):
            return f"overdue: {clock(t)}"
        return "today" + at
    if days == 1:
        return "tomorrow" + at
    if days < 7:
        return f"{d:%A}" + at
    return f"{d:%b} {d.day}" + (f" {d.year}" if d.year != today.year else "") + at


def due_state(due: str | None, today: date | None = None, now: datetime | None = None) -> str:
    """'' | 'overdue' | 'today' | 'soon' (within 2 days) | 'later' -- the window colours cards by it."""
    d = _parse_iso(due) if due else None
    if d is None:
        return ""
    now = _now_for(today, now)
    today = today or (now.date() if now else date.today())
    moment = due_at(due)
    if moment is not None and now is not None and d == today and now >= moment:
        return "overdue"
    days = (d - today).days
    return "overdue" if days < 0 else "today" if days == 0 else "soon" if days <= 2 else "later"


def _is_done_column(col: dict) -> bool:
    return _norm(col["name"]) in ("done", "finished", "complete", "completed", "archive", "archived")


def due_cards(within_days: int | None = 7, today: date | None = None) -> list[tuple[dict, dict]]:
    """(card, column) with a due date up to `within_days` from today (overdue included; None = any),
    soonest first, leaving out cards in a "Done" column."""
    today = today or date.today()
    found = []
    with _LOCK:
        for col in _cols():
            if _is_done_column(col):
                continue
            for card in col["cards"]:
                d = _parse_iso(card["due"]) if card["due"] else None
                if d is not None and (within_days is None or (d - today).days <= within_days):
                    found.append((copy.deepcopy(card), {"id": col["id"], "name": col["name"]}))
    return sorted(found, key=lambda pair: pair[0]["due"])


# ---------------------------------------------------------------------------
# Reminders: once a day, for every card that is due that day or overdue (not in a "Done" column)
# ---------------------------------------------------------------------------

def take_due_reminders(now: datetime | None = None, remind_at: dtime = dtime(9, 0)) -> list[tuple[dict, dict]]:
    """The (card, column) pairs to remind about now, marked as reminded for today (and saved), so each
    card is mentioned at most once a day however often this is called. Nothing before `remind_at`; after
    it (including when the assistant only starts later in the day), everything due today or earlier.
    A card with a time is also announced once, AT_TIME_LEAD before it (whatever `remind_at` says),
    unless that time is more than an hour gone."""
    now = now or datetime.now()
    today = now.date()
    due: list[tuple[dict, dict]] = []
    with _LOCK:
        for col in _cols():
            if _is_done_column(col):
                continue
            for card in col["cards"]:
                d = _parse_iso(card["due"]) if card["due"] else None
                moment = due_at(card["due"])
                if (moment is not None and moment - AT_TIME_LEAD <= now <= moment + timedelta(hours=1)
                        and card.get("reminded_at") != card["due"]):
                    card["reminded_at"] = card["due"]
                    card["reminded"] = today.isoformat()      # the morning round needn't repeat it
                    due.append((copy.deepcopy(card), {"id": col["id"], "name": col["name"]}))
                elif (now.time() >= remind_at and d is not None and d <= today
                      and card.get("reminded") != today.isoformat()):
                    card["reminded"] = today.isoformat()
                    due.append((copy.deepcopy(card), {"id": col["id"], "name": col["name"]}))
        if due:
            _save()
    return sorted(due, key=lambda pair: pair[0]["due"])


def reminder_text(pairs: list[tuple[dict, dict]], today: date | None = None) -> str:
    """'Pay rent is due today.' / 'Dinner with Mom is due today at 4 PM.' / 'Old bill was due on Monday.' /
    '3 cards are due: Pay rent (today), ...'."""
    today = today or date.today()

    def when(card: dict) -> str:
        d = _parse_iso(card["due"])
        days = (today - d).days
        if days <= 0:
            t = due_time(card["due"])
            return f"today at {clock(t)}" if t else "today"
        if days == 1:
            return "yesterday"
        return f"on {d:%A}" if days < 7 else f"on {d:%B} {d.day}"

    if len(pairs) == 1:
        card = pairs[0][0]
        w = when(card)
        return f"{card['title']} is due {w}." if w.startswith("today") else f"{card['title']} was due {w}."
    parts = [f"{c['title']} ({when(c) if when(c).startswith('today') else 'overdue'})" for c, _col in pairs[:5]]
    more = f" and {len(pairs) - 5} more" if len(pairs) > 5 else ""
    listing = ", ".join(parts[:-1]) + " and " + parts[-1] if not more else ", ".join(parts) + more
    return f"{len(pairs)} cards are due: {listing}."


# ---------------------------------------------------------------------------
# Finding things by what was said
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", str(text).lower())
    t = re.sub(r"\b(?:the|a|an|my)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip().replace("to do", "todo").replace("to-do", "todo")


def find_column(spoken: str) -> dict | None:
    s = _norm(re.sub(r"\b(?:list|column)\b", " ", spoken))
    if not s:
        return None
    with _LOCK:
        cols = _cols()
        for col in cols:
            if _norm(col["name"]) == s or _norm(col["name"]).replace(" ", "") == s.replace(" ", ""):
                return copy.deepcopy(col)
        close = difflib.get_close_matches(s, [_norm(c["name"]) for c in cols], n=1, cutoff=0.8)
        if close:
            return copy.deepcopy(next(c for c in cols if _norm(c["name"]) == close[0]))
    return None


def find_card(spoken: str, within: dict | None = None) -> tuple[dict, dict] | tuple[None, None]:
    """(card, column) whose title best matches what was said, or (None, None). `within`: a column to
    look in first (then everywhere)."""
    s = _norm(re.sub(r"\b(?:card|task)\b", " ", spoken))
    if not s:
        return None, None
    if within is not None:
        found = find_card(spoken)
        with _LOCK:
            inside = [(card, col) for col in _cols() if col["id"] == within["id"] for card in col["cards"]
                      if _norm(card["title"]) == s or (len(s) >= 3 and s in _norm(card["title"]))]
        if inside:
            card, col = inside[0]
            return copy.deepcopy(card), {"id": col["id"], "name": col["name"]}
        return found
    with _LOCK:
        pairs = [(card, col) for col in _cols() for card in col["cards"]]
        for card, col in pairs:
            if _norm(card["title"]) == s:
                return copy.deepcopy(card), {"id": col["id"], "name": col["name"]}
        contains = [(card, col) for card, col in pairs if s in _norm(card["title"]) and len(s) >= 3]
        if len(contains) == 1:
            card, col = contains[0]
            return copy.deepcopy(card), {"id": col["id"], "name": col["name"]}
        titles = [_norm(card["title"]) for card, _col in pairs]
        close = difflib.get_close_matches(s, titles, n=1, cutoff=0.75)
        if close:
            card, col = pairs[titles.index(close[0])]
            return copy.deepcopy(card), {"id": col["id"], "name": col["name"]}
    return None, None


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    t = re.sub(r"[!?]", "", str(text).lower()).strip().rstrip(".")
    t = re.sub(r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)+", "", t)
    return re.sub(r"\s+please$", "", t).strip()


_DUE_WORDS = r"(?:due|by|on|for|before|until|till|at)"
_DATE_WORDS_MAX = 6          # "on the 22nd of september 2027" is the longest date worth looking for


def _split_due(text: str, today: date | None = None) -> tuple[str, date | None, bool]:
    """A card title with its due date taken out: 'pay rent due friday' -> ('pay rent', friday, True),
    'Complete project on september 22nd' -> ('Complete project', Sep 22, True), and a date at the
    start works too ('by tomorrow, call the bank'). The bool says a due date was found."""
    words = str(text).split()
    # The longest run of words at the end that is a date: "next monday" before "monday". At least one
    # word is always left for the title.
    for n in range(min(_DATE_WORDS_MAX, len(words) - 1), 0, -1):
        when = parse_due(" ".join(words[-n:]), today)
        if when is not None and re.fullmatch(r"(?:the )?\d{1,2}(?:st|nd|rd|th)", " ".join(words[-n:]).lower()):
            before = [w.lower() for w in words[:-n] if w.lower() != "the"]
            if before and before[-1] in _WEEKDAYS:
                continue                               # "Friday the 13th" (not a Friday): part of the title
        if when is not None:
            rest = re.sub(rf"(?:[\s,]+{_DUE_WORDS}\b)+[\s,]*$|[\s,]+$", "", " " + " ".join(words[:-n])).strip()
            if rest:
                return rest, when, True
    # ...or at the start, when it's said as one: "on friday, dentist", "tomorrow: call the bank".
    for n in range(min(_DATE_WORDS_MAX, len(words) - 1), 0, -1):
        lead = " ".join(words[:n]).rstrip(",:;-")
        if not re.match(rf"(?:{_DUE_WORDS}|today|tonight|tomorrow|next)\b", lead, re.I):
            continue                                   # "friday night plans" is a title, not a date
        when = parse_due(re.sub(rf"^{_DUE_WORDS}\s+", "", lead, flags=re.I), today)
        if when is not None:
            rest = " ".join(words[n:]).lstrip(",:;- ").strip()
            if rest:
                return rest, when, True
    return text, None, False


_HOUR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
               "ten": 10, "eleven": 11, "twelve": 12}
_HOUR = r"\d{1,2}|" + "|".join(_HOUR_WORDS)
# What may follow a bare "at 7" (no am / pm) for it to count as a time: the end, or a day.
_AFTER_BARE = (r"(?=\s*(?:$|,|;|(?:today|tonight|tomorrow|on|next|this|in the|every|o'?\s?clock|"
               + "|".join(_WEEKDAYS) + r")\b))")
_TIME_IN_TEXT = re.compile(
    r"[\s,]*(?:\b(?:at|by|@|around|before|from)\s+)?\b(?P<h>" + _HOUR + r")(?:[:.](?P<m>[0-5]\d))?\s*"
    r"(?P<ap>[ap])\.?\s?m\b\.?"                                              # 4pm, at 4:30 p.m.
    r"|[\s,]*\b(?:at|by|@|around|before)\s+(?P<h2>" + _HOUR + r")(?::(?P<m2>[0-5]\d))?(?:\s*o'?\s?clock)?"
    + _AFTER_BARE +                                                            # at 16:00, at 7 tomorrow
    r"|[\s,]*\b(?P<h3>[01]?\d|2[0-3]):(?P<m3>[0-5]\d)\b"                          # 16:00
    r"|[\s,]*\b(?:at|by|around)\s+(?P<word>noon|midday|midnight)\b", re.I)
_DAY_PART = re.compile(r"[\s,]*\b(?:in the (?P<part>morning|afternoon|evening)|at (?P<night>night))\b", re.I)


def _hour(text: str) -> int:
    return int(text) if text.isdigit() else _HOUR_WORDS[text.lower()]


def _split_time(text: str) -> tuple[str, dtime | None]:
    """A time taken out of a card title: 'dinner with mom at 4pm tomorrow' -> ('dinner with mom tomorrow',
    16:00). Without am / pm, 'in the morning' / 'evening' / 'tonight' decide, else 1 to 7 is afternoon or
    evening (people rarely plan things for 3 in the morning) and 8 to 11 is morning."""
    for m in _TIME_IN_TEXT.finditer(str(text)):
        word = (m.group("word") or "").lower()
        if word:
            at = dtime(23, 59) if word == "midnight" else dtime(12, 0)
        else:
            raw = m.group("h") or m.group("h2") or m.group("h3")
            hour, minute = _hour(raw), int(m.group("m") or m.group("m2") or m.group("m3") or 0)
            ap = (m.group("ap") or "").lower()
            rest = text[:m.start()] + text[m.end():]
            part = _DAY_PART.search(rest)
            if ap:
                if not 1 <= hour <= 12:
                    continue
                hour = hour % 12 + (12 if ap == "p" else 0)
            elif hour > 23:
                continue
            elif 1 <= hour <= 11 and not m.group("h3"):
                later = part and (part.group("part") in ("afternoon", "evening") or part.group("night")) \
                    or re.search(r"\btonight\b", rest, re.I)
                earlier = part and part.group("part") == "morning"
                hour += 12 if later or (not earlier and hour <= 7) else 0
            at = dtime(hour, minute)
        rest = text[:m.start()] + text[m.end():]
        if not word:
            rest = _DAY_PART.sub("", rest, count=1)
        return " ".join(rest.split()), at
    return text, None


_VAGUE = re.compile(r"^(?:next week|weekend|end of week|in (?:\S+) (?:week|month)s?)$")
_FILLER_WORDS = re.compile(r"\b(?:on|by|the|this coming|this)\b")


def mentions_when(text: str, today: date | None = None) -> bool:
    """True if `text` names a specific day or time anywhere in it ("dinner tomorrow at 4pm", "due Friday",
    "Oct 3", "at 9:30"). Vague spans ("next week", "in two weeks", "this weekend") don't count."""
    text = str(text or "")
    if _split_time(" " + text)[1] is not None:
        return True
    words = re.sub(r"[,.!?;:()\[\]\"]", " ", text.lower()).split()
    for n in range(min(_DATE_WORDS_MAX, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            span = " ".join(words[i:i + n])
            if parse_due(span, today) is not None:
                bare = " ".join(_FILLER_WORDS.sub(" ", span).split())
                if not _VAGUE.match(bare) and not re.fullmatch(r"\d{1,2}", bare):
                    return True
    return False


def split_due(text: str, today: date | None = None, now: datetime | None = None) -> tuple[str, str | None]:
    """A card title with its due date (and time) taken out -> (title, stored due or None):
    'dinner with mom at 4pm tomorrow' -> ('dinner with mom', '2026-09-26T16:00'). A time without a day
    means today, or tomorrow once it has passed."""
    rest, at = _split_time(text)
    if at is None or not rest.strip():
        title, when, _found = _split_due(text, today)
        return title, when.isoformat() if when else None
    title, when, _found = _split_due(rest, today)
    if when is None:
        now = _now_for(today, now) or datetime.combine(today, dtime(0, 0))
        when = now.date() if now.time() < at else now.date() + timedelta(days=1)
    return title, make_due(when, at)


def parse_due_at(text: str, today: date | None = None, now: datetime | None = None) -> str | None:
    """A whole phrase that is a due date, possibly with a time ('friday at 5pm', 'tomorrow', '4pm') ->
    the stored due, or None."""
    rest, at = _split_time(" " + str(text))
    rest = re.sub(rf"^(?:{_DUE_WORDS})\s+|\s+(?:{_DUE_WORDS})$", "", rest.strip())
    when = parse_due(rest, today) if rest else None
    if at is None:
        return when.isoformat() if when else None
    if when is None:
        if rest:
            return None                                  # "4pm blah": not a date
        now = _now_for(today, now) or datetime.combine(today, dtime(0, 0))
        when = now.date() if now.time() < at else now.date() + timedelta(days=1)
    return make_due(when, at)


def _spoken_day(d: date, today: date | None = None) -> str:
    """'today' / 'tomorrow' / 'on Friday' / 'on October 3'."""
    days = (d - (today or date.today())).days
    if days in (0, 1):
        return "today" if days == 0 else "tomorrow"
    return f"on {d:%A}" if 1 < days < 7 else f"on {d:%B} {d.day}"


def _spoken_due(due: str, today: date | None = None) -> str:
    """'tomorrow' / 'tomorrow at 4 PM' / 'on Friday at 9:30 AM'."""
    t = due_time(due)
    return _spoken_day(_parse_iso(due), today) + (f" at {clock(t)}" if t else "")


def _as_said(title: str, original: str) -> str:
    """The title with the capitals it was said / typed with ('dinner with mom' in 'Make a card for dinner
    with Mom' -> 'dinner with Mom'): commands are matched in lower case."""
    words = title.split()
    if not words:
        return title
    m = re.search(r"\s+".join(re.escape(w) for w in words), str(original), re.I)
    return m.group(0) if m else title


def _list_titles(cards: list[dict]) -> str:
    titles = [c["title"] for c in cards]
    if len(titles) <= 1:
        return "".join(titles)
    return ", ".join(titles[:-1]) + " and " + titles[-1]


_ADD_TO = re.compile(r"(?:add|put|create|make)(?: a| new| a new)?(?: card| task)?(?: called| named| for)? (?P<title>.+?) "
                     r"(?:to|on|in|into|under) (?:the |my )?(?P<col>.+?)(?: list| column)?(?:,? (?:due|by) (?P<due>.+))?")
# The same, split at the *last* "to / on / in": "add dinner with mom on september 24th to my board"
# (the first "on" belongs to the date, which _split_due then takes out of the title).
_ADD_TO_LAST = re.compile(_ADD_TO.pattern.replace("(?P<title>.+?)", "(?P<title>.+)", 1))
_ADD_CARD = re.compile(r"(?:add|create|make)(?: a| new| a new) (?:card|task)(?: called| named| for| to)? (?P<title>.+)")
_COL_END = r"(?: list| column| cards)?"
_MOVE = re.compile(r"(?:move|put|send|drag|shift|take) (?:the )?(?:card |task )?(?P<card>.+?)(?: card| task)?"
                   r"(?: from (?:the |my )?(?P<src>.+?)" + _COL_END + r")?(?: back| over)?"
                   r" (?:to|into|in|onto|over to|across to) (?:the |my )?(?P<col>.+?)" + _COL_END)
_MOVE_ALL = re.compile(r"(?:move|put|send|shift|take) (?:everything|all(?: of)?(?: the| my)?(?: cards| tasks)?|all cards)"
                       r" (?:from|in|on) (?:the |my )?(?P<src>.+?)" + _COL_END +
                       r"(?: back| over)? (?:to|into|onto|over to|across to) (?:the |my )?(?P<col>.+?)" + _COL_END)
_DONE = re.compile(r"(?:mark|set) (?:the )?(?:card |task )?(?P<card>.+?) (?:as )?(?:done|finished|complete|completed)"
                   r"|(?:i (?:finished|did|completed)|i'm done with|i am done with) (?:the )?(?:card |task )?(?P<card2>.+)")
_DELETE = re.compile(r"(?:delete|remove|get rid of) (?:the )?(?:card|task) (?P<card>.+?)(?: from (?:the |my )?board)?"
                     r"|(?:delete|remove) (?P<card2>.+?) from (?:the |my )?board")
_DUE = re.compile(r"(?P<card>.+?) is due (?P<due>.+)"
                  r"|(?:set|change|make) (?:the )?due date (?:of|for|on) (?P<card2>.+?) (?:to|as) (?P<due2>.+)"
                  r"|make (?P<card3>.+?) due (?P<due3>.+)")
_NO_DUE = re.compile(r"(?:remove|clear|delete) the due date (?:from|of|on|for) (?:the )?(?:card |task )?(?P<card>.+)")
_ADD_COLUMN = re.compile(r"(?:add|create|make)(?: a| new| a new)? (?:column|list)(?: called| named)? (?P<name>.+?)"
                         r"(?: to (?:the |my )?board)?")
_DELETE_COLUMN = re.compile(r"(?:delete|remove) (?:the )?(?:column|list) (?P<name>.+)")
_RENAME_COLUMN = re.compile(r"rename (?:the )?(?:column|list) (?P<name>.+?) to (?P<new>.+)")
_SHOW = re.compile(r"(?:open|show|show me)(?: me)? (?:the |my )?(?:board|trello|cards|task board|kanban)")
_WHATS_ON = re.compile(r"(?:what's|what is|whats) on (?:the |my )?board|read (?:me )?(?:the |my )?board"
                       r"|what(?:'s| is| are)? (?:my )?cards")
_WHATS_IN = re.compile(r"(?:what's|what is|whats|what are|what have i got|what do i have|what's left|what is left|"
                       r"whats left|anything|is there anything) (?:in|on) (?:the |my )?(?P<col>.+?)" + _COL_END +
                       r"|(?:read me|read out|read|list|show me|tell me|go through)(?: what's (?:in|on))? "
                       r"(?:the |my )?(?P<col2>.+?) (?:list|column|cards)")
_TODO_Q = re.compile(r"what (?:do|should|must) i (?:have|need|still have)? ?to do(?: today| next)?"
                     r"|what(?:'s| is)? (?:left|still) to do|whats left to do|what's next on my list")
_WHATS_DUE = re.compile(r"(?:what's|what is|whats|what do i have|what have i got|anything|is anything|what cards are|"
                        r"which cards are) (?P<what>due|overdue)(?: (?P<when>today|tomorrow|this week|soon|"
                        r"next week))?")


def handle_board_command(text: str, today: date | None = None) -> str | None:
    """The reply to a board voice command, or None if `text` isn't one."""
    t = _clean_text(text)
    if not t:
        return None

    if _SHOW.fullmatch(t):
        hook = HOOKS["show"]
        if hook is not None:
            hook()
        return "Here's your board."

    m = _WHATS_DUE.fullmatch(t)
    if m:
        today = today or date.today()
        when, overdue_only = m.group("when"), m.group("what") == "overdue"
        days = {"today": 0, "tomorrow": 1, "this week": 6 - today.weekday(), "next week": 13 - today.weekday(),
                "soon": 2}.get(when or "", 7)
        cards = [c for c, _col in due_cards(days, today) if not overdue_only or due_state(c["due"], today) == "overdue"]
        if not cards:
            return "Nothing's overdue." if overdue_only else "Nothing's due" + (f" {when}." if when else " soon.")
        parts = [f"{c['title']}, {describe_due(c['due'], today)}" for c in cards[:6]]
        more = f" And {len(cards) - 6} more." if len(cards) > 6 else ""
        text = "; ".join(parts)
        return text[:1].upper() + text[1:] + "." + more

    if _WHATS_ON.fullmatch(t):
        cols = columns()
        if not any(c["cards"] for c in cols):
            return "Your board is empty. Say \"add a card\" and what it's for."
        parts = [f"{c['name']}: {_list_titles(c['cards'][:5])}" + (f" and {len(c['cards']) - 5} more"
                 if len(c["cards"]) > 5 else "") for c in cols if c["cards"]]
        return ". ".join(parts) + "."

    m = _WHATS_IN.fullmatch(t)
    todo_q = _TODO_Q.fullmatch(t)
    if m or todo_q:
        if todo_q:                                   # "what do I have to do": the to-do column, else the first
            col = find_column("to do") or (columns() or [None])[0]
        else:
            col = find_column(m.group("col") or m.group("col2"))
        if col is not None:
            if not col["cards"]:
                return f"{col['name']} is empty."
            return f"In {col['name']}: {_list_titles(col['cards'][:8])}" + (
                f", and {len(col['cards']) - 8} more." if len(col["cards"]) > 8 else ".")

    m = _RENAME_COLUMN.fullmatch(t)
    if m:
        col = find_column(m.group("name"))
        if col is not None:
            new = m.group("new").strip().title() if m.group("new").islower() else m.group("new").strip()
            rename_column(col["id"], new)
            return f"Renamed {col['name']} to {new}."

    m = _DELETE_COLUMN.fullmatch(t)
    if m:
        col = find_column(m.group("name"))
        if col is not None:
            delete_column(col["id"])
            lost = len(col["cards"])
            return f"Deleted the {col['name']} column" + (f" and its {lost} card{'s' if lost != 1 else ''}." if lost else ".")

    m = _ADD_COLUMN.fullmatch(t)
    if m:
        name = m.group("name").strip()
        col = add_column(name[:1].upper() + name[1:])
        return f"Added a column called {col['name']}." if col else None

    m = _NO_DUE.fullmatch(t)
    if m:
        card, _col = find_card(m.group("card"))
        if card is not None:
            update_card(card["id"], due=None)
            return f"{card['title']} has no due date now."

    m = _DUE.fullmatch(t)
    if m:
        spoken = m.group("card") or m.group("card2") or m.group("card3")
        when = parse_due_at(m.group("due") or m.group("due2") or m.group("due3"), today)
        card, _col = find_card(spoken)
        if card is not None and when is not None:
            update_card(card["id"], due=when)
            return f"{card['title']} is due {_spoken_due(when, today)}."

    m = _DONE.fullmatch(t)
    if m:
        card, col = find_card(m.group("card") or m.group("card2"))
        if card is not None:
            done = next((c for c in columns() if _is_done_column(c)), None)
            if done is None:
                done = add_column("Done")
            if col["id"] == done["id"]:
                return f"{card['title']} is already in {done['name']}."
            move_card(card["id"], done["id"])
            return f"Nice. Moved {card['title']} to {done['name']}."

    m = _MOVE_ALL.fullmatch(t)
    if m:
        src, col = find_column(m.group("src")), find_column(m.group("col"))
        if src is not None and col is not None:
            if src["id"] == col["id"]:
                return f"They're already in {col['name']}."
            if not src["cards"]:
                return f"{src['name']} is empty."
            for card in src["cards"]:
                move_card(card["id"], col["id"])
            n = len(src["cards"])
            return f"Moved {n} card{'s' if n != 1 else ''} from {src['name']} to {col['name']}."

    m = _MOVE.fullmatch(t)
    if m:
        col = find_column(m.group("col"))
        card, _old = find_card(m.group("card"), within=find_column(m.group("src")) if m.group("src") else None)
        if col is not None and card is not None:
            if _old["id"] == col["id"]:
                return f"{card['title']} is already in {col['name']}."
            move_card(card["id"], col["id"])
            return f"Moved {card['title']} to {col['name']}."
        if col is not None and t.startswith("move ") and card is None:
            return f"I couldn't find a card called {m.group('card')} on your board."

    m = _DELETE.fullmatch(t)
    if m:
        card, _col = find_card(m.group("card") or m.group("card2"))
        if card is not None:
            delete_card(card["id"])
            return f"Deleted {card['title']}."
        return "I couldn't find that card on your board."

    for title_said, spoken_col, due_said in _add_to_splits(t):
        col_due = None
        col = find_column(spoken_col) if _norm(spoken_col) not in ("board", "trello") else (columns() or [None])[0]
        if col is None:                                 # "add dentist to to do on friday": the day came after
            spoken_col, col_due, _tried = _split_due(spoken_col, today)
            col = find_column(spoken_col) if _norm(spoken_col) not in ("board", "trello") else (columns() or [None])[0]
        if col is not None:
            title, when = split_due(title_said, today)
            if col_due is not None:                         # "add dinner at 4pm to to do on friday"
                when = make_due(col_due, due_time(when))
            if due_said:
                when = parse_due_at(due_said, today) or when
            return _added(col, _as_said(title, text), when, today)

    m = _ADD_CARD.fullmatch(t)
    if m:
        cols = columns()
        if not cols:
            cols = [add_column(DEFAULT_COLUMNS[0])]
        title, when = split_due(m.group("title").strip(), today)
        return _added(cols[0], _as_said(title, text), when, today)
    return None


_ADD_LEAD = re.compile(r"(?:add|put|create|make)(?: a| new| a new)?(?: card| task)?(?: called| named| for)? ")
_INTO = re.compile(r" (?:to|on|in|into|under) (?:the |my )?")


def _add_to_splits(t: str):
    """Ways to read "add <title> to <column>": the first "to / on / in", the last, then every other one
    ("add gym at 7 in the morning to doing on monday" only makes sense split at "to")."""
    tried = set()
    for pattern in (_ADD_TO, _ADD_TO_LAST):
        m = pattern.fullmatch(t)
        if m:
            tried.add((m.group("title"), m.group("col")))
            yield m.group("title"), m.group("col"), m.group("due")
    lead = _ADD_LEAD.match(t)
    if lead is None:
        return
    rest = t[lead.end():]
    for sep in _INTO.finditer(rest):
        title, spoken_col = rest[:sep.start()], re.sub(r"(?: list| column)$", "", rest[sep.end():])
        if title and spoken_col and (title, spoken_col) not in tried:
            yield title, spoken_col, None


def _added(col: dict, title: str, when: str | None, today: date | None = None) -> str | None:
    title = title.strip()
    if not title:
        return None
    card = add_card(col["id"], title[:1].upper() + title[1:], when)
    if card is None:
        return None
    return f"Added {card['title']} to {col['name']}" + (f", due {_spoken_due(when, today)}." if when else ".")
