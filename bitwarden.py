"""
bitwarden.py -- "what's my username for Proton Mail", "copy my Proton password", "my GitHub 2FA code",
through the official Bitwarden command-line tool (`bw`, `winget install Bitwarden.CLI`).

How secrets are handled:
- You log in to `bw` once yourself (`bw login`, in a terminal). NEON never sees that.
- Unlocking asks for the master password in NEON's own password box (never by voice). It reaches `bw`
  through the child process's environment, not its command line (which other programs can read).
- The session key `bw` hands back lives in this process's memory only, is passed to `bw` the same way,
  and is dropped when the vault locks again: after `bitwarden_lock_minutes` without use, on "lock
  Bitwarden", and when NEON exits.
- Before asking for the master password, `bw list items` is tried as it is: when `bw` is already unlocked
  for this user (a BW_SESSION set in the Windows environment, say), the vault is used without a password.
  Such a session belongs to the user, so NEON never locks it: it only forgets the item list.
- On unlock the vault's item list is read once; only names, addresses and usernames are kept. A password
  or 2FA code is fetched from `bw` at the moment it's asked for and handed straight to the clipboard
  (cleared after 30 s, kept out of the clipboard history) or typed after you say yes. Passwords are
  never spoken, shown or logged.
"""

from __future__ import annotations

import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

import neon_log

log = neon_log.get("bitwarden")

CREATE_NO_WINDOW = 0x08000000
CALL_TIMEOUT = 40.0


@dataclass
class Item:
    id: str
    name: str
    username: str = ""
    uris: list[str] = field(default_factory=list)
    has_totp: bool = False

    @property
    def hosts(self) -> list[str]:
        out = []
        for uri in self.uris:
            m = re.match(r"(?:[a-z]+://)?(?:www\.)?([^/:?#]+)", uri.strip().lower())
            if m:
                out.append(m.group(1))
        return out


_STATE = {"session": "", "items": [], "timer": None, "cli": "", "pending": "", "choice": None}
# The "session" when bw works without one from us: use the environment as it is (see open_without_password).
AMBIENT = "ambient"
_LOCK = threading.RLock()
# Set by the controller: "unlock"() opens the password box; "status"(text) shows a status line.
HOOKS = {"unlock": None}
_CFG = {"config": {}}


def configure(config: dict) -> None:
    """Give the module the live config (bitwarden_cli, bitwarden_lock_minutes...)."""
    _CFG["config"] = config


def _cfg(key: str, default):
    value = _CFG["config"].get(key, default)
    return default if value in (None, "") else value


# ---------------------------------------------------------------------------
# Talking to bw
# ---------------------------------------------------------------------------

def find_cli() -> str:
    """Path to bw.exe ("" if it isn't installed): the configured path, PATH, or winget's own folders."""
    configured = str(_cfg("bitwarden_cli", "")).strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("bw")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for pattern in (os.path.join(local, "Microsoft", "WinGet", "Links", "bw.exe"),
                    os.path.join(local, "Microsoft", "WinGet", "Packages", "Bitwarden.CLI*", "bw.exe"),
                    os.path.join(os.environ.get("APPDATA", ""), "npm", "bw.cmd")):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return ""


class BitwardenError(Exception):
    pass


def _run(args: list[str], env_extra: dict | None = None, timeout: float = CALL_TIMEOUT,
         ambient: bool = False) -> str:
    """Run bw with `args`; returns stdout. The session (if any) and anything secret travel in the
    environment. Raises BitwardenError with bw's own message on failure -- never including stdout,
    which may hold a secret. ambient=True keeps a BW_SESSION the environment already has."""
    cli = find_cli()
    if not cli:
        raise BitwardenError("the Bitwarden command-line tool isn't installed")
    env = dict(os.environ)
    env["BW_NOINTERACTION"] = "true"
    with _LOCK:
        session = _STATE["session"]
    if not (ambient or session == AMBIENT):
        env.pop("BW_SESSION", None)
        if session:
            env["BW_SESSION"] = session
    env.update(env_extra or {})
    try:
        done = subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=timeout, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BitwardenError(f"bw didn't run ({exc.__class__.__name__})") from None
    if done.returncode != 0:
        message = " ".join((done.stderr or "").split())[:200] or f"bw exited with {done.returncode}"
        raise BitwardenError(message)
    return done.stdout


