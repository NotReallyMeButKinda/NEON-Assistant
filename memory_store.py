"""
memory_store.py -- things the assistant is told to remember, kept between runs.

  "remember that my wifi password is hunter2"   -> a keyed fact
  "remember I parked on level 3"                -> a free-form note
  "what's my wifi password"                     -> recall by key
  "where did I park"                            -> recall by content
  "forget my wifi password" / "forget everything"
  "remember until Friday that my locker is 12"  -> a fact that forgets itself (also "for 2 hours",
                                                   "just for today", "... until tomorrow")

Facts live in one small JSON file next to the settings (`enable_persistence`), so they survive a
restart but never leave the machine. Nothing here talks to the network or the language model; the
router asks `context_for()` for the handful of facts worth putting in front of the model, which is
what makes ordinary chat ("what should I cook?") aware of "I'm vegetarian".

Design notes
  * Matching is token overlap, not exact text: "wifi password" finds "the wi-fi password", and a
    question's filler words ("what is my ...") are stripped before matching.
  * A recall needs a *real* overlap (`MIN_SCORE`), so "what's the weather" never answers from memory.
  * Writes are atomic (write-then-rename) and guarded by a lock, like the config.
  * Optional meaning-based recall: when word overlap finds nothing, an embedding function (set by the
    app with `set_embedder`, backed by a local Ollama embedding model) compares meanings, so "how do I
    get online" can find "the wifi password is ...". Vectors are cached in the file per model.
  * Each memory has a stable `id`, so the Settings browser edits or forgets exactly the row you picked.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

MAX_MEMORIES = 200          # oldest untouched facts fall off the end
MAX_VALUE_CHARS = 400
MIN_SCORE = 0.34            # below this a "recall" is treated as "I don't know that"
CONTEXT_LIMIT = 4           # how many facts are handed to the language model at once

_LOCK = threading.RLock()
_MEMORIES: list[dict] = []          # {"id", "key", "value", "created", "used", "expires"}; newest last
_STORE = {"path": None, "loaded": False}
# Meaning-based recall: fn(list of texts) -> list of vectors (or None when unavailable), and its name.
_EMBED = {"fn": None, "model": "", "threshold": 0.62}

# Words that carry no meaning when matching a topic ("what is *my* wifi *password*" -> wifi password).
_FILLER = {
    "a", "an", "the", "my", "mine", "our", "your", "his", "her", "their", "its", "is", "are", "was",
    "were", "be", "am", "do", "does", "did", "i", "me", "we", "you", "it", "that", "this", "these",
    "those", "what", "whats", "which", "who", "whose", "where", "when", "why", "how", "again",
    "please", "tell", "say", "remind", "remember", "recall", "know", "about", "of", "for", "to",
    "in", "on", "at", "with", "and", "or", "s", "have", "has", "had", "get", "got", "there",
}
_WORD = re.compile(r"[a-z0-9']+")


def _stem(word: str) -> str:
    """A very small suffix stripper, just enough that 'where did I park' finds 'I parked...'.
    Not linguistics -- only the endings that would otherwise make an obvious recall miss."""
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 4 and word.endswith("es"):
        return word[:-2] if word[-3] in "sxzho" else word[:-1]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Meaningful lowercase word stems, with hyphens flattened so 'wi-fi' == 'wifi'."""
    flat = re.sub(r"[-_/]", "", str(text).lower())
    return {_stem(w) for w in _WORD.findall(flat) if w not in _FILLER and len(w) > 1}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def enable_persistence(path) -> None:
    """Keep memories in `path` and load whatever is already there."""
    with _LOCK:
        _STORE["path"] = Path(path)
        _STORE["loaded"] = False
        _load()


def _load() -> None:
    path = _STORE["path"]
    if path is None or _STORE["loaded"]:
        return
    _STORE["loaded"] = True
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    _MEMORIES.clear()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not str(row.get("value", "")).strip():
            continue
        item = {"id": str(row.get("id") or _new_id()),
                "key": str(row.get("key", "")).strip(),
                "value": str(row["value"]).strip()[:MAX_VALUE_CHARS],
                "created": float(row.get("created", 0) or 0),
                "used": float(row.get("used", 0) or 0),
                "expires": float(row.get("expires", 0) or 0)}
        if isinstance(row.get("vec"), list) and row.get("vec_model"):
            item["vec"], item["vec_model"] = row["vec"], str(row["vec_model"])
        _MEMORIES.append(item)
    del _MEMORIES[:-MAX_MEMORIES]
    _prune()


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _prune(now: float | None = None) -> bool:
    """Drop memories whose time is up. True if any were dropped."""
    now = time.time() if now is None else now
    before = len(_MEMORIES)
    _MEMORIES[:] = [m for m in _MEMORIES if not (m.get("expires") and m["expires"] <= now)]
    return len(_MEMORIES) != before


