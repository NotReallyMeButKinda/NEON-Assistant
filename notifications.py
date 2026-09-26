"""
notifications.py -- take over Windows notifications.

Windows has no "intercept every toast" hook, but it does have an official *listener* API
(UserNotificationListener) that lets an ordinary desktop app read every toast: which app sent it,
its title and its text. A small PowerShell helper (notify_watcher.ps1) polls it and reports new
toasts here as JSON lines; nothing is installed and no admin rights are needed.

  * NotificationWatcher   runs the helper, yields `Notification`s, can dismiss them from the
                          Windows Action Center afterwards. It also reads Windows' notification database
                          (DatabaseWatch): while a game or video is fullscreen, Windows' automatic Do Not
                          Disturb files toasts away silently, and the listener doesn't always report
                          them. Whatever the listener misses is delivered from there a few seconds later.
  * SilenceState          optionally switches Windows' own pop-ups off while the assistant runs
                          (the master "Notifications" toggle), remembering the original value in a
                          file so a crash can never leave them off; it is restored on exit and, if
                          the app died, at the next start.
  * enable_silence()      turns that on, then proves the toasts are still delivered to us (sends a
                          test toast). If they aren't, it turns pop-ups back on and says so.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
import winreg
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

import app_paths
import neon_log

log = neon_log.get("notifications")

SCRIPT = app_paths.resource_path("notify_watcher.ps1")
RESTORE_FILE = app_paths.data_path("notify_restore.json")
PS_EXE = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The master "Get notifications from apps and other senders" switch (absent = on).
PUSH_KEY = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications"
PUSH_VALUE = "ToastEnabled"
# Toasts sent by the test button / delivery check are attributed to Windows PowerShell.
_TEST_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
CHECK_TITLE = "Neon assistant notification check"


@dataclass
class Notification:
    id: int
    app: str
    title: str
    body: str
    created: float = 0.0
    received: float = field(default_factory=time.time)
    backlog: bool = False        # it was already waiting in the Action Center when the watcher started
    aumid: str = ""              # the sending app's AppUserModelId: the card's Open button launches it
    launch: str = ""             # the link the toast itself opens when clicked (see launch_uri), if it has one

    @property
    def text(self) -> str:
        return f"{self.title}. {self.body}".strip(". ") if self.body else self.title

    def as_dict(self) -> dict:
        return {"id": self.id, "app": self.app, "title": self.title, "body": self.body,
                "created": self.created, "received": self.received, "aumid": self.aumid, "launch": self.launch}


def parse_toast(obj: dict) -> Notification | None:
    """One `toast` line from the helper -> Notification (None if it has no text at all)."""
    texts = obj.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t).strip() for t in texts if str(t).strip()]
    if not texts:
        return None
    return Notification(id=int(obj.get("id", 0)), app=str(obj.get("app") or "an app").strip() or "an app",
                        title=texts[0], body=" ".join(texts[1:]), created=float(obj.get("created", 0)) / 1000.0,
                        backlog=bool(obj.get("backlog", False)), aumid=str(obj.get("aumid") or "").strip())


TEST_ID = -1                     # Settings' simulated notification: never sent to the Action Center


# ---------------------------------------------------------------------------
# Attachment names, said like a person would
# ---------------------------------------------------------------------------
# Chat apps put the attachment's file name in the notification ("IMG_20260924_183012.jpg",
# "8f3a2c1b9e.png", "Screenshot 2026-09-24 101010.png"), which is gibberish read aloud. Names made only
# of camera prefixes, dates, counters and ids become "an image"; a name with real words in it keeps
# them: "beach_trip.jpg" -> "an image called beach trip".

_FILE_KINDS = {
    "an image": "jpg jpeg png webp heic heif bmp tif tiff avif svg",
    "a GIF": "gif",
    "a video": "mp4 mov webm mkv avi m4v 3gp",
    "an audio clip": "mp3 wav ogg oga opus m4a aac flac weba",
    "a PDF": "pdf",
    "a document": "doc docx odt rtf pages",
    "a spreadsheet": "xls xlsx ods csv numbers",
    "a presentation": "ppt pptx odp key",
    "a zip file": "zip rar 7z tar gz",
    "a text file": "txt log md",
}
_KIND_OF = {ext: kind for kind, exts in _FILE_KINDS.items() for ext in exts.split()}
_EXTS = "|".join(sorted(_KIND_OF, key=len, reverse=True))
# "Screenshot 2026-09-24 at 10.10.10.png": a spaced name that starts with what it is
_SPACED_NAME = re.compile(rf"\b(?P<what>screenshot|screen shot|screen recording|photo|image|video|recording)"
                          rf"[ _-]+\d[\d ._:,at-]*\.(?:{_EXTS})\b", re.I)
_FILE_NAME = re.compile(rf"(?<![\w/\\])(?P<base>[^\s/\\:*?\"<>|]+?)\.(?P<ext>{_EXTS})(?![\w.])", re.I)
_NAME_NOISE = {"img", "image", "images", "vid", "video", "pxl", "dsc", "dscn", "dscf", "dcim", "mvimg", "pano",
               "screenshot", "screenshots", "screen", "shot", "capture", "recording", "rec", "photo", "pic",
               "picture", "file", "attachment", "unknown", "untitled", "download", "downloaded", "received",
               "whatsapp", "wa", "signal", "telegram", "snapchat", "discord", "copy", "edited", "burst", "cover",
               "original", "scaled", "thumb", "thumbnail", "voice", "audio", "message", "msg", "ptt", "at", "am",
               "pm"}
_SPACED_KIND = {"screenshot": "a screenshot", "screen shot": "a screenshot", "screen recording": "a screen recording",
                "photo": "a photo", "image": "an image", "video": "a video", "recording": "a recording"}


def _real_words(base: str) -> list[str]:
    """The parts of a file name a person chose, if any ("beach_trip-2" -> ["beach", "trip"])."""
    base = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", base)  # myCat2026 -> my Cat 2026
    words = []
    for part in re.split(r"[\s_.\-()\[\]+,~]+", base):
        if (len(part) >= 3 and part.isalpha() and part.lower() not in _NAME_NOISE
                and re.search(r"[aeiouy]", part, re.I) and not re.fullmatch(r"[a-f]+", part, re.I)
                and not re.search(r"[bcdfghjklmnpqrstvwxz]{5}", part, re.I)):
            words.append(part.lower())
    return words[:5]


def spoken_file_names(text: str) -> str:
    """File names in a notification -> what they are: "IMG_2041.jpg" -> "an image"."""
    if "." not in str(text):
        return str(text)
    text = _SPACED_NAME.sub(lambda m: _SPACED_KIND[" ".join(m.group("what").lower().split())], str(text))

    def said(m: re.Match) -> str:
        kind = _KIND_OF[m.group("ext").lower()]
        words = _real_words(m.group("base"))
        return f"{kind} called {' '.join(words)}" if words else kind
    return _FILE_NAME.sub(said, text)

# ---------------------------------------------------------------------------
# Taking the user's chosen words out before reading (Settings > Notifications > "Remove before reading")
# ---------------------------------------------------------------------------
# Apps pad names and titles with things nobody wants read aloud: "Alex (My Server)", "#general - Work",
# "Mail - Outlook", "(3 new)". One rule per line: plain text is removed wherever it appears (any capitals);
# a line in slashes is a regular expression ("/\(\d+ new\)/"). A broken pattern is skipped, not fatal.

def strip_rules(setting) -> list:
    """The setting (text, one rule per line, or a list) -> compiled patterns."""
    lines = setting if isinstance(setting, (list, tuple)) else str(setting or "").splitlines()
    rules = []
    for line in lines:
        line = str(line).strip()
        if not line:
            continue
        if len(line) > 2 and line.startswith("/") and line.endswith("/"):
            try:
                rules.append(re.compile(line[1:-1], re.I))
            except re.error:
                continue
        else:
            rules.append(re.compile(re.escape(line), re.I))
    return rules


def strip_problems(setting) -> list[str]:
    """What's wrong with the rules, for the editor: 'Line 2: missing ), unterminated subpattern'."""
    problems = []
    for number, line in enumerate(str(setting or "").splitlines(), 1):
        line = line.strip()
        if len(line) > 2 and line.startswith("/") and line.endswith("/"):
            try:
                re.compile(line[1:-1])
            except re.error as exc:
                problems.append(f"Line {number} isn't a valid pattern ({exc.msg}), so it's skipped.")
    return problems


