"""NEON's title bar on real native windows (parked far off-screen, never activated): the caption can be
dragged in both modes, switching modes while a window is open keeps a working title bar, and the content
keeps its size. Needs the real Windows platform, so tests/frame_probe.py runs in its own process."""

import os
import re
import subprocess
import sys
import unittest

from tests.common import live_only

HERE = os.path.dirname(os.path.abspath(__file__))


@live_only
class TitleBarLiveTests(unittest.TestCase):
    def test_draggable_in_both_modes_and_after_switching(self):
        for scale in ("1", "1.25"):                          # the second monitor runs at 125%
            env = {**os.environ, "QT_QPA_PLATFORM": "windows", "QT_SCALE_FACTOR": scale}
            out = subprocess.run([sys.executable, os.path.join(HERE, "frame_probe.py")], env=env,
                                 capture_output=True, text=True, timeout=120).stdout
            hits = re.findall(r"hit test 18px down, 200px in: (-?\d+)", out)
            self.assertEqual(hits, ["2"] * 4, out)              # the caption answers "drag me" every time
            reports = re.findall(r"client (\d+)x(\d+), client starts (\d+)px .*frameless flag=(\w+)", out)
            self.assertEqual(len(reports), 4, out)
            for width, height, top, frameless in reports:
                self.assertEqual(int(top) > 0, frameless == "False", out)   # native: a real title bar above
            sizes = {(w, h) for w, h, _t, _f in reports}
            self.assertEqual(len(sizes), 1, out)                # the content never changes size
            self.assertRegex(out, r"close glyph font: Segoe (?:Fluent Icons|MDL2 Assets)")   # an X, not a box
            self.assertIn("outline visible: True, painted: True", out)
            self.assertIn("capture released: True", out)                  # no stuck text cursor
            self.assertIn("click on the bar moves: True", out)