def _save() -> None:
    path = _STORE["path"]
    if path is None:
        return
    try:
        if _MEMORIES:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(_MEMORIES, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass                    # a fact that isn't written down is still remembered this session


def reset() -> None:
    """Drop everything, in memory and on disk (used by the tests and 'forget everything')."""
    with _LOCK:
        _MEMORIES.clear()
        _save()


# ---------------------------------------------------------------------------
# The facts themselves
# ---------------------------------------------------------------------------

def remember(key: str, value: str, expires: float = 0.0) -> dict:
    """Store (or overwrite) one fact. `key` may be '' for a free-form note; `expires` is a Unix time
    after which it is forgotten (0 = keep it)."""
    key, value = str(key).strip(" .,:;"), str(value).strip()[:MAX_VALUE_CHARS]
    with _LOCK:
        _load()
        existing = _exact(key) if key else None
        if existing is not None:
            existing.update(value=value, created=time.time(), expires=float(expires or 0))
            existing.pop("vec", None)
            item = existing
        else:
            item = {"id": _new_id(), "key": key, "value": value, "created": time.time(), "used": 0.0,
                    "expires": float(expires or 0)}
            _MEMORIES.append(item)
            del _MEMORIES[:-MAX_MEMORIES]
        _save()
        return _public(item)


def update(item_id: str, key: str | None = None, value: str | None = None, expires: float | None = None) -> bool:
    """Edit one memory in place (the Settings browser). False if it no longer exists."""
    with _LOCK:
        _load()
        item = next((m for m in _MEMORIES if m["id"] == item_id), None)
        if item is None:
            return False
        if key is not None:
            item["key"] = str(key).strip(" .,:;")
        if value is not None and str(value).strip():
            item["value"] = str(value).strip()[:MAX_VALUE_CHARS]
        if expires is not None:
            item["expires"] = float(expires or 0)
        if key is not None or value is not None:
            item.pop("vec", None)
        _save()
        return True


def forget_id(item_id: str) -> bool:
    with _LOCK:
        _load()
        before = len(_MEMORIES)
        _MEMORIES[:] = [m for m in _MEMORIES if m["id"] != item_id]
        if len(_MEMORIES) != before:
            _save()
            return True
        return False


def _public(item: dict) -> dict:
    """A copy without the cached vector (nobody outside needs 768 floats)."""
    return {k: v for k, v in item.items() if k not in ("vec", "vec_model")}


def _exact(key: str) -> dict | None:
    wanted = _tokens(key)
    return next((m for m in _MEMORIES if m["key"] and _tokens(m["key"]) == wanted), None)


def _score(query_tokens: set[str], item: dict) -> float:
    """How well a memory answers `query_tokens`: overlap with the key counts double, because
    'password' matching the *topic* is a much stronger signal than matching the text."""
    if not query_tokens:
        return 0.0
    key_tokens, value_tokens = _tokens(item["key"]), _tokens(item["value"])
    haystack = key_tokens | value_tokens
    if not haystack:
        return 0.0
    hits = query_tokens & haystack
    if not hits:
        return 0.0
    overlap = len(hits) / len(query_tokens)
    return min(1.0, overlap + 0.35 * (len(query_tokens & key_tokens) / len(query_tokens)))


def search(query: str, limit: int = 3, min_score: float = MIN_SCORE, semantic: bool = True) -> list[dict]:
    """The best-matching memories for `query`, strongest first. Word overlap first; if that finds
    nothing and an embedder is set, by meaning."""
    with _LOCK:
        _load()
        if _prune():
            _save()
        tokens = _tokens(query)
        scored = [(_score(tokens, m), m) for m in _MEMORIES]
        hits = sorted(((s, m) for s, m in scored if s >= min_score), key=lambda p: -p[0])[:limit]
    if not hits and semantic:
        hits = _semantic(query, limit)
    with _LOCK:
        for _s, m in hits:
            m["used"] = time.time()
        return [_public(m) for _s, m in hits]


# ---- meaning-based recall -----------------------------------------------------------------

def set_embedder(fn, model: str = "", threshold: float = 0.62) -> None:
    """fn(texts) -> list of vectors, or None when it can't (Ollama down). None turns it off."""
    _EMBED.update(fn=fn, model=str(model), threshold=float(threshold))


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _semantic(query: str, limit: int) -> list[tuple[float, dict]]:
    fn, model = _EMBED["fn"], _EMBED["model"]
    if fn is None or not str(query).strip():
        return []
    with _LOCK:
        items = list(_MEMORIES)
    if not items:
        return []
    missing = [m for m in items if m.get("vec_model") != model or not m.get("vec")]
    try:
        if missing:
            vectors = fn([describe(m) for m in missing])
            if not vectors or len(vectors) != len(missing):
                return []
            with _LOCK:
                for m, vec in zip(missing, vectors):
                    m["vec"], m["vec_model"] = [round(float(x), 5) for x in vec], model
                _save()
        wanted = fn([str(query)])
    except Exception:  # noqa: BLE001 -- the embedder is best-effort: no answer is better than a crash
        return []
    if not wanted:
        return []
    scored = sorted(((_cosine(wanted[0], m["vec"]), m) for m in items if m.get("vec")), key=lambda p: -p[0])
    return [(s, m) for s, m in scored if s >= _EMBED["threshold"]][:limit]


def all_memories() -> list[dict]:
    with _LOCK:
        _load()
        if _prune():
            _save()
        return [_public(m) for m in _MEMORIES]


def count() -> int:
    with _LOCK:
        _load()
        _prune()
        return len(_MEMORIES)


def forget(query: str) -> list[dict]:
    """Remove every memory matching `query`; returns what was removed."""
    with _LOCK:
        _load()
        tokens = _tokens(query)
        gone = [m for m in _MEMORIES if _score(tokens, m) >= MIN_SCORE]
        for m in gone:
            _MEMORIES.remove(m)
        if gone:
            _save()
        return [_public(m) for m in gone]


def describe(item: dict) -> str:
    """One memory as a sentence to speak back."""
    key, value = item.get("key", ""), item.get("value", "")
    if not key:
        return value
    if re.match(r"(?i)^(?:it|that|they|he|she)\b", value) or value.lower().startswith(key.lower()):
        return value
    return f"Your {key} is {value}." if not key.lower().startswith(("i ", "you ")) else f"{key}: {value}"


# Facts the user stated about *themselves* colour every answer, not just questions that happen to
# share a word with them ("I'm vegetarian" should reach "what should I cook?"), so they always go
# to the model. Anything else has to earn its place by matching the question.
_TRAIT = re.compile(
    r"^(?:i'?m|i am|i'?ve|i have|i like|i love|i hate|i prefer|i don'?t|i do not|i can'?t|i cannot|"
    r"i work|i live|i use|i always|i never|i usually|i'?m allergic|my name is|call me)\b", re.I)
TRAIT_LIMIT = 3


def traits(limit: int = TRAIT_LIMIT) -> list[dict]:
    """Standing facts about the user ('I'm vegetarian'), newest first."""
    with _LOCK:
        _load()
        return [_public(m) for m in reversed(_MEMORIES) if _TRAIT.match(m["value"])][:limit]


def context_for(text: str, limit: int = CONTEXT_LIMIT) -> str:
    """Facts worth showing the language model for this utterance, as plain lines ('' if none).

    Deliberately lenient about topical matches (a lower bar than `search`), because an irrelevant
    line costs the model almost nothing while a missing one makes the assistant look forgetful."""
    lines, seen = [], set()
    for item in traits() + search(text, limit=limit, min_score=MIN_SCORE * 0.6):
        line = describe(item)
        if line not in seen:
            seen.add(line)
            lines.append(f"- {line}")
    return "\n".join(lines[:limit + TRAIT_LIMIT])


# ---------------------------------------------------------------------------
# Understanding what was said
# ---------------------------------------------------------------------------

_LEAD = r"(?:(?:please|hey|ok|okay|so|and|can you|could you|would you|go ahead and)\s+)*"
_STORE_VERB = r"(?:remember|note|keep in mind|don't forget|do not forget|make a note|take note|memori[sz]e)"

# "remember that my wifi password is hunter2"  ->  key "wifi password", value "hunter2"
_KEYED = re.compile(
    rf"^{_LEAD}{_STORE_VERB}(?:\s+(?:that|this))?\s+"
    r"(?:that\s+)?(?:my|our|the)\s+(?P<key>[a-z0-9'’\- ]{2,40}?)\s+"
    r"(?:is|are|was|were|=|equals)\s+(?P<value>.+)$")
# "remember I parked on level 3", "note that the meeting moved to Thursday"
_FREEFORM = re.compile(rf"^{_LEAD}{_STORE_VERB}(?:\s+(?:that|this))?[,:]?\s+(?P<value>.+)$")
# "what's my wifi password", "what is the wifi password"
_RECALL_KEYED = re.compile(
    r"^" + _LEAD + r"what(?:'s| is| was| are| were)?\s+(?:my|our|the)\s+(?P<key>.+?)"
    r"(?:\s+again)?[?]?$")
# "what do you remember about the meeting", "do you remember my wifi password".
# The "do you / can you" lead-in is required: a bare "remember ..." is *storing* something.
_RECALL_ABOUT = re.compile(
    rf"^{_LEAD}(?:do|did|can|could|would) you (?:remember|recall|know)(?:\s+(?:what|anything|something))?"
    r"(?:\s+about)?\s+(?:my|our|the)?\s*(?P<key>.+?)[?]?$")
# "what do you remember about the meeting", "what did I tell you about the car"
_RECALL_WHAT_ABOUT = re.compile(
    rf"^{_LEAD}what (?:do you (?:remember|know)|did i (?:say|tell you))\s+about\s+"
    r"(?:my|our|the)?\s*(?P<key>.+?)[?]?$")
# "where did I park", "when is my dentist appointment"
_RECALL_WH = re.compile(
    rf"^{_LEAD}(?:where|when|who|which|how much|how many)\s+(?:did|do|does|is|was|are|were)\s+"
    r"(?:i|my|the|our|we)\s+(?P<key>.+?)[?]?$")
_FORGET_ALL = re.compile(
    rf"^{_LEAD}(?:forget|erase|wipe|clear|delete)\s+(?:everything|it all|all of it|all your memories|"
    r"your memories|all memories|what you know(?: about me)?|everything you know(?: about me)?)[.!?]?$")
_FORGET = re.compile(
    rf"^{_LEAD}(?:forget|erase|delete|drop)\s+(?:about\s+)?(?:my|our|the|that|what i (?:said|told you) about)?\s*"
    r"(?P<key>.+?)[.!?]?$")
_LIST = re.compile(
    rf"^{_LEAD}(?:what|which things?)\s+do you (?:remember|know)(?:\s+about me)?[?]?$|"
    rf"^{_LEAD}(?:list|show me|read me|tell me)\s+(?:all\s+)?(?:your |the |my )?(?:memories|notes|facts)"
    r"(?:\s+about me)?[?]?$|"
    rf"^{_LEAD}what(?:'s| is)? in your memory[?]?$")

# Things that look like a memory command but belong to another feature.
_NOT_A_MEMORY = re.compile(
    r"^(?:remind me\b|remember to (?:remind|wake|set a timer)|forget (?:it|that)[.!]?$)")
# Topics another feature answers better than a stored fact, so a question about them never
# becomes a (failing) memory lookup first.
_OTHER_FEATURE = re.compile(
    r"^(?:weather|forecast|temperature|time|date|day|news|battery|cpu|ram|memory usage|volume|"
    r"ip address|timers?|reminders?|notifications?|song|music|persona|personality)\b")
# A "remember to ..." that is really a reminder with a time attached is left to timers.py.
_HAS_TIME = re.compile(r"\b(?:in|after|at)\s+(?:\d+|a|an|half|one|two|three|four|five|six|seven|eight|nine|ten)\b"
                       r"|\btomorrow\b|\btonight\b|\bnext (?:week|month|monday|tuesday|wednesday|thursday|friday)\b")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().strip(".!")


# ---- "until Friday", "for two hours", "just for today" ------------------------------------------

_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "a few": 3}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_UNIT = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400}
_COUNT = r"(?:\d+|a couple of|a few|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_WHEN = (r"(?:the (?:rest of the )?day|today|tonight|this evening|tomorrow(?: (?:morning|night|evening))?|"
         r"the weekend|this week|the week|next week|(?:next |this )?(?:" + "|".join(_WEEKDAYS) + r")|"
         r"half an hour|" + _COUNT + r" (?:minutes?|mins?|hours?|hrs?|days?|weeks?)|"
         r"noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)")
