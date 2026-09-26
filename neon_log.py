"""
neon_log.py -- a rotating log file (neon.log, 3 x 512 KB) for everything that used to fail silently.

    import neon_log
    log = neon_log.get(__name__)
    log.warning("Ollama unreachable: %s", exc)

`install()` (called once at startup) also routes uncaught exceptions -- on any thread -- into the
log, so "it stopped working overnight" leaves a trail.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import app_paths

LOG_PATH = app_paths.data_path("neon.log")
_installed = False
logging.getLogger("neon").addHandler(logging.NullHandler())   # silent until install() adds the file


def install(level: int = logging.INFO) -> None:
    global _installed
    if _installed:
        return
    _installed = True
    root = logging.getLogger("neon")
    root.setLevel(level)
    try:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    except OSError:                       # read-only folder etc.: logging must never stop the app
        root.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)

    previous = sys.excepthook

    def hook(kind, value, tb):
        root.critical("Uncaught exception", exc_info=(kind, value, tb))
        previous(kind, value, tb)

    def thread_hook(args):
        root.critical("Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    root.info("---- started (python %s) ----", sys.version.split()[0])


def get(name: str = "") -> logging.Logger:
    """A logger under the 'neon' tree (so it reaches the file handler)."""
    return logging.getLogger("neon" + (f".{name}" if name and name != "neon" else ""))
