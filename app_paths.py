"""
app_paths.py -- where the app's files live.

Running from source, everything sits in the project folder. Packaged as an .exe (PyInstaller), the
read-only bundle is one place (RESOURCE_DIR: the PowerShell helper, the tool index) and the user's
writable data another (DATA_DIR = %APPDATA%\\NeonAssistant: settings, voices, logs, caches), so an
upgrade never overwrites a user's settings and the program can live in Program Files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))

# NEON_DATA_DIR moves everything writable somewhere else: a second profile, a portable copy on a
# USB stick, or a throwaway folder for trying the app out without touching your real settings.
_OVERRIDE = os.environ.get("NEON_DATA_DIR", "").strip()
DATA_DIR = (Path(_OVERRIDE).expanduser() if _OVERRIDE else
            (Path(os.environ.get("APPDATA") or Path.home()) / "NeonAssistant") if FROZEN else SOURCE_DIR)


def data_path(name: str) -> Path:
    """A writable file / folder for the user's data (created on demand by the caller)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def resource_path(name: str) -> Path:
    """A read-only file shipped with the program."""
    return RESOURCE_DIR / name


def launch_command() -> str:
    """The command line that starts this app (for the Windows autostart entry)."""
    if FROZEN:
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")           # windowless interpreter, same environment
    return f'"{pythonw if pythonw.exists() else exe}" "{SOURCE_DIR / "main.py"}" --minimized'