_EXPIRY_LEAD = re.compile(rf"^(?P<head>{_LEAD}{_STORE_VERB})\s+(?:(?:just|only)\s+)?(?P<how>until|till|til|for)\s+"
                          rf"(?P<when>{_WHEN})\s*,?\s+(?:that\s+)?(?P<rest>.+)$", re.I)
# At the end of a sentence "for two hours" is usually part of the fact ("the meeting is for two hours"),
# so a trailing limit needs "until ...", "just / only for ...", or "for today / the day / this week".
_EXPIRY_TAIL = re.compile(
    rf"^(?P<body>.+?),?\s+(?:(?P<how>until|till|til)\s+(?P<when>{_WHEN})|(?:just|only)\s+(?P<how2>for)\s+(?P<when2>{_WHEN})|"
    r"(?P<how3>for)\s+(?P<when3>today|tonight|the day|the rest of the day|this week|the week|the weekend))$", re.I)


def _end_of(day: datetime) -> datetime:
    return day.replace(hour=23, minute=59, second=59, microsecond=0)


def expiry_time(how: str, when: str, now: datetime | None = None) -> float:
    """Unix time for 'until friday' / 'for 2 hours' / 'for today' (0 if it can't be understood)."""
    now = now or datetime.now()
    when = when.strip().lower().replace(".", "")
    m = re.fullmatch(rf"({_COUNT}) (minute|min|hour|hr|day|week)s?", when)
    if m:
        amount = int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1)]
        return (now + timedelta(seconds=amount * _UNIT[m.group(2)])).timestamp()
    if when == "half an hour":
        return (now + timedelta(minutes=30)).timestamp()
    if when in ("today", "tonight", "this evening", "the day", "the rest of the day"):
        return _end_of(now).timestamp()
    if when.startswith("tomorrow"):
        return _end_of(now + timedelta(days=1)).timestamp()
    if when in ("this week", "the week"):
        return _end_of(now + timedelta(days=6 - now.weekday())).timestamp()
    if when == "next week":
        return _end_of(now + timedelta(days=13 - now.weekday())).timestamp()
    if when == "the weekend":
        return _end_of(now + timedelta(days=(6 - now.weekday()) % 7)).timestamp()
    day = when.removeprefix("next ").removeprefix("this ")
    if day in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(day) - now.weekday()) % 7 + (7 if when.startswith("next ") and
                                                               _WEEKDAYS.index(day) <= now.weekday() else 0)
        return _end_of(now + timedelta(days=ahead)).timestamp()
    if when in ("noon", "midnight"):
        at = now.replace(hour=12 if when == "noon" else 0, minute=0, second=0, microsecond=0)
        at = at if at > now else at + timedelta(days=1)
        return at.timestamp()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", when)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return 0.0
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (at if at > now else at + timedelta(days=1)).timestamp()
    return 0.0


