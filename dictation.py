"""
dictation.py -- "take dictation": what you say is typed into whatever window has focus.

Keystrokes go through SendInput with KEYEVENTF_UNICODE, so any character types correctly regardless
of the keyboard layout (an accented letter, an em dash, an emoji) and the target app sees ordinary
WM_KEYDOWN/WM_CHAR traffic rather than a clipboard paste -- which matters in editors and terminals
that treat a paste differently, and means nothing of yours is left on the clipboard afterwards.

    "take dictation" / "start dictation"    -> on
    "stop dictation" / "that's enough"      -> off
    "new line", "new paragraph", "full stop", "comma", "question mark", "open quote"...
    "scratch that" / "delete that"          -> backspace over the last thing typed

While dictation is on the controller sends recognised speech here instead of to the router, so
"open Chrome" types the words instead of launching anything -- except for the handful of control
phrases above, which is what makes it possible to stop.
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
from ctypes import wintypes

import neon_log
import osinfo

log = neon_log.get("dictation")

user32 = ctypes.WinDLL("user32", use_last_error=True) if osinfo.IS_WINDOWS else None

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_BACK, _VK_RETURN = 0x08, 0x0D


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    """Not used, but INPUT's union must be as wide as its largest member or Windows rejects the
    structure size outright (SendInput returns 0 and types nothing)."""
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


if user32 is not None:
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

BATCH = 60          # keystrokes per SendInput call: big enough to be fast, small enough to be safe


def _key_events(text: str) -> list[_INPUT]:
    events: list[_INPUT] = []
    for char in text:
        if char == "\n":
            for flags in (0, _KEYEVENTF_KEYUP):
                event = _INPUT(type=_INPUT_KEYBOARD)
                event.u.ki = _KEYBDINPUT(wVk=_VK_RETURN, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
                events.append(event)
            continue
        for code in _utf16_units(char):
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                event = _INPUT(type=_INPUT_KEYBOARD)
                event.u.ki = _KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None)
                events.append(event)
    return events


def _utf16_units(char: str) -> list[int]:
    """Characters outside the basic plane (emoji) need their two surrogates sent separately."""
    encoded = char.encode("utf-16-le")
    return [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]


def type_text(text: str) -> bool:
    """Type `text` into whatever has focus. True if Windows accepted every keystroke."""
    if not text:
        return True
    events = _key_events(text)
    ok = True
    for start in range(0, len(events), BATCH * 2):
        chunk = events[start:start + BATCH * 2]
        array = (_INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), array, ctypes.sizeof(_INPUT))
        if sent != len(chunk):
            log.warning("SendInput sent %d of %d events (error %d)", sent, len(chunk),
                        ctypes.get_last_error())
            ok = False
    return ok


def press_backspace(times: int = 1) -> None:
    events: list[_INPUT] = []
    for _ in range(max(0, times)):
        for flags in (0, _KEYEVENTF_KEYUP):
            event = _INPUT(type=_INPUT_KEYBOARD)
            event.u.ki = _KEYBDINPUT(wVk=_VK_BACK, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
            events.append(event)
    if events:
        array = (_INPUT * len(events))(*events)
        user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))


# ---------------------------------------------------------------------------
# Key combinations: a routine's "keys: ctrl+shift+esc" step
# ---------------------------------------------------------------------------

_KEYEVENTF_EXTENDEDKEY = 0x0001
if user32 is not None:
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT

MODIFIER_KEYS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
                 "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "super": 0x5B}
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pgdn": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "print": 0x2C, "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "menu": 0x5D, "apps": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "mute": 0xAD,
    "next": 0xB0, "medianext": 0xB0, "previous": 0xB1, "mediaprevious": 0xB1, "stop": 0xB2, "mediastop": 0xB2,
    "playpause": 0xB3, "mediaplay": 0xB3,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}
# Keys that live on the "extended" part of the keyboard: without the flag, Windows reads the arrows,
# Delete, Home... as their number-pad twins, and the Windows key not at all.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
             0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
# Combinations Windows keeps for itself: nothing an app sends can press them.
_RESERVED = {
    frozenset({0x11, 0x12, 0x2E}): "Windows doesn't let any app press Ctrl+Alt+Del. Use \"lock the PC\" or "
                                   "\"sign out\" instead, or keys: ctrl+shift+esc for Task Manager.",
    frozenset({0x5B, ord("L")}): "Windows doesn't let any app press Win+L. Use \"lock the PC\" instead.",
}


def _key_code(name: str) -> int:
    if name in MODIFIER_KEYS:
        return MODIFIER_KEYS[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    raise ValueError(f"I don't know the key \"{name}\".")


def parse_keys(spec: str) -> list[list[int]]:
    """"ctrl+shift+esc, win+d" -> one list of key codes per combination, pressed one after another.
    Raises ValueError with a message meant for the user."""
    combos: list[list[int]] = []
    for chunk in re.split(r"\s*,\s*|\s+then\s+", str(spec).strip().lower()):
        names = [" ".join(n.split()).replace(" ", "") for n in chunk.split("+")]
        if chunk.strip().endswith("+"):                   # "ctrl++" means Ctrl and the plus key
            names = [n for n in names if n] + ["="]
        names = [n for n in names if n]
        if not names:
            continue
        codes = [_key_code(n) for n in names]
        for reserved, message in _RESERVED.items():
            if reserved <= set(codes):
                raise ValueError(message)
        combos.append(codes)
    if not combos:
        raise ValueError("Name the keys to press, like ctrl+shift+esc.")
    return combos


def _key_event(vk: int, up: bool) -> _INPUT:
    flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    event = _INPUT(type=_INPUT_KEYBOARD)
    event.u.ki = _KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0), dwFlags=flags, time=0, dwExtraInfo=None)
    return event


def press_keys(spec: str) -> bool:
    """Press each combination in `spec` in the window that has focus: every key down in order, then
    up in reverse, the way fingers do it. True if Windows accepted every event."""
    ok = True
    for i, codes in enumerate(parse_keys(spec)):
        if i:
            time.sleep(0.05)                              # let the app react before the next one
        events = [_key_event(vk, False) for vk in codes] + [_key_event(vk, True) for vk in reversed(codes)]
        array = (_INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
        if sent != len(events):
            log.warning("SendInput sent %d of %d key events (error %d)", sent, len(events), ctypes.get_last_error())
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Turning spoken words into typed text
# ---------------------------------------------------------------------------

# Spoken -> what to type. Order matters only in that longer phrases are matched first.
PUNCTUATION = {
    "full stop": ".", "period": ".", "comma": ",", "question mark": "?", "exclamation mark": "!",
    "exclamation point": "!", "colon": ":", "semicolon": ";", "semi colon": ";",
    "dash": " - ", "hyphen": "-", "em dash": " -- ", "ellipsis": "...", "dot dot dot": "...",
    "open bracket": "(", "close bracket": ")", "open parenthesis": "(", "close parenthesis": ")",
    "open quote": "“", "close quote": "”", "quote": "\"", "apostrophe": "'",
    "ampersand": "&", "at sign": "@", "hash": "#", "hash tag": "#", "percent sign": "%",
    "dollar sign": "$", "pound sign": "£", "euro sign": "€", "plus sign": "+",
    "equals sign": "=", "slash": "/", "forward slash": "/", "back slash": "\\",
    "asterisk": "*", "star": "*", "underscore": "_", "tilde": "~", "pipe": "|",
    "smiley face": ":)", "emoji smile": "\U0001f642",
}
NEWLINES = {"new line": "\n", "newline": "\n", "next line": "\n", "line break": "\n",
            "new paragraph": "\n\n", "paragraph break": "\n\n", "blank line": "\n\n"}
_TAB = {"tab", "tab key", "indent"}

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you)\s+)*"
START = re.compile(
    rf"^{_LEAD}(?:take|start|begin|enter|turn on)\s+(?:dictation|dictation mode|typing mode|"
    r"transcription)[.!]?$"
    rf"|^{_LEAD}(?:type|write)\s+(?:what|whatever)\s+i\s+say[.!]?$"
    rf"|^{_LEAD}dictation\s+(?:mode\s+)?on[.!]?$")
STOP = re.compile(
    rf"^{_LEAD}(?:stop|end|finish|exit|leave|turn off|cancel)\s+(?:the\s+)?(?:dictation|dictating|"
    r"dictation mode|typing mode|transcription)[.!]?$"
    rf"|^{_LEAD}(?:that'?s (?:enough|it)|that is (?:enough|it)|done dictating|stop typing)[.!]?$"
    rf"|^{_LEAD}dictation\s+(?:mode\s+)?off[.!]?$")
SCRATCH = re.compile(
    rf"^{_LEAD}(?:scratch|delete|undo|remove|forget)\s+(?:that|the last (?:bit|line|sentence|thing))[.!]?$")
# "type this: hello there" -- a single line, without entering dictation mode at all.
ONE_SHOT = re.compile(
    rf"^{_LEAD}(?:type|write|enter|insert)\s+(?:this|the following|out)?[:,]?\s+(?P<what>.+)$")

_SENTENCE_END = re.compile(r"[.!?:;]\s*$")
_CAP_AFTER = re.compile(r"(?:^|[.!?]\s+|\n\s*)([a-z])")


def transform(spoken: str, *, capitalize: bool = True, leading_space: bool = True) -> str:
    """One recognised phrase -> the characters to type.

    Spoken punctuation becomes real punctuation, sentences are capitalised, and a space is put in
    front unless the phrase starts with punctuation or a new line."""
    text = " ".join(str(spoken).split())
    if not text:
        return ""
    lowered = text.lower().strip(" .,!?")

    for table in (NEWLINES, PUNCTUATION):
        if lowered in table:
            return table[lowered]
    if lowered in _TAB:
        return "\t"

    # Punctuation words inside a sentence: "hello there comma how are you question mark".
    for phrase in sorted({**NEWLINES, **PUNCTUATION}, key=len, reverse=True):
        replacement = NEWLINES.get(phrase) or PUNCTUATION[phrase]
        glue = "" if replacement in (".", ",", "?", "!", ":", ";", ")", "”") else " "
        text = re.sub(rf"\s*\b{re.escape(phrase)}\b\s*", glue + replacement + " ", text, flags=re.I)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"([(“])\s+", r"\1", text)          # no gap after an opening bracket or quote
    text = re.sub(r"[ \t]+", " ", text).strip(" ")
    if not text:
        return ""

    if capitalize:
        text = _CAP_AFTER.sub(lambda m: m.group(0).upper(), text)
        text = text[0].upper() + text[1:]
    if leading_space and text[0] not in ".,!?;:)\n”":
        text = " " + text
    return text


class Session:
    """Dictation state: whether it is on, and enough history to undo the last phrase."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = False
        self._typed: list[int] = []            # how many characters each phrase put on screen

    def start(self) -> str:
        with self._lock:
            self.active = True
            self._typed.clear()
        return ("Dictation on. Everything you say goes into the focused window. "
                "Say \"stop dictation\" when you're done.")

    def stop(self) -> str:
        with self._lock:
            was, self.active = self.active, False
            self._typed.clear()
        return "Dictation off." if was else "Dictation wasn't on."

    def feed(self, spoken: str) -> str:
        """Handle one phrase while dictation is on. Returns the status line to show (never spoken
        aloud by the caller, so the assistant doesn't talk over the typing)."""
        text = " ".join(str(spoken).split())
        if not text:
            return ""
        lowered = re.sub(r"[.!?,]+$", "", text.lower()).strip()
        if STOP.match(lowered):
            return self.stop()
        if SCRATCH.match(lowered):
            return self.scratch()
        first = not self._typed
        typed = transform(text, leading_space=not first)
        if not typed:
            return ""
        if type_text(typed):
            with self._lock:
                self._typed.append(len(typed))
            return f"Typed: {typed.strip()}"
        return "I couldn't type that into the focused window."

    def scratch(self) -> str:
        with self._lock:
            count = self._typed.pop() if self._typed else 0
        if not count:
            return "There's nothing to take back."
        press_backspace(count)
        return "Taken back."


if not osinfo.IS_WINDOWS:                       # Linux: wtype / ydotool / xdotool (linuxdesk/input.py)
    from linuxdesk import input as _input

    def type_text(text: str) -> bool:  # noqa: F811
        ok = _input.type_text(text)
        if not ok and text:
            log.warning("typing failed: %s", _input.MISSING if not _input.tool() else "the helper refused")
        return ok

    def press_backspace(times: int = 1) -> None:  # noqa: F811
        try:
            _input.press_backspace(times)
        except ValueError as exc:
            log.warning("backspace failed: %s", exc)

    def parse_keys(spec: str):  # noqa: F811
        return _input.parse(spec)

    def press_keys(spec: str) -> bool:  # noqa: F811
        return _input.press_keys(spec)
