"""
selection.py -- read the text the user has highlighted in another app (Windows).

Two strategies, no screenshots / OCR:
  * copy_selection():  simulate Ctrl+C, read the clipboard, then put the user's previous
                       clipboard back (every format, not just text).
  * uia_selection():   ask Windows UI Automation for the focused element's selection, which
                       never touches the clipboard. Needs the optional `uiautomation` package.

The caller (assistant.py) decides which apps are safe for a Ctrl+C.
"""

from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes

import osinfo

user32 = kernel32 = None
if osinfo.IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)      # private handles: our argtypes don't
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # leak into other ctypes users

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_HWND_MESSAGE = -3
COPY_WAIT = 0.7     # seconds to wait for the target app to put its selection on the clipboard

# Formats whose handles aren't plain global memory (bitmaps, metafiles, palettes, owner-drawn,
# private GDI ranges) can't be copied as bytes. The DIB formats cover images and Windows
# re-synthesizes the bitmap ones from them.
_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x85, 0x8E}


class SelectionError(Exception):
    """The message is ready to be spoken to the user."""


@contextmanager
def _clipboard():
    """Opens the clipboard with a real owner window (an ownerless clipboard can be read but
    not emptied-and-refilled), retrying briefly because other apps hold it for a moment."""
    hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, _HWND_MESSAGE, None, None, None)
    try:
        for _ in range(15):
            if user32.OpenClipboard(hwnd):
                break
            time.sleep(0.02)
        else:
            raise OSError("the clipboard is busy")
        try:
            yield
        finally:
            user32.CloseClipboard()
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)


def _snapshot() -> list[tuple[int, bytes]]:
    """Every byte-copyable format currently on the clipboard. Call inside _clipboard()."""
    items: list[tuple[int, bytes]] = []
    fmt = 0
    while True:
        fmt = user32.EnumClipboardFormats(fmt)
        if not fmt:
            return items
        if fmt in _HANDLE_FORMATS or 0x300 <= fmt <= 0x3FF:
            continue
        handle = user32.GetClipboardData(fmt)
        size = kernel32.GlobalSize(handle) if handle else 0
        ptr = kernel32.GlobalLock(handle) if size else None
        if not ptr:
            continue
        try:
            items.append((fmt, ctypes.string_at(ptr, size)))
        finally:
            kernel32.GlobalUnlock(handle)


def _restore(items: list[tuple[int, bytes]]) -> None:
    """Replaces the clipboard contents with a _snapshot()."""
    with _clipboard():
        user32.EmptyClipboard()
        for fmt, data in items:
            handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data) or 1)
            if not handle:
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                continue
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)   # ownership only transfers on success


def _read_text() -> str:
    """Unicode text on the clipboard ('' if none). Call inside _clipboard()."""
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    ptr = kernel32.GlobalLock(handle) if handle else None
    if not ptr:
        return ""
    try:
        return ctypes.wstring_at(ptr)
    finally:
        kernel32.GlobalUnlock(handle)


def _send_ctrl_c() -> None:
    for vk in (0x11, 0x43):                 # Ctrl down, C down
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (0x43, 0x11):                 # C up, Ctrl up
        user32.keybd_event(vk, 0, 2, 0)


def copy_selection() -> str | None:
    """Ctrl+C in the focused app; returns what it copied, or None if nothing was copied (no
    selection, or an app that blocks copy). The user's clipboard is put back afterwards."""
    try:
        with _clipboard():
            saved = _snapshot()
    except OSError:
        return None     # can't read the clipboard, so we couldn't restore it either: don't touch it

    before = user32.GetClipboardSequenceNumber()
    _send_ctrl_c()
    deadline = time.monotonic() + COPY_WAIT
    while user32.GetClipboardSequenceNumber() == before:
        if time.monotonic() >= deadline:
            return None     # the clipboard never changed: nothing was selected (or copy is blocked)
        time.sleep(0.03)
    time.sleep(0.05)        # some apps clear, then fill in several steps

    text = None
    try:
        with _clipboard():
            text = _read_text()
    except OSError:
        pass
    finally:
        try:
            _restore(saved)
        except OSError:
            pass
    return text


def uia_selection() -> str:
    """The focused element's selected text via UI Automation ('' if unavailable). Never uses
    the clipboard or sends keys, so it is also safe in terminals."""
    try:
        import uiautomation as auto
    except ImportError:
        return ""
    try:
        with auto.UIAutomationInitializerInThread():
            element = auto.GetFocusedControl()
            pattern = element.GetPattern(auto.PatternId.TextPattern) if element else None
            if pattern is None:
                return ""
            return "".join(r.GetText(-1) for r in pattern.GetSelection())
    except Exception:  # noqa: BLE001 -- UIA raises assorted COM errors for unsupported controls
        return ""


# ---------------------------------------------------------------------------
# Plain clipboard access (used by clipboard.py's history and "copy that")
# ---------------------------------------------------------------------------

def sequence_number() -> int:
    """Bumped by Windows whenever anything changes the clipboard: a cheap 'has it changed?' poll."""
    return int(user32.GetClipboardSequenceNumber())


def read_text() -> str:
    """Whatever text is on the clipboard right now ('' if none, or if it is busy)."""
    try:
        with _clipboard():
            return _read_text()
    except OSError:
        return ""


def set_text(text: str) -> bool:
    """Put `text` on the clipboard, replacing what was there. True if it worked."""
    payload = str(text).encode("utf-16-le") + b"\x00\x00"
    try:
        with _clipboard():
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)     # ownership only transfers on success
                return False
            return True
    except OSError:
        return False


if not osinfo.IS_WINDOWS:
    # Linux: the highlighted text *is* available directly (the primary selection), so nothing is pressed and
    # the clipboard is never touched. wl-clipboard (xclip on X11) does the reading and writing.
    from linuxdesk import clip as _clip

    def copy_selection() -> str | None:  # noqa: F811
        text = _clip.read(primary=True)
        return text if text.strip() else None

    def uia_selection() -> str:  # noqa: F811
        return ""

    def sequence_number() -> int:  # noqa: F811
        return _clip.sequence_number()

    def read_text() -> str:  # noqa: F811
        return _clip.read()

    def set_text(text: str) -> bool:  # noqa: F811
        return _clip.write(text)
