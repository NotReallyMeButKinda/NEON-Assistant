"""Start the whole app the way `main.py --selftest` does, but as Linux: main.py's Linux start-up (the command
socket instead of the Windows mutex, compositor hotkeys, the Hyprland reload watcher, the tray, every window,
every Settings page, the engines) against a fake desktop, in a throwaway data folder. Nothing is shown, spoken
or recorded (see selftest.py).

    python tests/linux_selftest.py hyprland|kde      -> prints selftest.txt; exit 1 if anything failed

Not part of the unit tests: it downloads the speech model (140 MB) into the throwaway folder each time.

The app ends its own process when it quits, so the check runs it in a child process and reads the report.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def child(desktop: str) -> None:
    sys.path.insert(0, str(HERE))
    import linux_sim
    linux_sim.pretend_linux(desktop)
    sys.path.insert(0, linux_sim.ROOT)
    import osinfo

    tools = {"hyprctl", "busctl", "wpctl", "playerctl", "wl-paste", "wl-copy", "wtype"} if desktop == "hyprland" \
        else {"kwriteconfig6", "kreadconfig6", "busctl", "wpctl", "playerctl", "wl-paste", "wl-copy", "ydotool"}

    def run(args, timeout, input_text):
        if args[0] == "hyprctl":
            if args[1] == "eval" or args[1] in ("keyword", "dispatch"):
                return osinfo.Result(True, "ok")
            if args[1:3] == ["-j", "monitors"]:
                return osinfo.Result(True, json.dumps([{"id": 0, "name": "DP-1", "x": 0, "y": 0, "width": 1920,
                                                        "height": 1080, "scale": 1.0, "refreshRate": 60.0}]))
            if args[1:3] == ["-j", "clients"]:
                return osinfo.Result(True, "[]")
            return osinfo.Result(True, "{}")
        if args[0] in ("kwriteconfig6", "kreadconfig6", "busctl"):
            return osinfo.Result(True, "")
        if args[0] == "wpctl":
            return osinfo.Result(True, "Volume: 0.50\n")
        return osinfo.Result(False, "", "not available in the simulation", 1)

    osinfo.RUNNER.update(run=run, which=lambda name: f"/usr/bin/{name}" if name in tools else None,
                         spawn=lambda args: True)
    import runpy
    sys.argv = [str(Path(linux_sim.ROOT) / "main.py"), "--selftest"]
    runpy.run_path(sys.argv[0], run_name="__main__")


def main(desktop: str) -> int:
    data = Path(tempfile.mkdtemp(prefix="neon-linux-selftest-"))
    env = dict(os.environ, NEON_DATA_DIR=str(data), QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, __file__, "--child", desktop], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    try:
        return _report(data, done)
    finally:
        shutil.rmtree(data, ignore_errors=True)        # the speech model it downloaded is 140 MB


def _report(data: Path, done) -> int:
    report = data / "selftest.txt"
    if not report.exists():
        print(f"no report; the app exited with {done.returncode}")
        print(done.stdout[-4000:], done.stderr[-4000:])
        log = data / "neon.log"
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace")[-4000:])
        return 1
    text = report.read_text(encoding="utf-8", errors="replace")
    print(text)
    return 1 if "FAIL" in text else 0


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "hyprland"))
