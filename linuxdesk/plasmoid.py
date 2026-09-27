"""
linuxdesk/plasmoid.py -- installs NEON's KDE Plasma panel widget (plasmoid/org.neon.assistant).

The widget follows NEON through panel_feed.py. Installing copies the package with two things filled in that
only this PC knows: the command that starts NEON (for the widget's "Start NEON" button) and the feed's port.
Plasma's own kpackagetool6 does the installing, into ~/.local/share/plasma/plasmoids; then the widget is in
the panel's "Add Widgets..." list as "NEON Assistant".
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

import app_paths
import osinfo

WIDGET_ID = "org.neon.assistant"
DEFAULT_PORT = 47812


def source_dir() -> Path:
    return app_paths.resource_path("plasmoid") / WIDGET_ID


def tool() -> str:
    return "kpackagetool6" if osinfo.which("kpackagetool6") else ""


def install_dir() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "plasma" / "plasmoids" / WIDGET_ID


def installed() -> bool:
    return (install_dir() / "metadata.json").is_file()


def launch_command() -> str:
    return shlex.join(app_paths.launch_args(["--minimized"]))


def prepared_copy(port: int) -> Path:
    """The package, copied to a temporary folder, with NEON's start command and port as the widget's defaults."""
    target = Path(tempfile.mkdtemp(prefix="neon-plasmoid-")) / WIDGET_ID
    shutil.copytree(source_dir(), target)
    config = target / "contents" / "config" / "main.xml"
    text = config.read_text(encoding="utf-8")
    text = text.replace("<default>@LAUNCH@</default>", f"<default>{escape(launch_command())}</default>")
    text = text.replace(f"<default>{DEFAULT_PORT}</default>", f"<default>{int(port)}</default>")
    config.write_text(text, encoding="utf-8")
    return target


def install(port: int) -> tuple[bool, str]:
    """Install the widget, or update it if it's already there. Returns (ok, what to tell the user)."""
    if not tool():
        return False, "Installing the panel widget needs Plasma's kpackagetool6 (part of KDE Plasma 6)."
    package = prepared_copy(port)
    try:
        action = "--upgrade" if installed() else "--install"
        done = osinfo.run([tool(), "--type", "Plasma/Applet", action, str(package)], timeout=60)
    finally:
        shutil.rmtree(package.parent, ignore_errors=True)
    if not done.ok:
        return False, "Plasma didn't install the widget: " + (done.err or done.out).strip()[:300]
    verb = "Updated" if action == "--upgrade" else "Installed"
    return True, (f"{verb}. Right-click the panel, choose Add Widgets..., and drag \"NEON Assistant\" onto it. "
                  "(A panel widget already there picks up the update after you sign out and in.)")


def uninstall() -> tuple[bool, str]:
    if not tool() or not installed():
        return False, "The panel widget isn't installed."
    done = osinfo.run([tool(), "--type", "Plasma/Applet", "--remove", WIDGET_ID], timeout=60)
    return done.ok, "Removed." if done.ok else "Plasma didn't remove it: " + (done.err or done.out).strip()[:300]
