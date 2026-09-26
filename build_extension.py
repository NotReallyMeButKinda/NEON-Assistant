"""
build_extension.py -- package browser_extension/ for Firefox / Zen and for Chrome / Edge / Brave.

    python build_extension.py            -> dist/neon-bridge.xpi           (Firefox, Zen: Manifest V2)
                                            dist/neon-bridge-chrome/        (Chrome, Edge, Brave: Manifest V3,
                                            dist/neon-bridge-chrome.zip      unpacked folder + the same as a zip)

The files are shared; only manifest.json differs. browser_extension/manifest.json is the Firefox one, and the
Chrome one is made from it here (a service worker instead of a background page, `action` instead of
`browserAction`, host permissions listed apart, and the `alarms` permission that wakes the service worker).

Installing: see INSTALL.md, "The browser extension".
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "browser_extension"
DIST = ROOT / "dist"


def _files() -> list[Path]:
    if not (SOURCE / "manifest.json").is_file():
        raise SystemExit("browser_extension/manifest.json is missing")
    return sorted(p for p in SOURCE.rglob("*")
                  if p.is_file() and not p.name.startswith(".") and p.suffix not in (".zip", ".xpi")
                  and p.name != "manifest.json")


def firefox_manifest() -> dict:
    return json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))


def chrome_manifest(firefox: dict | None = None) -> dict:
    """The Manifest V3 version of the Firefox manifest, for Chrome, Edge and Brave."""
    ff = firefox or firefox_manifest()
    hosts = [p for p in ff.get("permissions", []) if "://" in p or p == "<all_urls>"]
    permissions = [p for p in ff.get("permissions", []) if p not in hosts]
    for extra in ("scripting", "alarms"):
        if extra not in permissions:
            permissions.append(extra)
    action = dict(ff.get("browser_action") or {})
    return {
        "manifest_version": 3,
        "name": ff["name"],
        "version": ff["version"],
        "description": ff.get("description", ""),
        "icons": ff.get("icons", {}),
        "permissions": permissions,
        "host_permissions": hosts,
        "background": {"service_worker": "background.js"},
        "action": action,
        "options_ui": ff.get("options_ui", {}),
    }


def _zip(out: Path, manifest: dict) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path in _files():
            archive.write(path, path.relative_to(SOURCE).as_posix())
    return out


def build(out: Path | None = None) -> Path:
    """The Firefox / Zen package (.xpi). Returns its path."""
    return _zip(out or DIST / "neon-bridge.xpi", firefox_manifest())


def build_chrome(folder: Path | None = None) -> tuple[Path, Path]:
    """The Chrome / Edge / Brave version, unpacked (for "Load unpacked") and zipped. Returns both paths."""
    folder = folder or DIST / "neon-bridge-chrome"
    manifest = chrome_manifest()
    if folder.exists():
        shutil.rmtree(folder)
    for path in _files():
        target = folder / path.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder, _zip(folder.with_suffix(".zip"), manifest)


if __name__ == "__main__":
    xpi = build(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"Built {xpi} ({xpi.stat().st_size // 1024} KB)  -- Firefox, Zen")
    folder, archive = build_chrome()
    print(f"Built {folder}\\ and {archive.name} ({archive.stat().st_size // 1024} KB)  -- Chrome, Edge, Brave")
