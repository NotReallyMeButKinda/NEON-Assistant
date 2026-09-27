"""
linuxdesk/hypr.py -- Hyprland through hyprctl: windows and monitors as JSON, window actions, and the runtime
settings NEON needs (keybinds for its hotkeys, window rules for its bar and pop-ups, reserved screen space,
a window's border colour for the "close this?" outline).

Hyprland has changed how these are spoken three times, and Arch users may be on any of them, so every
action here is written once per dialect and the running Hyprland's dialect is found on first use:

  * "lua"      -- 0.55 and later with a hyprland.lua config: `hyprctl eval '<lua>'` (hl.bind, hl.window_rule,
                  hl.dispatch(hl.dsp...)); `hyprctl keyword` is refused there.
  * "hyprlang" -- 0.53 to 0.56 with a hyprland.conf: `hyprctl keyword` / `dispatch` with the 0.53 rule syntax
                  (`windowrule = match:title ..., float on`).
  * "legacy"   -- 0.52 and older: the same, with the old rule syntax (`windowrule = float, title:...`).

Everything set here lasts until Hyprland reloads its config, so NEON sets it again at startup and whenever the
settings change; nothing is written to the config files.
"""

from __future__ import annotations

import json
import re
import weakref

import osinfo

_DIALECT: dict = {"value": None}


def available() -> bool:
    return osinfo.desktop() == "hyprland" and bool(osinfo.which("hyprctl"))


def query(what: str):
    """`hyprctl -j <what>` parsed ('clients', 'activewindow', 'monitors', 'version'...), or None."""
    done = osinfo.run(["hyprctl", "-j", what], timeout=3)
    if not done.ok or not done.out.strip():
        return None
    try:
        return json.loads(done.out)
    except ValueError:
        return None


def _ok(out: str) -> bool:
    return out.strip().lower().startswith("ok")


def version() -> tuple[int, int]:
    """(major, minor) of the running Hyprland, (0, 0) if unknown."""
    info = query("version") or {}
    text = str(info.get("tag") or info.get("version") or "") if isinstance(info, dict) else ""
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def dialect() -> str:
    """'lua' | 'hyprlang' | 'legacy' (see the module docstring). Asked once, then remembered."""
    if _DIALECT["value"] is None:
        done = osinfo.run(["hyprctl", "eval", "return true"], timeout=3)
        if done.ok and _ok(done.out):
            _DIALECT["value"] = "lua"
        else:
            _DIALECT["value"] = "hyprlang" if version() >= (0, 53) else "legacy"
    return _DIALECT["value"]


def forget_dialect() -> None:
    """Ask again next time (the config language changed, or tests)."""
    _DIALECT["value"] = None


def eval_lua(code: str) -> bool:
    done = osinfo.run(["hyprctl", "eval", code], timeout=3)
    return done.ok and _ok(done.out)


def dispatch(*args: str) -> bool:
    """A hyprlang dispatcher (`closewindow address:0x...`). With a Lua config, use the actions below."""
    done = osinfo.run(["hyprctl", "dispatch", *args], timeout=3)
    return done.ok and _ok(done.out)


def keyword(name: str, value: str) -> bool:
    done = osinfo.run(["hyprctl", "keyword", name, value], timeout=3)
    return done.ok and _ok(done.out)


