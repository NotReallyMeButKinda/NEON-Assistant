"""
hotkeys.py -- system-wide hotkeys (they work while another app has focus).

  * HotkeyManager   any number of independent binds via Win32 RegisterHotKey; WM_HOTKEY
                    arrives on the Qt UI thread through a native event filter, so callbacks can
                    touch widgets/controllers directly.
  * CopilotKeyHook  takes over the Copilot key and Win+C, which Windows reserves for itself
                    (RegisterHotKey can't claim them), with a low-level keyboard hook that
                    swallows the keys so Copilot never opens.
  * HoldKeyHook     hold-to-talk (key down / key up), with the same kind of hook.
  * KeyCapture      while the quick box is open over a fullscreen game, brings the typing to it
                    without taking focus from the game.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'Ctrl+Alt+V' -> (modifier flags, virtual-key code). Raises ValueError with a
    user-readable message when the combination isn't usable."""
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        raise ValueError("Press a key combination.")
    key_name, mod_names = parts[-1], parts[:-1]
    mods = 0
    for name in mod_names:
        if name not in _MODIFIERS:
            raise ValueError(f"Unknown modifier '{name}'.")
        mods |= _MODIFIERS[name]
    if key_name in _MODIFIERS:
        raise ValueError("Add a regular key after the modifiers (e.g. Ctrl+Alt+V).")
    if len(key_name) == 1 and key_name.isalnum():
        vk = ord(key_name.upper())
    elif key_name.startswith("f") and key_name[1:].isdigit() and 1 <= int(key_name[1:]) <= 24:
        vk = 0x70 + int(key_name[1:]) - 1
    elif key_name in _NAMED_KEYS:
        vk = _NAMED_KEYS[key_name]
    else:
        raise ValueError(f"Unsupported key '{key_name}'.")
    if not mods and not (key_name.startswith("f") and key_name[1:].isdigit()):
        raise ValueError("Include at least one modifier (Ctrl, Alt, Shift or Win), "
                         "otherwise the key would stop working everywhere else.")
    return mods, vk


class HotkeyManager(QAbstractNativeEventFilter):
    """Owns every RegisterHotKey bind. Call apply() with the full set each time settings change."""

    _BASE_ID = 0xA11C

    def __init__(self):
        super().__init__()
        self._callbacks: dict[int, callable] = {}

    def apply(self, binds: list[tuple[str, str, callable]]) -> list[str]:
        """binds: (label, spec, callback). Replaces all registered hotkeys; an empty spec means
        'unmapped' and is skipped. Returns a list of error messages (empty on full success)."""
        self.unregister()
        errors: list[str] = []
        for label, spec, callback in binds:
            spec = str(spec or "").strip()
            if not spec:
                continue
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
                continue
            hotkey_id = self._BASE_ID + len(self._callbacks)
            if not ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                hint = (" Windows keeps Win+C for Copilot; use the Copilot key option instead."
                        if spec.lower().replace(" ", "") == "meta+c" or spec.lower() == "win+c" else "")
                errors.append(f"{label}: couldn't register {spec} -- another program probably already "
                              f"uses it.{hint}")
                continue
            self._callbacks[hotkey_id] = callback
        return errors

    def unregister(self) -> None:
        for hotkey_id in self._callbacks:
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
        self._callbacks.clear()

    def nativeEventFilter(self, event_type, message):
        if self._callbacks and bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self._callbacks:
                self._callbacks[msg.wParam]()
                return True, 0
        return False, 0