def status() -> str:
    """'missing' | 'logged_out' | 'locked' | 'unlocked'."""
    if not find_cli():
        return "missing"
    try:
        data = json.loads(_run(["status"], timeout=20) or "{}")
    except (BitwardenError, ValueError):
        return "locked" if not _STATE["session"] else "unlocked"
    state = str(data.get("status") or "")
    if state == "unauthenticated":
        return "logged_out"
    return "unlocked" if state == "unlocked" and _STATE["session"] else "locked"


def is_unlocked() -> bool:
    with _LOCK:
        return bool(_STATE["session"])


def unlock(master_password: str) -> tuple[bool, str]:
    """Unlock the vault. (True, 'n items') or (False, why). Blocking: call off the UI thread."""
    if not master_password:
        return False, "No password given."
    try:
        session = _run(["unlock", "--raw", "--passwordenv", "NEON_BW_PW"],
                       env_extra={"NEON_BW_PW": master_password}).strip()
    except BitwardenError as exc:
        text = str(exc)
        if "not logged in" in text.lower():
            return False, "Bitwarden isn't logged in yet. Run `bw login` once in a terminal."
        if "invalid master password" in text.lower():
            return False, "That master password didn't work."
        return False, f"Couldn't unlock: {text}"
    finally:
        master_password = ""                                # noqa: F841 -- drop our reference promptly
    if not session or len(session) < 20:
        return False, "Bitwarden didn't return a session."
    with _LOCK:
        _STATE["session"] = session
    try:
        count = refresh_items()
    except BitwardenError as exc:
        lock()
        return False, f"Unlocked, but couldn't read the vault: {exc}"
    touch()
    return True, f"{count} item{'s' if count != 1 else ''}"


def open_without_password() -> tuple[bool, str]:
    """Try `bw list items` without a master password (bw may already be unlocked for this user). If it
    works the vault is open: (True, 'n items'); else (False, why). Blocking: call off the UI thread."""
    if not find_cli():
        return False, "the Bitwarden command-line tool isn't installed"
    try:
        count = refresh_items(ambient=True)
    except BitwardenError as exc:
        return False, str(exc)
    with _LOCK:
        _STATE["session"] = AMBIENT
    touch()
    log.info("Bitwarden opened without a master password (bw was already unlocked)")
    return True, f"{count} item{'s' if count != 1 else ''}"


def refresh_items(ambient: bool = False) -> int:
    """Read the vault's item list, keeping only what's safe to hold: names, addresses, usernames."""
    raw = _run(["list", "items"], ambient=ambient)
    try:
        data = json.loads(raw)
    except ValueError:
        raise BitwardenError("bw returned something unreadable") from None
    finally:
        raw = ""                                            # noqa: F841
    items = []
    for entry in data if isinstance(data, list) else []:
        login = entry.get("login") or {}
        if entry.get("type") != 1 and not login:
            continue
        items.append(Item(id=str(entry.get("id") or ""), name=str(entry.get("name") or "").strip(),
                          username=str(login.get("username") or "").strip(),
                          uris=[str(u.get("uri") or "") for u in login.get("uris") or [] if isinstance(u, dict)],
                          has_totp=bool(login.get("totp"))))
    data = None                                             # the passwords go with it
    with _LOCK:
        _STATE["items"] = [i for i in items if i.id and i.name]
        return len(_STATE["items"])