def lua_string(text: str) -> str:
    """A Lua string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _lua_dispatch(call: str) -> bool:
    return eval_lua(f"hl.dispatch({call})")


# ---------------------------------------------------------------------------
# Window actions (a window is its address, "0x55d3c2a1b2c0")
# ---------------------------------------------------------------------------

def _win(address: str) -> str:
    return f"address:{address}"


def close_window(address: str) -> bool:
    if dialect() == "lua":
        return _lua_dispatch(f"hl.dsp.window.close({{ window = {lua_string(_win(address))} }})")
    return dispatch("closewindow", _win(address))


def focus_window(address: str) -> bool:
    if dialect() == "lua":
        return _lua_dispatch(f"hl.dsp.focus({{ window = {lua_string(_win(address))} }})")
    return dispatch("focuswindow", _win(address))


def move_to_workspace(address: str, workspace: str, follow: bool = False) -> bool:
    if dialect() == "lua":
        return _lua_dispatch(f"hl.dsp.window.move({{ window = {lua_string(_win(address))}, "
                             f"workspace = {lua_string(workspace)}, follow = {str(follow).lower()} }})")
    return dispatch("movetoworkspace" if follow else "movetoworkspacesilent", f"{workspace},{_win(address)}")


def maximize(address: str) -> bool:
    if dialect() == "lua":
        return _lua_dispatch(f"hl.dsp.window.fullscreen({{ window = {lua_string(_win(address))}, "
                             'mode = "maximized", action = "set" })')
    return focus_window(address) and dispatch("fullscreen", "1")


def exit_session() -> bool:
    """Log out of Hyprland (hyprshutdown closes apps gracefully first, where it's installed)."""
    if osinfo.which("hyprshutdown"):
        return osinfo.spawn(["hyprshutdown"])
    if dialect() == "lua":
        return _lua_dispatch("hl.dsp.exit()")
    return dispatch("exit")


# ---------------------------------------------------------------------------
# Keybinds: NEON's hotkeys as Hyprland binds that run the `neon` command
# ---------------------------------------------------------------------------

_KEY_NAMES = {"space": "SPACE", "return": "RETURN", "enter": "RETURN", "esc": "ESCAPE", "escape": "ESCAPE",
              "tab": "TAB", "backspace": "BACKSPACE", "del": "DELETE", "delete": "DELETE", "ins": "INSERT",
              "home": "HOME", "end": "END", "pgup": "PRIOR", "pgdown": "NEXT", "up": "UP", "down": "DOWN",
              "left": "LEFT", "right": "RIGHT", "print": "PRINT", "pause": "PAUSE", "`": "grave", "-": "minus",
              "=": "equal", "[": "bracketleft", "]": "bracketright", ";": "semicolon", "'": "apostrophe",
              ",": "comma", ".": "period", "/": "slash", "\\": "backslash"}
_MOD_NAMES = {"ctrl": "CTRL", "control": "CTRL", "alt": "ALT", "shift": "SHIFT", "win": "SUPER", "meta": "SUPER",
              "super": "SUPER", "cmd": "SUPER"}


def bind_spec(qt_spec: str) -> tuple[str, str] | None:
    """A Qt key sequence ('Ctrl+Alt+N', 'Meta+Space') -> Hyprland (mods, key): ('CTRL ALT', 'N')."""
    parts = [p.strip() for p in str(qt_spec or "").replace(" ", "").split("+")]
    if not parts or not parts[-1]:
        if str(qt_spec or "").endswith("++"):                 # 'Ctrl++': the plus key
            parts = parts[:-2] + ["plus"]
        else:
            return None
    *mods, key = parts
    names = []
    for mod in mods:
        name = _MOD_NAMES.get(mod.lower())
        if name is None:
            return None
        names.append(name)
    lowered = key.lower()
    if lowered in _KEY_NAMES:
        key = _KEY_NAMES[lowered]
    elif len(key) == 1:
        key = key.upper() if key.isalpha() else key
    elif lowered.startswith("f") and lowered[1:].isdigit():
        key = key.upper()
    return " ".join(names), key


def lua_keys(mods: str, key: str) -> str:
    """('CTRL ALT', 'N') -> 'CTRL + ALT + N', the way hl.bind names keys."""
    return " + ".join(mods.split() + [key])


class Binds:
    """NEON's current Hyprland binds, so a settings change can take the old ones away first."""

    _all: "weakref.WeakSet[Binds]" = weakref.WeakSet()

    def __init__(self):
        self._active: list[tuple[str, str]] = []
        Binds._all.add(self)

    @classmethod
    def forget_all(cls) -> None:
        """A config reload already removed NEON's binds: unbinding them again could remove the user's own
        binds on the same keys, which the reload just brought back."""
        for binds in list(cls._all):
            binds._active = []

    def apply(self, binds: list[tuple[str, str, str]], command: list[str]) -> list[str]:
        """binds: (name, qt_spec, action). Each becomes a bind running `<command> <action>`; an action ending
        in ':hold' gets a press bind (<base>-start) and a release bind (<base>-stop). Returns the names
        Hyprland refused."""
        self.clear()
        failed = []
        exec_line = " ".join(_quote(part) for part in command)
        lua = dialect() == "lua"
        for name, spec, action in binds:
            parsed = bind_spec(spec)
            if parsed is None:
                failed.append(name)
                continue
            mods, key = parsed
            if action.endswith(":hold"):
                base = action[: -len(":hold")]
                pairs = [(f"{exec_line} {base}-start", False), (f"{exec_line} {base}-stop", True)]
            else:
                pairs = [(f"{exec_line} {action}", False)]
            ok = True
            for line, release in pairs:
                if lua:
                    flags = ", { release = true }" if release else ""
                    ok = eval_lua(f"hl.bind({lua_string(lua_keys(mods, key))}, "
                                  f"hl.dsp.exec_cmd({lua_string(line)}){flags})") and ok
                else:
                    ok = keyword("bindr" if release else "bind", f"{mods}, {key}, exec, {line}") and ok
            if ok:
                self._active.append((mods, key))
            else:
                failed.append(name)
        return failed

    def clear(self) -> None:
        if not self._active:
            return
        lua = dialect() == "lua"
        for mods, key in self._active:
            if lua:
                eval_lua(f"hl.unbind({lua_string(lua_keys(mods, key))})")
            else:
                keyword("unbind", f"{mods}, {key}")
        self._active = []


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


# ---------------------------------------------------------------------------
# NEON's own windows: rules so the bar and pop-ups float, stay put and don't take focus
# ---------------------------------------------------------------------------

def _regex(text: str) -> str:
    return "".join("\\" + c if c in ".^$*+?()[]{}|\\" else c for c in text)


def window_rules(title: str, focus: bool = False) -> bool:
    """Float and pin (on every workspace) NEON's window with this exact title, with no border, shadow, blur,
    rounding or animation, and (unless `focus`) never focused. Rules apply when a window opens, so call this
    before it first shows; place() then puts it where it belongs."""
    pattern = f"^({_regex(title)})$"
    kind = dialect()
    if kind == "lua":
        name = "neon-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        fields = ["float = true", "pin = true", "border_size = 0", "no_shadow = true", "no_blur = true",
                  "no_anim = true", "rounding = 0"]
        if not focus:
            fields += ["no_focus = true", "no_initial_focus = true"]
        return eval_lua(f"hl.window_rule({{ name = {lua_string(name)}, match = {{ title = {lua_string(pattern)} }}, "
                        + ", ".join(fields) + " })")
    if kind == "hyprlang":
        effects = ["float on", "pin on", "border_size 0", "no_shadow on", "no_blur on", "no_anim on", "rounding 0"]
        if not focus:
            effects += ["no_focus on", "no_initial_focus on"]
        return keyword("windowrule", f"match:title {pattern}, " + ", ".join(effects))
    effects = ["float", "pin", "noborder", "noshadow", "noblur", "noanim", "rounding 0"]
    if not focus:
        effects += ["nofocus", "noinitialfocus"]
    match = f"title:{pattern}"
    ok = all(keyword("windowrule", f"{effect}, {match}") for effect in effects)
    return ok or all(keyword("windowrulev2", f"{effect}, {match}") for effect in effects)


