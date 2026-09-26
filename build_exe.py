"""
build_exe.py -- package the assistant as a Windows program (a folder with NeonAssistant.exe).

    pip install pyinstaller
    python build_exe.py [--out DIR]

The result is dist/NeonAssistant/ (or DIR/dist). Run NeonAssistant.exe from there; README.md,
INSTALL.md and the browser extension (neon-bridge.xpi for Firefox / Zen, the neon-bridge-chrome folder and
.zip for Chrome / Edge / Brave) are copied next to it. The exe keeps
its settings, downloaded voices, log and caches in %APPDATA%\\NeonAssistant, separate from the program
files, so it can live anywhere (Program Files, a USB stick) and be replaced by a newer build.
No voices or AI models are bundled: voices, Whisper and the wake-word models download when first
picked, and the Needle model is fetched the first time it runs (`needle fetch --generation 2`).
The icons (icons/neon.ico, browser_extension/icons/) are repainted from ui/app_icon.py first.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE, help="where build/ and dist/ go (default: this folder)")
    args = parser.parse_args()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller isn't installed. Run:  pip install pyinstaller")
        return 1
    out = args.out.resolve()
    subprocess.check_call([sys.executable, "-m", "ui.app_icon"], cwd=HERE)   # the glowing-dot icons
    import build_extension
    xpi = build_extension.build(out / "dist" / "neon-bridge.xpi")
    chrome_folder, chrome_zip = build_extension.build_chrome(out / "dist" / "neon-bridge-chrome")
    command = [sys.executable, "-m", "PyInstaller", str(HERE / "neon.spec"), "--noconfirm", "--clean",
               "--workpath", str(out / "build"), "--distpath", str(out / "dist")]
    print(" ".join(command))
    code = subprocess.call(command, cwd=HERE)
    if code == 0:
        target = out / "dist" / "NeonAssistant"
        for doc in ("README.md", "INSTALL.md"):
            shutil.copy2(HERE / doc, target / doc)
        shutil.copy2(xpi, target / xpi.name)
        shutil.copy2(chrome_zip, target / chrome_zip.name)
        shutil.copytree(chrome_folder, target / chrome_folder.name, dirs_exist_ok=True)
        print(f"\nBuilt: {target / 'NeonAssistant.exe'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