def split_expiry(text: str) -> tuple[str, float]:
    """('remember that my locker is 12', expiry time) from 'remember until friday that my locker is 12'
    or 'remember my locker is 12 until friday'; (text, 0) when there is no expiry clause."""
    m = _EXPIRY_LEAD.match(text)
    if m:
        when = expiry_time(m.group("how"), m.group("when"))
        if when:
            return f"{m.group('head')} that {m.group('rest')}", when
    m = _EXPIRY_TAIL.match(text)
    if m and re.match(rf"^{_LEAD}{_STORE_VERB}\b", m.group("body"), re.I):
        how = m.group("how") or m.group("how2") or m.group("how3")
        when = expiry_time(how, m.group("when") or m.group("when2") or m.group("when3"))
        if when:
            return m.group("body"), when
    return text, 0.0


def describe_expiry(expires: float, now: datetime | None = None) -> str:
    """'until 5:30 PM', 'until Friday', 'until Sep 30' -- for replies and the Settings browser."""
    if not expires:
        return ""
    now = now or datetime.now()
    at = datetime.fromtimestamp(expires)
    clock = at.strftime("%I:%M %p").lstrip("0")
    if at.date() == now.date():
        return "until the end of today" if at.hour == 23 and at.minute == 59 else f"until {clock}"
    if at.date() == (now + timedelta(days=1)).date():
        return "until the end of tomorrow" if at.hour == 23 and at.minute == 59 else f"until tomorrow {clock}"
    if (at.date() - now.date()).days < 7:
        return f"until {at:%A}"
    return f"until {at:%b} {at.day}"


