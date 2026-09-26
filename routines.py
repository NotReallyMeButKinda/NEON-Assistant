"""
routines.py -- one phrase, several things: "work mode", "wind down", "start my morning routine".

A routine is a name and a list of steps. Each step is either a prefixed instruction handled here
or, failing that, an ordinary command run through the assistant's normal router -- so anything you
can say, a routine can do, and a new feature works inside routines the day it lands.

    say: Right, let's get to it        speak a line
    wait: 3                            pause (seconds)
    open: Visual Studio Code           launch an app
    url: https://news.ycombinator.com  open a site
    run: C:\\tools\\backup.bat -quiet    run a program directly
    keys: ctrl+shift+esc               press a key combination (several: "win+d, alt+tab")
    volume 30                          ...anything else goes to the router

Routines live in the config under "routines", so they survive restarts, export and import with
everything else, and can be edited in Settings > Routines.

A routine can also run by itself (its "when"):
    weekdays at 9:00          every day at 22:30         mon, wed and fri at 8am
    weekends at 10            at startup                 when discord starts
`Scheduler` checks the clock and the running programs every few seconds and runs what is due. An app
trigger fires when the program appears (not for programs already open when the assistant starts).
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime

import neon_log

log = neon_log.get("routines")

MAX_STEPS = 40
MAX_WAIT = 120.0          # a single "wait:" step is capped, so a typo can't hang a routine forever
_CREATE_NO_WINDOW = 0x08000000

_RUNNING = threading.Lock()

# Sample routines (the tests use them).
EXAMPLES = [
    {"name": "work mode", "steps": ["open: Visual Studio Code", "url: https://github.com",
                                    "set the volume to 25 percent", "say: Focus time. Go."]},
    {"name": "wind down", "steps": ["pause the music", "set the volume to 10 percent",
                                    "say: That's enough for today."]},
    {"name": "focus", "steps": ["minimize everything", "pause notifications for 1 hour",
                                "set a 25 minute timer", "say: Twenty-five minutes. Nothing else exists."]},
]


def normalize(raw) -> list[dict]:
    """Whatever is in the config -> a clean list of {"name", "steps"} (bad entries dropped)."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name", "")).split()).strip().lower()
        if not name or name in seen:
            continue
        steps_raw = item.get("steps", [])
        if isinstance(steps_raw, str):
            steps_raw = [s for s in steps_raw.splitlines()]
        steps = [" ".join(str(s).split()) for s in (steps_raw if isinstance(steps_raw, list) else [])]
        steps = [s for s in steps if s][:MAX_STEPS]
        if not steps:
            continue
        seen.add(name)
        routine = {"name": name, "steps": steps}
        when = " ".join(str(item.get("when", "") or "").split())
        if when:
            routine["when"] = when
        out.append(routine)
    return out


def names(config: dict) -> list[str]:
    return [r["name"] for r in normalize(config.get("routines"))]


def find(config: dict, name: str) -> dict | None:
    wanted = " ".join(str(name).split()).lower()
    return next((r for r in normalize(config.get("routines")) if r["name"] == wanted), None)


# ---------------------------------------------------------------------------
# Understanding what was said
# ---------------------------------------------------------------------------

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you|go ahead and)\s+)*"
_RUN = re.compile(
    rf"^{_LEAD}(?:run|start|begin|do|activate|trigger|launch|execute|enter|engage|switch to|go into)"
    r"\s+(?:my |the |a )?(?P<name>.+?)(?P<tail>\s+(?:routine|mode|macro|scene))?[.!]?$")
_BARE = re.compile(rf"^{_LEAD}(?P<name>.+?)(?P<tail>\s+(?:routine|mode|macro|scene))?[.!]?$")
_LIST = re.compile(
    rf"^{_LEAD}(?:what|which)\s+routines?(?:\s+(?:do (?:i|you) have|are there|can i run))?[?.!]?$"
    rf"|^{_LEAD}list (?:my |the )?routines?[?.!]?$")


def _candidates(text: str):
    t = re.sub(r"[.!?,]", " ", str(text).lower())
    t = re.sub(r"\s+(?:please|now|for me)$", "", " ".join(t.split()))
    if not t:
        return
    for pattern in (_RUN, _BARE):
        m = pattern.match(t)
        if not m:
            continue
        name = " ".join(m.group("name").split())
        yield name
        if m.group("tail"):     # a routine may itself be called "work mode"
            yield f"{name}{m.group('tail')}".strip()
    yield t


def spoken_routine(text: str, available) -> str | None:
    """The routine `text` is asking for, or None. Names are matched whole, longest first, so a
    routine called "work" never swallows "work mode"."""
    known = sorted({" ".join(str(n).split()).lower() for n in available if str(n).strip()},
                   key=len, reverse=True)
    if not known:
        return None
    if _LIST.match(" ".join(str(text).lower().split()).strip(".?!")):
        return None
    for candidate in _candidates(text):
        for name in known:
            if candidate == name or candidate == f"{name} mode" or candidate == f"{name} routine":
                return name
    return None


