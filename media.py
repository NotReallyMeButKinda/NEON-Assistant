"""
media.py -- now playing from any player, through Windows' own media sessions.

Every app that shows up in the volume flyout's media controls (Spotify, a browser tab, Pear Desktop,
the Media Player app...) reports its track there. A small PowerShell helper (media_watcher.ps1) reads
that and can press play / pause / next / previous on the same session: the same approach as the
notification watcher, so no extra Python packages.

  * MediaWatcher.song()      the latest track as a Pear-Desktop-shaped dict (the status bar's widget
                             already draws those), or None when nothing is playing or paused
  * MediaWatcher.command()   play | pause | toggle | next | previous, sent to that session
  * MediaWatcher.now_playing()   "Blinding Lights by The Weeknd, in Spotify."
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import app_paths
import neon_log
from notifications import _NO_WINDOW, PS_EXE

log = neon_log.get("media")

SCRIPT = app_paths.resource_path("media_watcher.ps1")
COMMANDS = ("play", "pause", "toggle", "next", "previous")
STALE_SECONDS = 5.0              # a report older than this means the helper has stopped


def friendly_app(aumid: str) -> str:
    """'Spotify.exe' -> 'Spotify', 'com.github.th-ch.youtube-music' -> 'YouTube Music'; a browser's
    per-profile hash (16 hex digits) -> 'your browser'."""
    raw = str(aumid or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[0-9A-F]{16}", raw):
        return "your browser"
    known = {"spotify": "Spotify", "youtube-music": "YouTube Music", "chrome": "Chrome", "msedge": "Edge",
             "firefox": "Firefox", "zen": "Zen", "brave": "Brave", "vlc": "VLC", "zunemusic": "Media Player",
             "zunevideo": "Films & TV", "foobar2000": "foobar2000", "tidal": "TIDAL", "applemusic": "Apple Music"}
    lower = raw.lower()
    for key, name in known.items():
        if key in lower:
            return name
    name = raw.split("!")[0].split("_")[0]
    name = name[:-4] if name.lower().endswith(".exe") else name
    return name.rsplit(".", 1)[-1] or name


def to_song(report: dict) -> dict | None:
    """A helper report -> the dict shape Pear Desktop's /song returns (what the bar already draws)."""
    title = str(report.get("title") or "").strip()
    if not report.get("app") or not title:
        return None
    return {"title": title, "artist": str(report.get("artist") or "").strip(),
            "isPaused": str(report.get("status")) != "Playing",
            "elapsedSeconds": float(report.get("position") or 0), "songDuration": float(report.get("duration") or 0),
            "app": friendly_app(report.get("app")), "source": "windows"}


class MediaWatcher:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._at = 0.0                                  # time.monotonic() of the latest report
        self.ready = threading.Event()                  # set once the first report has arrived
        self._cmd_file = Path(os.environ.get("TEMP") or app_paths.DATA_DIR) / f"neon_media_cmd_{os.getpid()}.txt"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Nova-Media", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.ready.clear()
        try:
            self._cmd_file.unlink(missing_ok=True)
        except OSError:
            pass

    def ensure(self, wait: float = 3.0) -> bool:
        """Started (if it wasn't) and has reported at least once, waiting up to `wait` seconds."""
        self.start()
        return self.ready.wait(wait)

    def song(self) -> dict | None:
        with self._lock:
            if self._latest is None or time.monotonic() - self._at > STALE_SECONDS:
                return None
            return to_song(self._latest)

    def now_playing(self) -> str | None:
        """A sentence about the current track, or None if nothing is playing or paused anywhere."""
        if not self.ensure():
            return None
        song = self.song()
        if not song:
            return None
        what = f"{song['title']} by {song['artist']}" if song["artist"] else song["title"]
        where = f", in {song['app']}" if song.get("app") else ""
        return f"{what}{where}." + (" It's paused." if song["isPaused"] else "")

    def command(self, action: str) -> bool:
        """Send play / pause / toggle / next / previous to the reported session. False if there is none
        (the caller falls back to the media keys)."""
        if action not in COMMANDS or not self.ensure() or self.song() is None:
            return False
        try:
            with self._cmd_file.open("a", encoding="utf-8") as f:
                f.write(action + "\n")
        except OSError:
            return False
        return True

    # ---- internals -------------------------------------------------------------------
    def _handle_line(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        if obj.get("type") == "media":
            with self._lock:
                self._latest, self._at = obj, time.monotonic()
            self.ready.set()
        elif obj.get("type") == "error":
            log.info("media watcher: %s", obj.get("message"))

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    PS_EXE + ["-File", str(SCRIPT), "-CmdFile", str(self._cmd_file), "-ParentPid", str(os.getpid())],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                for raw in self._proc.stdout:
                    if self._stop.is_set():
                        return
                    self._handle_line(raw)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    log.warning("couldn't run the media watcher: %s", exc)
            finally:
                self._reap()
            failures = failures + 1 if time.monotonic() - started < 20 else 0
            if self._stop.is_set() or failures >= 3:
                if failures >= 3:
                    log.warning("the media watcher keeps stopping; giving up on it")
                return
            self._stop.wait(3.0)

    def _reap(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except (OSError, ValueError):
                    pass
