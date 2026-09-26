"""The shade: a soft glow that drops down from the top of the screen with my reply in large text, for when
the status bar can't show it -- the bar is switched off, or another app is fullscreen (a video, a
borderless-windowed game). Notifications can appear in it too.

It paints everything itself (an accent glow sized to the text, and the text), never takes focus and
lets clicks through to whatever is underneath. Nothing is polled: whether it's needed is checked when
there is something to show. Games in exclusive fullscreen draw over every window, so it can't appear
over those.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

from PySide6.QtCore import QRectF, Qt, QTimer, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget

import assistant as backend

from .motion import animations_enabled
from .status_bar import _MONITORINFO, list_monitors
from .theme import COLORS

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]

MONITOR_DEFAULTTONEAREST = 2
QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 2, 3, 4
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

TEXT_PX = 30               # the reply's size (logical pixels)
LABEL_PX = 15              # the small line above it: who is talking, or which app
MAX_CHARS = 260            # a long reply shows its latest part
MAX_LINES = 4
WINDOW_PX = 280            # the see-through window's height; the glow inside it is sized to the text
GLOW_ALPHA = 150           # the glow's strength at its centre (0-255)
GROW_EASE = 0.2            # how far the glow moves toward its new size each frame
SLIDE_MS = 260
PREVIEW_TEXT = "This is the shade. My replies show up here when the status bar can't show them."


def covers_monitor(client: tuple, monitor: tuple) -> bool:
    """True if a window's client area (left, top, right, bottom) covers the whole monitor. The client
    area, not the window: a maximized window reaches a few pixels past every edge of the screen, but
    its title bar and borders are outside its client area, which stops short of the top. A borderless
    window (a game in "borderless windowed", F11 in a browser, a video player) is all client area."""
    return (client[0] <= monitor[0] and client[1] <= monitor[1]
            and client[2] >= monitor[2] and client[3] >= monitor[3])


def _exclusive_fullscreen() -> bool:
    """Windows' own answer to "is a fullscreen game or presentation running?". It catches exclusive
    fullscreen (Direct3D), which can change the resolution, so the rectangles alone may not match."""
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except (AttributeError, OSError):
        return False
    return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)


def fullscreen_monitor() -> tuple | None:
    """The monitor (left, top, right, bottom) that another app's foreground window fills -- true
    fullscreen or borderless windowed -- else None. Physical pixels, like list_monitors()."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    if name.value in _SHELL_CLASSES:
        return None                                  # the desktop itself
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info)):
        return None
    m = info.rcMonitor
    monitor = (m.left, m.top, m.right, m.bottom)
    client = wintypes.RECT()
    corner = wintypes.POINT(0, 0)
    if user32.GetClientRect(hwnd, ctypes.byref(client)) and user32.ClientToScreen(hwnd, ctypes.byref(corner)):
        area = (corner.x, corner.y, corner.x + client.right, corner.y + client.bottom)
        if covers_monitor(area, monitor):
            return monitor
    return monitor if _exclusive_fullscreen() else None


def _readable(text: str) -> str:
    """Markdown symbols out, and only the latest part of a long reply."""
    text = re.sub(r"[*_`#>~|]+", "", " ".join(str(text).split()))
    if len(text) > MAX_CHARS:
        tail = text[-MAX_CHARS:]
        text = "…" + tail[tail.find(" ") + 1:] if " " in tail else "…" + tail
    return text


