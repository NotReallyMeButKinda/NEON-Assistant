"""Top-of-screen status bar.

Registers as a Windows AppBar (the same mechanism the taskbar uses), so the
desktop work area starts *below* the bar: maximized windows sit under it
instead of being covered, like the macOS menu bar.
"""

from __future__ import annotations

import atexit
import ctypes
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime

from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint, QPointF, QPropertyAnimation, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPolygonF
from PySide6.QtWidgets import (QAbstractButton, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSizePolicy, QWidget)

import assistant as backend
import calendar_feed
import neon_log
import osinfo
import sysinfo

import persona
import timers
import tts

from . import wayland_place
from .effects import EffectOverlay
from .motion import animations_enabled
from .theme import COLORS, FONT_FAMILY, STATE_COLORS, apply_theme, text_on
from .theme import signals as theme_signals
from .widgets import Spectrum, StateOrb, Waveform


def bar_height() -> int:
    """Configured height in device-independent pixels, kept in a sane range."""
    return max(30, min(80, int(backend.cfg_num("bar_height"))))


STATE_LABELS = {
    "idle": "Idle", "listening": "Listening", "thinking": "Thinking",
    "speaking": "Speaking", "error": "Error",
}

RAINBOW_SECONDS = 7.0       # how long "party mode" cycles the accent colour before restoring it
RAINBOW_STEP_MS = 60

# ---- AppBar plumbing (shell32) ---------------------------------------------------
ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS = 0, 1, 2, 3
ABE_TOP, ABE_BOTTOM = 1, 3
ABN_FULLSCREENAPP = 2
ABN_POSCHANGED = 1
HWND_TOPMOST, HWND_BOTTOM = -1, 1
SWP_NOACTIVATE = 0x0010
WM_APP_CALLBACK = 0x0400 + 0x2A  # WM_USER + n, private to this window


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_MONITORENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                      wintypes.LPARAM)


def list_monitors() -> list[tuple[int, int, int, int, float]]:
    """(left, top, right, bottom, scale) of every monitor in *physical* pixels, straight from Win32
    (Qt's logical coordinates are unreliable with mixed DPI): the primary first, then left to right.
    On Linux: Qt's screens in the compositor's layout (logical) pixels, with scale 1.0, since windows are
    placed through Qt (and Hyprland) there, not with SetWindowPos."""
    if not osinfo.IS_WINDOWS:
        return [m[:5] for m in linux_screens()]
    found: list[tuple[bool, int, int, int, int, float]] = []

    def visit(hmon, _hdc, _rect, _lparam) -> bool:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            scale = 1.0
            try:
                dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
                if ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                    scale = dpi_x.value / 96.0
            except Exception:  # noqa: BLE001
                pass
            r = info.rcMonitor
            found.append((bool(info.dwFlags & 1), r.left, r.top, r.right, r.bottom, scale))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(visit), 0)
    found.sort(key=lambda m: (not m[0], m[1], m[2]))
    return [(l, t, r, b, sc) for _primary, l, t, r, b, sc in found]


def linux_screens() -> list[tuple[int, int, int, int, float, str]]:
    """(left, top, right, bottom, 1.0, output name) of every screen, the primary first, then left to right."""
    primary = QGuiApplication.primaryScreen()
    screens = sorted(QGuiApplication.screens(), key=lambda s: (s is not primary, s.geometry().x(), s.geometry().y()))
    out = []
    for screen in screens:
        g = screen.geometry()
        out.append((g.x(), g.y(), g.x() + g.width(), g.y() + g.height(), 1.0, screen.name()))
    return out


BAR_TITLE = "NEON status bar"          # Hyprland's window rules find the bar by this exact title


def compute_rect(edge: int, monitor: tuple, height_px: int) -> tuple[int, int, int, int]:
    """The strip (left, top, right, bottom) a bar of `height_px` occupies along one edge of a monitor."""
    left, top, right, bottom = monitor[:4]
    if edge == ABE_BOTTOM:
        return left, bottom - height_px, right, bottom
    return left, top, right, top + height_px


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


def _appbar_call(message: int, data: APPBARDATA) -> int:
    return ctypes.windll.shell32.SHAppBarMessage(message, ctypes.byref(data))


class ElidedLabel(QLabel):
    """Single-line label that trims with an ellipsis instead of growing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        fm = QFontMetrics(self.font())
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self._full, Qt.ElideRight, self.width()))


IN_MS, OUT_MS = 280, 230          # caption roll-in / roll-out durations
STRIP_MARGIN, STRIP_TOP = 8, 3    # the notification card spans the bar, inset by this much
WEATHER_REFRESH_MS = 15 * 60 * 1000


class _Line(ElidedLabel):
    """An ElidedLabel that can fade (the caption ticker cross-fades two of them)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fx = QGraphicsOpacityEffect(self)
        self.fx.setOpacity(1.0)
        self.setGraphicsEffect(self.fx)