# ---------------------------------------------------------------------------
# Copilot key / Win+C
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)   # private handle: our argtypes stay ours
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
_WM_KEYDOWN, _WM_SYSKEYDOWN = 0x0100, 0x0104
VK_C, VK_F23 = 0x43, 0x86              # the Copilot key sends LWin + LShift + F23
_VK_LWIN, _VK_RWIN, _VK_SHIFT, _VK_CONTROL, _VK_MENU = 0x5B, 0x5C, 0x10, 0x11, 0x12
_MASK_VK = 0xE8                        # an unassigned key, pressed to cancel the Start menu
_MARK = 0x4E454F4E                     # "NEON" in dwExtraInfo: our own injected keys, ignored by the hook
_KEYEVENTF_KEYUP = 0x0002


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _held(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class CopilotKeyHook:
    """Swallows the Copilot key (Win+Shift+F23) and Win+C and calls `on_trigger` instead.

    Runs a low-level keyboard hook on the thread that calls install() (the Qt UI thread, whose
    message loop services it). The callback only schedules `on_trigger` -- hook procedures have
    a strict time limit. `keys` is overridable so tests can use a harmless key."""

    def __init__(self, on_trigger, keys: tuple[int, ...] = (VK_C, VK_F23)):
        self._on_trigger = on_trigger
        self._keys = frozenset(keys)
        self._hook = None
        self._proc = None              # keep a reference: ctypes callbacks are freed when collected
        self._swallowed: set[int] = set()

    @property
    def installed(self) -> bool:
        return self._hook is not None

    def install(self) -> bool:
        if self._hook is not None:
            return True
        self._proc = _HOOKPROC(self._callback)
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self._hook, self._proc = None, None
            return False
        return True

    def uninstall(self) -> None:
        if self._hook is not None:
            _user32.UnhookWindowsHookEx(self._hook)
        self._hook, self._proc = None, None
        self._swallowed.clear()

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0:
                kb = _KBDLLHOOKSTRUCT.from_address(l_param)
                if kb.dwExtraInfo != _MARK and kb.vkCode in self._keys:
                    if self._handle(kb.vkCode, w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN)):
                        return 1       # swallowed: nobody else sees this key event
        except Exception:  # noqa: BLE001 -- a hook must never raise; pass the key on untouched
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _handle(self, vk: int, down: bool) -> bool:
        if not down:
            if vk in self._swallowed:      # the release of a key whose press we swallowed
                self._swallowed.discard(vk)
                return True
            return False
        if not (_held(_VK_LWIN) or _held(_VK_RWIN)):
            return False
        if vk == VK_C and (_held(_VK_CONTROL) or _held(_VK_MENU) or _held(_VK_SHIFT)):
            return False                   # Ctrl/Alt/Shift+Win+C is some other shortcut
        first = vk not in self._swallowed  # holding the key auto-repeats; fire once
        self._swallowed.add(vk)
        if first:
            # Win was pressed and released with nothing typed in between would open the Start
            # menu; tap an unassigned key so Windows sees a chord.
            _user32.keybd_event(_MASK_VK, 0, 0, _MARK)
            _user32.keybd_event(_MASK_VK, 0, _KEYEVENTF_KEYUP, _MARK)
            QTimer.singleShot(0, self._on_trigger)
        return True


class HoldKeyHook:
    """Hold-to-talk: calls `on_press` when the chosen combination goes down and `on_release` when
    its main key comes back up. RegisterHotKey can't see key-up, so this uses the same low-level
    hook as CopilotKeyHook. The key is swallowed so it doesn't also type or trigger anything."""

    def __init__(self, on_press, on_release):
        self._on_press, self._on_release = on_press, on_release
        self._mods = 0
        self._vk = 0
        self._hook = None
        self._proc = None
        self._active = False

    @property
    def installed(self) -> bool:
        return self._hook is not None

    def set_combo(self, spec: str) -> str | None:
        """Use `spec` ('Ctrl+Alt+Space'); '' switches it off. Returns an error message, or None."""
        self.uninstall()
        spec = str(spec or "").strip()
        if not spec:
            return None
        try:
            self._mods, self._vk = parse_hotkey(spec)
        except ValueError as exc:
            return f"Hold to talk: {exc}"
        self._proc = _HOOKPROC(self._callback)
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self._hook, self._proc = None, None
            return "Hold to talk: Windows wouldn't let me listen for that key."
        return None

    def uninstall(self) -> None:
        if self._hook is not None:
            _user32.UnhookWindowsHookEx(self._hook)
        self._hook, self._proc, self._active = None, None, False

    def _mods_held(self) -> bool:
        needed = ((MOD_CONTROL, _VK_CONTROL), (MOD_ALT, _VK_MENU), (MOD_SHIFT, _VK_SHIFT))
        if any(self._mods & flag and not _held(vk) for flag, vk in needed):
            return False
        return not (self._mods & MOD_WIN) or _held(_VK_LWIN) or _held(_VK_RWIN)

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0:
                kb = _KBDLLHOOKSTRUCT.from_address(l_param)
                if kb.dwExtraInfo != _MARK and kb.vkCode == self._vk:
                    down = w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                    if down and (self._active or self._mods_held()):
                        if not self._active:
                            self._active = True
                            if self._mods & MOD_WIN:               # don't let releasing Win open Start
                                _user32.keybd_event(_MASK_VK, 0, 0, _MARK)
                                _user32.keybd_event(_MASK_VK, 0, _KEYEVENTF_KEYUP, _MARK)
                            QTimer.singleShot(0, self._on_press)
                        return 1                                    # (auto-repeat is swallowed too)
                    if not down and self._active:
                        self._active = False
                        QTimer.singleShot(0, self._on_release)
                        return 1
        except Exception:  # noqa: BLE001 -- a hook must never raise; pass the key on untouched
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)


# ---------------------------------------------------------------------------
# Typing into the quick box without taking focus (over a fullscreen game)
# ---------------------------------------------------------------------------

