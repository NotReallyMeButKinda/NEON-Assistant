"""
lookup_cache.py -- things fetched from the web that are worth reusing: the answers to recent fact questions,
the search results behind them, and where places are. Asking "when did Silksong come out" twice searches
once; the weather for a city you ask about looks the city up once.

Each kind keeps its most recent LIMIT entries (the least recently used goes first) and each entry has an
age limit chosen by whoever reads it. The store is one JSON file beside the settings (`enable_persistence`),
written atomically; failures to read or write only ever mean "not cached". Settings > AI & chat can switch
it off and clear it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

LIMIT = 50                                  # per kind: "the past 50 searches"

_LOCK = threading.RLock()
_STATE: dict = {"data": None, "path": None}


def enable_persistence(path) -> None:
    """Keep the cache in `path` (read lazily)."""
    with _LOCK:
        _STATE.update(path=Path(path) if path else None, data=None)


def _data() -> dict:
    """{kind: {key: {"t": saved_at, "v": value}}}, oldest use first (call with _LOCK held)."""
    if _STATE["data"] is None:
        loaded: dict = {}
        path = _STATE["path"]
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("kinds"), dict):
                    loaded = {k: v for k, v in raw["kinds"].items() if isinstance(v, dict)}
            except (OSError, ValueError):
                loaded = {}
        _STATE["data"] = loaded
    return _STATE["data"]


def _save() -> None:
    path = _STATE["path"]
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "kinds": _STATE["data"]}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def normalize(text: str) -> str:
    """The key for something said or typed: case, punctuation and filler spaces don't make it different."""
    t = re.sub(r"[^\w\s]", " ", str(text).lower())
    return " ".join(t.split())


def get(kind: str, key: str, max_age: float):
    """The value saved under `key`, if it's younger than `max_age` seconds (else None)."""
    with _LOCK:
        bucket = _data().get(kind) or {}
        entry = bucket.get(key)
        if not isinstance(entry, dict) or time.time() - float(entry.get("t", 0)) > max_age:
            return None
        bucket[key] = bucket.pop(key)             # most recently used goes last
        return entry.get("v")


def put(kind: str, key: str, value) -> None:
    """Save `value` (anything JSON can hold) under `key`, keeping only the newest LIMIT of this kind."""
    with _LOCK:
        bucket = _data().setdefault(kind, {})
        bucket.pop(key, None)
        bucket[key] = {"t": time.time(), "v": value}
        while len(bucket) > LIMIT:
            bucket.pop(next(iter(bucket)))
        _save()


def count() -> int:
    with _LOCK:
        return sum(len(b) for b in _data().values())


def clear() -> None:
    with _LOCK:
        _STATE["data"] = {}
        _save()