# Ready-made rules for the editor's "Add a common rule" menu: (what it does, the rule).
COMMON_STRIP_RULES = (
    ("Anything in (parentheses)", r"/\([^)]*\)/"),
    ("Anything in [square brackets]", r"/\[[^\]]*\]/"),
    ("Channel names like #general", r"/#[\w-]+/"),
    ("Everything after \" - \"", r"/\s[-\u2013\u2014|]\s.*$/"),
    ("Counts like \"3 new messages\"", r"/\b\d+ (?:new )?(?:messages?|notifications?|mentions?)\b/"),
    ("Emoji", r"/[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]/"),
)


def strip_text(text: str, rules: list) -> str:
    """`text` with every rule's matches taken out, and the separators they leave dangling tidied away."""
    out = str(text or "")
    for rule in rules:
        out = rule.sub(" ", out)
    out = re.sub(r"\(\s*\)|\[\s*\]", " ", out)                       # emptied brackets
    out = re.sub(r"\s+([,.:;!?])", r"\1", " ".join(out.split()))
    return re.sub(r"^[\s\-\u2013\u2014|:\u00b7,]+|[\s\-\u2013\u2014|:\u00b7,]+$", "", out)


def stripped(n: dict, setting) -> dict:
    """A notification dict with the rules applied to its app name, title and text (a copy)."""
    rules = strip_rules(setting)
    if not rules:
        return n
    return {**n, **{k: strip_text(n.get(k) or "", rules) for k in ("app", "title", "body")}}


