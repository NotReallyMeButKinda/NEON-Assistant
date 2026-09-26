"""
system_control.py -- the PC itself: volume, screen, power, disk, screenshots.

Everything here is ctypes against Windows' own APIs, so there is nothing to install:

  * volume    the real endpoint mixer (IAudioEndpointVolume through COM), so "set the volume to
              40 percent" lands on 40 percent instead of tapping the volume-up key six times.
              Falls back to the media keys if COM isn't available.
  * screen    a screenshot straight from GDI into a PNG written by hand (zlib + four chunks), so
              no image library is needed and it works from a worker thread.
  * power     lock, sleep, sign out, restart and shut down. The destructive ones go through
              `shutdown.exe` with a delay you can cancel by saying "cancel the shutdown".
  * info      uptime, free disk space, screen layout.

Anything that can't be undone by saying the opposite (shut down, restart, sign out, emptying the
recycle bin) returns a confirmation request first: `handle_system_command` hands back a
`(question, do_it)` pair via PENDING, and the router asks for a yes.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import time
import zlib
from ctypes import wintypes
from pathlib import Path

import neon_log

log = neon_log.get("system")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_VK_VOLUME_MUTE, _VK_VOLUME_DOWN, _VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
_CREATE_NO_WINDOW = 0x08000000


def _run(args: list[str]) -> bool:
    """Run a Windows helper without flashing a console window. True if it exited cleanly."""
    try:
        return subprocess.run(args, creationflags=_CREATE_NO_WINDOW, capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", args[0], exc)
        return False


# ---------------------------------------------------------------------------
# COM plumbing: just enough to reach IAudioEndpointVolume without comtypes
# ---------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


ole32 = ctypes.WinDLL("ole32", use_last_error=True)
ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_CLSCTX_ALL = 0x17
_E_RENDER, _E_CONSOLE = 0, 0            # eRender / eConsole
_RPC_E_CHANGED_MODE = -2147417850


def _guid(text: str) -> _GUID:
    out = _GUID()
    ole32.CLSIDFromString(text, ctypes.byref(out))
    return out


def _vcall(pointer, index: int, *args, argtypes=()):
    """Call method `index` of a COM object's vtable. Returns the HRESULT."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0]
    address = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return proto(address)(pointer, *args)


def _release(pointer) -> None:
    if pointer:
        try:
            _vcall(pointer, 2)
        except OSError:
            pass


class _Volume:
    """A short-lived IAudioEndpointVolume for the default playback device.

    Used as a context manager so COM is initialised and torn down on whichever worker thread asked;
    the interface is cheap to create and holding one across a device change would go stale."""

    def __enter__(self):
        self._com = False
        self._enumerator = ctypes.c_void_p()
        self._device = ctypes.c_void_p()
        self.ptr = ctypes.c_void_p()
        hr = ole32.CoInitializeEx(None, 0x2)           # COINIT_APARTMENTTHREADED
        self._com = hr >= 0 or hr == _RPC_E_CHANGED_MODE
        if ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_MMDeviceEnumerator)), None, _CLSCTX_ALL,
                                  ctypes.byref(_guid(_IID_IMMDeviceEnumerator)),
                                  ctypes.byref(self._enumerator)) < 0:
            raise OSError("no audio device enumerator")
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(dataFlow, role, **device)
        _vcall(self._enumerator, 4, _E_RENDER, _E_CONSOLE, ctypes.byref(self._device),
               argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
        if not self._device:
            raise OSError("no default playback device")
        # IMMDevice::Activate(iid, clsctx, params, **interface)
        _vcall(self._device, 3, ctypes.byref(_guid(_IID_IAudioEndpointVolume)), _CLSCTX_ALL, None,
               ctypes.byref(self.ptr),
               argtypes=(ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)))
        if not self.ptr:
            raise OSError("no volume control on the default device")
        return self

    def __exit__(self, *_exc) -> None:
        for pointer in (self.ptr, self._device, self._enumerator):
            _release(pointer)
        if self._com:
            try:
                ole32.CoUninitialize()
            except OSError:
                pass

    # IAudioEndpointVolume vtable slots
    def get_scalar(self) -> float:
        out = ctypes.c_float()
        _vcall(self.ptr, 9, ctypes.byref(out), argtypes=(ctypes.POINTER(ctypes.c_float),))
        return float(out.value)

    def set_scalar(self, value: float) -> None:
        _vcall(self.ptr, 7, ctypes.c_float(value), None,
               argtypes=(ctypes.c_float, ctypes.POINTER(_GUID)))

    def get_mute(self) -> bool:
        out = wintypes.BOOL()
        _vcall(self.ptr, 15, ctypes.byref(out), argtypes=(ctypes.POINTER(wintypes.BOOL),))
        return bool(out.value)

    def set_mute(self, muted: bool) -> None:
        _vcall(self.ptr, 14, wintypes.BOOL(bool(muted)), None,
               argtypes=(wintypes.BOOL, ctypes.POINTER(_GUID)))