_user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR,
                                ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
_user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_user32.GetKeyboardLayout.restype = ctypes.c_void_p
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetKeyState.argtypes = [ctypes.c_int]
_user32.GetKeyState.restype = ctypes.c_short

_VK_CAPITAL, _VK_LSHIFT, _VK_RSHIFT, _VK_LCONTROL, _VK_RCONTROL, _VK_LMENU, _VK_RMENU = (
    0x14, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
# Keys that always reach the game: modifiers (so its idea of what is held stays right) and lock keys.
_PASS_THROUGH = {_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN, _VK_CAPITAL, _VK_LSHIFT, _VK_RSHIFT,
                 _VK_LCONTROL, _VK_RCONTROL, _VK_LMENU, _VK_RMENU, 0x90, 0x91}   # + Num Lock, Scroll Lock
_TOUNICODE_NO_STATE_CHANGE = 0x4            # don't disturb dead keys for the app that has focus


def key_text(vk: int, scan: int) -> str:
    """The character a key press would type with the current Shift / Caps Lock / AltGr state and the
    foreground window's keyboard layout ("" for keys that type nothing)."""
    state = (ctypes.c_ubyte * 256)()
    for key in (_VK_SHIFT, _VK_LSHIFT, _VK_RSHIFT, _VK_CONTROL, _VK_LCONTROL, _VK_RCONTROL, _VK_MENU,
                _VK_LMENU, _VK_RMENU):
        if _held(key):
            state[key] = 0x80
    if _user32.GetKeyState(_VK_CAPITAL) & 1:
        state[_VK_CAPITAL] = 0x01
    thread = _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), None)
    buffer = ctypes.create_unicode_buffer(8)
    count = _user32.ToUnicodeEx(vk, scan, state, buffer, 8, _TOUNICODE_NO_STATE_CHANGE,
                                _user32.GetKeyboardLayout(thread))
    text = buffer.value[:count] if count > 0 else ""
    return text if text.isprintable() else ""


class KeyCapture:
    """While installed, every key press goes to `on_key(vk, text, ctrl)` instead of the app that has
    focus -- so the quick box can be typed into over a fullscreen game without taking focus from it
    (a game in exclusive fullscreen minimizes the moment it loses focus).

    Modifiers and lock keys pass through untouched, and so does any Alt / Win combination or a
    Ctrl one other than Ctrl+V / Ctrl+A / Ctrl+Backspace: Alt+Tab, the Win key and the app's own
    hotkeys keep working. Uninstall it the moment the box closes."""

    CTRL_KEYS = {0x56, 0x41, 0x08}          # V (paste), A (select all), Backspace (delete a word)

    def __init__(self, on_key):
        self._on_key = on_key
        self._hook = None
        self._proc = None
        self._swallowed: set[int] = set()

    @property
    def installed(self) -> bool:
        return self._hook is not None

    def install(self) -> bool:
        if self._hook is not None:
            return True
        self._proc = _HOOKPROC(self._callback)
        self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            self._hook, self._proc = None, None
            return False
        return True

    def uninstall(self) -> None:
        if self._hook is not None:
            _user32.UnhookWindowsHookEx(self._hook)
        self._hook, self._proc = None, None
        self._swallowed.clear()

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and self._hook is not None:
                kb = _KBDLLHOOKSTRUCT.from_address(l_param)
                if kb.dwExtraInfo != _MARK and self._take(kb.vkCode, kb.scanCode,
                                                          w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN)):
                    return 1
        except Exception:  # noqa: BLE001 -- a hook must never raise; pass the key on untouched
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _take(self, vk: int, scan: int, down: bool) -> bool:
        """True to swallow this key event."""
        if vk in _PASS_THROUGH:
            return False
        if not down:
            if vk in self._swallowed:            # the release of a press we took
                self._swallowed.discard(vk)
                return True
            return False
        ctrl = _held(_VK_CONTROL)
        if _held(_VK_MENU) and not ctrl or _held(_VK_LWIN) or _held(_VK_RWIN):
            return False                         # Alt+Tab, Win+anything: Windows' own
        if ctrl and not _held(_VK_MENU) and vk not in self.CTRL_KEYS:
            return False                         # Ctrl shortcuts (Ctrl+Alt is AltGr: that types)
        if ctrl and _held(_VK_MENU) and not key_text(vk, scan):
            return False                         # a Ctrl+Alt hotkey, not an AltGr character
        self._swallowed.add(vk)
        shortcut = ctrl and not _held(_VK_MENU)
        text = "" if shortcut else key_text(vk, scan)
        QTimer.singleShot(0, lambda: self._on_key(vk, text, shortcut))     # hooks must return quickly
        return True