# ---------------------------------------------------------------------------
# What a toast opens when clicked
# ---------------------------------------------------------------------------
# The listener API only exposes a toast's text. Windows keeps the whole toast (its XML, including the
# `launch` attribute) in its own notification database, under the same id, so it is read from there,
# read-only. Only protocol toasts carry a link anyone can open ("ms-screensketch:edit?...",
# "https://..."); most apps (Discord, Chrome...) handle the click inside themselves, and for those the
# Open button launches the app instead.

WPN_DB = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db"))
# Schemes a notification never gets to open, whatever its app says: files, shell folders, scripts,
# and Windows' historically abused handlers.
_BLOCKED_SCHEMES = {"file", "shell", "javascript", "vbscript", "data", "ms-msdt", "search-ms", "search",
                    "ms-officecmd", "ms-appinstaller", "ms-cxh", "ms-cxh-full", "mk", "its", "ms-its"}


def safe_launch(uri: str) -> str:
    """`uri` if it is a link a notification may open, else ""."""
    m = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]{1,40}):", str(uri or "").strip())
    if not m or m.group(1).lower() in _BLOCKED_SCHEMES or len(uri) > 2048:
        return ""
    return uri.strip()


def launch_from_xml(payload) -> str:
    """The link a toast's XML opens on a click: its `launch` attribute, for protocol toasts only."""
    import xml.etree.ElementTree as ET
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    try:
        root = ET.fromstring(text.strip().lstrip("\ufeff"))
    except ET.ParseError:
        return ""
    if root.tag != "toast" or root.get("activationType", "").lower() != "protocol":
        return ""
    return safe_launch(root.get("launch", ""))