def spoken_memory_command(text: str):
    """Returns ('remember', key, value) | ('recall', query, '') | ('forget', query, '')
    | ('forget_all', '', '') | ('list', '', '') | None. See `spoken_memory_expiry` for the time limit."""
    return _parse_memory_command(text)[0]


def spoken_memory_expiry(text: str) -> float:
    """The expiry time a 'remember ...' command asked for (0 = none)."""
    return _parse_memory_command(text)[1]


def _parse_memory_command(text: str):
    raw = _clean(text)
    stripped, expires = split_expiry(raw)
    command = _parse_plain(stripped if expires else raw)
    if command is None or command[0] != "remember":
        expires = 0.0
    return command, expires


def _parse_plain(raw: str):
    t = raw.lower()
    if not t or _NOT_A_MEMORY.match(t):
        return None

    if _FORGET_ALL.match(t):
        return "forget_all", "", ""
    if _LIST.match(t):
        return "list", "", ""

    m = _KEYED.match(t)
    if m and not _HAS_TIME.search(m.group("value")):
        key, value = _clean(m.group("key")), _clean(m.group("value"))
        if key and value:
            return "remember", key, _original_case(raw, value)

    m = _FREEFORM.match(t)
    if m:
        value = _clean(m.group("value"))
        # "remember to call mum in an hour" is a reminder; timers.py owns those.
        if value and not (value.startswith("to ") and _HAS_TIME.search(value)):
            return "remember", "", _original_case(raw, value)

    m = _FORGET.match(t)
    if m:
        key = _clean(m.group("key"))
        if key and len(key.split()) <= 8:
            return "forget", key, ""

    for pattern in (_RECALL_WHAT_ABOUT, _RECALL_ABOUT, _RECALL_KEYED, _RECALL_WH):
        m = pattern.match(t)
        if m:
            key = _clean(m.group("key"))
            if key and len(key.split()) <= 8 and not _OTHER_FEATURE.search(key):
                return "recall", key, ""
    return None