def lock() -> None:
    """Forget the session and the item list, and tell bw to lock too."""
    with _LOCK:
        had = _STATE["session"]
        _STATE["session"] = ""
        _STATE["items"] = []
        _STATE["choice"] = None
        timer = _STATE["timer"]
        _STATE["timer"] = None
    if timer is not None:
        timer.cancel()
    if had and had != AMBIENT and find_cli():          # a session the user made is theirs to lock
        try:
            _run(["lock"], timeout=15)
        except BitwardenError:
            pass


def touch() -> None:
    """Restart the auto-lock countdown (every use keeps the vault open a little longer)."""
    minutes = float(_cfg("bitwarden_lock_minutes", 10.0))
    timer = threading.Timer(max(60.0, minutes * 60.0), lock)
    timer.daemon = True
    with _LOCK:
        old = _STATE["timer"]
        _STATE["timer"] = timer
    if old is not None:
        old.cancel()
    timer.start()


def secret(item: Item, what: str) -> str:
    """'password' | 'totp' | 'username' for one item, fetched now. Raises BitwardenError."""
    if what not in ("password", "totp", "username"):
        raise ValueError(what)
    touch()
    return _run(["get", what, item.id]).strip()


# ---------------------------------------------------------------------------
# Finding the item that was meant
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_FILLER = {"my", "the", "account", "login", "for", "on", "at", "a", "an", "app", "website", "site", "com"}


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text).lower()) if w not in _FILLER]


def _score(query: str, item: Item) -> float:
    wanted = _words(query)
    if not wanted:
        return 0.0
    joined = "".join(wanted)
    name_words = _words(item.name)
    hay = set(name_words)
    for host in item.hosts:
        hay |= set(_words(host.replace(".", " ")))
        hay.add(host.split(".")[0])
    hits = sum(1 for w in wanted if w in hay or any(h.startswith(w) and len(w) >= 3 for h in hay))
    score = hits / len(wanted)
    squashed = "".join(name_words)
    if joined == squashed:
        score += 1.0                                           # "protonmail" == "Proton Mail"
    elif joined in squashed or squashed in joined:
        score += 0.4
    score += 0.3 * difflib.SequenceMatcher(None, joined, squashed).ratio()
    return score


def find(query: str) -> list[Item]:
    """The items `query` most likely means, best first; several only when they're equally good."""
    with _LOCK:
        items = list(_STATE["items"])
    scored = sorted(((round(_score(query, i), 3), i) for i in items), key=lambda s: -s[0])
    if not scored or scored[0][0] < 0.6:
        return []
    top = scored[0][0]
    return [i for s, i in scored if s >= top - 0.05][:4]


def spoken_list(items: list[Item]) -> str:
    names = [f"{i.name}" + (f" ({i.username})" if i.username and sum(j.name == i.name for j in items) > 1 else "")
             for i in items]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def remember_choice(kind: str, items: list[Item]) -> None:
    with _LOCK:
        _STATE["choice"] = {"kind": kind, "items": items, "until": time.time() + 45}


def take_choice(text: str) -> tuple[str, Item] | None:
    """The answer to "Proton Mail or Proton Pass?" -- (kind, item) -- or None if `text` isn't one."""
    with _LOCK:
        choice = _STATE["choice"]
    if not choice or time.time() > choice["until"]:
        return None
    t = " ".join(_words(text))
    ordinals = {"first": 0, "the first one": 0, "second": 1, "the second one": 1, "third": 2, "last": -1}
    items = choice["items"]
    for word, index in ordinals.items():
        if re.search(rf"\b{word}\b", text.lower()):
            with _LOCK:
                _STATE["choice"] = None
            return choice["kind"], items[index]
    best = max(items, key=lambda i: _score(t, i) + (1.0 if i.username and i.username.lower() in text.lower() else 0))
    if _score(t, best) < 0.6 and not (best.username and best.username.lower() in text.lower()):
        return None
    with _LOCK:
        _STATE["choice"] = None
    return choice["kind"], best


# ---------------------------------------------------------------------------
# What was said
# ---------------------------------------------------------------------------

