"""
linuxdesk/clip.py -- the clipboard and the highlighted text, on Linux.

Wayland only lets the focused app read the clipboard through the toolkit, so this uses wl-clipboard
(wl-paste / wl-copy), which the compositor allows for any app; X11 sessions use xclip.

The *primary selection* is what's highlighted right now, in any app (the text a middle-click would paste),
so "summarize this" doesn't need the Windows trick of pressing Ctrl+C for you and putting your clipboard
back afterwards.
"""

from __future__ import annotations

import hashlib

import osinfo

MISSING = "I need wl-clipboard for that (Arch: the wl-clipboard package)."


def _paste_args(primary: bool) -> list[str] | None:
    if osinfo.wayland() and osinfo.which("wl-paste"):
        return ["wl-paste", "--no-newline", "--type", "text"] + (["--primary"] if primary else [])
    if osinfo.which("xclip"):
        return ["xclip", "-o", "-selection", "primary" if primary else "clipboard"]
    return None


def read(primary: bool = False) -> str:
    """The clipboard's text (or, with primary=True, the highlighted text); '' when empty or unreadable."""
    args = _paste_args(primary)
    if args is None:
        return ""
    done = osinfo.run(args, timeout=3)
    return done.out if done.ok else ""


def write(text: str) -> bool:
    if osinfo.wayland() and osinfo.which("wl-copy"):
        return osinfo.run(["wl-copy", "--type", "text/plain"], timeout=3, input_text=str(text)).ok
    if osinfo.which("xclip"):
        return osinfo.run(["xclip", "-selection", "clipboard"], timeout=3, input_text=str(text)).ok
    return False


def available() -> bool:
    return _paste_args(False) is not None


_LAST = {"text": ""}


def sequence_number() -> int:
    """A number that changes when the clipboard does (Windows has a real counter; here it's a hash of the
    text, and the text is kept so read_text() doesn't have to run wl-paste twice)."""
    text = read()
    _LAST["text"] = text
    return int(hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=6).hexdigest(), 16) if text else 0


def cached_text() -> str:
    return _LAST["text"]
