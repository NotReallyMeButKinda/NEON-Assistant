"""
filesearch.py -- "find the file called resume", "where's my tax folder", "open the pdf called invoice",
through Everything (voidtools.com), the instant file-name search for Windows.

Everything keeps an index of every file name on NTFS drives and answers over a documented window-message
protocol (the SDK's everything_ipc.h): a WM_COPYDATA query to its hidden "EVERYTHING_TASKBAR_NOTIFICATION"
window, answered with a WM_COPYDATA to a window of ours. That is spoken here with ctypes, so nothing is
downloaded or shipped: no Everything64.dll, no es.exe. Everything itself must be installed; it is started in
the background if it isn't running.

  * search()               one query -> [Hit], newest first, system and program folders left out
  * spoken_file_command()  what was said -> FileCommand (find / open / reveal / pick), or None
  * handle_file_command()  the reply, remembering the results so "open the second one" works

Programs and scripts found by a search are never run: "open" shows them in their folder instead (apps are
opened by name through the app launcher, which only uses Start-menu shortcuts).
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import neon_log

log = neon_log.get("filesearch")

# ---------------------------------------------------------------------------
# The Everything IPC protocol (everything_ipc.h)
# ---------------------------------------------------------------------------

IPC_WNDCLASS = "EVERYTHING_TASKBAR_NOTIFICATION"
WM_USER, WM_COPYDATA = 0x0400, 0x004A
IPC_IS_DB_LOADED = 401
COPYDATA_QUERY2W = 18
REQUEST_NAME, REQUEST_PATH, REQUEST_FULL_PATH, REQUEST_EXTENSION = 0x1, 0x2, 0x4, 0x8
REQUEST_SIZE, REQUEST_DATE_CREATED, REQUEST_DATE_MODIFIED = 0x10, 0x20, 0x40
SORT_NAME_ASCENDING, SORT_DATE_MODIFIED_DESCENDING = 1, 14
ITEM_FOLDER = 0x1
SMTO_ABORTIFHUNG = 0x0002
QS_ALLINPUT, PM_REMOVE = 0x04FF, 0x0001
MSGFLT_ALLOW = 1
_REPLY_ID = 0x4E454F4E                         # "NEON": the dwData Everything echoes back
_UNKNOWN_TIME = (0, 0xFFFFFFFFFFFFFFFF)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


@dataclass
class Hit:
    name: str
    folder: str
    is_folder: bool = False
    size: int = -1
    modified: datetime | None = None

    @property
    def path(self) -> str:
        return os.path.join(self.folder, self.name) if self.folder else self.name


class EverythingError(Exception):
    """Everything isn't installed, isn't running, or didn't answer. The message is ready to say."""


_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_API: dict = {}


def _api():
    """user32 with argument types set (64-bit handles and LPARAMs need them)."""
    if _API:
        return _API
    u = _user32
    u.FindWindowW.argtypes, u.FindWindowW.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT,
                                      wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = LRESULT
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    u.RegisterClassW.argtypes, u.RegisterClassW.restype = [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = LRESULT
    u.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                            wintypes.DWORD]
    u.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    try:
        u.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    except AttributeError:
        pass
    _API["u"] = u
    return _API


_REPLIES: dict[int, dict] = {}                 # our reply window -> {"data": bytes}
_CLASS = {"name": "", "proc": None}
_CLASS_LOCK = threading.Lock()


def _window_proc(hwnd, msg, wparam, lparam):
    if msg == WM_COPYDATA and lparam:
        cds = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
        box = _REPLIES.get(int(hwnd or 0))
        if box is not None and cds.dwData == _REPLY_ID and cds.lpData:
            box["data"] = ctypes.string_at(cds.lpData, cds.cbData)
            return 1
    return _api()["u"].DefWindowProcW(hwnd, msg, wparam, lparam)