def place(title: str, x: int, y: int, width: int, height: int) -> bool:
    """Move and size NEON's open floating window with this exact title (layout pixels)."""
    target = f"title:^({_regex(title)})$"
    x, y, width, height = int(x), int(y), int(width), int(height)
    if dialect() == "lua":
        window = lua_string(target)
        return _lua_dispatch(f"hl.dsp.window.resize({{ window = {window}, x = {width}, y = {height} }})") and \
            _lua_dispatch(f"hl.dsp.window.move({{ window = {window}, x = {x}, y = {y} }})")
    return dispatch("resizewindowpixel", f"exact {width} {height},{target}") and \
        dispatch("movewindowpixel", f"exact {x} {y},{target}")


def monitor_at(x: int, y: int) -> str:
    """The name of the monitor containing the layout point (x, y) ('DP-1'), or ''."""
    for m in query("monitors") or []:
        scale = float(m.get("scale") or 1.0)
        width, height = int(m.get("width", 0)) / scale, int(m.get("height", 0)) / scale
        if int(m.get("transform") or 0) % 2:
            width, height = height, width                   # rotated 90 or 270 degrees
        left, top = int(m.get("x", 0)), int(m.get("y", 0))
        if left <= x < left + width and top <= y < top + height:
            return str(m.get("name", ""))
    return ""