def spoken_routine_list(text: str) -> bool:
    t = " ".join(str(text).lower().split()).strip(".?!")
    return bool(_LIST.match(t))


def catalogue(config: dict) -> str:
    known = names(config)
    if not known:
        return ("You don't have any routines yet. Add one in Settings under Routines -- "
                "a name like \"work mode\" and the steps to run.")
    return f"You have {len(known)}: " + ", ".join(known) + ". Say \"run\" and the name."


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

_STEP = re.compile(r"^(?P<verb>say|wait|sleep|pause for|open|url|site|web|run|exec|shell|keys|key|press|hotkey|"
                   r"shortcut)\s*[:=]\s*(?P<rest>.+)$", re.I)


def parse_step(step: str) -> tuple[str, str]:
    """('say' | 'wait' | 'open' | 'url' | 'run' | 'keys' | 'command', argument)."""
    m = _STEP.match(step.strip())
    if not m:
        return "command", step.strip()
    verb, rest = m.group("verb").lower(), m.group("rest").strip()
    if verb in ("wait", "sleep", "pause for"):
        return "wait", rest
    if verb in ("url", "site", "web"):
        return "url", rest
    if verb in ("run", "exec", "shell"):
        return "run", rest
    if verb in ("keys", "key", "press", "hotkey", "shortcut"):
        return "keys", rest
    return verb, rest


def check_step(step: str) -> str | None:
    """What's wrong with a step, in words for the user, or None. Only catches what can be known
    before running it (a key that doesn't exist, a combination Windows won't allow)."""
    kind, argument = parse_step(step)
    if kind == "keys":
        import dictation
        try:
            dictation.parse_keys(argument)
        except ValueError as exc:
            return str(exc)
    return None


def run(routine: dict, *, run_command, say=None, open_app=None, open_url=None, press_keys=None,
        should_stop=None) -> str:
    """Execute a routine's steps in order and return the line to speak afterwards.

    `run_command(text) -> str` is the assistant's ordinary router; the other callbacks let the
    caller decide how an app or a URL is opened and how keys are pressed (without `press_keys`, a
    keys: step is skipped rather than handed to the router as a command). Any step that raises is logged and skipped so one
    bad line doesn't strand the rest of the routine."""
    spoken: list[str] = []
    done = 0
    with _RUNNING:
        for step in routine.get("steps", [])[:MAX_STEPS]:
            if should_stop is not None and should_stop():
                break
            kind, argument = parse_step(step)
            try:
                if kind == "say":
                    spoken.append(argument)
                    if say is not None:
                        say(argument)
                elif kind == "wait":
                    time.sleep(max(0.0, min(MAX_WAIT, _seconds(argument))))
                elif kind == "open" and open_app is not None:
                    open_app(argument)
                elif kind == "url" and open_url is not None:
                    open_url(argument)
                elif kind == "run":
                    subprocess.Popen(argument, shell=True, creationflags=_CREATE_NO_WINDOW)
                elif kind == "keys":
                    if press_keys is None:
                        raise RuntimeError("key presses aren't available here")
                    press_keys(argument)
                else:
                    run_command(argument)
                done += 1
            except Exception as exc:  # noqa: BLE001 -- one bad step must not abandon the routine
                log.warning("routine %r step %r failed: %s", routine.get("name"), step, exc)
    if spoken:
        return " ".join(spoken)
    return f"{routine.get('name', 'Routine').capitalize()}: {done} step{'s' if done != 1 else ''} done."


# ---------------------------------------------------------------------------
# Running by themselves: "weekdays at 9:00", "when discord starts", "at startup"
# ---------------------------------------------------------------------------

_DAY_NAMES = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3,
              "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6,
              "sunday": 6}
_DAY_GROUPS = {"every day": set(range(7)), "daily": set(range(7)), "each day": set(range(7)),
               "everyday": set(range(7)), "weekdays": set(range(5)), "every weekday": set(range(5)),
               "on weekdays": set(range(5)), "weekends": {5, 6}, "every weekend": {5, 6}, "on weekends": {5, 6}}
_WORD_HOURS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
               "ten": 10, "eleven": 11, "twelve": 12, "noon": 12, "midnight": 0}
_APP_TRIGGER = re.compile(r"^(?:when(?:ever)?|once)\s+(?:i (?:open|start|launch)\s+)?(?P<app>[\w .+-]+?)"
                          r"(?:\s+(?:starts|opens|launches|is opened|is started|runs))?$")