_BW = r"(?:\s+(?:from|in|on|using|via|out of)\s+(?:my\s+)?(?:bitwarden|bit warden|vault|password manager))?"
_Q = r"(?:my\s+|the\s+)?(?P<q>.+?)(?:\s+account)?"
_LEAD = r"^(?:(?:please|hey|can you|could you|would you)\s+)*"
_USER = r"(?:user ?name|login name|login|email|e-mail|email address|account name|user id|handle)"
_PATTERNS: list[tuple[str, re.Pattern]] = [(kind, re.compile(p)) for kind, p in (
    ("lock", _LEAD + r"lock (?:my |the )?(?:bitwarden|bit warden|vault|password vault|passwords)$"),
    ("unlock", _LEAD + r"(?:unlock|open) (?:my |the )?(?:bitwarden|bit warden|vault|password vault)$"),
    ("username", _LEAD + r"(?:pull(?: up)?|get|grab|fetch|tell me|give me|read(?: me)?|show(?: me)?|look up|find|say|"
                 r"what(?:'s| is| was))(?: me)? (?:my|the) " + _USER + r" (?:for|on|from|of|at|to) " + _Q + _BW + r"$"),
    ("username", _LEAD + r"(?:pull(?: up)?|get|grab|fetch|tell me|give me|read(?: me)?|show(?: me)?|look up|find|say|"
                 r"what(?:'s| is| was))(?: me)? my " + _Q + r" " + _USER + _BW + r"$"),
    ("username", _LEAD + r"which " + _USER + r" do i use (?:for|on|at) " + _Q + _BW + r"$"),
    ("type", _LEAD + r"(?:type|enter|fill in|put in|paste)(?: in)? (?:my|the) password (?:for|of) " + _Q + _BW + r"$"),
    ("type", _LEAD + r"(?:type|enter|fill in|put in|paste)(?: in)? my " + _Q + r" password" + _BW + r"$"),
    ("password", _LEAD + r"(?:copy|get|pull(?: up)?|grab|fetch|give me)(?: me)? (?:my|the) password (?:for|of|to) "
                 + _Q + _BW + r"(?: to (?:the |my )?clipboard)?$"),
    ("password", _LEAD + r"(?:copy|get|pull(?: up)?|grab|fetch|give me)(?: me)? my " + _Q + r" password" + _BW
                 + r"(?: to (?:the |my )?clipboard)?$"),
    ("totp", _LEAD + r"(?:what(?:'s| is)|get|give me|read(?: me)?|pull(?: up)?|tell me)(?: me)? (?:my|the) "
             r"(?:2fa|two factor|two-factor|mfa|authenticator|verification|one time|one-time|totp|auth) code "
             r"(?:for|on|from) " + _Q + _BW + r"$"),
    ("totp", _LEAD + r"(?:what(?:'s| is)|get|give me|read(?: me)?|pull(?: up)?|tell me)(?: me)? my " + _Q +
             r" (?:2fa|two factor|two-factor|mfa|authenticator|verification|one time|one-time|totp|auth) code" + _BW + r"$"),
)]


def spoken_command(text: str) -> tuple[str, str, bool] | None:
    """(kind, what, said_bitwarden) -- kind is username / password / type / totp / lock / unlock."""
    t = " ".join(re.sub(r"[?!.,]+(?=\s|$)", "", str(text).lower()).split())
    for kind, pattern in _PATTERNS:
        m = pattern.match(t)
        if m:
            what = (m.groupdict().get("q") or "").strip()
            if kind not in ("lock", "unlock") and not _words(what):
                continue
            return kind, what, bool(re.search(r"\b(?:bitwarden|bit warden|vault|password manager)\b", t))
    return None


def set_pending(text: str) -> None:
    """A command that came in while the vault was locked: the controller re-runs it after unlocking."""
    with _LOCK:
        _STATE["pending"] = text


def take_pending() -> str:
    with _LOCK:
        text, _STATE["pending"] = _STATE["pending"], ""
    return text