def _tap(vk: int, times: int = 1) -> None:
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def get_volume() -> int | None:
    """System volume as a whole percentage, or None if it can't be read."""
    try:
        with _Volume() as volume:
            return int(round(volume.get_scalar() * 100))
    except OSError as exc:
        log.warning("reading the volume failed: %s", exc)
        return None


def set_volume(percent: float) -> str:
    """Set the system volume. Falls back to tapping the volume keys if COM is unavailable."""
    target = max(0, min(100, int(round(percent))))
    try:
        with _Volume() as volume:
            volume.set_scalar(target / 100.0)
            if target > 0 and volume.get_mute():
                volume.set_mute(False)
        return f"Volume set to {target} percent."
    except OSError as exc:
        log.warning("setting the volume failed: %s", exc)
        _tap(_VK_VOLUME_DOWN, 50)                       # down to zero, then up in ~2% steps
        _tap(_VK_VOLUME_UP, int(round(target / 2)))
        return f"Volume set to about {target} percent."


def nudge_volume(delta: int) -> str:
    """Relative change ('a bit louder'), reported with the number it landed on."""
    current = get_volume()
    if current is None:
        _tap(_VK_VOLUME_UP if delta > 0 else _VK_VOLUME_DOWN, max(1, abs(delta) // 2))
        return "Turning it up." if delta > 0 else "Turning it down."
    return set_volume(current + delta)


def set_mute(muted: bool | None = None) -> str:
    """True / False to set it, None to toggle."""
    try:
        with _Volume() as volume:
            target = (not volume.get_mute()) if muted is None else bool(muted)
            volume.set_mute(target)
            return "Sound muted." if target else "Sound unmuted."
    except OSError:
        _tap(_VK_VOLUME_MUTE)
        return "Toggled mute."


# ---------------------------------------------------------------------------
# Screenshots (GDI -> a PNG written by hand)
# ---------------------------------------------------------------------------

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_SRCCOPY, _CAPTUREBLT, _DIB_RGB_COLORS, _BI_RGB = 0x00CC0020, 0x40000000, 0, 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (len(payload).to_bytes(4, "big") + kind + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big"))


def _write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """Bottom-up BGRA rows (what GDI gives us) -> a top-down RGB PNG."""
    stride = width * 4
    rows = bytearray()
    for y in range(height - 1, -1, -1):                  # GDI's DIB is bottom-up
        row = bgra[y * stride:(y + 1) * stride]
        pixels = bytearray(width * 3)
        pixels[0::3] = row[2::4]                         # R  (the DIB is B, G, R, unused)
        pixels[1::3] = row[1::4]                         # G
        pixels[2::3] = row[0::4]                         # B
        rows.append(0)                                   # PNG filter type 0 (none)
        rows.extend(pixels)
    header = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
              + bytes([8, 2, 0, 0, 0]))                  # 8-bit, truecolour
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
                     + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
                     + _png_chunk(b"IEND", b""))


def screenshot(path: Path | None = None, whole_desktop: bool = True) -> Path:
    """Capture the screen to a PNG and return where it landed. Raises OSError if GDI says no."""
    if whole_desktop:
        left, top = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN), user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        width, height = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    else:
        left = top = 0
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if width <= 0 or height <= 0:
        raise OSError("the screen has no size")

    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    try:
        gdi32.SelectObject(memory_dc, bitmap)
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top,
                            _SRCCOPY | _CAPTUREBLT):
            raise OSError("the screen copy failed")
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, height
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        info.bmiHeader.biCompression = _BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), _DIB_RGB_COLORS):
            raise OSError("reading the captured pixels failed")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)

    if path is None:
        folder = Path.home() / "Pictures" / "Screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / time.strftime("neon-%Y%m%d-%H%M%S.png")
    _write_png(Path(path), width, height, buffer.raw)
    return Path(path)


