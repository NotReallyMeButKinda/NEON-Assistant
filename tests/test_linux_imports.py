"""Linux, simulated on this PC: every module imports without Windows (tests/linux_sim.py), and the windows and
hotkeys drive a fake Hyprland (each config dialect) and a fake KDE correctly (tests/linux_smoke.py). Each run is
its own process, since pretending to be Linux can't be undone."""

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=str(HERE.parent))


class LinuxSimulationTests(unittest.TestCase):
    def test_every_module_imports(self):
        for desktop in ("hyprland", "kde"):
            with self.subTest(desktop=desktop):
                done = run(str(HERE / "linux_sim.py"), "--desktop", desktop)
                failures = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
                self.assertEqual(failures, [], done.stdout[-3000:] + done.stderr[-2000:])
                self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-2000:])

    def test_windows_and_hotkeys_on_a_fake_desktop(self):
        for args in (("hyprland", "lua"), ("hyprland", "hyprlang"), ("hyprland", "legacy"), ("kde",)):
            with self.subTest(desktop=" ".join(args)):
                done = run(str(HERE / "linux_smoke.py"), *args)
                failures = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
                self.assertEqual(failures, [], done.stdout[-3000:] + done.stderr[-2000:])
                self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-2000:])
                self.assertIn("checks passed", done.stdout)


if __name__ == "__main__":
    unittest.main()
