"""
shortcuts.py -- "Neon Assistant" in the Start menu and on the desktop (Settings > General > Startup, and the
welcome tour's Shortcuts page).

A shortcut starts the packaged NeonAssistant.exe, or, running from source, main.py with the windowless
pythonw.exe, in the project folder, with the glowing-dot icon. The folders are the user's own (no administrator
rights needed); the desktop is found through Windows (it may have moved into OneDrive). Whether a shortcut
exists is read from disk, so Settings always shows the truth, even after you delete one by hand.

The shortcuts carry NEON's app ID (APP_ID), which the running app also claims (`claim_app_id`, at startup).
That's how Windows ties the window on the taskbar to the Start menu entry, so it's called "Neon Assistant"
with its own icon rather than by the program's file name. Shortcuts made under an older name are renamed
at startup (`rename_old`).
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

import app_paths
import neon_log

log = neon_log.get("shortcuts")

NAME = "Neon Assistant.lnk"
OLD_NAMES = ("NEON Assistant.lnk",)             # renamed to NAME at startup
APP_ID = "NeonAssistant.Assistant"              # the AppUserModelID the app and its shortcuts share
KINDS = ("start_menu", "desktop")
_FOLDER_IDS = {"start_menu": "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}",     # FOLDERID_Programs (this user)
               "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"}        # FOLDERID_Desktop


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _known_folder(folder_id: str) -> Path | None:
    guid = _GUID()
    raw = uuid.UUID(folder_id).bytes_le
    ctypes.memmove(ctypes.byref(guid), raw, 16)
    path = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        return Path(path.value)
    except (AttributeError, OSError):
        return None
    finally:
        if path:
            ctypes.windll.ole32.CoTaskMemFree(path)


def _default_folder(kind: str) -> Path:
    found = _known_folder(_FOLDER_IDS[kind])
    if found is not None:
        return found
    if kind == "desktop":
        return Path.home() / "Desktop"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# kind -> callable returning the folder. Tests point these at throwaway folders.
FOLDERS = {kind: (lambda k=kind: _default_folder(k)) for kind in KINDS}


def path(kind: str) -> Path:
    return Path(FOLDERS[kind]()) / NAME


def exists(kind: str) -> bool:
    try:
        return path(kind).is_file() or any((Path(FOLDERS[kind]()) / old).is_file() for old in OLD_NAMES)
    except OSError:
        return False


def claim_app_id() -> None:
    """Tell Windows this process is "Neon Assistant" (call before the first window is shown)."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except (AttributeError, OSError):
        pass


def _stamp_app_id(link: Path) -> None:
    """Put APP_ID on a shortcut (its property store), so Windows matches it to the running app."""
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell
    except ImportError:
        return
    pythoncom.CoInitialize()
    try:
        link_obj = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                              shell.IID_IShellLinkW)
        persist = link_obj.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Load(str(link), 2)                     # STGM_READWRITE
        store = link_obj.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
        store.Commit()
        persist.Save(str(link), True)
        del store, persist, link_obj
    except Exception as exc:  # noqa: BLE001 -- the shortcut works without it; only the grouping is lost
        log.info("couldn't set the app ID on %s: %s", link, exc)
    finally:
        pythoncom.CoUninitialize()


def rename_old() -> list[Path]:
    """Shortcuts made under an older name ("NEON Assistant") are renamed "Neon Assistant" and given the app
    ID. Only NEON's own shortcuts are touched. Returns the shortcuts that were renamed."""
    renamed = []
    for kind in KINDS:
        try:
            folder = Path(FOLDERS[kind]())
            if not folder.is_dir():
                continue
            new = folder / NAME
            # Windows ignores case in file names, so "NEON Assistant.lnk" *is* "Neon Assistant.lnk" to
            # is_file(): compare the names exactly as they're stored.
            on_disk = {p.name for p in folder.iterdir()}
            for old_name in OLD_NAMES:
                if old_name not in on_disk:
                    continue
                old = folder / old_name
                if old_name.lower() == NAME.lower():
                    step = old.with_name(old_name + ".renaming")     # a case-only rename goes through a
                    old.rename(step)                                 # temporary name
                    step.rename(new)
                elif NAME in on_disk:
                    old.unlink()                        # both exist: the new one wins
                    continue
                else:
                    old.rename(new)
                renamed.append(new)
                _stamp_app_id(new)
        except OSError as exc:
            log.info("couldn't rename the %s shortcut: %s", kind, exc)
    return renamed


def launch_target() -> tuple[str, str, str, str]:
    """(program, arguments, working folder, icon) a shortcut should use."""
    if app_paths.FROZEN:
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent), f"{exe},0"
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    icon = app_paths.SOURCE_DIR / "icons" / "neon.ico"
    return (str(pythonw if pythonw.exists() else python), f'"{app_paths.SOURCE_DIR / "main.py"}"',
            str(app_paths.SOURCE_DIR), f"{icon},0")


def _write(link: Path) -> None:
    target, arguments, folder, icon = launch_target()
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        try:
            shortcut = Dispatch("WScript.Shell").CreateShortCut(str(link))
            shortcut.TargetPath, shortcut.Arguments, shortcut.WorkingDirectory = target, arguments, folder
            shortcut.IconLocation, shortcut.Description = icon, "NEON voice assistant"
            shortcut.save()
            del shortcut                                # released before COM shuts down on this thread
        finally:
            pythoncom.CoUninitialize()
        _stamp_app_id(link)
        return
    except ImportError:
        pass                                            # no pywin32: PowerShell makes the same shortcut
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:NEON_LNK); "
              "$s.TargetPath = $env:NEON_TARGET; $s.Arguments = $env:NEON_ARGS; $s.WorkingDirectory = $env:NEON_DIR; "
              "$s.IconLocation = $env:NEON_ICON; $s.Description = 'NEON voice assistant'; $s.Save()")
    env = {**__import__("os").environ, "NEON_LNK": str(link), "NEON_TARGET": target, "NEON_ARGS": arguments,
           "NEON_DIR": folder, "NEON_ICON": icon}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                   capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def set_enabled(kind: str, enabled: bool) -> None:
    """Make or remove the shortcut. Raises OSError (or CalledProcessError) if Windows refuses."""
    link = path(kind)
    if enabled:
        _write(link)
        log.info("made the %s shortcut: %s", kind, link)
        return
    for name in (NAME, *OLD_NAMES):
        found = link.with_name(name)
        if found.is_file():
            found.unlink()
            log.info("removed the %s shortcut: %s", kind, found)


def apply(wanted: dict) -> list[str]:
    """{kind: bool} -> make / remove each; returns what went wrong, for the user (empty when all is well)."""
    problems = []
    for kind, enabled in wanted.items():
        if kind not in KINDS or bool(enabled) == exists(kind):
            continue
        try:
            set_enabled(kind, bool(enabled))
        except (OSError, subprocess.SubprocessError) as exc:
            where = "Start menu" if kind == "start_menu" else "desktop"
            problems.append(f"Couldn't {'add' if enabled else 'remove'} the {where} shortcut: {exc}")
    return problems