def launch_uri(notification_id: int, db: Path | None = None) -> str:
    """The link notification `notification_id` opens when clicked, or "" (none, gone, or unreadable)."""
    import sqlite3
    if int(notification_id) < 0:
        return ""
    path = Path(db or WPN_DB)
    if not path.exists():
        return ""
    try:
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT Payload FROM Notification WHERE Id = ? AND Type = 'toast'",
                              (int(notification_id),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return launch_from_xml(row[0]) if row else ""


def texts_from_xml(payload) -> list[str]:
    """The text lines of a toast's XML (title first), as the listener would report them."""
    import xml.etree.ElementTree as ET
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload or "")
    try:
        root = ET.fromstring(text.strip().lstrip("\ufeff"))
    except ET.ParseError:
        return []
    return [" ".join(el.text.split()) for binding in root.iter("binding") for el in binding.iter("text")
            if el.text and el.text.strip() and el.get("placement") != "attribution"]


_GENERIC_AUMID_PARTS = {"app", "exe", "desktop", "client", "com", "org", "net", "microsoft", "windows", "squirrel",
                        "github", "electron", "notifications", "toast", "main"}


def app_from_aumid(aumid: str) -> str:
    """A readable app name from an AppUserModelId when Windows' display name isn't at hand:
    'com.squirrel.Discord.Discord' -> 'Discord', 'Microsoft.ScreenSketch_8wekyb3d8bbwe!App' -> 'ScreenSketch'."""
    name = re.split(r"[\\/]", str(aumid or ""))[-1].split("!")[0]
    name = re.sub(r"\.exe$", "", name, flags=re.I)
    name = re.sub(r"_[a-z0-9]{13}$", "", name)                     # a Store app's publisher id
    parts = [p for p in name.split(".") if p and p.lower() not in _GENERIC_AUMID_PARTS and not p.isdigit()
             and not re.fullmatch(r"[0-9a-fA-F]{8,}", p)]                # install hashes: "Firefox.6F193CCC..."
    return parts[-1] if parts else "an app"


class DatabaseWatch:
    """New toasts straight from Windows' notification database (read-only), for the ones the listener
    misses. `poll()` returns rows that have been in the database for `grace` seconds without the listener
    reporting them (`seen(id)` marks what it did report)."""

    def __init__(self, db: Path | None = None, grace: float = 3.0):
        self._db = Path(db or WPN_DB)
        self.grace = grace
        self._known: set[int] = set()              # delivered, by either the listener or this
        self._already: set[int] = set()            # in the database before we started: the listener's backlog
        self._waiting: dict[int, float] = {}
        self._lock = threading.Lock()
        self._started = False

    def seen(self, notification_id: int) -> bool:
        """Mark a notification as delivered; False if it already was."""
        with self._lock:
            if notification_id in self._known:
                return False
            self._known.add(notification_id)
            self._waiting.pop(notification_id, None)
            return True

    def _rows(self) -> list[tuple]:
        import sqlite3
        if not self._db.exists():
            return []
        try:
            con = sqlite3.connect(f"{self._db.as_uri()}?mode=ro", uri=True, timeout=1.0)
            try:
                return con.execute(
                    "SELECT n.Id, n.Payload, n.ArrivalTime, h.PrimaryId FROM Notification n "
                    "JOIN NotificationHandler h ON n.HandlerId = h.RecordId WHERE n.Type = 'toast'").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    def poll(self, now: float | None = None) -> list[dict]:
        """Toasts the listener hasn't reported, as helper-style dicts ({"id", "aumid", "texts", "created"}).
        The first call only notes what is already there (the listener's backlog covers that)."""
        now = time.monotonic() if now is None else now
        rows = self._rows()
        out = []
        with self._lock:
            if not self._started:
                self._started = True
                self._already.update(int(r[0]) for r in rows)
                return []
            present = set()
            for nid, payload, arrival, aumid in rows:
                nid = int(nid)
                present.add(nid)
                if nid in self._known or nid in self._already:
                    continue
                first = self._waiting.setdefault(nid, now)
                if now - first < self.grace:
                    continue
                self._known.add(nid)
                self._waiting.pop(nid, None)
                created = (int(arrival or 0) / 10_000 - 11_644_473_600_000) if arrival else 0   # FILETIME -> Unix ms
                out.append({"id": nid, "aumid": str(aumid or ""), "texts": texts_from_xml(payload),
                            "created": max(0, created)})
            self._waiting = {k: v for k, v in self._waiting.items() if k in present}
            if len(self._known) > 5000:
                self._known &= present
            self._already &= present
        return out