# ---------------------------------------------------------------------------
# Power, session and the shell
# ---------------------------------------------------------------------------

def lock() -> str:
    return "Locking the PC." if user32.LockWorkStation() else "Windows wouldn't let me lock the PC."


def sleep() -> str:
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    ok = powrprof.SetSuspendState(0, 0, 0)              # hibernate=0, force=0, disableWake=0
    return "Going to sleep." if ok else "Windows wouldn't let me sleep the PC."


SHUTDOWN_DELAY = 20          # seconds you get to say "cancel the shutdown"


def shutdown(restart: bool = False) -> str:
    flag, gerund, verb = ("/r", "Restarting", "restart") if restart else ("/s", "Shutting down", "shut down")
    if _run(["shutdown", flag, "/t", str(SHUTDOWN_DELAY)]):
        return f"{gerund} in {SHUTDOWN_DELAY} seconds. Say \"cancel the shutdown\" if you change your mind."
    return f"Windows wouldn't let me {verb} the PC."


def cancel_shutdown() -> str:
    return ("Cancelled -- staying on." if _run(["shutdown", "/a"])
            else "There was no shutdown to cancel.")


def sign_out() -> str:
    return "Signing you out." if _run(["shutdown", "/l"]) else "Windows wouldn't let me sign you out."


def empty_recycle_bin() -> str:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    result = shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
    if result == 0:
        return "Recycle bin emptied."
    if result == -2147418113:            # E_UNEXPECTED (0x8000FFFF), which is what "already empty" is
        return "The recycle bin was already empty."
    return "I couldn't empty the recycle bin."


def show_desktop() -> str:
    user32.keybd_event(0x5B, 0, 0, 0)                   # Win
    user32.keybd_event(0x44, 0, 0, 0)                   # D
    user32.keybd_event(0x44, 0, 0x0002, 0)
    user32.keybd_event(0x5B, 0, 0x0002, 0)
    return "Showing the desktop."


def set_brightness(percent: int) -> str:
    """Laptop / built-in panels only (WMI). Desktop monitors don't expose this."""
    target = max(0, min(100, int(percent)))
    ok = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
               f".WmiSetBrightness(1,{target})"])
    return (f"Brightness set to {target} percent." if ok else
            "This screen doesn't let Windows change its brightness (that's usually desktop monitors).")


def uptime() -> str:
    seconds = kernel32.GetTickCount64() / 1000.0
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = ([f"{days} day{'s' if days != 1 else ''}"] if days else []) + \
            ([f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []) + \
            ([f"{minutes} minute{'s' if minutes != 1 else ''}"] if minutes or not (days or hours) else [])
    return "This PC has been up for " + " and ".join(parts) + "."


def disk_free(drive: str = "") -> str:
    root = f"{(drive or 'C').strip(':').upper()}:\\"
    free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not kernel32.GetDiskFreeSpaceExW(wintypes.LPCWSTR(root), None, ctypes.byref(total),
                                        ctypes.byref(free)):
        return f"I couldn't read drive {root[0]}."
    gb = 1024 ** 3
    percent = 100 * free.value / total.value if total.value else 0
    return (f"Drive {root[0]} has {free.value / gb:.0f} gigabytes free "
            f"out of {total.value / gb:.0f}, about {percent:.0f} percent.")


# ---------------------------------------------------------------------------
# Understanding what was said
# ---------------------------------------------------------------------------

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you|go ahead and)\s+)*"
_NUM = r"(\d{1,3})"