class Shade(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
                            | Qt.WindowTransparentForInput | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._label = ""
        self._text = ""
        self._reveal = 0.0                            # 0 = up out of sight, 1 = fully down
        self._busy = False                            # I'm still listening / thinking / speaking
        self._kind = ""                               # "reply" | "notification" | "preview"
        self._monitor: tuple | None = None
        self._placed: tuple | None = None
        self._glow = (0.0, 0.0)                       # the glow's current width and depth
        self._glow_target = (0.0, 0.0)
        self._level = 0.0                             # my voice's loudness, smoothed
        self._grow = QTimer(self)
        self._grow.setInterval(16)
        self._grow.timeout.connect(self._grow_step)

        self._slide = QVariantAnimation(self)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.valueChanged.connect(self._set_reveal)
        self._slide.finished.connect(self._slid)
        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self.retract)

        controller.caption.connect(self._on_caption)
        controller.state.connect(self._on_state)
        controller.audio_level.connect(self._on_level)
        controller.notification.connect(self._on_notification)
        controller.shade_preview.connect(self.preview)

    # ---- when it's needed ------------------------------------------------------------------
    @staticmethod
    def _where() -> tuple | None:
        """The monitor to drop down on, or None when the status bar is showing things anyway."""
        if not backend.cfg_bool("shade_enabled"):
            return None
        full = fullscreen_monitor()
        if full is not None:
            return full
        if backend.cfg_bool("show_status_bar"):
            return None
        monitors = list_monitors()
        if not monitors:
            return None
        return monitors[min(len(monitors) - 1, max(0, int(backend.cfg_num("bar_monitor"))))][:4]

    def _on_caption(self, sender: str, text: str) -> None:
        if sender == "You":
            if self._kind == "reply":                 # a new question: the old answer goes
                self.retract()
            return
        if self._kind == "reply" and self.isVisible():
            self._show(sender, text, "reply", self._monitor)     # the reply growing as it streams in
            return
        monitor = self._where()
        if monitor is not None:
            self._show(sender, text, "reply", monitor)

    def _on_notification(self, n: dict) -> None:
        if not backend.cfg_bool("shade_notifications") or (self._kind == "reply" and self._busy):
            return                                    # don't cover what I'm saying
        monitor = self._where()
        if monitor is None:
            return
        title, body = str(n.get("title") or "").strip(), str(n.get("body") or "").strip()
        count = int(n.get("count") or 1)
        label = str(n.get("app") or "Notification") + (f"  ·  {count} new" if count > 1 else "")
        self._show(label, f"{title}: {body}" if title and body else (title or body), "notification", monitor,
                   seconds=float(n.get("seconds") or backend.cfg_num("notify_seconds")))

    def _on_state(self, state: str) -> None:
        self._busy = state in ("listening", "thinking", "speaking")
        if self._kind == "reply" and self.isVisible():
            if self._busy:
                self._linger.stop()
            else:
                self._linger.start(int(backend.cfg_num("shade_seconds") * 1000))

    def preview(self) -> None:
        """Settings' "Show the shade": on the bar's monitor, whatever the conditions."""
        monitors = list_monitors()
        if monitors:
            monitor = monitors[min(len(monitors) - 1, max(0, int(backend.cfg_num("bar_monitor"))))][:4]
            self._show(backend.CONFIG.get("assistant_name") or "Nova", PREVIEW_TEXT, "preview", monitor,
                       seconds=max(3.0, backend.cfg_num("shade_seconds")))

    # ---- showing ---------------------------------------------------------------------------
    def _show(self, label: str, text: str, kind: str, monitor: tuple, seconds: float = 0.0) -> None:
        self._label, self._text, self._kind = str(label), _readable(text), kind
        if not self._text or monitor is None:
            return
        self._monitor = monitor
        self._place(monitor)
        self._fit_glow()
        self._linger.stop()
        if seconds:
            self._linger.start(int(seconds * 1000))
        elif kind == "reply" and not self._busy:
            self._linger.start(int(backend.cfg_num("shade_seconds") * 1000))
        self.update()
        if not self.isVisible() or self._slide.endValue() == 0.0:
            self.show()
            self._animate_to(1.0)

    def _place(self, monitor: tuple) -> None:
        """Across the top of `monitor` (the window is fixed and see-through; only the glow inside it
        changes size). Positioned in physical pixels with Win32: Qt's own coordinates are unreliable
        across monitors with different scaling."""
        if monitor == self._placed and self.isVisible():
            return
        self._placed = monitor
        left, top, right, _bottom = monitor
        scale = next((m[4] for m in list_monitors() if tuple(m[:4]) == tuple(monitor)), 1.0) or 1.0
        width = (right - left) / scale
        self.resize(int(width), WINDOW_PX)
        user32.SetWindowPos(wintypes.HWND(int(self.winId())), wintypes.HWND(HWND_TOPMOST), left, top,
                            int(width * scale), int(WINDOW_PX * scale), SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _layout(self) -> tuple[QRectF, QRectF]:
        """Where the label and the text go: centred, wrapped to a comfortable width."""
        wrap = min(self.width() * 0.6, 980.0)
        flags = int(Qt.AlignHCenter | Qt.TextWordWrap)
        label = QFontMetricsF(self._font(LABEL_PX, 600)).boundingRect(QRectF(0, 0, wrap, 100), flags, self._label)
        text = QFontMetricsF(self._font(TEXT_PX, 700)).boundingRect(QRectF(0, 0, wrap, 10_000), flags, self._text)
        text.setHeight(min(text.height(), TEXT_PX * 1.35 * MAX_LINES))
        top = 18.0
        label_box = QRectF((self.width() - wrap) / 2, top, wrap, label.height() if self._label else 0)
        text_box = QRectF((self.width() - wrap) / 2, top + label_box.height() + (6 if self._label else 0),
                          wrap, text.height())
        return label_box, text_box

    def _fit_glow(self) -> None:
        """The glow follows what's said: a word gets a small one, a long answer a wide, deep one."""
        _label, text = self._layout()
        flags = int(Qt.AlignHCenter | Qt.TextWordWrap)
        used = QFontMetricsF(self._font(TEXT_PX, 700)).boundingRect(text, flags, self._text).width()
        self._glow_target = (max(360.0, used + 320.0), min(WINDOW_PX - 10.0, text.bottom() * 1.15 + 70.0))
        if not animations_enabled() or not self.isVisible() or self._reveal <= 0.0:
            self._glow = self._glow_target
        elif not self._grow.isActive():
            self._grow.start()

    def _grow_step(self) -> None:
        (w, h), (tw, th) = self._glow, self._glow_target
        w, h = w + (tw - w) * GROW_EASE, h + (th - h) * GROW_EASE
        if abs(tw - w) < 1 and abs(th - h) < 1:
            w, h = tw, th
            self._grow.stop()
        self._glow = (w, h)
        self.update()

    def _on_level(self, level: float) -> None:
        self._level = self._level * 0.6 + max(0.0, min(1.0, float(level))) * 0.4
        if self.isVisible() and self._kind == "reply":
            self.update()

    def retract(self) -> None:
        self._linger.stop()
        if self.isVisible():
            self._animate_to(0.0)

    def _animate_to(self, target: float) -> None:
        self._slide.stop()
        if not animations_enabled():
            self._set_reveal(target)
            self._slid()
            return
        self._slide.setDuration(SLIDE_MS)
        self._slide.setStartValue(self._reveal)
        self._slide.setEndValue(target)
        self._slide.start()

    def _set_reveal(self, value) -> None:
        self._reveal = float(value)
        self.update()

    def _slid(self) -> None:
        if self._reveal <= 0.0:
            self.hide()
            self._kind = ""
            self._glow = (0.0, 0.0)

    # ---- painting --------------------------------------------------------------------------
    @staticmethod
    def _font(px: int, weight: int) -> QFont:
        font = QFont()
        font.setPixelSize(px)
        font.setWeight(QFont.Weight(weight))
        return font

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        reveal = self._reveal
        p.setOpacity(max(0.0, min(1.0, reveal * 1.2)))

        # A soft oval glow hanging from the middle of the top edge, breathing a little with my voice.
        pulse = 1.0 + 0.06 * self._level
        w, h = self._glow[0] * pulse, self._glow[1] * pulse * (0.4 + 0.6 * reveal)
        if w > 1 and h > 1:
            accent = QColor(COLORS["accent"])
            glow = QRadialGradient(0.0, 0.0, 1.0)
            for stop, alpha in ((0.0, GLOW_ALPHA + 50 * self._level), (0.45, GLOW_ALPHA * 0.55), (1.0, 0)):
                color = QColor(accent)
                color.setAlpha(int(max(0, min(255, alpha))))
                glow.setColorAt(stop, color)
            p.save()
            p.translate(self.width() / 2, 0)
            p.scale(w / 2, h)
            p.fillRect(QRectF(-1, 0, 2, 1), glow)
            p.restore()

        # My words in the app's own text color, with a soft shadow so they read over anything.
        p.translate(0, -(1.0 - reveal) * 24)
        ink = QColor(COLORS["text"])
        shade = QColor(0, 0, 0) if ink.lightness() > 110 else QColor(255, 255, 255)
        label_box, text_box = self._layout()
        # the same wrapping _layout measured; not clipped to the box (drawn lines can sit a little lower)
        flags = int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap | Qt.TextDontClip)
        for text, box, px, weight, alpha in ((self._label, label_box, LABEL_PX, 600, 190),
                                             (self._text, text_box, TEXT_PX, 700, 255)):
            if not text:
                continue
            p.setFont(self._font(px, weight))
            for dx, dy, strength in _SHADOW:
                shade.setAlpha(strength)
                p.setPen(shade)
                p.drawText(box.translated(dx, dy), flags, text)
            color = QColor(ink)
            color.setAlpha(alpha)
            p.setPen(color)
            p.drawText(box, flags, text)
        p.end()


# (dx, dy, alpha): a few offset copies make a soft shadow, a little heavier below the text
_SHADOW = [(dx, dy, 34) for dx, dy in ((-2, 1), (2, 1), (0, -1), (-1, 3), (1, 3))] + [(0, 2, 70)]
