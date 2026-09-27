"""
linuxdesk/system.py -- the PC itself on Linux: volume, power, lock, screenshots, brightness, uptime, disk,
the trash, and the CPU / memory / battery numbers the status bar shows.

Same functions and replies as system_control.py on Windows, built on the standard tools of a current Arch
desktop: PipeWire's wpctl (pactl as a fallback), loginctl / systemctl, grim (+ slurp) or KDE's spectacle,
brightnessctl, gio. Missing tools give a sentence saying which package to install instead of failing.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import osinfo

SINK = "@DEFAULT_AUDIO_SINK@"


def _need(tool: str, package: str) -> str:
    return f"I need {tool} for that. On Arch it's in the {package} package."


# ---------------------------------------------------------------------------
# Volume (PipeWire: wpctl; PulseAudio: pactl)
# ---------------------------------------------------------------------------

def _wpctl_state() -> tuple[int, bool] | None:
    done = osinfo.run(["wpctl", "get-volume", SINK], timeout=3)
    m = re.search(r"Volume:\s*([\d.]+)", done.out) if done.ok else None
    if not m:
        return None
    return int(round(float(m.group(1)) * 100)), "[MUTED]" in done.out


def get_volume() -> int | None:
    if osinfo.which("wpctl"):
        state = _wpctl_state()
        return state[0] if state else None
    if osinfo.which("pactl"):
        done = osinfo.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], timeout=3)
        m = re.search(r"(\d+)%", done.out) if done.ok else None
        return int(m.group(1)) if m else None
    return None


def set_volume(percent: float) -> str:
    target = max(0, min(100, int(round(percent))))
    if osinfo.which("wpctl"):
        ok = osinfo.run(["wpctl", "set-volume", SINK, f"{target / 100:.2f}"], timeout=3).ok
        if ok and target > 0:
            osinfo.run(["wpctl", "set-mute", SINK, "0"], timeout=3)
    elif osinfo.which("pactl"):
        ok = osinfo.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{target}%"], timeout=3).ok
        if ok and target > 0:
            osinfo.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], timeout=3)
    else:
        return _need("wpctl", "wireplumber")
    return f"Volume set to {target} percent." if ok else "I couldn't change the volume."


def nudge_volume(delta: int) -> str:
    current = get_volume()
    if current is None:
        return "I can't read the volume right now."
    return set_volume(current + delta)


def set_mute(muted: bool | None = None) -> str:
    if osinfo.which("wpctl"):
        value = "toggle" if muted is None else ("1" if muted else "0")
        if not osinfo.run(["wpctl", "set-mute", SINK, value], timeout=3).ok:
            return "I couldn't change the sound."
        state = _wpctl_state()
        now = state[1] if state else bool(muted)
    elif osinfo.which("pactl"):
        value = "toggle" if muted is None else ("1" if muted else "0")
        if not osinfo.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", value], timeout=3).ok:
            return "I couldn't change the sound."
        now = bool(muted) if muted is not None else True
    else:
        return _need("wpctl", "wireplumber")
    return "Sound muted." if now else "Sound unmuted."


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def screenshots_dir() -> Path:
    pictures = Path.home() / "Pictures"
    if osinfo.which("xdg-user-dir"):
        done = osinfo.run(["xdg-user-dir", "PICTURES"], timeout=3)
        if done.ok and done.out.strip():
            pictures = Path(done.out.strip())
    return pictures / "Screenshots"


def screenshot(path: Path | None = None, whole_desktop: bool = True) -> Path:
    """Capture the screen to a PNG and return where it landed. Raises OSError if no tool could."""
    path = Path(path) if path else screenshots_dir() / f"Screenshot {datetime.now():%Y-%m-%d %H%M%S}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    attempts = []
    if osinfo.desktop() == "kde" and osinfo.which("spectacle"):
        attempts.append(["spectacle", "--background", "--nonotify", "--fullscreen", "--output", str(path)])
    if osinfo.which("grim"):
        attempts.append(["grim", str(path)])
    if osinfo.which("spectacle") and not attempts:
        attempts.append(["spectacle", "--background", "--nonotify", "--fullscreen", "--output", str(path)])
    if osinfo.which("scrot"):                          # X11 without either
        attempts.append(["scrot", "--overwrite", str(path)])
    for args in attempts:
        if osinfo.run(args, timeout=15).ok and path.exists():
            return path
    raise OSError("no screenshot tool worked (install grim, or spectacle on KDE)")


# ---------------------------------------------------------------------------
# Power and the session
# ---------------------------------------------------------------------------

def lock() -> str:
    if osinfo.run(["loginctl", "lock-session"], timeout=5).ok:
        return "Locking the PC."
    if osinfo.desktop() == "hyprland" and osinfo.which("hyprlock"):
        return "Locking the PC." if osinfo.spawn(["hyprlock"]) else "I couldn't lock the PC."
    return "I couldn't lock the PC. Is a screen locker set up (hyprlock, or KDE's own)?"


def sleep() -> str:
    return "Going to sleep." if osinfo.run(["systemctl", "suspend"], timeout=10).ok else "I couldn't put the PC to sleep."


SHUTDOWN_DELAY = 20          # seconds you get to say "cancel the shutdown", as on Windows
_PENDING = {"timer": None}


def shutdown(restart: bool = False) -> str:
    """Power off / reboot after SHUTDOWN_DELAY seconds, so "cancel the shutdown" still works."""
    gerund = "Restarting" if restart else "Shutting down"

    def go() -> None:
        _PENDING["timer"] = None
        osinfo.run(["systemctl", "reboot" if restart else "poweroff"], timeout=15)

    cancel_shutdown()
    timer = threading.Timer(SHUTDOWN_DELAY, go)
    timer.daemon = True
    _PENDING["timer"] = timer
    timer.start()
    return f"{gerund} in {SHUTDOWN_DELAY} seconds. Say \"cancel the shutdown\" if you change your mind."


def cancel_shutdown() -> str:
    timer, _PENDING["timer"] = _PENDING["timer"], None
    if timer is None:
        return "There was no shutdown to cancel."
    timer.cancel()
    return "Cancelled -- staying on."


def sign_out() -> str:
    desktop = osinfo.desktop()
    if desktop == "hyprland":
        from linuxdesk import hypr
        ok = hypr.exit_session()
    elif desktop == "kde":
        ok = osinfo.run(["busctl", "--user", "call", "org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown", "logout"],
                        timeout=5).ok
    else:
        session = os.environ.get("XDG_SESSION_ID", "")
        ok = bool(session) and osinfo.run(["loginctl", "terminate-session", session], timeout=5).ok
    return "Signing you out." if ok else "I couldn't sign you out."


def empty_recycle_bin() -> str:
    trash = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "Trash" / "files"
    if trash.is_dir() and not any(trash.iterdir()):
        return "The trash was already empty."
    if osinfo.which("gio") and osinfo.run(["gio", "trash", "--empty"], timeout=30).ok:
        return "Trash emptied."
    return "I couldn't empty the trash."


def show_desktop() -> str:
    desktop = osinfo.desktop()
    if desktop == "kde":
        from linuxdesk import wm
        ok = wm.kde_shortcut("Show Desktop")
        return "Showing the desktop." if ok else "I couldn't show the desktop."
    if desktop == "hyprland":
        from linuxdesk import wm
        return wm.minimize_all()
    return "I can't show the desktop on this desktop environment."


def set_brightness(percent: int) -> str:
    target = max(0, min(100, int(percent)))
    if osinfo.which("brightnessctl"):
        if osinfo.run(["brightnessctl", "set", f"{target}%"], timeout=5).ok:
            return f"Brightness set to {target} percent."
        return "This screen doesn't let me change its brightness."
    if osinfo.which("ddcutil"):                       # external monitors over DDC/CI
        if osinfo.run(["ddcutil", "setvcp", "10", str(target)], timeout=15).ok:
            return f"Brightness set to {target} percent."
    return _need("brightnessctl", "brightnessctl")


def uptime() -> str:
    try:
        seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return "I couldn't read the uptime."
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = ([f"{days} day{'s' if days != 1 else ''}"] if days else []) + \
            ([f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []) + \
            ([f"{minutes} minute{'s' if minutes != 1 else ''}"] if minutes or not (days or hours) else [])
    return "This PC has been up for " + " and ".join(parts) + "."


def disk_free(drive: str = "") -> str:
    """Linux has no drive letters: the home folder's disk (and the system disk, when that's another one)."""
    places = [("your home folder", Path.home()), ("the system disk", Path("/"))]
    parts, seen = [], set()
    for label, place in places:
        try:
            usage = shutil.disk_usage(place)
            device = os.stat(place).st_dev
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        gb = 1024 ** 3
        percent = 100 * usage.free / usage.total if usage.total else 0
        parts.append(f"{label} has {usage.free / gb:.0f} gigabytes free out of {usage.total / gb:.0f}, "
                     f"about {percent:.0f} percent")
    if not parts:
        return "I couldn't read the disk."
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] + "."


# ---------------------------------------------------------------------------
# CPU, memory, battery, processes (the status bar's numbers; sysinfo.py on Windows)
# ---------------------------------------------------------------------------

def ram_percent() -> int:
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        total = int(info["MemTotal"].split()[0])
        available = int(info["MemAvailable"].split()[0])
    except (OSError, KeyError, ValueError):
        return 0
    return int(round(100 * (total - available) / total)) if total else 0


def cpu_times() -> tuple[int, int]:
    """(idle, total) jiffies since boot, from /proc/stat."""
    try:
        fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return 0, 0
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return idle, sum(fields)


class CpuMeter:
    """Same shape as sysinfo.CpuMeter: percent busy since the previous call."""

    def __init__(self):
        self._last = cpu_times()

    def percent(self) -> int:
        idle, total = cpu_times()
        last_idle, last_total = self._last
        self._last = (idle, total)
        busy_total = total - last_total
        if busy_total <= 0:
            return 0
        return int(round(100 * (1 - (idle - last_idle) / busy_total)))


def battery() -> tuple[int, bool] | None:
    """(percent, charging) of the first battery, or None on a desktop PC."""
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        try:
            if (supply / "type").read_text().strip() != "Battery":
                continue
            percent = int((supply / "capacity").read_text().strip())
            status = (supply / "status").read_text().strip()
        except (OSError, ValueError):
            continue
        return percent, status in ("Charging", "Full")
    return None


def process_names() -> set[str]:
    """Lower-case names of running programs (routines' "when discord starts")."""
    names = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            names.add((proc / "comm").read_text().strip().lower())
        except OSError:
            continue
    return names


def boot_time() -> float:
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.time()
