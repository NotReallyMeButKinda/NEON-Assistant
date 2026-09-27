"""
linuxdesk/media.py -- now playing, and play / pause / next / previous, for any player on Linux through
MPRIS (every Linux media player, browser tab and Pear Desktop speaks it), using playerctl.

Same interface as media.MediaWatcher on Windows: song() gives the Pear-Desktop-shaped dict the status bar
draws, command() sends play | pause | toggle | next | previous to the player that is playing (or the last one
that was).
"""

from __future__ import annotations

import threading
import time

import osinfo

COMMANDS = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous"}
POLL_SECONDS = 2.0
_FORMAT = "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}\t{{position}}\t{{mpris:length}}"
_NAMES = {"firefox": "Firefox", "chromium": "Chromium", "chrome": "Chrome", "brave": "Brave", "spotify": "Spotify",
          "vlc": "VLC", "mpv": "mpv", "zen": "Zen", "elisa": "Elisa", "youtube-music": "Pear Desktop",
          "pear-desktop": "Pear Desktop", "kdeconnect": "KDE Connect", "plasma-browser-integration": "your browser"}


def friendly(player: str) -> str:
    base = player.split(".")[0].lower()
    return _NAMES.get(base, base.capitalize())


def parse(output: str) -> dict | None:
    """playerctl's lines (one per player) -> the report for the player that matters: playing beats paused."""
    best = None
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[3].strip():
            continue
        player, status, artist, title = (p.strip() for p in parts[:4])
        position = parts[4].strip() if len(parts) > 4 else ""
        length = parts[5].strip() if len(parts) > 5 else ""
        report = {"player": player, "status": status, "artist": artist, "title": title,
                  "position": int(position) / 1e6 if position.isdigit() else 0.0,
                  "duration": int(length) / 1e6 if length.isdigit() else 0.0}
        if status == "Playing":
            return report
        if best is None and status == "Paused":
            best = report
    return best


def to_song(report: dict | None) -> dict | None:
    if not report or not report.get("title"):
        return None
    return {"title": report["title"], "artist": report.get("artist", ""), "isPaused": report.get("status") != "Playing",
            "elapsedSeconds": float(report.get("position") or 0), "songDuration": float(report.get("duration") or 0),
            "app": friendly(report.get("player", "")), "source": "mpris"}


class MediaWatcher:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self.ready = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not osinfo.which("playerctl"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Nova-Media", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.ready.clear()

    def poll(self) -> None:
        done = osinfo.run(["playerctl", "--all-players", "metadata", "--format", _FORMAT], timeout=3)
        report = parse(done.out) if done.ok else None
        with self._lock:
            self._latest = report
        self.ready.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(POLL_SECONDS)

    def ensure(self, wait: float = 3.0) -> bool:
        if not osinfo.which("playerctl"):
            return False
        if not self.running:
            self.poll()                            # an immediate answer, then keep polling
            self.start()
        return self.ready.wait(wait)

    def song(self) -> dict | None:
        with self._lock:
            return to_song(self._latest)

    def now_playing(self) -> str | None:
        if not self.ensure():
            return None
        song = self.song()
        if not song:
            return None
        what = f"{song['title']} by {song['artist']}" if song["artist"] else song["title"]
        where = f", in {song['app']}" if song.get("app") else ""
        return f"{what}{where}." + (" It's paused." if song["isPaused"] else "")

    def command(self, action: str) -> bool:
        if action not in COMMANDS or not self.ensure():
            return False
        with self._lock:
            player = (self._latest or {}).get("player")
        args = ["playerctl"] + (["--player", player] if player else []) + [COMMANDS[action]]
        ok = osinfo.run(args, timeout=3).ok
        if ok:
            time.sleep(0.2)
            self.poll()
        return ok