def _reply_class() -> str:
    with _CLASS_LOCK:
        if not _CLASS["name"]:
            proc = WNDPROC(_window_proc)                  # kept alive for as long as the class exists
            wc = WNDCLASSW(lpfnWndProc=proc, lpszClassName="NEON_EVERYTHING_REPLY",
                           hInstance=ctypes.windll.kernel32.GetModuleHandleW(None))
            if not _api()["u"].RegisterClassW(ctypes.byref(wc)):
                raise EverythingError("I couldn't set up the file search.")
            _CLASS.update(name="NEON_EVERYTHING_REPLY", proc=proc)
        return _CLASS["name"]


def everything_window() -> int:
    """Everything's IPC window (0 when it isn't running)."""
    if _user32 is None:
        return 0
    return int(_api()["u"].FindWindowW(IPC_WNDCLASS, None) or 0)


def _ask(hwnd: int, command: int, timeout_ms: int = 1000) -> int | None:
    result = ctypes.c_size_t(0)
    ok = _api()["u"].SendMessageTimeoutW(hwnd, WM_USER, command, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    return int(result.value) if ok else None


def database_loaded() -> bool:
    hwnd = everything_window()
    return bool(hwnd) and bool(_ask(hwnd, IPC_IS_DB_LOADED))


def query_bytes(search: str, reply_hwnd: int, max_results: int, sort: int, request: int) -> bytes:
    """An EVERYTHING_IPC_QUERY2 (packed, little-endian) followed by the UTF-16 search and its terminator."""
    return (struct.pack("<7I", reply_hwnd & 0xFFFFFFFF, _REPLY_ID, 0, 0, max_results, request, sort)
            + search.encode("utf-16-le") + b"\0\0")


def _filetime(value: int) -> datetime | None:
    if value in _UNKNOWN_TIME:
        return None
    try:
        return datetime(1601, 1, 1) + timedelta(microseconds=value // 10) + (datetime.now() - datetime.utcnow())
    except (OverflowError, ValueError):
        return None


def parse_list2(data: bytes) -> list[Hit]:
    """An EVERYTHING_IPC_LIST2 reply -> hits. Each item's fields come in a fixed order (name, path,
    full path, extension, size, dates...), present only when requested."""
    if len(data) < 20:
        return []
    _total, count, _offset, flags, _sort = struct.unpack_from("<5I", data, 0)
    hits = []
    for i in range(count):
        item_flags, p = struct.unpack_from("<2I", data, 20 + 8 * i)
        fields: dict = {}

        def text() -> str:
            nonlocal p
            (length,) = struct.unpack_from("<I", data, p)
            value = data[p + 4:p + 4 + 2 * length].decode("utf-16-le", "replace")
            p += 4 + 2 * (length + 1)
            return value
        for bit, key in ((REQUEST_NAME, "name"), (REQUEST_PATH, "folder"), (REQUEST_FULL_PATH, "full"),
                         (REQUEST_EXTENSION, "ext")):
            if flags & bit:
                fields[key] = text()
        if flags & REQUEST_SIZE:
            (fields["size"],) = struct.unpack_from("<q", data, p)
            p += 8
        if flags & REQUEST_DATE_CREATED:
            p += 8
        if flags & REQUEST_DATE_MODIFIED:
            (stamp,) = struct.unpack_from("<Q", data, p)
            fields["modified"] = _filetime(stamp)
            p += 8
        hits.append(Hit(name=fields.get("name", ""), folder=fields.get("folder", ""),
                        is_folder=bool(item_flags & ITEM_FOLDER), size=fields.get("size", -1),
                        modified=fields.get("modified")))
    return hits


def _query_thread(search: str, max_results: int, sort: int, timeout: float, box: dict) -> None:
    """Everything answers by sending a message to a window of ours, so the query runs on its own thread with
    its own reply window and message loop (as the SDK does)."""
    u = _api()["u"]
    target = everything_window()
    if not target:
        box["error"] = "not running"
        return
    hwnd = u.CreateWindowExW(0, _reply_class(), "", 0, 0, 0, 0, 0, None, None,
                             ctypes.windll.kernel32.GetModuleHandleW(None), None)
    if not hwnd:
        box["error"] = "no window"
        return
    key = int(hwnd)
    _REPLIES[key] = box
    try:
        try:                                              # Everything may run elevated: let its reply in
            u.ChangeWindowMessageFilterEx(hwnd, WM_COPYDATA, MSGFLT_ALLOW, None)
        except (AttributeError, OSError):
            pass
        payload = query_bytes(search, key, max_results, sort,
                              REQUEST_NAME | REQUEST_PATH | REQUEST_SIZE | REQUEST_DATE_MODIFIED)
        buffer = ctypes.create_string_buffer(payload, len(payload))
        cds = COPYDATASTRUCT(COPYDATA_QUERY2W, len(payload), ctypes.cast(buffer, ctypes.c_void_p))
        result = ctypes.c_size_t(0)
        sent = u.SendMessageTimeoutW(target, WM_COPYDATA, key, ctypes.addressof(cds), SMTO_ABORTIFHUNG, 3000,
                                     ctypes.byref(result))
        if not sent or not result.value:
            box["error"] = "refused"
            return
        deadline = time.monotonic() + timeout
        msg = wintypes.MSG()
        while "data" not in box:
            left = deadline - time.monotonic()
            if left <= 0:
                box["error"] = "timeout"
                return
            u.MsgWaitForMultipleObjects(0, None, False, int(left * 1000) + 1, QS_ALLINPUT)
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):  # sent messages are handled in here
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
    finally:
        _REPLIES.pop(key, None)
        u.DestroyWindow(hwnd)


def _raw_search(search: str, max_results: int = 60, sort: int = SORT_DATE_MODIFIED_DESCENDING,
                timeout: float = 5.0) -> list[Hit]:
    box: dict = {}
    worker = threading.Thread(target=_query_thread, args=(search, max_results, sort, timeout, box),
                              name="Nova-Everything", daemon=True)
    worker.start()
    worker.join(timeout + 4)
    if "data" in box:
        return parse_list2(box["data"])
    log.info("Everything query %r failed: %s", search, box.get("error", "no answer"))
    raise EverythingError("Everything didn't answer. It may still be building its index; try again in a moment.")


# ---------------------------------------------------------------------------
# Finding and starting Everything
# ---------------------------------------------------------------------------

def installed_exe() -> str:
    """Everything.exe's path, or "" when it isn't installed."""
    candidates = []
    try:
        import winreg
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\voidtools\Everything"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\voidtools\Everything")):
            try:
                with winreg.OpenKey(root, key) as k:
                    candidates.append(os.path.join(winreg.QueryValueEx(k, "InstallLocation")[0], "Everything.exe"))
            except OSError:
                pass
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Everything", "Everything.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), "")