class CaptionTicker(QWidget):
    """One line of text that rolls: a new line rises in from below while the old one slides up and
    out (what you said, then the reply). Updates from the same speaker just change the text, so a
    reply that streams in isn't re-animated on every word."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self._live = _Line(self)
        self._ghost = _Line(self)
        self._ghost.hide()
        self._sender = ""
        self._group: QParallelAnimationGroup | None = None

    @property
    def text(self) -> str:
        return self._live._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        for line in (self._live, self._ghost):
            line.resize(self.size())
        if self._group is None:
            self._live.move(0, 0)

    def show_caption(self, sender: str, text: str, css: str, animate: bool = True) -> None:
        if not text:
            self.clear(animate)
            return
        if sender == self._sender and self._live._full:      # same speaker: just update the words
            self._live.setStyleSheet(css)
            self._live.set_full_text(text)
            return
        self._settle()
        h = max(self.height(), 12)
        had_text = bool(self._live._full)
        if had_text:                                          # the old line becomes the outgoing ghost
            self._hand_to_ghost()
        self._live.setStyleSheet(css)
        self._live.set_full_text(text)
        self._sender = sender
        if not animate:
            self._live.move(0, 0)
            self._live.fx.setOpacity(1.0)
            self._ghost.hide()
            return
        self._live.move(0, h)
        self._live.fx.setOpacity(0.0)
        group = QParallelAnimationGroup(self)
        group.addAnimation(self._animate(self._live, b"pos", QPoint(0, h), QPoint(0, 0), IN_MS, QEasingCurve.OutCubic))
        group.addAnimation(self._animate(self._live.fx, b"opacity", 0.0, 1.0, IN_MS, QEasingCurve.OutCubic))
        if had_text:
            group.addAnimation(self._animate(self._ghost, b"pos", self._ghost.pos(), QPoint(0, -h), OUT_MS,
                                             QEasingCurve.InCubic))
            group.addAnimation(self._animate(self._ghost.fx, b"opacity", self._ghost.fx.opacity(), 0.0, OUT_MS,
                                             QEasingCurve.InCubic))
        self._run(group)

    def clear(self, animate: bool = True) -> None:
        """Slide the current line out (leaving the ticker empty)."""
        if not self._live._full:
            return
        self._settle()
        self._hand_to_ghost()
        self._live.set_full_text("")
        self._sender = ""
        if not animate:
            self._ghost.hide()
            return
        h = max(self.height(), 12)
        group = QParallelAnimationGroup(self)
        group.addAnimation(self._animate(self._ghost, b"pos", self._ghost.pos(), QPoint(0, -h), OUT_MS, QEasingCurve.InCubic))
        group.addAnimation(self._animate(self._ghost.fx, b"opacity", 1.0, 0.0, OUT_MS, QEasingCurve.InCubic))
        self._run(group)

    # ---- internals ---------------------------------------------------------------
    def _hand_to_ghost(self) -> None:
        g, live = self._ghost, self._live
        g.set_full_text(live._full)
        g.setStyleSheet(live.styleSheet())
        g.move(live.pos())
        g.fx.setOpacity(live.fx.opacity())
        g.show()
        g.raise_()

    @staticmethod
    def _animate(target, prop: bytes, start, end, ms: int, curve) -> QPropertyAnimation:
        a = QPropertyAnimation(target, prop)
        a.setStartValue(start)
        a.setEndValue(end)
        a.setDuration(ms)
        a.setEasingCurve(curve)
        return a

    def _run(self, group: QParallelAnimationGroup) -> None:
        self._group = group
        group.finished.connect(self._settle)
        group.start()

    def _settle(self) -> None:
        """Snap any running animation to its end state (also called when one finishes)."""
        group, self._group = self._group, None
        if group is not None:
            group.stop()
            group.deleteLater()
        self._ghost.hide()
        self._ghost.set_full_text("")
        self._live.move(0, 0)
        self._live.fx.setOpacity(1.0 if self._live._full else 0.0)


class NotificationStrip(QWidget):
    """A Windows notification, shown over the caption: app, title and text, with Summarize and Open
    (the app that sent it) on the right, and a close button (an X) at the very end. It drops in from the top edge and
    leaves the same way."""

    summarize_clicked = Signal()
    dismiss_clicked = Signal()
    open_clicked = Signal(dict)

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("notifstrip")
        # A plain QWidget ignores a style sheet background unless asked; without this the card was
        # transparent and its text was drawn straight over the caption and widgets beneath it.
        self.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 8, 0)
        row.setSpacing(8)
        self.icon = QLabel("\U0001f514")
        self.app = QLabel()
        self.text = ElidedLabel()
        self.more = QLabel()
        self.summarize_btn = QPushButton("Summarize")
        self.dismiss_btn = QPushButton("✕")
        self.dismiss_btn.setObjectName("notifclose")
        self.dismiss_btn.setToolTip("Dismiss")
        self.dismiss_btn.setAccessibleName("Dismiss the notification")
        self.open_btn = QPushButton("Open")
        self.open_btn.setToolTip("Open the app this notification came from")
        self._note: dict = {}
        for b in (self.summarize_btn, self.dismiss_btn, self.open_btn):
            b.setFixedHeight(24)
            b.setCursor(Qt.PointingHandCursor)
        self.dismiss_btn.setFixedWidth(24)
        self.summarize_btn.clicked.connect(self.summarize_clicked)
        self.dismiss_btn.clicked.connect(self.dismiss_clicked)
        self.open_btn.clicked.connect(lambda: self.open_clicked.emit(dict(self._note)))
        row.addWidget(self.icon)
        row.addWidget(self.app)
        row.addWidget(self.text, 1)
        row.addWidget(self.more)
        row.addWidget(self.summarize_btn)
        row.addWidget(self.open_btn)
        row.addWidget(self.dismiss_btn)
        self.fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.fx)
        self._group: QParallelAnimationGroup | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_animated)
        self.hide()
        self.restyle()

    def restyle(self) -> None:
        self.setStyleSheet(
            f"QWidget#notifstrip {{ background: {COLORS['panel_alt']}; border: 1px solid {COLORS['accent']}; "
            f"border-radius: 8px; }}"
            f"QLabel {{ background: transparent; }}"
            f"QPushButton#notifclose {{ background: transparent; border: none; border-radius: 12px; padding: 0; "
            f"color: {COLORS['muted']}; font-size: 13px; }}"
            f"QPushButton#notifclose:hover {{ background: {COLORS['accent']}; color: {COLORS['on_accent']}; }}")
        self.app.setStyleSheet(f"color: {COLORS['accent_bright']}; font-weight: 700;")
        self.more.setStyleSheet(f"color: {COLORS['muted']};")
        self.text.setStyleSheet(f"color: {COLORS['text']};")

    def show_notification(self, n: dict, animate: bool = True) -> None:
        count = int(n.get("count", 1))
        self.app.setText(str(n.get("app", "")))
        body = str(n.get("body") or "")
        self.text.set_full_text(f"{n.get('title', '')}" + (f" - {body}" if body else ""))
        self.more.setText(f"+{count - 1} more" if count > 1 else "")
        buttons = str(n.get("ask", "speech")) != "none"
        self.summarize_btn.setVisible(buttons)            # the X is always there: it closes the card
        self._note = dict(n)
        self.open_btn.setVisible(bool(n.get("can_open")))       # a real notification, whatever the mode
        self._stop()
        self._place()
        self.show()
        self.raise_()
        seconds = float(n.get("seconds", 12) or 12)
        self._timer.start(int(max(2.0, seconds) * 1000))
        if not animate:
            self.fx.setOpacity(1.0)
            return
        h = max(self.parentWidget().height(), 12)
        self.fx.setOpacity(0.0)
        group = QParallelAnimationGroup(self)
        group.addAnimation(CaptionTicker._animate(self, b"pos", QPoint(STRIP_MARGIN, -h), QPoint(STRIP_MARGIN, STRIP_TOP),
                                                  IN_MS, QEasingCurve.OutCubic))
        group.addAnimation(CaptionTicker._animate(self.fx, b"opacity", 0.0, 1.0, IN_MS, QEasingCurve.OutCubic))
        self._group = group
        group.start()

    def hide_animated(self) -> None:
        self._timer.stop()
        if not self.isVisible():
            return
        self._stop()
        h = max(self.parentWidget().height(), 12)
        group = QParallelAnimationGroup(self)
        group.addAnimation(CaptionTicker._animate(self, b"pos", self.pos(), QPoint(STRIP_MARGIN, -h), OUT_MS,
                                                  QEasingCurve.InCubic))
        group.addAnimation(CaptionTicker._animate(self.fx, b"opacity", self.fx.opacity(), 0.0, OUT_MS, QEasingCurve.InCubic))
        group.finished.connect(self.hide)
        self._group = group
        group.start()

    def _stop(self) -> None:
        group, self._group = self._group, None
        if group is not None:
            group.stop()
            group.deleteLater()

    def _place(self) -> None:
        parent = self.parentWidget()
        self.setGeometry(STRIP_MARGIN, STRIP_TOP, max(60, parent.width() - 2 * STRIP_MARGIN),
                         max(20, parent.height() - 2 * STRIP_TOP - 2))

    def fit(self) -> None:
        """The bar was resized: keep spanning it (a running slide animation only needs the new size)."""
        if self.isVisible() and self._group is None:
            self._place()
        else:
            parent = self.parentWidget()
            self.resize(max(60, parent.width() - 2 * STRIP_MARGIN), max(20, parent.height() - 2 * STRIP_TOP - 2))


class ClickLabel(QLabel):
    """A label you can click (left) or right-click."""

    clicked = Signal()
    right_clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        elif event.button() == Qt.RightButton:
            self.right_clicked.emit()

    def contextMenuEvent(self, event) -> None:
        event.accept()              # its right-click is its own action, not the bar's menu

    def enterEvent(self, event) -> None:
        self.setCursor(Qt.PointingHandCursor)
        super().enterEvent(event)


class MediaButton(QAbstractButton):
    """A small flat transport button (previous / play / pause / next) drawn as vector shapes, so it
    stays crisp at any scale and takes the theme's colours."""

    SHAPES = ("previous", "play", "pause", "next")
    TIPS = {"previous": "Previous track", "play": "Play", "pause": "Pause", "next": "Next track"}

    def __init__(self, shape: str, parent=None):
        super().__init__(parent)
        self._shape = ""
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(24, 24)
        self.set_shape(shape)

    def set_shape(self, shape: str) -> None:
        if shape == self._shape:
            return
        self._shape = shape
        self.setToolTip(self.TIPS[shape])
        self.setAccessibleName(self.TIPS[shape])
        self.update()

    @property
    def shape(self) -> str:
        return self._shape

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover or self.isDown():
            back = QColor(COLORS["accent"])
            back.setAlpha(90 if self.isDown() else 45)
            p.setPen(Qt.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        colour = QColor(COLORS["accent_bright"] if self._hover else COLORS["text"])
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        s = min(self.width(), self.height())
        cx, cy, h = self.width() / 2, self.height() / 2, s * 0.42     # h = glyph height
        if self._shape == "pause":
            w = h * 0.3
            p.drawRoundedRect(QRectF(cx - w * 1.4, cy - h / 2, w, h), 1, 1)
            p.drawRoundedRect(QRectF(cx + w * 0.4, cy - h / 2, w, h), 1, 1)
            return
        tri = h * 0.85
        if self._shape == "play":
            p.drawPolygon(QPolygonF([QPointF(cx - tri * 0.4, cy - h / 2), QPointF(cx - tri * 0.4, cy + h / 2),
                                     QPointF(cx + tri * 0.6, cy)]))
            return
        bar = h * 0.18
        forward = self._shape == "next"
        sign = 1 if forward else -1
        tip = cx + sign * tri * 0.45                 # the triangle points at the bar
        base = tip - sign * tri
        p.drawPolygon(QPolygonF([QPointF(base, cy - h / 2), QPointF(base, cy + h / 2), QPointF(tip, cy)]))
        x = tip if forward else tip - bar
        p.drawRect(QRectF(x, cy - h / 2, bar, h))


class NowPlaying(QWidget):
    """'♪ Artist - Title' with a thin progress line; click to play / pause."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._fraction = 0.0
        self._paused = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(80)
        self.setMaximumWidth(280)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set_song(self, text: str, fraction: float, paused: bool) -> None:
        self._text, self._fraction, self._paused = text, max(0.0, min(1.0, fraction)), paused
        self.setToolTip(f"{text}\nClick to {'play' if paused else 'pause'}")
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(min(280, QFontMetrics(self.font()).horizontalAdvance(self._text) + 8), 24)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        color = QColor(COLORS["muted"] if self._paused else COLORS["accent2"])
        p.setPen(color)
        p.setFont(self.font())
        text = QFontMetrics(self.font()).elidedText(self._text, Qt.ElideRight, self.width() - 4)
        p.drawText(self.rect().adjusted(0, 0, 0, -4), Qt.AlignVCenter | Qt.AlignLeft, text)
        track = QColor(COLORS["muted"])
        track.setAlpha(70)
        p.fillRect(0, self.height() - 3, self.width(), 2, track)
        p.fillRect(0, self.height() - 3, int(self.width() * self._fraction), 2, color)


class CaptionArea(QWidget):
    """The stretchy middle of the bar: the caption ticker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.ticker = CaptionTicker(self)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.ticker.setGeometry(0, 0, self.width(), self.height())


class StatusBar(QWidget):
    toggle_main_requested = Signal()
    settings_requested = Signal()
    weather_ready = Signal(object)          # dict from backend.weather_snapshot() or None (worker thread -> UI)
    music_ready = Signal(object)            # the song dict from Pear Desktop, or None
    calendar_ready = Signal(object)         # (Event | None, [Event]) or None if the feed failed

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._registered = False
        self._state = "idle"

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(BAR_TITLE)
        self._reserved_on = ""                          # Linux (Hyprland): the output whose edge is reserved
        self.setMinimumHeight(0)
        self.setObjectName("statusbar")
        self._caption_sender = "You"
        self._weather: dict | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 12, 2)
        row.setSpacing(10)

        self.orb = StateOrb(28)
        self.orb.setAccessibleName("Assistant state")
        row.addWidget(self.orb)
        self.state_label = QLabel(STATE_LABELS["idle"])      # its width follows the text size
        row.addWidget(self.state_label)
        self.wave = Waveform(7)
        row.addWidget(self.wave)
        self.spectrum = Spectrum(tts.SPECTRUM_BANDS)      # the real FFT of the voice, when enabled
        row.addWidget(self.spectrum)

        self.area = CaptionArea()
        row.addWidget(self.area, 1)
        self.strip = NotificationStrip(self)        # child of the bar itself: it can use the bar's full width
        self.strip.summarize_clicked.connect(lambda: controller.answer_notification(True))
        self.strip.dismiss_clicked.connect(lambda: controller.answer_notification(False))
        self.strip.open_clicked.connect(lambda n: controller.open_notification_app(n))

        self.music = NowPlaying()
        self.music.clicked.connect(self._toggle_music)
        self.media_box = QWidget()                      # the transport buttons, then the song
        media_row = QHBoxLayout(self.media_box)
        media_row.setContentsMargins(0, 0, 0, 0)
        media_row.setSpacing(2)
        self.media_buttons = QWidget()
        buttons_row = QHBoxLayout(self.media_buttons)
        buttons_row.setContentsMargins(0, 0, 4, 0)
        buttons_row.setSpacing(0)
        self.prev_btn = MediaButton("previous")
        self.play_btn = MediaButton("pause")
        self.next_btn = MediaButton("next")
        self.prev_btn.clicked.connect(lambda: self._media("previous"))
        self.play_btn.clicked.connect(self._toggle_music)
        self.next_btn.clicked.connect(lambda: self._media("next"))
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            buttons_row.addWidget(b)
        media_row.addWidget(self.media_buttons)
        media_row.addWidget(self.music)
        self.media_box.setVisible(False)
        self.calendar_label = QLabel()
        self.sys_label = QLabel()
        self.persona_label = QLabel()
        self.persona_label.setToolTip("The persona I'm using (say \"act normal\" to drop it)")
        self.timer_label = ClickLabel("")
        self.timer_label.setToolTip("Time left on your nearest timer -- click for your timers, right-click to "
                                    "cancel them all")
        self.timer_label.clicked.connect(lambda: self.controller.show_timers())
        self.timer_label.right_clicked.connect(self._cancel_timers)
        self.stopwatch = ClickLabel("\u23f1 0:00")
        self.stopwatch.setToolTip("Click to start / pause, right-click to reset")
        self.stopwatch.clicked.connect(self._stopwatch_toggle)
        self.stopwatch.setAccessibleName("Stopwatch")
        self.timer_label.setAccessibleName("Nearest timer")
        self.music.setAccessibleName("Now playing")
        self.stopwatch.right_clicked.connect(self._stopwatch_reset)
        self.weather_label = QLabel()
        self.clock_label = QLabel()
        for w in (self.media_box, self.calendar_label, self.sys_label, self.persona_label, self.timer_label,
                  self.stopwatch, self.weather_label, self.clock_label):
            row.addWidget(w)

        self._buttons: dict[str, QPushButton] = {}

        def button(key: str, text: str, tip: str, checkable: bool = False) -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAccessibleName(tip)                     # screen readers: the gear has no words of its own
            b.setCheckable(checkable)
            b.setFixedHeight(26)
            b.setCursor(Qt.PointingHandCursor)
            row.addWidget(b)
            self._buttons[key] = b
            return b

        talk = button("bar_show_talk", "\U0001f3a4 Talk", "Push to talk")
        talk.clicked.connect(controller.push_to_talk)
        stop = button("bar_show_stop", "\u25a0 Stop", "Stop speaking")
        stop.clicked.connect(controller.stop_speaking)
        self.wake_btn = button("bar_show_wake", "Wake", "Toggle wake word", checkable=True)
        self.wake_btn.toggled.connect(controller.set_wake)
        self.mute_btn = button("bar_show_mute", "Mute", "Mute the microphone (wake word included)", checkable=True)
        self.mute_btn.toggled.connect(controller.set_muted)
        window = button("bar_show_window", "Window", "Show / hide the main window")
        window.clicked.connect(self.toggle_main_requested)
        gear = button("bar_show_settings", "\u2699", "Settings")
        gear.setFixedWidth(34)
        gear.clicked.connect(self.settings_requested)

        self._fade = QTimer(self)
        self._fade.setSingleShot(True)
        self._fade.timeout.connect(lambda: self.area.ticker.clear(self._animated()))

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._weather_timer = QTimer(self)
        self._weather_timer.timeout.connect(self._refresh_weather)
        self.weather_ready.connect(self._show_weather)

        self._cpu = sysinfo.CpuMeter()
        self._sys_timer = QTimer(self)
        self._sys_timer.timeout.connect(self._tick_sys)
        self._music_timer = QTimer(self)                 # polls Pear Desktop
        self._music_timer.timeout.connect(self._refresh_music)
        self._music_tick = QTimer(self)                  # smooth progress between polls
        self._music_tick.timeout.connect(self._advance_music)
        self._song: dict | None = None
        self._song_at = 0.0
        self.music_ready.connect(self._show_music)
        self._cal_timer = QTimer(self)                   # re-reads the calendar feed
        self._cal_timer.timeout.connect(self._refresh_calendar)
        self._cal_text_timer = QTimer(self)              # "in 25 min" counts down without a fetch
        self._cal_text_timer.timeout.connect(self._redescribe_calendar)
        self._cal_event = None
        self._cal_upcoming: list = []
        self.calendar_ready.connect(self._show_calendar)
        self._timer_tick = QTimer(self)                  # counts the nearest running timer down
        self._timer_tick.timeout.connect(self._tick_timer)
        self._sw_timer = QTimer(self)
        self._sw_timer.timeout.connect(self._stopwatch_tick)
        self._sw_running = False
        self._sw_started = 0.0
        self._sw_elapsed = 0.0
        self._layout_key = self._bar_layout_key()

        self.effects = EffectOverlay(self)               # "do a barrel roll" and friends
        self._rainbow_timer = QTimer(self)
        self._rainbow_timer.timeout.connect(self._rainbow_step)
        self._rainbow_frames = 0

        controller.state.connect(self._on_state)
        controller.audio_level.connect(self._on_level)
        controller.spectrum.connect(self.spectrum.set_levels)
        controller.effect.connect(self.play_effect)
        controller.persona_changed.connect(lambda _key: self._apply_extras())
        controller.caption.connect(self._on_caption)
        controller.wake_changed.connect(self._on_wake_changed)
        controller.settings_saved.connect(self.apply_config)
        controller.live_changed.connect(lambda _keys: self.apply_config())   # instant Settings pages
        controller.muted_changed.connect(self._on_muted)
        controller.notification.connect(self._on_notification)
        controller.notification_settled.connect(self.strip.hide_animated)
        self._muted = False
        theme_signals.changed.connect(self._apply_styles)
        self._apply_elements()
        self._apply_styles()

    def _animated(self) -> bool:
        return animations_enabled()

    @staticmethod
    def _bar_layout_key() -> tuple:
        return (str(backend.CONFIG.get("bar_position", "top")), int(backend.cfg_num("bar_monitor")))

    def _text_size(self) -> int:
        return max(10, min(20, int(backend.cfg_num("bar_text_size"))))

    # ---- what is shown -------------------------------------------------------------
    def _apply_elements(self) -> None:
        """Show / hide each part of the bar per the settings (Settings > Status bar)."""
        on = backend.cfg_bool
        self.orb.setVisible(on("bar_show_orb"))
        self.state_label.setVisible(on("bar_show_state"))
        self.wave.setVisible(on("bar_show_wave"))
        for key, b in self._buttons.items():
            b.setVisible(on(key))
        self.area.ticker.setVisible(on("bar_show_caption"))
        self.area.setVisible(on("bar_show_caption"))
        self.clock_label.setVisible(on("bar_show_clock"))
        self.weather_label.setVisible(on("bar_show_weather"))
        self.stopwatch.setVisible(on("bar_show_stopwatch"))
        self.spectrum.setVisible(on("bar_show_spectrum"))
        self._apply_extras()

        if on("bar_show_clock"):
            self._tick_clock()
            self._clock_timer.start(1000)
        else:
            self._clock_timer.stop()
        if on("bar_show_weather"):
            if not self._weather_timer.isActive():
                self._weather_timer.start(WEATHER_REFRESH_MS)
            self._refresh_weather()
        else:
            self._weather_timer.stop()

    def _apply_extras(self) -> None:
        """Start / stop the pollers behind the optional bar elements."""
        on = backend.cfg_bool
        if on("bar_show_timer"):
            self._tick_timer()
            self._timer_tick.start(500)
        else:
            self._timer_tick.stop()
            self.timer_label.setVisible(False)

        current = persona.valid(backend.CONFIG.get("persona"))
        show_persona = on("bar_show_persona") and current != persona.DEFAULT
        self.persona_label.setVisible(show_persona)
        if show_persona:
            self.persona_label.setText(f"\U0001f3ad {persona.label(current)}")

        sys_on = on("bar_show_cpu") or on("bar_show_ram") or on("bar_show_battery")
        self.sys_label.setVisible(sys_on)
        if sys_on:
            self._tick_sys()
            self._sys_timer.start(2000)
        else:
            self._sys_timer.stop()

        music_on = on("bar_show_music") and (on("ytm_enabled") or on("media_any_player"))
        if music_on:
            self._music_timer.start(3000)
            self._music_tick.start(1000)
            self._refresh_music()
        else:
            self._music_timer.stop()
            self._music_tick.stop()
            self.media_box.setVisible(False)
        self.media_buttons.setVisible(on("bar_show_media_buttons"))

        source = str(backend.CONFIG.get("calendar_ics", "")).strip()
        cal_on = on("bar_show_calendar") and bool(source)
        if cal_on:
            self._cal_timer.start(5 * 60 * 1000)
            self._cal_text_timer.start(30 * 1000)
            self._refresh_calendar()
        else:
            self._cal_timer.stop()
            self._cal_text_timer.stop()
            self.calendar_label.setVisible(False)

    # ---- system meters ------------------------------------------------------------------
    def _tick_sys(self) -> None:
        on = backend.cfg_bool
        parts = []
        if on("bar_show_cpu"):
            parts.append(f"CPU {self._cpu.percent()}%")
        if on("bar_show_ram"):
            parts.append(f"RAM {sysinfo.ram_percent()}%")
        if on("bar_show_battery"):
            level = sysinfo.battery()
            if level is not None:
                parts.append(f"{'\u26a1' if level[1] else '\U0001f50b'} {level[0]}%")
        self.sys_label.setText("   ".join(parts))
        self.sys_label.setVisible(bool(parts))

    # ---- stopwatch -------------------------------------------------------------------------
    # ---- timer countdown ---------------------------------------------------------------
    def _tick_timer(self) -> None:
        """The nearest running timer, counted down. Hidden entirely when none is running, so the
        bar doesn't carry a dead element around."""
        item = timers.next_timer()
        if item is None:
            self.timer_label.setVisible(False)
            return
        left = int(item["remaining"] + 0.5)
        hours, rest = divmod(left, 3600)
        minutes, seconds = divmod(rest, 60)
        clock = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        label = item["label"].strip()
        icon = "\u23f3" if item["reminder"] else "\u23f2"
        self.timer_label.setText(f"{icon} {clock}" + (f" {label}" if label and len(label) <= 14 else ""))
        # Under a minute it turns the "error" colour, so a timer about to go off is obvious.
        colour = STATE_COLORS["error"] if left <= 60 else COLORS["muted"]
        self.timer_label.setStyleSheet(f"color: {colour}; font-size: {self._text_size()}px; font-weight: 600;")
        self.timer_label.setVisible(True)

    def _cancel_timers(self) -> None:
        timers.cancel_timers()
        self._tick_timer()

    # ---- effects ("do a barrel roll") ---------------------------------------------------
    def play_effect(self, name: str) -> None:
        """Perform one of fun.EFFECTS. Anything unknown is ignored rather than raising: the effect
        names come from the backend and this is the last thing that should break the bar."""
        if not self.isVisible():
            return
        if name == "rainbow":
            self._start_rainbow()
            return
        if name == "matrix":
            self._flash_theme("matrix", 4.0)
        self.effects.play(name)

    def _start_rainbow(self) -> None:
        if self._rainbow_timer.isActive():
            return
        self._rainbow_frames = 0
        self._rainbow_timer.start(RAINBOW_STEP_MS)

    def _rainbow_step(self) -> None:
        total = max(1, int(RAINBOW_SECONDS * 1000 / RAINBOW_STEP_MS))
        self._rainbow_frames += 1
        if self._rainbow_frames > total:
            self._rainbow_timer.stop()
            self._restore_theme()
            return
        hue = (self._rainbow_frames * 7) % 360
        colour = QColor.fromHsv(hue, 235, 255)
        apply_theme(str(backend.CONFIG.get("theme", "neon")), colour.name(),
                    backend.CONFIG.get("state_colors"))

    def _flash_theme(self, theme: str, seconds: float) -> None:
        """Wear another theme for a moment, then put the user's own back."""
        apply_theme(theme, "", backend.CONFIG.get("state_colors"))
        QTimer.singleShot(int(seconds * 1000), self._restore_theme)

    def _restore_theme(self) -> None:
        apply_theme(str(backend.CONFIG.get("theme", "neon")), str(backend.CONFIG.get("theme_accent", "")),
                    backend.CONFIG.get("state_colors"))

    def _stopwatch_text(self) -> str:
        seconds = int(self._sw_elapsed + (time.monotonic() - self._sw_started if self._sw_running else 0.0))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        return f"\u23f1 {hours}:{minutes:02}:{secs:02}" if hours else f"\u23f1 {minutes}:{secs:02}"

    def _stopwatch_toggle(self) -> None:
        if self._sw_running:
            self._sw_elapsed += time.monotonic() - self._sw_started
            self._sw_running = False
            self._sw_timer.stop()
        else:
            self._sw_started = time.monotonic()
            self._sw_running = True
            self._sw_timer.start(250)
        self._stopwatch_tick()

    def _stopwatch_reset(self) -> None:
        self._sw_running = False
        self._sw_elapsed = 0.0
        self._sw_timer.stop()
        self._stopwatch_tick()

    def _stopwatch_tick(self) -> None:
        self.stopwatch.setText(self._stopwatch_text())

    # ---- now playing (Pear Desktop, else any player Windows knows about) ----------------------------
    def _refresh_music(self) -> None:
        def work() -> None:
            song = None
            try:
                result = backend.YTM.run("song") if backend.YTM.enabled else None
                song = result if isinstance(result, dict) else None
                if song is None and backend.cfg_bool("media_any_player"):
                    backend.MEDIA.start()                # reports within a second; the next poll shows it
                    song = backend.MEDIA.song()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.music_ready.emit(song)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _show_music(self, song) -> None:
        self._song, self._song_at = song, time.monotonic()
        self._advance_music()

    def _advance_music(self) -> None:
        song = self._song
        if not song or not backend.cfg_bool("bar_show_music"):
            self.media_box.setVisible(False)
            return
        duration = float(song.get("songDuration") or 0)
        elapsed = float(song.get("elapsedSeconds") or 0)
        paused = bool(song.get("isPaused"))
        if not paused:
            elapsed += time.monotonic() - self._song_at
        artist, title = song.get("artist") or "", song.get("title") or ""
        self.music.set_song(f"\u266a {artist} - {title}" if artist else f"\u266a {title}",
                            elapsed / duration if duration else 0.0, paused)
        self.play_btn.set_shape("play" if paused else "pause")
        self.media_box.setVisible(True)

    def _toggle_music(self) -> None:
        if self._song is not None:                   # flip the icon now; the next poll confirms it
            self._song = {**self._song, "isPaused": not self._song.get("isPaused"),
                          "elapsedSeconds": self._elapsed_now()}
            self._song_at = time.monotonic()
            self._advance_music()
        self._media("playpause")

    def _elapsed_now(self) -> float:
        song = self._song or {}
        elapsed = float(song.get("elapsedSeconds") or 0)
        return elapsed if song.get("isPaused") else elapsed + time.monotonic() - self._song_at

    def _media(self, action: str) -> None:
        """previous / playpause / next: Pear Desktop when it answers, else the player Windows reports,
        else the Windows media keys."""
        def work() -> None:
            backend.media_control(action)
            time.sleep(0.5)
            self._refresh_music()
        threading.Thread(target=work, daemon=True).start()

    # ---- calendar --------------------------------------------------------------------------------
    def _refresh_calendar(self) -> None:
        source = str(backend.CONFIG.get("calendar_ics", "")).strip()

        def work() -> None:
            try:
                result = calendar_feed.next_event(source)
            except Exception as exc:  # noqa: BLE001 -- bad link, offline, unreadable file
                neon_log.get("statusbar").warning("calendar feed failed: %s", exc)
                result = None
            try:
                self.calendar_ready.emit(result)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _show_calendar(self, result) -> None:
        if result is not None:                 # keep the last good reading if a refresh fails
            self._cal_event, self._cal_upcoming = result
        self._redescribe_calendar()

    def _redescribe_calendar(self) -> None:
        event = self._cal_event
        if event is None or not backend.cfg_bool("bar_show_calendar"):
            self.calendar_label.setVisible(False)
            return
        if event.end < datetime.now().astimezone() and not event.all_day:       # it's over: show the next one
            later = [e for e in self._cal_upcoming if e.end >= datetime.now().astimezone()]
            event = later[0] if later else None
            self._cal_event = event
            if event is None:
                self.calendar_label.setVisible(False)
                return
        twenty_four = str(backend.CONFIG.get("time_format", "12h")) == "24h"
        self.calendar_label.setText("\U0001f4c5 " + calendar_feed.describe(event, use_24h=twenty_four))
        self.calendar_label.setToolTip("\n".join(calendar_feed.describe(e, use_24h=twenty_four) for e in self._cal_upcoming))
        self.calendar_label.setVisible(True)

    def _clock_text(self) -> str:
        fmt = str(backend.CONFIG.get("bar_clock_format", "auto")).strip().lower()
        if fmt == "auto":
            fmt = str(backend.CONFIG.get("time_format", "12h")).strip().lower()
        twenty_four = fmt == "24h"
        seconds = backend.cfg_bool("bar_clock_seconds")
        now = datetime.now()
        pattern = ("%H:%M" if twenty_four else "%I:%M") + (":%S" if seconds else "") + ("" if twenty_four else " %p")
        text = now.strftime(pattern)
        if not twenty_four:
            text = text.lstrip("0")
        if backend.cfg_bool("bar_clock_date"):
            text = f"{now:%a %b} {now.day}   {text}"
        return text

    def _tick_clock(self) -> None:
        text = self._clock_text()
        if text != self.clock_label.text():
            self.clock_label.setText(text)

    def _refresh_weather(self) -> None:
        def work() -> None:
            try:
                data = backend.weather_snapshot()
            except Exception:  # noqa: BLE001 -- network hiccup: keep showing the last reading
                data = None
            try:
                self.weather_ready.emit(data)
            except RuntimeError:
                pass                     # the bar was destroyed while fetching
        threading.Thread(target=work, daemon=True).start()

    def _show_weather(self, data) -> None:
        if data is not None:
            self._weather = data
        w = self._weather
        if not w:
            self.weather_label.setText("")
            return
        text = f"{backend.weather_icon(w['code'], w['is_day'])} {round(w['temp'])}{w['symbol']}"
        if backend.cfg_bool("bar_weather_text"):
            text += f"  {str(w['desc']).capitalize()}"
        self.weather_label.setText(text)
        self.weather_label.setToolTip(f"{w['city']}: {w['desc']}, wind {w['wind']} km/h")

    def _apply_styles(self) -> None:
        """Colors baked into stylesheets; re-run on every theme change."""
        size = self._text_size()
        self.setStyleSheet(f"""
            QWidget#statusbar {{ background: {COLORS['bg']}; }}
            QPushButton {{ padding: 2px 10px; border-radius: 8px; font-size: 12px; }}
            QPushButton:checked {{ background: {COLORS['accent']}; color: {COLORS['on_accent']}; border: none; }}
        """)
        self.mute_btn.setStyleSheet(
            f"QPushButton:checked {{ background: {STATE_COLORS['muted']}; "
            f"color: {text_on(STATE_COLORS['muted'])}; border: none; }}")
        self.clock_label.setStyleSheet(f"color: {COLORS['text']}; font-size: {size}px; font-weight: 600;")
        for label in (self.calendar_label, self.sys_label, self.stopwatch):
            label.setStyleSheet(f"color: {COLORS['muted']}; font-size: {size}px;")
        self.persona_label.setStyleSheet(f"color: {COLORS['accent2']}; font-size: {size}px; font-weight: 600;")
        self._tick_timer()          # re-colours the countdown for the new palette
        font = self.music.font()
        font.setPixelSize(size)
        self.music.setFont(font)
        self.weather_label.setStyleSheet(f"color: {COLORS['text']}; font-size: {size}px;")
        self.strip.restyle()
        self._show_state_label()
        self._apply_caption_color(self._caption_sender)
        self.orb.update()   # state colors can change without the stylesheet text changing
        self.update()

    # ---- slots -----------------------------------------------------------------
    def _show_state_label(self) -> None:
        if self._muted and self._state in ("idle", "listening", "error"):
            text, color = "Muted", STATE_COLORS["muted"]
        else:
            text, color = STATE_LABELS.get(self._state, self._state), STATE_COLORS.get(self._state, COLORS["muted"])
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color}; font-weight: 600; font-size: {self._text_size()}px;")
        # As wide as the longest state at this text size (a fixed 78 px cut "Listening" off at 20 px),
        # and fixed, so the rest of the bar doesn't shift each time the state changes.
        font = QFont(FONT_FAMILY)
        font.setPixelSize(self._text_size())
        font.setWeight(QFont.DemiBold)
        metrics = QFontMetrics(font)
        self.state_label.setFixedWidth(max(metrics.horizontalAdvance(t) for t in (*STATE_LABELS.values(), "Muted")) + 6)

    # ---- right-click menu: what the bar shows ------------------------------------------------
    MENU_ITEMS = [("bar_show_orb", "Status orb"), ("bar_show_state", "State text"),
                  ("bar_show_wave", "Speaking waveform"), ("bar_show_spectrum", "Voice spectrum"),
                  ("bar_show_caption", "What you said and my reply"), None,
                  ("bar_show_music", "Now playing"), ("bar_show_media_buttons", "Media buttons"),
                  ("bar_show_calendar", "Next calendar event"), ("bar_show_cpu", "CPU load"),
                  ("bar_show_ram", "Memory use"), ("bar_show_battery", "Battery"),
                  ("bar_show_stopwatch", "Stopwatch"), ("bar_show_timer", "Timer countdown"),
                  ("bar_show_persona", "Persona badge"), ("bar_show_weather", "Weather"),
                  ("bar_show_clock", "Clock")]
    MENU_BUTTONS = [("bar_show_talk", "Talk"), ("bar_show_stop", "Stop"), ("bar_show_wake", "Wake word toggle"),
                    ("bar_show_mute", "Mute"), ("bar_show_window", "Window"), ("bar_show_settings", "Settings (gear)")]

    def build_menu(self) -> QMenu:
        """Every bar element as a tick box: switching one here is the same as in Settings > Status bar."""
        menu = QMenu(self)
        title = menu.addAction("Show on the bar")
        title.setEnabled(False)

        def add(target: QMenu, key: str, label: str) -> None:
            action = target.addAction(label)
            action.setCheckable(True)
            action.setChecked(backend.cfg_bool(key))
            action.toggled.connect(lambda on, k=key: self.set_element(k, on))

        for item in self.MENU_ITEMS:
            if item is None:
                menu.addSeparator()
            else:
                add(menu, *item)
        buttons = menu.addMenu("Buttons")
        for key, label in self.MENU_BUTTONS:
            add(buttons, key, label)
        menu.addSeparator()
        menu.addAction("Board...").triggered.connect(self.controller.show_board)
        menu.addAction("Timers...").triggered.connect(self.controller.show_timers)
        menu.addAction("Recent notifications...").triggered.connect(self.controller.show_notification_history)
        menu.addAction("Status bar settings...").triggered.connect(self.settings_requested)
        return menu

    def set_element(self, key: str, on: bool) -> None:
        backend.persist_keys({key: bool(on)})
        self.controller.apply_live([key])

    def contextMenuEvent(self, event) -> None:
        self.build_menu().exec(event.globalPos())

    def _on_muted(self, on: bool) -> None:
        self._muted = on
        self.orb.set_muted(on)
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(on)
        self.mute_btn.blockSignals(False)
        self._show_state_label()

    def _on_state(self, state: str) -> None:
        self._state = state
        self.orb.set_state(state)
        self._show_state_label()
        self.wave.set_active(state == "speaking", COLORS["accent2"])
        self.spectrum.set_active(state == "speaking")
        if state == "idle":
            self._fade.start(int(backend.cfg_num("bar_caption_seconds") * 1000))
        else:
            self._fade.stop()
        self.update()

    def _on_level(self, level: float) -> None:
        self.orb.set_level(level)
        self.wave.set_level(level)

    def _caption_css(self, sender: str) -> str:
        color = COLORS["accent2"] if sender == "You" else COLORS["accent_bright"]
        return f"font-size: {self._text_size()}px; color: {color};"

    def _apply_caption_color(self, sender: str) -> None:
        self._caption_sender = sender
        ticker = self.area.ticker
        if ticker.text:
            ticker._live.setStyleSheet(self._caption_css(sender))

    def _on_caption(self, sender: str, text: str) -> None:
        """What you said appears (rolling in); when the reply starts it rolls out and the reply
        rolls in. The same speaker's later updates (a streaming reply) just extend the text."""
        self._caption_sender = sender
        self._fade.stop()
        self.area.ticker.show_caption(sender, f"{sender}: {text}", self._caption_css(sender), self._animated())

    def _on_notification(self, n: dict) -> None:
        self.strip.show_notification(n, self._animated())

    def _on_wake_changed(self, on: bool) -> None:
        self.wake_btn.blockSignals(True)
        self.wake_btn.setChecked(on)
        self.wake_btn.blockSignals(False)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        color = QColor(STATE_COLORS.get(self._state, COLORS["accent"]))
        color.setAlpha(200)
        edge_y = 0 if str(backend.CONFIG.get("bar_position", "top")) == "bottom" else self.height() - 2
        p.fillRect(0, edge_y, self.width(), 2, color)  # state-tinted edge, on the side facing the screen

    # ---- AppBar ----------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32" and not self._registered:
            QTimer.singleShot(0, self._register_appbar)
        elif sys.platform != "win32":
            QTimer.singleShot(0, self._place_linux)

    def linux_rect(self) -> tuple[int, int, int, int]:
        """(x, y, width, height) of the bar on Linux, in layout pixels."""
        left, top, right, bottom = compute_rect(self._edge(), self._target_monitor(), bar_height())
        return left, top, right - left, bottom - top

    def prepare_linux(self) -> None:
        """Before the bar first opens on Linux: the compositor's rules for it (see ui/wayland_place.py): floating
        on every workspace, above everything, never focused, no border; on KDE also its exact place."""
        if sys.platform == "win32":
            return
        wayland_place.prepare(self, BAR_TITLE, focus=False)
        self.setGeometry(*self.linux_rect())
        wayland_place.settle(self, fixed_size=True)

    def relayout_linux(self) -> None:
        """Hyprland reloaded its config (dropping the reserved strip): put the bar back and reserve it again."""
        if sys.platform != "win32" and self.isVisible():
            self._reserved_on = ""
            self._place_linux()

    def _place_linux(self) -> None:
        """Put the bar along its edge and, on Hyprland, keep that strip free of tiled windows."""
        x, y, width, height = self.linux_rect()
        self.setGeometry(x, y, width, height)
        wayland_place.settle(self, fixed_size=True)
        from linuxdesk import hypr
        if not hypr.available():
            return
        name = hypr.monitor_at(x + width // 2, y + height // 2)
        if self._reserved_on and self._reserved_on != name:
            hypr.reserve(self._reserved_on)
        if name:
            bottom = self._edge() == ABE_BOTTOM
            hypr.reserve(name, top=0 if bottom else height, bottom=height if bottom else 0)
            self._reserved_on = name
        self._layout_key = self._bar_layout_key()

    def _target_monitor(self) -> tuple:
        """(left, top, right, bottom, scale) in physical pixels of the monitor the bar lives on
        (`bar_monitor`: 0 = primary, 1 = the next one, ...); falls back to the primary if it's gone."""
        monitors = list_monitors()
        if not monitors:
            geo = QGuiApplication.primaryScreen().geometry()
            return geo.x(), geo.y(), geo.right() + 1, geo.bottom() + 1, QGuiApplication.primaryScreen().devicePixelRatio()
        index = int(backend.cfg_num("bar_monitor"))
        return monitors[index if 0 <= index < len(monitors) else 0]

    @staticmethod
    def _edge() -> int:
        return ABE_BOTTOM if str(backend.CONFIG.get("bar_position", "top")) == "bottom" else ABE_TOP

    def _register_appbar(self) -> None:
        try:
            data = APPBARDATA()
            data.cbSize = ctypes.sizeof(APPBARDATA)
            data.hWnd = int(self.winId())
            data.uCallbackMessage = WM_APP_CALLBACK
            if not _appbar_call(ABM_NEW, data):
                raise OSError("ABM_NEW failed")
            self._registered = True
            atexit.register(self.unregister_appbar)  # never leave a stale reserved strip behind
            self._position_appbar()
            QTimer.singleShot(400, self._enforce_rect)  # after Qt has settled its own moves
        except Exception as exc:  # noqa: BLE001
            self._registered = False
            print(f"[status bar] couldn't reserve screen space ({exc}); running as a plain overlay.")
            geo = QGuiApplication.primaryScreen().geometry()
            y = geo.bottom() + 1 - bar_height() if self._edge() == ABE_BOTTOM else geo.y()
            self.setGeometry(geo.x(), y, geo.width(), bar_height())

    def _position_appbar(self) -> None:
        monitor = self._target_monitor()
        height = round(bar_height() * monitor[4])
        edge = self._edge()

        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        data.hWnd = int(self.winId())
        data.uEdge = edge
        data.rc.left, data.rc.top, data.rc.right, data.rc.bottom = compute_rect(edge, monitor, height)
        _appbar_call(ABM_QUERYPOS, data)  # let the shell adjust for other appbars (e.g. the taskbar)
        if edge == ABE_BOTTOM:            # keep our height, anchored to whatever edge the shell allowed
            data.rc.top = data.rc.bottom - height
        else:
            data.rc.bottom = data.rc.top + height
        _appbar_call(ABM_SETPOS, data)
        self._target = (data.rc.left, data.rc.top,
                        data.rc.right - data.rc.left, data.rc.bottom - data.rc.top)
        self._layout_key = self._bar_layout_key()
        self._enforce_rect()

    def _enforce_rect(self) -> None:
        """Force the window onto exactly the rectangle reserved from the shell."""
        target = getattr(self, "_target", None)
        if not self._registered or target is None:
            return
        hwnd = wintypes.HWND(int(self.winId()))
        cur = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(cur))
        if (cur.left, cur.top, cur.right - cur.left, cur.bottom - cur.top) != target:
            ctypes.windll.user32.SetWindowPos(
                hwnd, wintypes.HWND(HWND_TOPMOST), *target, SWP_NOACTIVATE)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if self._registered:
            QTimer.singleShot(0, self._enforce_rect)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.strip.fit()
        self.effects.setGeometry(self.rect())
        if self._registered:
            QTimer.singleShot(0, self._enforce_rect)

    def apply_config(self) -> None:
        """Re-read show/height/element settings after the Settings dialog saved."""
        self._apply_elements()
        self._apply_styles()
        if self._registered and self._layout_key != self._bar_layout_key():
            self.unregister_appbar()                    # release the strip on the old edge / monitor first
            QTimer.singleShot(60, self._register_appbar)
            return
        if not backend.cfg_bool("show_status_bar"):
            self.unregister_appbar()
            self.hide()
        elif not self.isVisible():
            self.show()  # showEvent re-registers the appbar
        elif self._registered:
            self._position_appbar()
        elif sys.platform != "win32":
            self._place_linux()

    def unregister_appbar(self) -> None:
        if self._reserved_on:                           # Linux (Hyprland): give the strip back
            from linuxdesk import hypr
            hypr.reserve(self._reserved_on)
            self._reserved_on = ""
        if not self._registered:
            return
        self._registered = False
        try:
            data = APPBARDATA()
            data.cbSize = ctypes.sizeof(APPBARDATA)
            data.hWnd = int(self.winId())
            _appbar_call(ABM_REMOVE, data)
        except Exception:  # noqa: BLE001 -- window may already be gone at interpreter exit
            pass

    def nativeEvent(self, event_type, message):
        if self._registered and bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_APP_CALLBACK:
                if msg.wParam == ABN_POSCHANGED:
                    self._position_appbar()
                elif msg.wParam == ABN_FULLSCREENAPP:
                    # Step aside while another app is fullscreen (video, games).
                    z = HWND_BOTTOM if msg.lParam else HWND_TOPMOST
                    ctypes.windll.user32.SetWindowPos(
                        wintypes.HWND(int(self.winId())), wintypes.HWND(z), 0, 0, 0, 0,
                        SWP_NOACTIVATE | 0x0001 | 0x0002)  # NOSIZE | NOMOVE
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event) -> None:
        self.unregister_appbar()
        super().closeEvent(event)