class NotificationWatcher:
    """Runs notify_watcher.ps1 and turns its output into `on_notification(Notification)` calls
    (on the reader thread). `ignore` returns lower-case app-name fragments to skip."""

    def __init__(self, on_notification, on_status=None, ignore=lambda: (), on_backlog=None):
        self._on_notification = on_notification
        self._on_backlog = on_backlog or (lambda notes: None)     # what was waiting before we started
        self._backlog: list[Notification] = []
        self._on_status = on_status or (lambda message: None)
        self._ignore = ignore
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cmd_file = Path(os.environ.get("TEMP") or app_paths.DATA_DIR) / f"neon_notify_cmd_{os.getpid()}.txt"
        self._expect: dict[str, threading.Event] = {}
        self.ready = threading.Event()
        self.access = ""           # "Allowed" / "Denied" / "Unspecified" once known
        self._db_watch = DatabaseWatch()
        self._db_thread: threading.Thread | None = None
        self._app_names: dict[str, str] = {}      # aumid -> the display name the listener reported

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.ready.clear()
        self._thread = threading.Thread(target=self._run, name="Nova-Notifications", daemon=True)
        self._thread.start()
        if self._db_thread is None or not self._db_thread.is_alive():
            self._db_watch = DatabaseWatch()
            self._db_thread = threading.Thread(target=self._watch_database, name="Nova-NotificationDb", daemon=True)
            self._db_thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.ready.clear()
        try:
            self._cmd_file.unlink(missing_ok=True)
        except OSError:
            pass

    def remove(self, notification_id: int) -> None:
        """Ask the helper to dismiss a notification from the Windows Action Center."""
        if int(notification_id) < 0:
            return
        try:
            with self._cmd_file.open("a", encoding="utf-8") as f:
                f.write(f"{int(notification_id)}\n")
        except OSError:
            pass

    def expect(self, title: str) -> threading.Event:
        """An Event that is set when a toast with exactly this title arrives. Such toasts are
        consumed here and never reach `on_notification` (used for the delivery check)."""
        event = threading.Event()
        self._expect[title] = event
        return event

    # ---- internals -------------------------------------------------------------------
    DB_POLL_SECONDS = 1.5

    def _watch_database(self) -> None:
        """Deliver what the listener missed (see DatabaseWatch), after it has had its chance."""
        while not self._stop.wait(self.DB_POLL_SECONDS):
            if not self.ready.is_set() and self.access in ("", "Allowed"):
                continue                           # let the listener report the backlog first
            try:
                missed = self._db_watch.poll()
            except Exception as exc:  # noqa: BLE001 -- the listener still works without this
                log.warning("reading Windows' notification database failed: %s", exc)
                continue
            for obj in missed:
                obj["app"] = self._app_names.get(obj["aumid"]) or app_from_aumid(obj["aumid"])
                note = parse_toast(obj)
                if note is not None:
                    log.info("a notification from %s came from Windows' database (the listener missed it)", note.app)
                    self._deliver(note, from_db=True)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    PS_EXE + ["-File", str(SCRIPT), "-CmdFile", str(self._cmd_file), "-ParentPid", str(os.getpid())],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                for raw in self._proc.stdout:
                    if self._stop.is_set():
                        return
                    self._handle_line(raw)
            except (OSError, ValueError) as exc:        # ValueError: the pipe was closed under us by stop()
                if not self._stop.is_set():
                    self._on_status(f"Couldn't start the notification watcher: {exc}")
            finally:
                self._reap()
            if self._stop.is_set() or self.access in ("Denied", "Unspecified"):
                return                      # stopped on purpose, or Windows said no: retrying won't help
            failures = failures + 1 if time.monotonic() - started < 20 else 0
            if failures >= 3:
                self._on_status("The notification watcher keeps stopping, so I gave up on it.")
                return
            self._stop.wait(3.0)

    def _reap(self) -> None:
        """Make sure the helper process is gone and its pipe closed (no zombie, no ResourceWarning)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except (OSError, ValueError):
                    pass

    def _handle_line(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        kind = obj.get("type")
        if kind == "access":
            self.access = str(obj.get("status", ""))
            if self.access != "Allowed":
                self._on_status("Windows hasn't allowed notification access. Turn it on in Settings > Privacy & "
                                "security > Notifications, then restart me.")
        elif kind == "ready":
            backlog, self._backlog = self._backlog, []
            self.ready.set()
            if backlog:
                self._on_backlog(backlog)
        elif kind == "error":
            self._on_status(f"Notification watcher: {obj.get('message', 'unknown error')}")
        elif kind == "toast":
            note = parse_toast(obj)
            if note is None:
                return
            if note.aumid:
                self._app_names[note.aumid] = note.app
            self._deliver(note)

    def _deliver(self, note: Notification, from_db: bool = False) -> None:
        """One toast, from the listener or the database, handed on once."""
        if not from_db and not self._db_watch.seen(note.id):
            return                                  # the database copy was delivered already
        event = self._expect.pop(note.title, None)
        if event is not None:
            event.set()
            self.remove(note.id)
            return
        if any(part and part in note.app.lower() for part in self._ignore()):
            return
        if note.backlog:
            self._backlog.append(note)              # delivered together once "ready" arrives
            return
        self._on_notification(note)


# ---------------------------------------------------------------------------
# Optionally silencing Windows' own pop-ups
# ---------------------------------------------------------------------------

def _read_toast_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PUSH_KEY) as key:
            return winreg.QueryValueEx(key, PUSH_VALUE)[0]
    except OSError:
        return None


def _write_toast_enabled(value) -> None:
    """value None = remove the setting (Windows' default: on)."""
    if value is None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PUSH_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, PUSH_VALUE)
        except OSError:
            pass
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PUSH_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, PUSH_VALUE, 0, winreg.REG_DWORD, int(value))


