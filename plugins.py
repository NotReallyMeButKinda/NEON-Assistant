r"""
plugins.py -- drop a Python file in `plugins/` and the assistant learns a new command.

The whole contract is: a module in the plugins folder may define any of

    NAME = "Coffee"                       shown in Settings and in "what plugins do I have"
    DESCRIPTION = "Counts your coffees."
    COMMANDS = [(r"^how many coffees", count), ...]   regex -> callable(match, text) -> str
    def handle(text): ...                 a catch-all, tried after COMMANDS
    def setup(api): ...                   run once at load, handed the small API below

and any of these hooks, which are told about things as they happen (their return value is ignored,
each runs on its own thread, and a slow or failing hook never delays the assistant):

    def on_reply(heard, reply): ...       after every answer: what you said and what I replied
    def on_notification(note): ...        a Windows notification arrived (dict: app, title, body, id)
    def on_timer(label, reminder): ...    a timer or reminder went off
    def on_routine(name, result): ...     a routine finished (by voice or on its schedule)

`api` gives a plugin the few things it can't get on its own without importing the assistant and
risking a circular import: `api.say(text)`, `api.run(command)`, `api.config`, `api.data_dir`,
`api.log`. Everything else is ordinary Python.

Plugins are the user's own code, so they are not sandboxed -- but they are *contained*: a module
that fails to import is reported and skipped, a handler that raises is logged and treated as "not
mine", and one slow plugin can't stop the others being tried. Reloading is a voice command
("reload plugins"), so editing one doesn't mean restarting the assistant.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import neon_log

log = neon_log.get("plugins")

FOLDER_NAME = "plugins"
HANDLER_TIMEOUT = 8.0          # a plugin that hangs this long is abandoned for this utterance
HOOKS = ("on_reply", "on_notification", "on_timer", "on_routine")

_LOCK = threading.RLock()
_LOADED: list[dict] = []       # {"name", "description", "file", "module", "commands", "handle", "hooks"}
_ERRORS: list[str] = []
_FOLDER = {"path": None}

EXAMPLE = '''"""
An example NEON plugin. Rename it, edit it, or delete it -- it is only here to show the shape.

