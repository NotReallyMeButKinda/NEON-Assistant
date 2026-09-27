"""Pretend to be Linux well enough to import NEON's modules on this Windows PC, and report which ones fail.

Run in its own process (tests/test_linux_imports.py does): it hides everything Windows-only that Linux
doesn't have -- ctypes.windll / WinDLL / WINFUNCTYPE, winreg, msvcrt, pywin32 -- and sets sys.platform to
"linux" before any app module is imported. On its own it proves every module at least loads there, which is
the first thing that breaks when Windows-only code runs at import time; tests/linux_smoke.py uses the same
pretence to run the Linux code paths of the windows themselves against a fake desktop.

    python tests/linux_sim.py [--desktop kde] [module ...]   -> "ok name" or "FAIL name: error" per module
"""
import builtins
import ctypes
import importlib
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NEON_DATA_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "neon-linux-sim"))

DESKTOPS = {
    "hyprland": {"XDG_CURRENT_DESKTOP": "Hyprland", "HYPRLAND_INSTANCE_SIGNATURE": "sim"},
    "kde": {"XDG_CURRENT_DESKTOP": "KDE", "KDE_SESSION_VERSION": "6"},
}
BLOCKED = {"winreg", "_winreg", "msvcrt", "pythoncom", "pywintypes", "win32com", "win32api", "win32con",
           "win32gui", "win32process", "win32clipboard", "comtypes"}
_REAL_IMPORT = builtins.__import__


def _import(name, *args, **kwargs):
    if name.split(".")[0] in BLOCKED:
        raise ModuleNotFoundError(f"No module named '{name}' (Linux)")
    return _REAL_IMPORT(name, *args, **kwargs)


def pretend_linux(desktop: str = "hyprland") -> None:
    """Hide Windows from everything imported after this, and set up the environment of a Wayland session on
    `desktop` ('hyprland' or 'kde'). Call before importing any NEON module."""
    for key in ("XDG_CURRENT_DESKTOP", "HYPRLAND_INSTANCE_SIGNATURE", "KDE_SESSION_VERSION"):
        os.environ.pop(key, None)
    os.environ.update(DESKTOPS[desktop])
    os.environ["WAYLAND_DISPLAY"] = "wayland-1"

    # Qt, the standard library and the third-party libraries that pick their native library by platform are
    # imported *before* pretending: their Windows builds are what's installed here.
    import sqlite3  # noqa: F401
    import subprocess  # noqa: F401
    import numpy  # noqa: F401
    for name in ("PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore", "PySide6.QtNetwork", "PySide6.QtSvg",
                 "PySide6.QtTest", "sounddevice", "speech_recognition", "piper", "onnxruntime", "faster_whisper",
                 "pyttsx3", "needle"):
        try:
            __import__(name)
        except Exception:  # noqa: BLE001 -- optional ones may be missing
            pass

    sys.platform = "linux"
    for name in ("windll", "oledll", "WinDLL", "OleDLL", "WINFUNCTYPE", "HRESULT", "FormatError", "WinError",
                 "GetLastError", "get_last_error", "set_last_error"):
        if hasattr(ctypes, name):
            delattr(ctypes, name)
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]
    builtins.__import__ = _import


MODULES = [
    "osinfo", "app_paths", "neon_log", "secrets_store", "startup", "shortcuts", "sysinfo", "system_control",
    "windows", "dictation", "selection", "clipboard", "hotkeys", "media", "notifications", "filesearch",
    "homeassistant", "tts", "stt", "listening", "vad", "wakeword", "wake_training", "sounds", "assistant",
    "controller", "ui.theme", "ui.motion", "ui.frame", "ui.status_bar", "ui.shade", "ui.quick_input",
    "ui.window_highlight", "ui.effects", "ui.main_window", "ui.settings_dialog", "ui.onboarding", "ui.board",
    "main", "linuxdesk.system", "linuxdesk.apps", "linuxdesk.media", "linuxdesk.hypr", "linuxdesk.input",
    "linuxdesk.clip", "linuxdesk.notify", "linuxdesk.secrets", "linuxdesk.files", "linuxdesk.single",
    "linuxdesk.keys", "linuxdesk.wm", "linuxdesk.kwin", "linuxdesk.portal_keys", "ui.wayland_place",
    "ui.app_icon", "ui.sound_picker", "ui.beep_maker", "routines", "board", "panel_feed", "linuxdesk.plasmoid",
]


def main(argv: list[str]) -> int:
    desktop = "hyprland"
    if "--desktop" in argv:
        at = argv.index("--desktop")
        desktop = argv[at + 1]
        argv = argv[:at] + argv[at + 2:]
    pretend_linux(desktop)
    modules = argv or MODULES
    failed = 0
    for module in modules:
        try:
            importlib.import_module(module)
            print(f"ok   {module}")
        except BaseException as exc:  # noqa: BLE001 -- report everything, keep going
            failed += 1
            where = traceback.extract_tb(exc.__traceback__)[-1]
            print(f"FAIL {module}: {type(exc).__name__}: {exc}  [{os.path.basename(where.filename)}:{where.lineno}]")
    print(f"{len(modules) - failed} of {len(modules)} modules import on (simulated) Linux ({desktop})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