def reserve(monitor: str, top: int = 0, bottom: int = 0) -> bool:
    """Keep `top` / `bottom` pixels of `monitor` free of tiled windows (the status bar's AppBar on Windows);
    reserve(monitor) gives the space back."""
    top, bottom = max(0, int(top)), max(0, int(bottom))
    if dialect() != "lua":
        return keyword("monitor", f"{monitor},addreserved,{top},{bottom},0,0")
    # hl.monitor() starts from the monitor's own named rule if it has one, else from defaults: pass its current
    # mode, position, scale and rotation so nothing else about the monitor changes.
    info = next((m for m in query("monitors") or [] if m.get("name") == monitor), None)
    fields = [f"output = {lua_string(monitor)}",
              f"reserved_area = {{ top = {top}, bottom = {bottom}, left = 0, right = 0 }}"]
    if info:
        mode = "{}x{}@{:.2f}".format(info.get("width", 0), info.get("height", 0), float(info.get("refreshRate") or 60))
        fields += [f"mode = {lua_string(mode)}",
                   f"position = {lua_string('{}x{}'.format(info.get('x', 0), info.get('y', 0)))}",
                   f"scale = {lua_string('{:g}'.format(float(info.get('scale') or 1)))}",
                   f"transform = {int(info.get('transform') or 0)}"]
    return eval_lua("hl.monitor({ " + ", ".join(fields) + " })")


def set_border(address: str, colour: str | None) -> bool:
    """Colour one window's border ('#ff2fb4'), or give it its normal colour back (None)."""
    kind = dialect()
    value = "unset" if colour is None else f"rgb({colour.lstrip('#')})"
    props = ("active_border_color", "inactive_border_color")
    if kind == "lua":
        window = lua_string(_win(address))
        return all([_lua_dispatch(f"hl.dsp.window.set_prop({{ window = {window}, prop = {lua_string(prop)}, "
                                  f"value = {lua_string(value)} }})") for prop in props])
    if kind == "hyprlang":
        return all([dispatch("setprop", f"{_win(address)} {prop} {value}") for prop in props])
    if colour is None:
        return all([dispatch("setprop", _win(address), prop, "-1")
                    for prop in ("activebordercolor", "inactivebordercolor")])
    return all([dispatch("setprop", _win(address), prop, value, "lock")
                for prop in ("activebordercolor", "inactivebordercolor")])


# ---------------------------------------------------------------------------
# Config reloads: everything above is set at runtime and a reload wipes it, so NEON sets it again
# ---------------------------------------------------------------------------

def event_socket() -> str:
    """Hyprland's event socket (socket2) for this session, or ''."""
    import os
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not signature:
        return ""
    for base in (os.environ.get("XDG_RUNTIME_DIR", ""), "/tmp"):
        path = os.path.join(base, "hypr", signature, ".socket2.sock") if base else ""
        if path and os.path.exists(path):
            return path
    return ""


def is_reload(line: str) -> bool:
    return line.split(">>", 1)[0].strip() == "configreloaded"


class ReloadWatcher:
    """Calls on_reload() (from its own thread) each time Hyprland reloads its config."""

    def __init__(self, on_reload):
        self._on_reload = on_reload
        self._sock = None
        self._thread = None

    def start(self) -> bool:
        import socket
        import threading
        path = event_socket()
        if not path or not hasattr(socket, "AF_UNIX"):
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
        except OSError:
            return False
        self._sock = sock
        self._thread = threading.Thread(target=self._run, args=(sock,), name="Nova-HyprEvents", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self, sock) -> None:
        buffer = b""
        while self._sock is sock:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            if any(is_reload(line.decode("utf-8", "replace")) for line in lines):
                forget_dialect()                     # the reload may have switched hyprland.conf <-> hyprland.lua
                Binds.forget_all()
                try:
                    self._on_reload()
                except Exception:  # noqa: BLE001 -- never let a callback end the watcher
                    pass