def status() -> str:
    """'ready' | 'indexing' | 'stopped' (installed, not running) | 'missing'."""
    if everything_window():
        return "ready" if database_loaded() else "indexing"
    return "stopped" if installed_exe() else "missing"


def ensure_running(wait: float = 8.0) -> None:
    """Start Everything in the background if it's installed but not running; raise EverythingError with
    what to say if it can't be used."""
    if everything_window():
        return
    exe = installed_exe()
    if not exe:
        raise EverythingError("Finding files needs Everything, the free file search from voidtools. Install it "
                              "with winget install voidtools.Everything, then ask again.")
    log.info("starting Everything (%s)", exe)
    try:
        subprocess.Popen([exe, "-startup"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise EverythingError(f"Everything is installed but wouldn't start ({exc.__class__.__name__}).") from None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if everything_window() and database_loaded():
            return
        time.sleep(0.25)
    raise EverythingError("I started Everything, but it's still building its index. Ask again in a moment.")


# ---------------------------------------------------------------------------
# Searching the way a person means it
# ---------------------------------------------------------------------------

# Where people's own files don't live. Everything's `!` excludes a term; a term with a backslash is matched
# against the whole path.
SYSTEM_EXCLUDES = ('!\\AppData\\', '!"C:\\Windows\\"', '!\\$Recycle.Bin\\', '!"\\Program Files"', '!\\ProgramData\\',
                   '!\\node_modules\\', '!\\__pycache__\\', '!\\site-packages\\', '!"\\System Volume Information\\"',
                   '!\\Windows.old\\', '!\\steamapps\\',
                   '!\\.')                    # hidden tool folders (.git, .venv, .cache, .vscode...) and dot-files

# What a spoken kind of file means in Everything's search syntax. Its filter macros (pic:, audio:...) belong
# to the search window and aren't understood over IPC, so the extensions are spelled out.
_PICS = "ext:jpg;jpeg;png;gif;webp;heic;heif;bmp;tif;tiff;avif;svg;raw;cr2;nef;arw"
_VIDEOS = "ext:mp4;mov;mkv;webm;avi;m4v;wmv;flv;mpg;mpeg;3gp"
_AUDIO = "ext:mp3;wav;flac;m4a;aac;ogg;opus;wma;aiff;alac"
_DOCS = "ext:doc;docx;odt;rtf;pdf;txt;md;pages;wpd"
_SHEETS = "ext:xlsx;xls;xlsm;csv;ods;numbers"
_SLIDES = "ext:pptx;ppt;odp;key"
_ZIPS = "ext:zip;rar;7z;tar;gz;bz2;xz;iso"
KINDS = {
    "file": "file:", "files": "file:", "folder": "folder:", "folders": "folder:", "directory": "folder:",
    "document": _DOCS, "documents": _DOCS, "doc": _DOCS, "docs": _DOCS,
    "pdf": "ext:pdf", "pdfs": "ext:pdf", "photo": _PICS, "photos": _PICS, "picture": _PICS, "pictures": _PICS,
    "image": _PICS, "images": _PICS, "screenshot": _PICS, "screenshots": _PICS,
    "video": _VIDEOS, "videos": _VIDEOS, "movie": _VIDEOS, "clip": _VIDEOS,
    "song": _AUDIO, "songs": _AUDIO, "audio file": _AUDIO, "music file": _AUDIO, "recording": _AUDIO,
    "spreadsheet": _SHEETS, "spreadsheets": _SHEETS, "presentation": _SLIDES, "presentations": _SLIDES,
    "zip": _ZIPS, "zip file": _ZIPS, "archive": _ZIPS,
}
_KIND_WORDS = "|".join(sorted((re.escape(k) for k in KINDS), key=len, reverse=True))
# Never run these from a search: "open" shows them in their folder instead.
RUNNABLE = {"exe", "com", "bat", "cmd", "msi", "msix", "appx", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf",
            "wsh", "hta", "scr", "pif", "cpl", "msc", "jar", "reg", "lnk", "url", "inf", "application", "gadget"}


def search_string(words: str, kind: str = "", system: bool = False) -> str:
    """What was asked for -> an Everything search: the words (Everything ANDs them, anywhere in the name),
    the kind's filter, and the system-folder exclusions."""
    clean = re.sub(r'[|!<>"*?;:]+', " ", str(words)).strip()
    parts = [clean] if clean else []
    if kind:
        parts.append(KINDS.get(kind, kind))
    if not system:
        parts.extend(SYSTEM_EXCLUDES)
    return " ".join(parts)


def _match_quality(hit: Hit, words: str) -> int:
    """3 exact name, 2 name starts with it, 1 every word in the name, 0 otherwise."""
    stem = Path(hit.name).stem.lower() if not hit.is_folder else hit.name.lower()
    wanted = " ".join(re.sub(r"[_\-.]+", " ", words.lower()).split())
    squash = lambda s: re.sub(r"[\s_\-.]+", "", s)       # noqa: E731
    if squash(stem) == squash(wanted) or hit.name.lower() == words.lower():
        return 3
    if squash(stem).startswith(squash(wanted)):
        return 2
    name = re.sub(r"[_\-.]+", " ", hit.name.lower())
    return 1 if all(w in name for w in wanted.split()) else 0


def rank(hits: list[Hit], words: str) -> list[Hit]:
    """Best first: how exactly the name matches, then the newest (Everything already sorted by date)."""
    order = {id(h): i for i, h in enumerate(hits)}
    return sorted(hits, key=lambda h: (-_match_quality(h, words), order[id(h)]))


def search(words: str, kind: str = "", system: bool = False, max_results: int = 60) -> list[Hit]:
    """Files and folders named like `words` (optionally of one kind), best first. Raises EverythingError."""
    ensure_running()
    return rank(_raw_search(search_string(words, kind, system), max_results), words)


# ---------------------------------------------------------------------------
# What was said
# ---------------------------------------------------------------------------

@dataclass
class FileCommand:
    action: str            # find | open | reveal | pick
    words: str = ""        # what the file is called
    kind: str = ""         # a key of KINDS ("pdf", "folder"...), or ""
    index: int = 0         # pick: which result (0-based, -1 = last)
    pick_action: str = ""  # pick: open | reveal


_FIND = r"(?:find|search for|look for|locate|look up|where(?:'s| is| are| did i (?:save|put))|where's|get me)"
_OWNER = r"(?:the |a |an |my |any |all (?:the |my )?|some |that )?"
_NAMED = r"(?:(?:called|named|titled|with the name|that's called|that is called)\s+)?"
_ON_PC = r"(?:\s+(?:on|in) (?:my|the|this) (?:pc|computer|laptop|drive|disk|machine|files|hard drive))?"
_PATTERNS = [
    # "find the file called resume", "where's the folder named taxes", "find pdfs called invoice"
    ("find", re.compile(rf"{_FIND} {_OWNER}(?P<kind>{_KIND_WORDS}) {_NAMED}(?P<q>.+?){_ON_PC}")),
    # "find my resume file", "where's the tax folder", "find my budget spreadsheet on my pc"
    ("find", re.compile(rf"{_FIND} {_OWNER}(?P<q>.+?) (?P<kind>{_KIND_WORDS}){_ON_PC}")),
    # "find resume on my pc", "search my computer for invoice", "search my files for taxes"
    ("find", re.compile(rf"{_FIND} {_OWNER}(?P<q>.+?) (?:on|in) (?:my|the|this) (?:pc|computer|laptop|drive|disk|"
                        r"machine|files|hard drive)")),
    ("find", re.compile(r"search (?:my|the|this) (?:pc|computer|laptop|files|drive|disk|machine|hard drive) for "
                        rf"{_OWNER}(?:(?P<kind>{_KIND_WORDS}) )?{_NAMED}(?P<q>.+)")),
    # "find resume.pdf" (a name with an extension is always a file)
    ("find", re.compile(rf"{_FIND} {_OWNER}(?P<q>[\w\-\s()]+\.[a-z0-9]{{1,5}})")),
    # "show resume in explorer", "open the folder that has my resume", "show me where resume.pdf is"
    ("reveal", re.compile(rf"(?:show|open)(?: me)? {_OWNER}(?:(?P<kind>{_KIND_WORDS}) )?{_NAMED}(?P<q>.+?) in "
                          r"(?:file )?explorer")),
    ("reveal", re.compile(rf"(?:open|show)(?: me)? the folder (?:that has|that contains|containing|with|where) "
                          rf"{_OWNER}(?:(?P<kind>{_KIND_WORDS}) )?{_NAMED}(?P<q>.+?)(?: is| lives| was saved)?")),
    ("reveal", re.compile(rf"show me where {_OWNER}(?:(?P<kind>{_KIND_WORDS}) )?{_NAMED}(?P<q>.+?) is")),
    # "open the file called budget", "open the pdf invoice", "open my resume file", "open budget.xlsx"
    ("open", re.compile(rf"open {_OWNER}(?P<kind>{_KIND_WORDS}) {_NAMED}(?P<q>.+?){_ON_PC}")),
    ("open", re.compile(rf"open {_OWNER}(?P<q>.+?) (?P<kind>{_KIND_WORDS}){_ON_PC}")),
    ("open", re.compile(r"open (?:the |my )?(?P<q>[\w\-\s()]+\.[a-z0-9]{1,5})")),
]
_ORDINALS = {"first": 0, "1st": 0, "one": 0, "1": 0, "top": 0, "second": 1, "2nd": 1, "two": 1, "2": 1,
             "third": 2, "3rd": 2, "three": 2, "3": 2, "fourth": 3, "4th": 3, "four": 3, "4": 3,
             "fifth": 4, "5th": 4, "five": 4, "5": 4, "last": -1}
_PICK = re.compile(r"(?P<verb>open|show|reveal)(?: me)? (?:the )?(?:number )?(?P<n>" + "|".join(_ORDINALS) +
                   r")(?: one| result| file| folder)?(?P<where> in (?:the |its )?(?:folder|explorer|file explorer))?")
_PICK_IT = re.compile(r"(?:open (?P<open>it|that|that one|that file|the file)"
                      r"|(?:show|open) (?:it|that|that one) in (?:the |its )?(?:folder|explorer|file explorer)"
                      r"|(?:open|show)(?: me)? (?:its|the|that) folder)")
_FILLER_START = re.compile(r"^(?:(?:please|hey|can you|could you|would you|will you|go ahead and|help me)\s+)+")
# Words that make "find my X" something else: a phone, the remote, people, places.
_NOT_FILES = re.compile(r"\b(?:phone|keys|wallet|remote|glasses|car|mouse|cursor|window|tab|song|music|"
                        r"restaurant|store|near me|nearby|route|directions|way)\b")


def _clean(text: str) -> str:
    t = str(text).lower().strip()
    t = re.sub(r"\s+dot\s+([a-z0-9]{1,5})\b", r".\1", t)          # "resume dot pdf" -> "resume.pdf"
    t = re.sub(r"[?!,]+", " ", t).strip().rstrip(".")
    t = _FILLER_START.sub("", t)
    return " ".join(re.sub(r"\s+please$", "", t).split())


def spoken_file_command(text: str) -> FileCommand | None:
    """What was said -> a file command, or None if it isn't one (whole sentences only)."""
    t = _clean(text)
    if not t:
        return None
    m = _PICK.fullmatch(t)
    if m:
        return FileCommand("pick", index=_ORDINALS[m.group("n")],
                           pick_action="reveal" if m.group("where") or m.group("verb") == "reveal" else "open")
    m = _PICK_IT.fullmatch(t)
    if m:
        return FileCommand("pick", index=0, pick_action="open" if m.group("open") else "reveal")
    for action, pattern in _PATTERNS:
        m = pattern.fullmatch(t)
        if not m:
            continue
        words = re.sub(rf"^{_NAMED}", "", m.group("q")).strip(" .")
        words = re.sub(r"^(?:the|my|a|an)\s+", "", words)
        kind = (m.groupdict().get("kind") or "").strip()
        if kind and words in KINDS and KINDS[words] != KINDS[kind]:
            words, kind = kind, words                      # "find my screenshots folder": a folder called screenshots
        if not words or re.fullmatch(r"(?:it|that|this|them|one|ones|something|anything)", words):
            continue
        if not kind and "." not in words and _NOT_FILES.search(words):
            continue
        if kind == "song" or kind == "songs":
            if action == "open":
                continue                                   # "open the song X": music, not a file
        return FileCommand(action, words, kind)
    return None


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------

ACTIONS = {"open": lambda path: os.startfile(path),                          # noqa: S606 -- the user's own file
           "reveal": lambda path: subprocess.Popen(["explorer", "/select,", path])}
HOOKS = {"listing": None}                     # set by the controller: shows the full list in the chat
_LAST = {"hits": [], "until": 0.0}
LAST_SECONDS = 180.0
LIST_SIZE = 5


def friendly_folder(folder: str) -> str:
    """Where a file is, said simply: 'Downloads', 'the Work folder in Documents', 'the root of drive D'."""
    p = Path(folder)
    home = Path.home()
    try:
        rel = p.relative_to(home)
        parts = rel.parts
        if not parts:
            return "your user folder"
        if len(parts) == 1:
            return parts[0]
        return f"the {parts[-1]} folder in {parts[0]}"
    except ValueError:
        pass
    if p.parent == p:
        return f"the root of drive {p.drive.rstrip(':')}"
    return f"the {p.name} folder on drive {p.drive.rstrip(':')}" if p.drive else f"the {p.name} folder"


def _when(moment: datetime | None, today: date | None = None) -> str:
    if moment is None:
        return ""
    today = today or date.today()
    days = (today - moment.date()).days
    if days <= 0:
        return "changed today"
    if days == 1:
        return "changed yesterday"
    if days < 7:
        return f"changed on {moment:%A}"
    return f"changed on {moment:%B} {moment.day}" + (f", {moment.year}" if moment.year != today.year else "")


def _what(command: FileCommand) -> str:
    kind = command.kind
    noun = "folders" if kind.startswith("folder") else (kind if kind.endswith("s") else kind + "s") if kind else "files"
    return f"{noun} with \"{command.words}\" in the name"


def _listing(hits: list[Hit]) -> str:
    lines = [f"{i}. {h.path}" + (" (folder)" if h.is_folder else "") for i, h in enumerate(hits[:LIST_SIZE], 1)]
    return "Found:\n" + "\n".join(lines)


def _show_listing(hits: list[Hit]) -> None:
    hook = HOOKS["listing"]
    if hook is not None and hits:
        try:
            hook(_listing(hits))
        except Exception:  # noqa: BLE001 -- the spoken answer matters more
            pass


def _act(hit: Hit, action: str) -> str:
    ext = Path(hit.name).suffix.lower().lstrip(".")
    if action == "open" and not hit.is_folder and ext in RUNNABLE:
        ACTIONS["reveal"](hit.path)
        return (f"{hit.name} is a program or script, so I won't run it from a search. I've shown it in its folder "
                "instead.")
    try:
        ACTIONS[action](hit.path)
    except OSError as exc:
        return f"I couldn't open {hit.name}: {exc.strerror or exc.__class__.__name__}."
    if action == "reveal":
        return f"Here's {hit.name}, in {friendly_folder(hit.folder)}."
    return f"Opening {hit.name}."


def handle_file_command(text: str, system: bool = False, today: date | None = None) -> str | None:
    """The reply to a file command, or None if `text` isn't one."""
    command = spoken_file_command(text)
    if command is None:
        return None
    if command.action == "pick":
        hits = _LAST["hits"] if time.monotonic() < _LAST["until"] else []
        if not hits:
            return None                                   # "open it" with nothing found lately: someone else's
        try:
            hit = hits[command.index]
        except IndexError:
            return f"I only found {len(hits)}."
        return _act(hit, command.pick_action)
    try:
        hits = search(command.words, command.kind, system)
    except EverythingError as exc:
        return str(exc)
    if not hits:
        return f"I couldn't find any {_what(command)}."
    _LAST.update(hits=hits[:LIST_SIZE], until=time.monotonic() + LAST_SECONDS)
    best = hits[0]
    if command.action in ("open", "reveal"):
        if len(hits) > 1:
            _show_listing(hits)
        return _act(best, command.action)
    _show_listing(hits)
    when = _when(best.modified, today)
    where = f"in {friendly_folder(best.folder)}" + (f", {when}" if when else "")
    if len(hits) == 1:
        return f"I found {best.name} {where}. Say \"open it\" to open it."
    many = f"{len(hits)}" if len(hits) < 60 else "lots of"
    return (f"I found {many} {_what(command)}. The best match is {best.name} {where}. "
            f"Say \"open the first one\", or another number, to open it.")
