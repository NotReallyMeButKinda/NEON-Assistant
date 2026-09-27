"""An accent-coloured glowing outline around other apps' windows, shown while I ask "Close Discord?" so it's
obvious which window a yes would close.

One see-through, click-through, never-focused overlay per window, placed in physical pixels on the window's
visible frame (DWM's extended frame bounds: GetWindowRect includes the invisible resize borders). It
follows the window if it moves, disappears if the window closes or is minimized, and takes itself away
after the question times out. The glow breathes gently unless Windows' animation effects are off.

On Linux an app can't put a window around another one (Wayland won't let it position itself), so on
Hyprland the window's own border turns the accent colour instead (and back afterwards); elsewhere nothing
is outlined and the spoken question does the job.
"""

from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import osinfo

from .motion import animations_enabled
from .status_bar import list_monitors
from .theme import COLORS

user32 = ctypes.windll.user32 if osinfo.IS_WINDOWS else None
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
DWMWA_EXTENDED_FRAME_BOUNDS = 9

MARGIN = 22                 # logical pixels of glow outside the window's edge
LINE = 3.0                  # the solid outline's width
RADIUS = 10.0               # corner rounding (Windows 11 windows are rounded ~8 px)
TRACK_MS = 120              # how often the window's position is re-read
PULSE_SECONDS = 1.6


def frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's visible frame (left, top, right, bottom) in physical pixels, or None if it's gone,
    hidden or minimized."""
    handle = wintypes.HWND(int(hwnd))
    if not user32.IsWindow(handle) or not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
        return None
    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(handle, DWMWA_EXTENDED_FRAME_BOUNDS,
                                                        ctypes.byref(rect), ctypes.sizeof(rect)) == 0
    except (AttributeError, OSError):
        ok = False
    if not ok and not user32.GetWindowRect(handle, ctypes.byref(rect)):
        return None
    if rect.right - rect.left < 20 or rect.bottom - rect.top < 20:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _scale_at(x: int, y: int) -> float:
    """The display scale of the monitor containing a physical point (1.0 if unknown)."""
    for m in list_monitors():
        if m[0] <= x < m[2] and m[1] <= y < m[3]:
            return float(m[4] or 1.0)
    return 1.0


class _Outline(QWidget):
    """The glow around one window."""

    def __init__(self, hwnd: int, started: float):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
                            | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hwnd = int(hwnd)
        self._started = started
        self._placed: tuple | None = None
        self._scale = 1.0

    def track(self) -> bool:
        """Follow the window; False once there's nothing left to outline."""
        bounds = frame_bounds(self.hwnd)
        if bounds is None:
            return False
        if bounds != self._placed:
            self._placed = bounds
            left, top, right, bottom = bounds
            self._scale = _scale_at((left + right) // 2, (top + bottom) // 2)
            pad = int(MARGIN * self._scale)
            width, height = right - left + 2 * pad, bottom - top + 2 * pad
            self.resize(int(width / self._scale), int(height / self._scale))
            if not self.isVisible():
                # Qt must show it itself: shown only by SetWindowPos, Qt thought it hidden and never painted it,
                # so the outline was an invisible window.
                self.show()
            user32.SetWindowPos(wintypes.HWND(int(self.winId())), wintypes.HWND(HWND_TOPMOST), left - pad, top - pad,
                                width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        return True

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        breath = 1.0
        if animations_enabled():
            phase = (time.monotonic() - self._started) / PULSE_SECONDS * 2 * math.pi
            breath = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(phase))
        accent = QColor(COLORS["accent"])
        inner = QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)

        # The glow: wide, faint strokes first, narrowing and strengthening towards the edge.
        for step in range(MARGIN, 0, -2):
            color = QColor(accent)
            strength = (1.0 - step / MARGIN) ** 2
            color.setAlpha(int(110 * strength * breath))
            p.setPen(QPen(color, 2.2))
            path = QPainterPath()
            path.addRoundedRect(inner.adjusted(-step, -step, step, step), RADIUS + step, RADIUS + step)
            p.drawPath(path)

        # The outline itself, crisp and bright.
        color = QColor(accent)
        color.setAlpha(int(255 * (0.8 + 0.2 * breath)))
        p.setPen(QPen(color, LINE))
        path = QPainterPath()
        path.addRoundedRect(inner.adjusted(-LINE / 2, -LINE / 2, LINE / 2, LINE / 2), RADIUS, RADIUS)
        p.drawPath(path)
        p.end()


class WindowHighlight:
    """Owns the outlines. Driven by the controller's `highlight_windows(hwnds, seconds)` and
    `highlight_clear()` signals (queued to the UI thread, whatever thread asked)."""

    def __init__(self, controller=None):
        self._outlines: list[_Outline] = []
        self._timer = QTimer()
        self._timer.setInterval(TRACK_MS)
        self._timer.timeout.connect(self._tick)
        self._pulse = QTimer()
        self._pulse.setInterval(33)
        self._pulse.timeout.connect(self._repaint)
        self._expire = QTimer()
        self._expire.setSingleShot(True)
        self._expire.timeout.connect(self.clear)
        if controller is not None:
            controller.highlight_windows.connect(self.show_windows)
            controller.highlight_clear.connect(self.clear)

    @property
    def active(self) -> bool:
        return bool(self._outlines)

    def show_windows(self, hwnds: list, seconds: float = 45.0) -> None:
        self.clear()
        started = time.monotonic()
        for hwnd in hwnds:
            outline = _Outline(int(hwnd), started)
            if outline.track():
                self._outlines.append(outline)
            else:
                outline.deleteLater()
        if not self._outlines:
            return
        self._timer.start()
        if animations_enabled():
            self._pulse.start()
        self._expire.start(int(max(1.0, float(seconds)) * 1000))

    def clear(self) -> None:
        self._timer.stop()
        self._pulse.stop()
        self._expire.stop()
        for outline in self._outlines:
            outline.hide()
            outline.deleteLater()
        self._outlines = []

    def _tick(self) -> None:
        for outline in list(self._outlines):
            if not outline.track():
                outline.hide()
                outline.deleteLater()
                self._outlines.remove(outline)
        if not self._outlines:
            self.clear()

    def _repaint(self) -> None:
        for outline in self._outlines:
            outline.update()


class BorderHighlight:
    """Linux: the same interface, recolouring the windows' own borders on Hyprland (ids are addresses)."""

    def __init__(self, controller=None):
        self._marked: list[str] = []
        self._expire = QTimer()
        self._expire.setSingleShot(True)
        self._expire.timeout.connect(self.clear)
        if controller is not None:
            controller.highlight_windows.connect(self.show_windows)
            controller.highlight_clear.connect(self.clear)

    @property
    def active(self) -> bool:
        return bool(self._marked)

    def show_windows(self, ids: list, seconds: float = 45.0) -> None:
        from linuxdesk import hypr
        self.clear()
        if not hypr.available():
            return
        colour = QColor(COLORS["accent"]).name()
        self._marked = [str(i) for i in ids if hypr.set_border(str(i), colour)]
        if self._marked:
            self._expire.start(int(max(1.0, float(seconds)) * 1000))

    def clear(self) -> None:
        from linuxdesk import hypr
        self._expire.stop()
        for address in self._marked:
            hypr.set_border(address, None)
        self._marked = []


if not osinfo.IS_WINDOWS:
    WindowHighlight = BorderHighlight  # noqa: F811
