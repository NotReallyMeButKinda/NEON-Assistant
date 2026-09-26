"""Whether to animate: the app's own setting, and (unless switched off) Windows' "Animation effects"."""

from __future__ import annotations

import ctypes

import assistant as backend

SPI_GETCLIENTAREAANIMATION = 0x1042


def os_animations() -> bool:
    """False when the user turned off "Animation effects" in Windows (Settings > Accessibility >
    Visual effects), which is also what "reduce motion" maps to."""
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