def _original_case(raw: str, lowered: str) -> str:
    """Give back the user's own capitalisation for a fragment we matched in lowercase."""
    at = raw.lower().find(lowered)
    return raw[at:at + len(lowered)] if at >= 0 else lowered


def handle_memory_command(text: str) -> str | None:
    """Runs a memory command and returns what to say, or None if this wasn't one."""
    command, expires = _parse_memory_command(text)
    if command is None:
        return None
    action, key, value = command

    if action == "remember":
        remember(key, value, expires)
        until = f", {describe_expiry(expires)}" if expires else ""
        return f"Got it -- your {key} is {value}{until}." if key else f"Noted{until}: {value}."

    if action == "list":
        items = all_memories()
        if not items:
            return "I'm not remembering anything for you yet. Say \"remember that ...\" and I will."
        recent = items[-6:][::-1]
        lines = "; ".join(describe(m).rstrip(".") for m in recent)
        extra = f" (and {len(items) - len(recent)} more)" if len(items) > len(recent) else ""
        return f"I remember {len(items)} thing{'s' if len(items) != 1 else ''}: {lines}{extra}."

    if action == "forget_all":
        n = count()
        reset()
        return f"Forgotten all {n} of them." if n else "There was nothing to forget."

    if action == "forget":
        gone = forget(key)
        if not gone:
            return None            # nothing matched: let the rest of the router have a go
        what = ", ".join(m["key"] or m["value"][:40] for m in gone)
        return f"Forgotten: {what}."

    hits = search(key)
    if not hits:
        return None                # not something I was told: fall through to search / chat
    if len(hits) == 1:
        return describe(hits[0])
    return " ".join(describe(h) for h in hits[:2])
