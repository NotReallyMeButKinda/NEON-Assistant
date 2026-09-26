"""Run by tests/test_frame_live.py in its own process (it needs real windows, not the offscreen platform).
Probes NEON's title bar with native windows parked far off-screen: never activated, never visible.
Prints, for each case, the frame layout and what the window answers to WM_NCHITTEST on its title bar."""
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NEON_DATA_DIR"] = tempfile.mkdtemp()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

app = QApplication([])
import assistant as backend  # noqa: E402
from ui import frame  # noqa: E402

user32 = ctypes.windll.user32
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
WM_NCHITTEST = 0x84
OFF = (-20000, -20000)


def pump(n=20):
    for _ in range(n):
        app.processEvents()


def rects(w):
    hwnd = wintypes.HWND(int(w.winId()))
    wr, cr = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    corner = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(corner))
    return wr, cr, corner


def hit(w, dx, dy):
    wr, _cr, _c = rects(w)
    x, y = wr.left + dx, wr.top + dy
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    return user32.SendMessageW(wintypes.HWND(int(w.winId())), WM_NCHITTEST, 0, lp)


def report(label, w):
    wr, cr, corner = rects(w)
    print(f"{label}: window {wr.right - wr.left}x{wr.bottom - wr.top}, client {cr.right}x{cr.bottom}, "
          f"client starts {corner.y - wr.top}px below the window top; frameless flag="
          f"{bool(w.windowFlags() & Qt.FramelessWindowHint)}; custom bar={getattr(w, '_neon_titlebar', None) is not None}")
    print(f"   hit test 18px down, 200px in: {hit(w, 200, 18)}   (2 = caption: draggable, 1 = client)")


def make():
    w = QWidget()
    w.setAttribute(Qt.WA_ShowWithoutActivating)
    w.setWindowTitle("probe")
    lay = QVBoxLayout(w)
    lay.addWidget(QLabel("content"))
    w.resize(600, 400)
    return w


if __name__ == "__main__":
    for start in (True, False):
        backend.CONFIG["custom_titlebar"] = start
        w = make()
        frame.install(w, minimize=True, maximize=True)
        w.move(*OFF)
        w.show()
        pump()
        report(f"start {'custom' if start else 'native'}", w)
        backend.CONFIG["custom_titlebar"] = not start
        frame.refresh_all()
        w.move(*OFF)
        pump()
        report(f"   switched to {'custom' if not start else 'native'}", w)
        w.close()
        pump()

    # The close button's glyph must be drawn in the icon font (the app's style sheet used to override it).
    from PySide6.QtGui import QFontInfo
    from ui import theme
    theme.apply_theme("ember", "", None)
    backend.CONFIG["custom_titlebar"] = True
    w = make()
    frame.install(w, minimize=True, maximize=True)
    w.move(*OFF)
    w.show()
    pump()
    close = next(b for b in w._neon_titlebar.buttons if b.kind == "close")
    print(f"close glyph font: {QFontInfo(close.font()).family()}")

    # A stuck mouse capture (a release the window never saw) is let go on the next move with no button down,
    # and a plain click on the bar still starts a move.
    hwnd = wintypes.HWND(int(w.winId()))
    user32.SetCapture.argtypes = [wintypes.HWND]
    user32.GetCapture.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetCapture(hwnd)
    user32.PostMessageW(hwnd, 0x200, 0, (100 << 16) | 100)
    pump()
    captured = user32.GetCapture()
    print(f"capture released: {not (captured and int(captured) == int(w.winId()))}")
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    moves = []
    w._neon_titlebar._start_move = lambda: moves.append(1)
    QTest.mousePress(w._neon_titlebar, Qt.LeftButton, Qt.NoModifier, QPoint(150, 18))
    print(f"click on the bar moves: {bool(moves)}")

    # The glowing outline around a window must really be shown and painted.
    from ui import window_highlight as wh
    painted = []
    real_paint = wh._Outline.paintEvent
    wh._Outline.paintEvent = lambda self, e: (painted.append(1), real_paint(self, e))
    outline = wh.WindowHighlight()
    outline.show_windows([int(w.winId())], 5)
    pump(40)
    print(f"outline visible: {all(o.isVisible() for o in outline._outlines) and bool(outline._outlines)}, "
          f"painted: {bool(painted)}")
    outline.clear()
