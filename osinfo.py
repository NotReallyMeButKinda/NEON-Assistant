"""
osinfo.py -- which operating system and desktop NEON is running on, and one way to run outside commands.

Windows is the original home; Linux (Arch, on Hyprland first, KDE Plasma too) is served by the modules in
linuxdesk/, which the Windows-specific modules hand over to when IS_LINUX. Everything that runs a command
(`wpctl`, `hyprctl`, `playerctl`...) goes through `run()`, so tests can swap RUNNER for a fake and check
exactly what would have been run, without touching the real system.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def desktop() -> str:
    """'windows' | 'hyprland' | 'kde' | 'gnome' | 'other' (another Linux desktop)."""
    if IS_WINDOWS:
        return "windows"
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    current = ":".join(os.environ.get(k, "") for k in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP",
                                                         "DESKTOP_SESSION")).lower()
    if "hyprland" in current:
        return "hyprland"
    if "kde" in current or "plasma" in current:
        return "kde"
    if "gnome" in current:
        return "gnome"
    return "other"


def wayland() -> bool:
    return IS_LINUX and bool(os.environ.get("WAYLAND_DISPLAY"))


@dataclass
class Result:
    ok: bool
    out: str = ""
    err: str = ""
    code: int = 0


def _real_run(args: list[str], timeout: float, input_text: str | None) -> Result:
    try:
        done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, input=input_text,
                              stdin=None if input_text is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        return Result(False, "", f"{args[0]} isn't installed", 127)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(False, "", str(exc), 1)
    return Result(done.returncode == 0, done.stdout or "", done.stderr or "", done.returncode)


# Tests replace this with a fake: callable(args, timeout, input_text) -> Result.
RUNNER = {"run": _real_run, "which": shutil.which}


def run(args: list[str], timeout: float = 5.0, input_text: str | None = None) -> Result:
    """Run a command and wait; never raises. `ok` is True when it exited with 0."""
    return RUNNER["run"]([str(a) for a in args], timeout, input_text)


def which(name: str) -> str | None:
    return RUNNER["which"](name)


def spawn(args: list[str]) -> bool:
    """Start a program detached from NEON (it keeps running if NEON quits). False if it can't start."""
    if "spawn" in RUNNER:
        return RUNNER["spawn"](list(args))
    try:
        kwargs = {"start_new_session": True} if not IS_WINDOWS else {}
        subprocess.Popen([str(a) for a in args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
        return True
    except OSError:
        return False