# "music volume" is included so that when Pear Desktop isn't running, "set the music volume to 40"
# still does the obvious thing to the PC's own mixer instead of falling through to the model.
_SET_VOLUME = re.compile(
    rf"^{_LEAD}(?:set|change|put|turn)\s+(?:the\s+)?"
    r"(?:system\s+|windows\s+|master\s+|music\s+|youtube music\s+)?volume"
    rf"\s*(?:to|at)\s*{_NUM}\s*(?:percent|%)?[.!]?$")
_GET_VOLUME = re.compile(
    rf"^{_LEAD}(?:what(?:'s| is)?|how loud is)\s+(?:the\s+)?(?:system\s+|current\s+)?volume"
    r"(?:\s+(?:at|set to|right now))?[?.]?$")
_MUTE_SYSTEM = re.compile(
    rf"^{_LEAD}(?P<off>un)?mute\s+(?:the\s+)?(?:system|pc|computer|windows|sound|audio|everything|speakers?)[.!]?$"
    rf"|^{_LEAD}(?:turn|switch)\s+(?:the\s+)?(?:sound|audio|volume)\s+(?P<onoff>off|on)[.!]?$"
    rf"|^{_LEAD}silence\s+(?:the\s+)?(?:pc|computer|system|speakers?)[.!]?$")
_LOCK = re.compile(rf"^{_LEAD}lock\s+(?:the\s+|my\s+)?(?:pc|computer|screen|workstation|machine|it)[.!]?$")
_SLEEP = re.compile(
    rf"^{_LEAD}(?:put\s+)?(?:the\s+|my\s+)?(?:pc|computer|machine|laptop)\s+(?:to\s+sleep|asleep)[.!]?$"
    rf"|^{_LEAD}(?:go to sleep|sleep the (?:pc|computer|machine|laptop)|suspend the (?:pc|computer))[.!]?$")
_SHUTDOWN = re.compile(
    rf"^{_LEAD}(?:shut\s*down|power\s*off|turn off)\s+(?:the\s+|my\s+)?(?:pc|computer|machine|laptop|windows|system)[.!]?$")
_RESTART = re.compile(
    rf"^{_LEAD}(?:restart|reboot)\s+(?:the\s+|my\s+)?(?:pc|computer|machine|laptop|windows|system)[.!]?$")
_CANCEL_SHUTDOWN = re.compile(
    rf"^{_LEAD}(?:cancel|stop|abort)\s+(?:the\s+)?(?:shutdown|shut down|restart|reboot)[.!]?$")
_SIGN_OUT = re.compile(rf"^{_LEAD}(?:sign|log)\s*(?:me\s+)?out(?:\s+of\s+windows)?[.!]?$")
_RECYCLE = re.compile(rf"^{_LEAD}(?:empty|clear|take out)\s+(?:the\s+)?(?:recycle bin|trash|rubbish bin|bin)[.!]?$")
_DESKTOP = re.compile(rf"^{_LEAD}(?:show|go to|minimi[sz]e to)\s+(?:the\s+)?desktop[.!]?$")
_SCREENSHOT = re.compile(
    rf"^{_LEAD}(?:take|grab|capture|get|make)?\s*(?:a\s+)?screen\s*(?:shot|grab|capture)"
    r"(?:\s+of\s+(?:the\s+)?(?:screen|desktop|everything))?[.!]?$")
