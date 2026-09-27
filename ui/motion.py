"""Whether to animate: the app's own setting, and (unless switched off) Windows' "Animation effects"."""

from __future__ import annotations

import ctypes
import json
import time

import assistant as backend
import osinfo

SPI_GETCLIENTAREAANIMATION = 0x1042


_LINUX = {"checked": -1000.0, "on": True}


def _linux_animations() -> bool:
    """The desktop's own switch: Hyprland's animations:enabled, KDE's animation speed (0 = instant), GNOME's
    enable-animations. Asked at most every 30 seconds (it's a subprocess, and this is called while painting)."""
    now = time.monotonic()
    if now - _LINUX["checked"] < 30:
        return _LINUX["on"]
    _LINUX["checked"] = now
    on = True
    kind = osinfo.desktop()
    if kind == "hyprland":
        done = osinfo.run(["hyprctl", "-j", "getoption", "animations:enabled"], timeout=2)
        try:
            on = not (done.ok and json.loads(done.out).get("int") == 0)
        except (ValueError, AttributeError):
            pass
    elif kind == "kde":
        for tool in ("kreadconfig6", "kreadconfig5"):
            if osinfo.which(tool):
                done = osinfo.run([tool, "--group", "KDE", "--key", "AnimationDurationFactor"], timeout=2)
                try:
                    on = not (done.ok and done.out.strip() and float(done.out.strip()) == 0.0)
                except ValueError:
                    pass
                break
    elif osinfo.which("gsettings"):
        done = osinfo.run(["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"], timeout=2)
        on = not (done.ok and done.out.strip() == "false")
    _LINUX["on"] = on
    return on


def os_animations() -> bool:
    """False when the user turned off "Animation effects" in Windows (Settings > Accessibility >
    Visual effects), which is also what "reduce motion" maps to. On Linux: the desktop's equivalent."""
    if not osinfo.IS_WINDOWS:
        return _linux_animations()
    try:
        flag = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(flag), 0):
            return bool(flag.value)
    except Exception:  # noqa: BLE001 -- can't tell: assume animations are fine
        pass
    return True


def animations_enabled() -> bool:
    """The `bar_animate` setting, honouring the OS preference when `follow_os_animations` is on."""
    if not backend.cfg_bool("bar_animate"):
        return False
    return os_animations() if backend.cfg_bool("follow_os_animations") else True
