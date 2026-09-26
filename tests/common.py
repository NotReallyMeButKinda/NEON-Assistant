"""Shared test setup: offscreen Qt, a throwaway config file, and a stub controller.

Run everything with:   python -m unittest discover -s tests -v
Live checks (real hooks, real toasts, network) are skipped unless NEON_LIVE=1 is set.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform finds no fonts on its own and draws empty boxes, which makes every text
# measurement (and screenshot) meaningless: point it at the real Windows fonts.
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import assistant as backend  # noqa: E402
import clipboard  # noqa: E402
import memory_store  # noqa: E402

LIVE = os.environ.get("NEON_LIVE") == "1"
live_only = unittest.skipUnless(LIVE, "live test: set NEON_LIVE=1 to run it")


def use_temp_config() -> Path:
    """Point the app at a throwaway config -- and at throwaway memories -- so no test ever touches
    the real ones, and no test is affected by what an earlier one remembered."""
    folder = Path(tempfile.mkdtemp())
    path = folder / "assistant_config.json"
    backend.CONFIG_PATH = path
    backend.CONFIG.clear()
    backend.CONFIG.update(backend.DEFAULT_CONFIG)
    # Never the real Pear Desktop: with it running, "set the volume to 40 percent" in a routing test
    # once set its volume for real. Every request now fails as if it weren't running.
    backend.YTM._request = _no_pear
    import notifications
    notifications.WPN_DB = folder / "no-wpndatabase.db"      # never Windows' real notification database
    backend.MEDIA = NoMedia()                                  # never the real players either
    backend.ROUTINE_HOOKS["keys"] = None                      # a routine's keys: steps never press real keys
    backend.SEARCH_HOOKS["article"] = None
    backend.WINDOW_HOOKS.update(highlight=None, clear=None)   # no outlines drawn over real windows
    backend.STATUS_HOOKS["status"] = None
    backend._PENDING["action"] = ""
    # The features that reach outside the process are off unless a test turns them on with stubs:
    # the AI's reading of commands and the fact lookups (a running Ollama / the web would answer),
    # the browser bridge (a real port, a token in Credential Manager) and Bitwarden (the real vault).
    backend.CONFIG.update(smart_commands=False, ground_facts=False, fact_check=False, browser_enabled=False,
                          bitwarden_enabled=False, thinking_lines_enabled=False, listen_sound="off",
                          files_enabled=False)
    import timers                                             # no timer left running from an earlier test,
    timers.enable_persistence(folder / "timers.json")         # and never the real timers.json
    timers.cancel_timers()
    import homeassistant                                      # never the real token in Credential Manager
    homeassistant.token = lambda: ""
    homeassistant.set_token = lambda value: True
    homeassistant.forget_names()
    import shortcuts                                          # never the real Start menu or desktop
    shortcuts.FOLDERS.update(start_menu=lambda: folder / "start_menu", desktop=lambda: folder / "desktop")
    import filesearch                                         # never the real Everything, never open real files
    filesearch.ACTIONS.update(open=_no_files, reveal=_no_files)
    filesearch.status = lambda: "missing"                     # screenshots don't depend on this PC
    filesearch.HOOKS["listing"] = None
    filesearch._LAST.update(hits=[], until=0.0)
    import bitwarden
    bitwarden.find_cli = lambda: ""                           # never the real bw.exe
    bitwarden._STATE.update(session="", items=[], pending="", choice=None)
    import browser_bridge
    browser_bridge.stop()
    import board
    board.enable_persistence(folder / "board.json")           # a fresh board, never the real one
    board.reset()
    backend.save_config(backend.CONFIG)
    memory_store.enable_persistence(folder / "memories.json")
    import lookup_cache
    lookup_cache.enable_persistence(folder / "lookup_cache.json")          # never the real one, and empty
    memory_store.reset()
    clipboard.clear()
    clipboard.note_reply("")
    return path


def _no_files(path):
    raise AssertionError(f"a test tried to open a real file: {path}")


def _no_pear(*_args, **_kwargs):
    import ytmusic
    raise ytmusic.Unavailable("tests never talk to the real Pear Desktop")


class NoMedia:
    """Stands in for media.MediaWatcher: no player is playing, and nothing is ever sent to one."""

    def start(self):
        pass

    def stop(self):
        pass

    def ensure(self, wait=0.0):
        return False

    def song(self):
        return None

    def now_playing(self):
        return None

    def command(self, action):
        return False


def pump(ms: int = 100) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def pump_until(condition, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while not condition() and time.time() < end:
        pump(30)
    return bool(condition())


class _Call:
    def __init__(self, calls, name):
        self._calls, self._name = calls, name

    def __call__(self, *args, **kwargs):
        self._calls.append((self._name, args))
        return ""

    def emit(self, *args):
        self._calls.append((self._name + ".emit", args))


class StubController:
    """Accepts any call or signal emit and records it in `.calls` (for dialogs that just poke it)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        return _Call(self.calls, name)

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]
