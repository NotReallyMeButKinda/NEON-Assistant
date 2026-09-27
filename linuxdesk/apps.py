"""
linuxdesk/apps.py -- apps on Linux: .desktop files.

Every installed app has a .desktop file (freedesktop.org Desktop Entry spec) in an "applications" folder
under $XDG_DATA_HOME and $XDG_DATA_DIRS: its name, what it is (Comment / GenericName, which NEON uses the way
it uses Wikipedia descriptions on Windows), and how to start it. The same format makes NEON's own Start
menu entry, desktop shortcut and autostart entry.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import osinfo

SKIP_CATEGORIES = {"Settings", "System"}         # still listed, but after real apps on a name clash


def data_dirs() -> list[Path]:
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    rest = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs, seen = [], set()
    for d in [home] + rest.split(":"):
        if d and d not in seen:
            seen.add(d)
            dirs.append(Path(d))
    return dirs


def applications_dirs() -> list[Path]:
    extra = [Path("/var/lib/flatpak/exports/share"), Path.home() / ".local/share/flatpak/exports/share"]
    return [d / "applications" for d in data_dirs() + extra if (d / "applications").is_dir()]


def parse_desktop(path: Path) -> dict:
    """The [Desktop Entry] group of a .desktop file as a dict ('' values for missing keys). Localised keys
    (Name[de]) are ignored; the plain ones are English."""
    entry: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entry
    group = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group != "Desktop Entry" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" not in key:
            entry.setdefault(key, value.strip())
    return entry


def _shown(entry: dict) -> bool:
    if entry.get("Type", "Application") != "Application":
        return False
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return False
    desktop = osinfo.desktop()
    only = [d.strip().lower() for d in entry.get("OnlyShowIn", "").split(";") if d.strip()]
    never = [d.strip().lower() for d in entry.get("NotShowIn", "").split(";") if d.strip()]
    if only and desktop not in only:
        return False
    if desktop in never:
        return False
    try_exec = entry.get("TryExec", "")
    if try_exec and not (Path(try_exec).is_file() or osinfo.which(try_exec)):
        return False
    return bool(entry.get("Name") and entry.get("Exec"))


def discover() -> dict[str, dict]:
    """{app name: {"path", "description", "exec"}} for every app shown in menus. The user's own entries
    (~/.local/share/applications) win over system ones with the same file name, as the spec says."""
    apps: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for folder in applications_dirs():
        for path in sorted(folder.rglob("*.desktop")):
            desktop_id = str(path.relative_to(folder)).replace(os.sep, "-")
            if desktop_id in seen_ids:
                continue
            seen_ids.add(desktop_id)
            entry = parse_desktop(path)
            if not _shown(entry):
                continue
            name = entry["Name"]
            description = " ".join(filter(None, (entry.get("GenericName", ""), entry.get("Comment", ""),
                                                 entry.get("Keywords", "").replace(";", " "))))
            system_tool = bool(set(entry.get("Categories", "").split(";")) & SKIP_CATEGORIES)
            if name in apps and system_tool:
                continue
            apps[name] = {"path": str(path), "description": description.strip(), "exec": entry["Exec"]}
    return apps


_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")


def exec_args(exec_line: str, targets: list[str] | None = None) -> list[str]:
    """The Exec line as a command: %f / %u (and friends) become the targets, the rest are dropped."""
    try:
        parts = shlex.split(exec_line)
    except ValueError:
        parts = exec_line.split()
    args: list[str] = []
    for part in parts:
        if part in ("%f", "%u", "%F", "%U"):
            args.extend(targets or [])
            targets = []                                # used once
        else:
            cleaned = _FIELD_CODES.sub("", part).replace("%%", "%")
            if cleaned:
                args.append(cleaned)
    return args


def launch(desktop_path: str, targets: list[str] | None = None) -> bool:
    """Start the app a .desktop file describes (with files / links to open, if given). Uses `gio launch`
    (the desktop's own launcher, from GLib) when it's there, else runs the Exec line itself."""
    path = Path(desktop_path)
    if osinfo.which("gio"):
        if osinfo.spawn(["gio", "launch", str(path), *(targets or [])]):
            return True
    entry = parse_desktop(path)
    if not entry.get("Exec"):
        return False
    args = exec_args(entry["Exec"], targets)
    if entry.get("Terminal", "").lower() == "true":
        terminal = next((t for t in ("kitty", "foot", "alacritty", "wezterm", "konsole", "xterm") if osinfo.which(t)),
                        None)
        if terminal is None:
            return False
        args = [terminal, "-e", *args]
    return bool(args) and osinfo.spawn(args)


def open_target(target: str) -> bool:
    """Open anything the way Windows' os.startfile does: an app's .desktop file starts that app; a file,
    folder or link opens in its default app (xdg-open)."""
    target = str(target)
    if target.endswith(".desktop") and Path(target).is_file():
        return launch(target)
    opener = "xdg-open" if osinfo.which("xdg-open") else ("gio" if osinfo.which("gio") else None)
    if opener is None:
        return False
    return osinfo.spawn([opener, target] if opener == "xdg-open" else ["gio", "open", target])


def reveal(path: str) -> bool:
    """Show a file in the file manager, selected (the FileManager1 D-Bus interface Dolphin, Nautilus and
    Thunar implement), else open its folder."""
    uri = Path(path).resolve().as_uri()
    if osinfo.which("gdbus"):
        done = osinfo.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                           "--object-path", "/org/freedesktop/FileManager1",
                           "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""], timeout=5)
        if done.ok:
            return True
    return open_target(str(Path(path).parent))


# ---------------------------------------------------------------------------
# Default apps ("open my browser")
# ---------------------------------------------------------------------------

_MIME_FOR = {".pdf": "application/pdf", ".txt": "text/plain", ".jpg": "image/jpeg", ".png": "image/png",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             ".mp3": "audio/mpeg", ".mp4": "video/mp4"}


def find_desktop_file(desktop_id: str) -> Path | None:
    for folder in applications_dirs():
        candidate = folder / desktop_id
        if candidate.is_file():
            return candidate
    return None


def default_app(assoc: str, is_protocol: bool) -> dict | None:
    """The default app for a protocol ('http') or an extension ('.pdf') as {"name", "path"}, via xdg-mime."""
    mime = f"x-scheme-handler/{assoc}" if is_protocol else _MIME_FOR.get(assoc.lower())
    if not mime or not osinfo.which("xdg-mime"):
        return None
    done = osinfo.run(["xdg-mime", "query", "default", mime], timeout=5)
    desktop_id = done.out.strip().splitlines()[0].strip() if done.ok and done.out.strip() else ""
    path = find_desktop_file(desktop_id) if desktop_id else None
    if path is None:
        return None
    name = parse_desktop(path).get("Name") or desktop_id.removesuffix(".desktop")
    return {"name": name, "path": str(path)}


def default_for(kind: str, assoc: str, is_protocol: bool) -> dict | None:
    """assistant.resolve_default_app's answer on Linux: {"kind", "name", "exe" (the .desktop file)}, or None."""
    found = default_app(assoc.lstrip(".") if is_protocol else assoc, is_protocol)
    if found is None:
        return None
    return {"kind": kind, "name": found["name"], "exe": found["path"]}


# ---------------------------------------------------------------------------
# NEON's own entries: Start menu, desktop, autostart
# ---------------------------------------------------------------------------

DESKTOP_ID = "neon-assistant.desktop"


def user_dir(kind: str) -> Path:
    """XDG user folders ('DESKTOP'), through xdg-user-dir when it's there."""
    if kind == "DESKTOP" and osinfo.which("xdg-user-dir"):
        done = osinfo.run(["xdg-user-dir", "DESKTOP"], timeout=3)
        if done.ok and done.out.strip():
            return Path(done.out.strip())
    return Path.home() / kind.capitalize()


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def entry_text(name: str, exec_line: str, icon: str, comment: str, extra: dict | None = None) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Comment={comment}", f"Exec={exec_line}",
             f"Icon={icon}", "Terminal=false", "Categories=Utility;", "StartupWMClass=neon-assistant"]
    lines += [f"{k}={v}" for k, v in (extra or {}).items()]
    return "\n".join(lines) + "\n"


def write_entry(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:                                  # desktops only run trusted (executable) desktop files
        path.chmod(0o755)