def _clock(text: str) -> tuple[int, int] | None:
    t = text.strip().lower().replace(".", "")
    if t in ("noon", "midnight"):
        return _WORD_HOURS[t], 0
    m = re.fullmatch(r"(\d{1,2}|" + "|".join(k for k in _WORD_HOURS if k not in ("noon", "midnight")) +
                     r")(?::(\d{2}))?\s*(am|pm)?(?:\s*o'?clock)?", t)
    if not m:
        return None
    hour = int(m.group(1)) if m.group(1).isdigit() else _WORD_HOURS[m.group(1)]
    minute = int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    elif m.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_trigger(text: str) -> dict | None:
    """{'kind': 'time', 'days': {0..6}, 'hour', 'minute'} | {'kind': 'app', 'app'} | {'kind': 'startup'} | None."""
    t = " ".join(str(text or "").lower().replace(",", " ").split())
    if not t:
        return None
    if t in ("at startup", "on startup", "when you start", "when i log in", "at login", "on login", "startup"):
        return {"kind": "startup"}
    m = _APP_TRIGGER.match(t)
    if m and " at " not in f" {t} ":
        app = m.group("app").strip()
        app = app[:-4] if app.endswith(".exe") else app
        if app and app not in ("i", "you"):
            return {"kind": "app", "app": app}
    m = re.fullmatch(r"(?:(?P<days>.+?)\s+)?(?:at|@)\s+(?P<clock>.+)", t)
    if not m:
        return None
    clock = _clock(m.group("clock"))
    if clock is None:
        return None
    days_text = (m.group("days") or "every day").strip()
    days = _DAY_GROUPS.get(days_text)
    if days is None:
        words = [w for w in re.split(r"\s+|/|&", days_text.removeprefix("every ").removeprefix("on "))
                 if w and w not in ("and", "every", "on")]
        days = {_DAY_NAMES[w.rstrip("s")] if w.rstrip("s") in _DAY_NAMES else _DAY_NAMES.get(w, -1) for w in words}
        if not days or -1 in days:
            return None
    return {"kind": "time", "days": set(days), "hour": clock[0], "minute": clock[1]}


def describe_trigger(trigger: dict | None) -> str:
    if not trigger:
        return ""
    if trigger["kind"] == "startup":
        return "when I start"
    if trigger["kind"] == "app":
        return f"when {trigger['app']} starts"
    days = trigger["days"]
    named = {frozenset(range(7)): "every day", frozenset(range(5)): "on weekdays", frozenset({5, 6}): "on weekends"}
    when = named.get(frozenset(days)) or "on " + ", ".join(
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d] for d in sorted(days))
    return f"{when} at {trigger['hour']:02d}:{trigger['minute']:02d}"


class Scheduler:
    """Runs routines whose "when" is due. `get_config()` returns the live config; `run_routine(name)` runs
    one (on its own thread -- the scheduler never waits for it). `processes()` lists running programs."""

    POLL = 5.0

    def __init__(self, get_config, run_routine, processes=None, now=None):
        self._config = get_config
        self._run = run_routine
        self._processes = processes
        self._now = now or datetime.now
        self._fired: dict[str, str] = {}          # routine name -> the minute it last ran ("2026-09-23 09:00")
        self._seen_apps: set[str] | None = None   # programs running at the previous check
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="Nova-Routines", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 -- a bad routine must not stop the others for good
                log.warning("routine scheduler: %s", exc)
            self._stop.wait(self.POLL)

    def check(self) -> list[str]:
        """Runs whatever is due now; returns the names started (for tests)."""
        started: list[str] = []
        items = [(r, parse_trigger(r.get("when", ""))) for r in normalize(self._config().get("routines"))]
        items = [(r, t) for r, t in items if t]
        now = self._now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        first = not self._started
        self._started = True
        for routine, trigger in items:
            name = routine["name"]
            if trigger["kind"] == "startup" and first:
                started.append(name)
            elif (trigger["kind"] == "time" and now.weekday() in trigger["days"]
                  and (now.hour, now.minute) == (trigger["hour"], trigger["minute"]) and self._fired.get(name) != stamp):
                self._fired[name] = stamp
                started.append(name)
        watched = [(r, t) for r, t in items if t["kind"] == "app"]
        if watched and self._processes is not None:
            running = self._processes()
            if self._seen_apps is not None:
                new = running - self._seen_apps
                for routine, trigger in watched:
                    wanted = trigger["app"].replace(" ", "")
                    if any(wanted in name.replace(" ", "") for name in new):
                        started.append(routine["name"])
            self._seen_apps = running
        for name in started:
            log.info("routine %r is due", name)
            self._run(name)
        return started


def _seconds(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    value = float(m.group(1)) if m else 0.0
    if re.search(r"\bmin", str(text), re.I):
        value *= 60
    return value