Everything is optional: a plugin can be nothing but a COMMANDS list.
"""

NAME = "Example"
DESCRIPTION = "Counts things you tell it to count, and says hello."

_counts = {}


def _hello(match, text):
    return "Hello from a plugin. Edit plugins/example.py to make me useful."


def _count(match, text):
    what = match.group("what").strip()
    _counts[what] = _counts.get(what, 0) + 1
    return f"That is {_counts[what]} for {what}."


def _total(match, text):
    what = match.group("what").strip()
    return f"{_counts.get(what, 0)} for {what}."


# Each entry is (regular expression, function). The first one whose pattern matches wins;
# return None from a handler to say "not mine after all" and let the assistant carry on.
COMMANDS = [
    (r"^(?:say )?hello plugin$", _hello),
    (r"^count (?:a|an|one)? ?(?P<what>.+)$", _count),
    (r"^how many (?P<what>.+?) have i counted\\??$", _total),
]


def setup(api):
    """Run once when the plugin loads. `api` has: say, run, config, data_dir, log."""
    api.log.info("example plugin ready")


# Hooks (all optional) are told about things as they happen; what they return is ignored.
# def on_reply(heard, reply): ...
# def on_notification(note): ...        note is a dict: app, title, body, id
# def on_timer(label, reminder): ...
# def on_routine(name, result): ...
'''


class PluginAPI:
    """What a plugin gets in `setup(api)`. Deliberately tiny."""

    def __init__(self, say, run, config, data_dir):
        self.say = say
        self.run = run
        self.config = config
        self.data_dir = data_dir
        self.log = neon_log.get("plugin")


_API = {"value": None}


def set_api(say=None, run=None, config=None, data_dir=None) -> None:
    """Called once by the backend before `load()`."""
    _API["value"] = PluginAPI(say or (lambda _t: None), run or (lambda _t: ""), config or {},
                              data_dir or Path("."))


def folder(create: bool = True) -> Path:
    """Where plugins live. Created (with the example inside) the first time it is asked for."""
    path = _FOLDER["path"]
    if path is None:
        import app_paths
        path = Path(app_paths.data_path(FOLDER_NAME))
        _FOLDER["path"] = path
    if create and not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
            example = path / "example.py"
            if not example.exists():
                example.write_text(EXAMPLE, encoding="utf-8")
        except OSError as exc:
            log.warning("couldn't create the plugins folder: %s", exc)
    return path


def set_folder(path) -> None:
    """Point the loader somewhere else (the tests use this)."""
    _FOLDER["path"] = Path(path)


def _load_one(file: Path) -> dict | None:
    module_name = f"neon_plugin_{file.stem}_{int(file.stat().st_mtime)}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ImportError(f"{file.name} isn't importable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise

    commands = []
    for entry in getattr(module, "COMMANDS", []) or []:
        try:
            pattern, handler = entry
            commands.append((re.compile(pattern, re.I), handler))
        except (TypeError, ValueError, re.error) as exc:
            log.warning("%s: bad COMMANDS entry %r (%s)", file.name, entry, exc)
    handle = getattr(module, "handle", None)
    hooks = {name: getattr(module, name) for name in HOOKS if callable(getattr(module, name, None))}
    if not commands and not callable(handle) and not hooks:
        return None                       # nothing to offer; not an error, just not a plugin

    setup = getattr(module, "setup", None)
    if callable(setup) and _API["value"] is not None:
        setup(_API["value"])
    return {"name": str(getattr(module, "NAME", file.stem)).strip() or file.stem,
            "description": str(getattr(module, "DESCRIPTION", "")).strip(),
            "file": file, "module": module, "commands": commands,
            "handle": handle if callable(handle) else None, "hooks": hooks}


def load() -> tuple[int, list[str]]:
    """(re)load every plugin. Returns (how many loaded, human-readable problems)."""
    path = folder()
    found, problems = [], []
    try:
        files = sorted(p for p in path.glob("*.py") if not p.name.startswith("_"))
    except OSError as exc:
        return 0, [f"couldn't read the plugins folder ({exc})"]
    for file in files:
        try:
            plugin = _load_one(file)
        except BaseException as exc:  # noqa: BLE001 -- user code: anything at all can come out
            log.warning("plugin %s failed to load:\n%s", file.name, traceback.format_exc())
            problems.append(f"{file.name}: {exc}")
            continue
        if plugin is not None:
            found.append(plugin)
    with _LOCK:
        _LOADED[:] = found
        _ERRORS[:] = problems
    log.info("loaded %d plugin(s)%s", len(found), f", {len(problems)} failed" if problems else "")
    return len(found), problems


def loaded() -> list[dict]:
    with _LOCK:
        return [{k: v for k, v in p.items() if k in ("name", "description", "file")} for p in _LOADED]


def errors() -> list[str]:
    with _LOCK:
        return list(_ERRORS)


def _call(handler, *args) -> str | None:
    """Run one plugin handler, giving up if it takes too long, and never letting it raise."""
    result: dict = {}

    def work() -> None:
        try:
            result["value"] = handler(*args)
        except BaseException as exc:  # noqa: BLE001 -- user code
            result["error"] = exc

    thread = threading.Thread(target=work, name="Nova-Plugin", daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(HANDLER_TIMEOUT)
    if thread.is_alive():
        log.warning("a plugin handler is still running after %.1fs; ignoring it", time.monotonic() - started)
        return None
    if "error" in result:
        log.warning("plugin handler failed: %s", result["error"])
        return None
    value = result.get("value")
    return str(value) if value is not None and str(value).strip() else None


def emit(hook: str, *args) -> int:
    """Tell every plugin that defines `hook` (one of HOOKS) about an event, each on its own thread.
    Returns how many were told."""
    with _LOCK:
        targets = [p["hooks"][hook] for p in _LOADED if hook in p.get("hooks", {})]
    for fn in targets:
        threading.Thread(target=_call, args=(fn, *args), name=f"Nova-Plugin-{hook}", daemon=True).start()
    return len(targets)


def handle(text: str) -> str | None:
    """Give `text` to each plugin in turn; the first real answer wins."""
    with _LOCK:
        snapshot = list(_LOADED)
    if not snapshot:
        return None
    stripped = str(text).strip()
    for plugin in snapshot:
        for pattern, handler in plugin["commands"]:
            match = pattern.search(stripped)
            if match is None:
                continue
            answer = _call(handler, match, stripped)
            if answer:
                return answer
    for plugin in snapshot:
        if plugin["handle"] is not None:
            answer = _call(plugin["handle"], stripped)
            if answer:
                return answer
    return None


# ---------------------------------------------------------------------------
# Voice commands about the plugins themselves
# ---------------------------------------------------------------------------

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you)\s+)*"
_RELOAD = re.compile(rf"^{_LEAD}(?:re-?load|refresh|re-?scan)\s+(?:my |the |your )?plugins?[.!]?$")
_LIST = re.compile(
    rf"^{_LEAD}(?:what|which)\s+plugins?(?:\s+(?:do (?:i|you) have|are (?:there|loaded)|are installed))?[?.!]?$"
    rf"|^{_LEAD}list (?:my |the |your )?plugins?[?.!]?$")


def handle_plugin_command(text: str) -> str | None:
    """"reload plugins" / "what plugins do I have", or None."""
    t = " ".join(str(text).lower().split())
    if _RELOAD.match(t):
        count, problems = load()
        base = f"Reloaded {count} plugin{'s' if count != 1 else ''}."
        return base + (f" {len(problems)} failed to load; see the log." if problems else "")
    if _LIST.match(t):
        items = loaded()
        if not items:
            return ("No plugins loaded. Drop a Python file in the plugins folder "
                    "(Settings, General, \"Open the data folder\") and say \"reload plugins\".")
        listed = ", ".join(f"{p['name']}" + (f" ({p['description']})" if p["description"] else "")
                           for p in items)
        problems = errors()
        return f"{len(items)} loaded: {listed}." + (f" {len(problems)} failed." if problems else "")
    return None
