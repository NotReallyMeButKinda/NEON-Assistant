"""
app_paths.py -- where the app's files live.

Running from source, everything sits in the project folder. Packaged as an .exe (PyInstaller), the
read-only bundle is one place (RESOURCE_DIR: the PowerShell helper, the tool index) and the user's
writable data another (DATA_DIR = %APPDATA%\\NeonAssistant: settings, voices, logs, caches), so an
upgrade never overwrites a user's settings and the program can live in Program Files.

On Linux the data goes to $XDG_DATA_HOME/neon-assistant (~/.local/share/neon-assistant) whenever the program
folder isn't the user's to write in (installed from the AUR package into /opt) or it's packaged; run from a
folder you own, it stays beside the code as on Windows.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))


def _default_data_dir() -> Path:
    # NEON_DATA_DIR moves everything writable somewhere else: a second profile, a portable copy on a
    # USB stick, or a throwaway folder for trying the app out without touching your real settings.
    override = os.environ.get("NEON_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        return (Path(os.environ.get("APPDATA") or Path.home()) / "NeonAssistant") if FROZEN else SOURCE_DIR
    if FROZEN or not os.access(SOURCE_DIR, os.W_OK):
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "neon-assistant"
    return SOURCE_DIR


DATA_DIR = _default_data_dir()


def data_path(name: str) -> Path:
    """A writable file / folder for the user's data (created on demand by the caller)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def resource_path(name: str) -> Path:
    """A read-only file shipped with the program."""
    return RESOURCE_DIR / name


def launch_args(extra: list[str] | None = None) -> list[str]:
    """The program and arguments that start this app (the packaged exe, or Python running main.py; on
    Windows the windowless pythonw.exe, so no console window appears)."""
    if FROZEN:
        return [sys.executable, *(extra or [])]
    exe = Path(sys.executable)
    if IS_WINDOWS:
        pythonw = exe.with_name("pythonw.exe")
        exe = pythonw if pythonw.exists() else exe
    return [str(exe), str(SOURCE_DIR / "main.py"), *(extra or [])]


def launch_command() -> str:
    """The command line that starts this app minimized (the autostart entry)."""
    args = launch_args(["--minimized"])
    if IS_WINDOWS:
        return " ".join(f'"{a}"' if " " in a or a.endswith(".exe") or a.endswith(".py") else a for a in args)
    return " ".join(shlex.quote(a) for a in args)


def command_args(command: str) -> list[str]:
    """How another program (a Hyprland keybind, a KDE custom shortcut) asks the running NEON to do something:
    `... main.py --command talk`."""
    return launch_args(["--command", command])
