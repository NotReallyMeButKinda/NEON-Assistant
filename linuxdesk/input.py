"""
linuxdesk/input.py -- typing text and pressing keys in the focused window, on Linux (dictation, routines'
"keys:" steps, Bitwarden's "type my password").

Wayland doesn't let one app type into another, so this uses a helper the compositor allows:
  * wtype      -- the virtual-keyboard protocol of wlroots compositors (Hyprland, Sway). Nothing to set up.
  * ydotool    -- works everywhere (KDE included) through the kernel's uinput; its daemon (ydotoold) must be
                  running, and your user needs access to /dev/uinput (Arch: the ydotool package; see INSTALL.md).
  * xdotool    -- X11 sessions.
The key spec is the same as on Windows: "ctrl+shift+esc, super+d".
"""

from __future__ import annotations

import re
import time

import osinfo

# names in NEON's key specs -> (wtype / xdotool name, Linux input event code for ydotool)
MODIFIERS = {"ctrl": ("ctrl", 29), "control": ("ctrl", 29), "alt": ("alt", 56), "shift": ("shift", 42),
             "win": ("logo", 125), "windows": ("logo", 125), "meta": ("logo", 125), "super": ("logo", 125)}
_XDO_MODS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "logo": "super"}
NAMED = {"space": ("space", 57), "enter": ("Return", 28), "return": ("Return", 28), "tab": ("Tab", 15),
         "esc": ("Escape", 1), "escape": ("Escape", 1), "backspace": ("BackSpace", 14), "delete": ("Delete", 111),
         "del": ("Delete", 111), "insert": ("Insert", 110), "ins": ("Insert", 110), "home": ("Home", 102),
         "end": ("End", 107), "pgup": ("Prior", 104), "pageup": ("Prior", 104), "pgdown": ("Next", 109),
         "pgdn": ("Next", 109), "pagedown": ("Next", 109), "left": ("Left", 105), "up": ("Up", 103),
         "right": ("Right", 106), "down": ("Down", 108), "print": ("Print", 99), "printscreen": ("Print", 99),
         "prtsc": ("Print", 99), "pause": ("Pause", 119), "capslock": ("Caps_Lock", 58),
         "numlock": ("Num_Lock", 69), "scrolllock": ("Scroll_Lock", 70), "menu": ("Menu", 127),
         "apps": ("Menu", 127), "volumeup": ("XF86AudioRaiseVolume", 115),
         "volumedown": ("XF86AudioLowerVolume", 114), "volumemute": ("XF86AudioMute", 113),
         "mute": ("XF86AudioMute", 113), "next": ("XF86AudioNext", 163), "medianext": ("XF86AudioNext", 163),
         "previous": ("XF86AudioPrev", 165), "mediaprevious": ("XF86AudioPrev", 165),
         "stop": ("XF86AudioStop", 166), "mediastop": ("XF86AudioStop", 166),
         "playpause": ("XF86AudioPlay", 164), "mediaplay": ("XF86AudioPlay", 164),
         "`": ("grave", 41), "-": ("minus", 12), "=": ("equal", 13), "[": ("bracketleft", 26),
         "]": ("bracketright", 27), ";": ("semicolon", 39), "'": ("apostrophe", 40), ",": ("comma", 51),
         ".": ("period", 52), "/": ("slash", 53), "\\": ("backslash", 43)}
_LETTER_CODES = dict(zip("qwertyuiop", range(16, 26))) | dict(zip("asdfghjkl", range(30, 39))) | \
    dict(zip("zxcvbnm", range(44, 51))) | {"1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
                                           "9": 10, "0": 11}
_F_CODES = {1: 59, 2: 60, 3: 61, 4: 62, 5: 63, 6: 64, 7: 65, 8: 66, 9: 67, 10: 68, 11: 87, 12: 88}


def tool() -> str:
    """'wtype' | 'ydotool' | 'xdotool' | '' -- the best typing helper here."""
    if osinfo.wayland():
        if osinfo.desktop() != "kde" and osinfo.which("wtype"):
            return "wtype"                        # KWin doesn't implement the virtual-keyboard protocol
        if osinfo.which("ydotool"):
            return "ydotool"
        return ""
    return "xdotool" if osinfo.which("xdotool") else ("ydotool" if osinfo.which("ydotool") else "")


MISSING = ("I need a typing helper for that: wtype on Hyprland, or ydotool (with its ydotoold service running) "
           "on KDE. See INSTALL.md.")


def key(name: str) -> tuple[str, int, bool]:
    """(wtype/xdotool name, ydotool code, is_modifier) for one key name. Raises ValueError for the user."""
    if name in MODIFIERS:
        sym, code = MODIFIERS[name]
        return sym, code, True
    if name in NAMED:
        sym, code = NAMED[name]
        return sym, code, False
    if len(name) == 1 and name.isalnum():
        return name.lower(), _LETTER_CODES.get(name.lower(), 0), False
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return name.upper(), _F_CODES[int(name[1:])], False
    raise ValueError(f"I don't know the key \"{name}\".")


def parse(spec: str) -> list[list[tuple[str, int, bool]]]:
    """"ctrl+shift+esc, super+d" -> one list of keys per combination. Raises ValueError for the user."""
    combos = []
    for chunk in re.split(r"\s*,\s*|\s+then\s+", str(spec).strip().lower()):
        names = [" ".join(n.split()).replace(" ", "") for n in chunk.split("+")]
        if chunk.strip().endswith("+"):
            names = [n for n in names if n] + ["="]
        names = [n for n in names if n]
        if names:
            combos.append([key(n) for n in names])
    if not combos:
        raise ValueError("Name the keys to press, like ctrl+shift+esc.")
    return combos


def _combo_args(helper: str, combo: list[tuple[str, int, bool]]) -> list[str]:
    mods = [k for k in combo if k[2]]
    keys = [k for k in combo if not k[2]]
    if helper == "wtype":
        args = ["wtype"]
        for sym, _code, _mod in mods:
            args += ["-M", sym]
        for sym, _code, _mod in keys:
            args += ["-k", sym]
        for sym, _code, _mod in reversed(mods):
            args += ["-m", sym]
        return args
    if helper == "xdotool":
        return ["xdotool", "key", "+".join([_XDO_MODS.get(s, s) for s, _c, _m in mods] + [s for s, _c, _m in keys])]
    # ydotool: every key down in order, then up in reverse ("29:1 46:1 46:0 29:0")
    codes = [code for _sym, code, _mod in combo]
    return ["ydotool", "key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)]]


def press_keys(spec: str) -> bool:
    """Press each combination in `spec` in the focused window. Raises ValueError for a bad spec."""
    combos = parse(spec)
    helper = tool()
    if not helper:
        raise ValueError(MISSING)
    ok = True
    for i, combo in enumerate(combos):
        if i:
            time.sleep(0.05)
        ok = osinfo.run(_combo_args(helper, combo), timeout=5).ok and ok
    return ok


def type_text(text: str) -> bool:
    """Type `text` into the focused window as if on the keyboard. False if no helper could."""
    if not text:
        return True
    helper = tool()
    if helper == "wtype":
        return osinfo.run(["wtype", "-"], timeout=30, input_text=text).ok
    if helper == "ydotool":
        return osinfo.run(["ydotool", "type", "--file", "-"], timeout=30, input_text=text).ok
    if helper == "xdotool":
        return osinfo.run(["xdotool", "type", "--delay", "4", "--file", "-"], timeout=30, input_text=text).ok
    return False


def press_backspace(times: int = 1) -> None:
    if times > 0:
        press_keys(", ".join(["backspace"] * times))