_BRIGHTNESS = re.compile(
    rf"^{_LEAD}(?:set|change|turn|put)\s+(?:the\s+)?(?:screen\s+|display\s+)?brightness"
    rf"\s*(?:to|at)\s*{_NUM}\s*(?:percent|%)?[.!]?$")
_UPTIME = re.compile(
    rf"^{_LEAD}(?:what(?:'s| is)?\s+(?:the\s+|my\s+)?uptime|how long (?:has|have) (?:the |this |my )?"
    r"(?:pc|computer|machine|it) been (?:on|up|running))[?.]?$")
_DISK = re.compile(
    rf"^{_LEAD}(?:how much (?:disk |drive |storage )?space (?:is|do i have) (?:left|free|available)"
    r"|what(?:'s| is) my (?:disk|drive|storage) space"
    r"|how much (?:free )?(?:disk|storage) (?:space|do i have)"
    r"|how much space (?:is|do i have) (?:left|free|available)?)"
    r"(?:\s+on\s+(?:drive\s+)?(?P<drive>[a-z]))?[?.]?$")

# Actions worth a second thought before they happen.
NEEDS_CONFIRMATION = {"shutdown", "restart", "sign_out", "recycle_bin"}


def spoken_system_command(text: str) -> tuple[str, object] | None:
    """(action, argument) for one of the system actions, else None."""
    t = re.sub(r"[,]", " ", str(text).lower())
    t = " ".join(t.split())
    if not t:
        return None

    m = _SET_VOLUME.match(t)
    if m:
        return "set_volume", int(m.group(1))
    m = _BRIGHTNESS.match(t)
    if m:
        return "brightness", int(m.group(1))
    if _GET_VOLUME.match(t):
        return "get_volume", None
    m = _MUTE_SYSTEM.match(t)
    if m:
        if m.group("onoff"):
            return "mute", m.group("onoff") == "off"
        return "mute", not bool(m.group("off"))
    m = _DISK.match(t)
    if m:
        return "disk", (m.group("drive") or "C").upper()
    for pattern, action in ((_CANCEL_SHUTDOWN, "cancel_shutdown"), (_LOCK, "lock"), (_SLEEP, "sleep"),
                            (_SHUTDOWN, "shutdown"), (_RESTART, "restart"), (_SIGN_OUT, "sign_out"),
                            (_RECYCLE, "recycle_bin"), (_DESKTOP, "desktop"),
                            (_SCREENSHOT, "screenshot"), (_UPTIME, "uptime")):
        if pattern.match(t):
            return action, None
    return None


CONFIRMATIONS = {
    "shutdown": "Shut the PC down?",
    "restart": "Restart the PC?",
    "sign_out": "Sign out of Windows? Anything unsaved will be lost.",
    "recycle_bin": "Empty the recycle bin? That can't be undone.",
}


def perform(action: str, argument=None) -> str:
    """Run one system action and return what to say."""
    if action == "set_volume":
        return set_volume(argument)
    if action == "get_volume":
        level = get_volume()
        return f"The volume is at {level} percent." if level is not None else "I can't read the volume right now."
    if action == "mute":
        return set_mute(bool(argument))
    if action == "brightness":
        return set_brightness(argument)
    if action == "lock":
        return lock()
    if action == "sleep":
        return sleep()
    if action == "shutdown":
        return shutdown(restart=False)
    if action == "restart":
        return shutdown(restart=True)
    if action == "cancel_shutdown":
        return cancel_shutdown()
    if action == "sign_out":
        return sign_out()
    if action == "recycle_bin":
        return empty_recycle_bin()
    if action == "desktop":
        return show_desktop()
    if action == "uptime":
        return uptime()
    if action == "disk":
        return disk_free(str(argument or "C"))
    if action == "screenshot":
        try:
            path = screenshot()
        except OSError as exc:
            log.warning("screenshot failed: %s", exc)
            return "I couldn't take a screenshot."
        return f"Saved a screenshot to {path.parent.name} as {path.name}."
    return f"I don't know how to do '{action}'."
