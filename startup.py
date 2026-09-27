"""
startup.py -- launch the assistant automatically when you sign in to Windows.

Uses the per-user Run registry key (HKCU), so no administrator rights are
needed, and starts pythonw.exe so no console window appears. The app is
launched with --minimized so it comes up in the tray + status bar instead of
throwing the main window in your face.
"""

from __future__ import annotations

import app_paths
import osinfo

if osinfo.IS_WINDOWS:
    import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NeonAssistant"


def startup_command() -> str:
    return app_paths.launch_command()


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """Adds or removes the Run entry. Raises OSError if the registry refuses."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Linux: an XDG autostart entry (~/.config/autostart), which KDE Plasma and most desktops run at sign-in.
# Hyprland runs them when started through uwsm, or with `exec-once = dex -a` (see INSTALL.md).
# ---------------------------------------------------------------------------

if not osinfo.IS_WINDOWS:
    from linuxdesk import apps as _apps

    def _autostart_file():
        return _apps.config_home() / "autostart" / _apps.DESKTOP_ID

    def is_enabled() -> bool:  # noqa: F811
        return _autostart_file().is_file()

    def set_enabled(enabled: bool) -> None:  # noqa: F811
        path = _autostart_file()
        if not enabled:
            path.unlink(missing_ok=True)
            return
        import shortcuts
        _apps.write_entry(path, _apps.entry_text("Neon Assistant", startup_command(), shortcuts.icon_path(),
                                                 "Start NEON when you sign in",
                                                 {"X-GNOME-Autostart-enabled": "true"}))