class SilenceState:
    """Switch Windows' pop-ups off, and reliably put the user's own setting back."""

    def __init__(self, restore_file: Path = RESTORE_FILE):
        self._file = restore_file

    @property
    def engaged(self) -> bool:
        return self._file.exists()

    def engage(self) -> None:
        if not self.engaged:                # never overwrite the saved original with our own 0
            self._file.write_text(json.dumps({"original": _read_toast_enabled()}), encoding="utf-8")
        _write_toast_enabled(0)

    def release(self) -> None:
        if not self.engaged:
            return
        try:
            original = json.loads(self._file.read_text(encoding="utf-8")).get("original")
        except (OSError, ValueError):
            original = None                 # unreadable marker: Windows' default is "on"
        _write_toast_enabled(original)
        try:
            self._file.unlink()
        except OSError:
            pass

    def recover(self) -> bool:
        """At startup: a leftover marker means the last run died while silenced. Undo that."""
        if self.engaged:
            self.release()
            return True
        return False


def send_test_toast(title: str, body: str = "") -> bool:
    """Shows a toast (attributed to Windows PowerShell). Used by the Settings test button and by
    the delivery check. Returns False if PowerShell couldn't be started."""
    script = (
        "$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t=$x.GetElementsByTagName('text');"
        "$null=$t.Item(0).AppendChild($x.CreateTextNode($env:NEON_TITLE));"
        "$null=$t.Item(1).AppendChild($x.CreateTextNode($env:NEON_BODY));"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_TEST_AUMID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    try:
        proc = subprocess.Popen(PS_EXE + ["-Command", script], env={**os.environ, "NEON_TITLE": title, "NEON_BODY": body},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError:
        return False
    threading.Thread(target=proc.wait, name="Nova-ToastSend", daemon=True).start()   # reap it when it finishes
    return True


def enable_silence(watcher: NotificationWatcher, state: SilenceState | None = None,
                   timeout: float = 8.0) -> tuple[bool, str]:
    """Turns Windows' pop-ups off, then proves we still receive toasts. If we don't (Windows may
    drop them once pop-ups are off) it reverts at once. Returns (worked, message to show)."""
    state = state or SilenceState()
    if not watcher.ready.wait(timeout=10):
        return False, "The notification watcher isn't running yet, so I left Windows' pop-ups alone."
    state.engage()
    title = f"{CHECK_TITLE} {uuid.uuid4().hex[:6]}"
    arrived = watcher.expect(title)
    if send_test_toast(title, "You can ignore this."):
        got_it = arrived.wait(timeout=timeout)
    else:
        got_it = False
    if not got_it:
        watcher._expect.pop(title, None)
        state.release()
        return False, ("Windows stopped delivering notifications to me once its pop-ups were off, so I turned "
                       "them back on. You can still use Windows' Do Not Disturb yourself.")
    return True, "Windows' own notification pop-ups are silenced while I'm running."


# ---------------------------------------------------------------------------
# Deciding what to do with each notification
# ---------------------------------------------------------------------------

RULE_MODES = ("ask", "summarize", "read", "message", "card", "silent", "ignore")
_RANK = {"ignore": 0, "silent": 1, "show": 2, "card": 3, "message": 4, "read": 4, "summarize": 4, "ask": 5}


@dataclass
class Decision:
    """action: ignore (drop it) | silent (remember it, show nothing) | show (a card, no buttons) |
    card (a card with Summarize / Dismiss) | read (a card, and read out loud straight away) |
    message (like read, but only 'Alex says <their message>', picked out by the local AI) |
    summarize (a card, and a spoken summary straight away: like ask, without the question) |
    ask (the card, plus a spoken 'want a summary?').
    sound: whether the notification sound plays."""
    action: str
    sound: bool = True


def _parse_clock(text: str, default: dtime) -> dtime:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return dtime(int(m.group(1)), int(m.group(2)))


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    """True inside the configured quiet hours (which may cross midnight, e.g. 22:00 to 07:00)."""
    value = cfg.get("notify_quiet_enabled", False)
    enabled = value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)
    if not enabled:
        return False
    start = _parse_clock(cfg.get("notify_quiet_start"), dtime(22, 0))
    end = _parse_clock(cfg.get("notify_quiet_end"), dtime(7, 0))
    t = (now or datetime.now()).time()
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def _words(text) -> list[str]:
    return [w.strip().lower() for w in str(text or "").split(",") if w.strip()]


def decide(note: Notification, cfg: dict, paused: bool = False, now: datetime | None = None) -> Decision:
    """How to treat one notification, from the settings: the global mode, per-app rules, quiet hours
    (cards only, no sound, no questions), VIP words that cut through quiet hours, and a temporary pause."""
    app = note.app.lower()
    mode = str(cfg.get("notify_ask", "speech")).strip().lower()
    action = {"speech": "ask", "summarize": "summarize", "read": "read", "message": "message", "card": "card"}.get(mode, "show")
    for rule in cfg.get("notify_rules") or []:
        if isinstance(rule, dict) and str(rule.get("app", "")).strip() and str(rule["app"]).strip().lower() in app:
            chosen = str(rule.get("mode", "ask")).strip().lower()
            if chosen in RULE_MODES:
                action = chosen
            break                                        # the first matching rule wins
    if action == "ignore":
        return Decision("ignore", False)
    if paused:
        return Decision("silent", False)                 # an explicit "don't read my notifications": absolute
    if in_quiet_hours(cfg, now):
        haystack = f"{note.app} {note.title} {note.body}".lower()
        if not any(word in haystack for word in _words(cfg.get("notify_vip"))):
            return Decision("silent" if action == "silent" else "show", False)
    return Decision(action, action != "silent")


def strongest(decisions: list[Decision]) -> str:
    return max((d.action for d in decisions), key=lambda a: _RANK[a], default="ignore")
