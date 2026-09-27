"""
linuxdesk/files.py -- finding files by name on Linux, the way Everything does on Windows.

plocate (Arch: the plocate package, with its daily updatedb timer) keeps an index of every file name and
answers in milliseconds. System folders, caches and hidden folders are left out unless asked for, like the
Windows version leaves out Windows, Program Files and AppData. Newer files come first.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import osinfo

MISSING = ("Finding files needs plocate. On Arch: sudo pacman -S plocate, then sudo updatedb (it then keeps "
           "itself up to date every day).")
SYSTEM_PREFIXES = ("/usr/", "/var/", "/proc/", "/sys/", "/etc/", "/boot/", "/opt/", "/run/", "/tmp/", "/dev/",
                   "/snap/", "/lib", "/bin/", "/sbin/", "/srv/", "/root/")
JUNK_PARTS = {"node_modules", "__pycache__", "site-packages", "steamapps", ".cache", "Trash"}


def tool() -> str:
    return "plocate" if osinfo.which("plocate") else ("locate" if osinfo.which("locate") else "")


def status() -> str:
    """'ready' | 'missing' (the same words filesearch.status() uses on Windows)."""
    return "ready" if tool() else "missing"


def kind_filter(kind_spec: str):
    """The Windows version's kind ('ext:pdf;docx', 'file:', 'folder:') -> (extensions or None, files, folders)."""
    spec = str(kind_spec or "")
    if spec.startswith("ext:"):
        return {"." + e.lower() for e in spec[4:].split(";") if e}, True, False
    if spec == "folder:":
        return None, False, True
    if spec == "file:":
        return None, True, False
    return None, True, True


def _is_system(path: str) -> bool:
    if path.startswith(SYSTEM_PREFIXES) and not path.startswith("/run/media/"):    # USB drives mount there
        return True
    parts = Path(path).parts
    return any(p.startswith(".") and len(p) > 1 for p in parts) or any(p in JUNK_PARTS for p in parts)


def search(words: str, kind_spec: str = "", system: bool = False, max_results: int = 60) -> list:
    """Hits (filesearch.Hit) for files and folders whose names contain every word, newest first.
    Raises filesearch.EverythingError when plocate isn't installed."""
    import filesearch
    helper = tool()
    if not helper:
        raise filesearch.EverythingError(MISSING)
    terms = [w for w in re.sub(r'[|!<>"*?;:]+', " ", str(words)).split() if w]
    if not terms:
        return []
    done = osinfo.run([helper, "--ignore-case", "--basename", "--limit", "3000", "--", *terms], timeout=10)
    if not done.ok and not done.out:
        return []
    extensions, want_files, want_folders = kind_filter(kind_spec)
    lowered = [t.lower() for t in terms]
    hits = []
    for line in done.out.splitlines():
        path = line.strip()
        if not path:
            continue
        name = os.path.basename(path.rstrip("/"))
        if not all(t in name.lower() for t in lowered):
            continue
        if not system and _is_system(path):
            continue
        if extensions is not None and Path(name).suffix.lower() not in extensions:
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue                                    # deleted since the index was built
        is_folder = os.path.isdir(path)
        if (is_folder and not want_folders) or (not is_folder and not want_files):
            continue
        hits.append(filesearch.Hit(name=name, folder=os.path.dirname(path.rstrip("/")), is_folder=is_folder,
                                   size=-1 if is_folder else info.st_size,
                                   modified=datetime.fromtimestamp(info.st_mtime)))
    hits.sort(key=lambda h: h.modified or datetime.min, reverse=True)
    return hits[:max_results]
