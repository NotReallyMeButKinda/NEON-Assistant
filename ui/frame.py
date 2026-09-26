"""NEON's own title bar (Settings > Appearance > "Use NEON's title bar"), or Windows' native one.

How the custom frame works, so it still behaves like a real window:
- Qt is told the window is frameless (so its geometry has no frame margins), and the native window gets
  Windows' caption and resize styles back. DWM then still draws the drop shadow and rounded corners, and
  Aero Snap, the maximize animation and Win+arrow keys all keep working.
- An application-wide native event filter answers two messages for registered windows:
  WM_NCCALCSIZE (the client area is the whole window: no native title bar; a maximized window is fitted
  to the monitor's work area so it doesn't spill past the edges) and WM_NCHITTEST (the edges resize, the
  title bar drags -- which gives snap, double-click-to-maximize and the right-click system menu for free).
- The TitleBar itself is an ordinary child widget sitting in the window's top margin.

Windows call `install(self, ...)` at the end of __init__; `refresh_all()` switches every open window when
the setting changes.
"""

from __future__ import annotations

import ctypes
import sys
import weakref
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QCursor, QFont, QIcon
from PySide6.QtWidgets import QAbstractButton, QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

import assistant as backend

from .theme import COLORS, enable_dark_title_bar
from .theme import signals as theme_signals

BAR_HEIGHT = 36
IS_WINDOWS = sys.platform == "win32"

WM_NCCALCSIZE, WM_NCHITTEST = 0x0083, 0x0084
WM_MOUSEMOVE = 0x0200
MK_ANY_BUTTON = 0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0040      # left, right, middle, X1, X2
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
GWL_STYLE = -16
WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00020000, 0x00010000, 0x00080000
SWP_FRAMECHANGED, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0020, 0x0002, 0x0001, 0x0004, 0x0010
SM_CXSIZEFRAME, SM_CXPADDEDBORDER = 32, 92
FLUENT_ICONS = {"min": "", "max": "", "restore": "", "close": ""}


def icon_font_family() -> str:
    """Windows' caption-button icon font: Segoe Fluent Icons (Windows 11), else Segoe MDL2 Assets (Windows 10)."""
    from PySide6.QtGui import QFontDatabase
    families = set(QFontDatabase.families())
    return next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), "Segoe MDL2 Assets")


def custom_enabled() -> bool:
    return IS_WINDOWS and backend.cfg_bool("custom_titlebar")


# ---------------------------------------------------------------------------
# The bar
# ---------------------------------------------------------------------------

class _CaptionButton(QPushButton):
    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName(f"caption_{kind}")
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(46, BAR_HEIGHT)
        self.setCursor(Qt.ArrowCursor)
        font = QFont(icon_font_family())
        font.setPixelSize(10)
        self.setFont(font)                     # the style sheet below repeats it: the app's own sheet would win
        self.setText(FLUENT_ICONS[kind])
        self.setAccessibleName({"min": "Minimize", "max": "Maximize", "restore": "Restore",
                                "close": "Close"}[kind])


class TitleBar(QWidget):
    """Icon, title, and the minimize / maximize / close buttons. Dragging and double-clicking are left to
    Windows (the bar answers WM_NCHITTEST as a caption), so snapping works exactly as usual."""

    def __init__(self, window: QWidget, minimize: bool, maximize: bool):
        super().__init__(window)
        self._window = window
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(BAR_HEIGHT)
        self.setCursor(Qt.ArrowCursor)         # never a text cursor left over from the window's contents
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(8)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        self.title = QLabel(window.windowTitle())
        self.title.setObjectName("titleText")
        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addStretch(1)
        self.buttons: list[_CaptionButton] = []
        if minimize:
            self._add("min", window.showMinimized, layout)
        if maximize:
            self.max_button = self._add("max", self._toggle_max, layout)
        else:
            self.max_button = None
        self._add("close", window.close, layout)
        window.windowTitleChanged.connect(self.title.setText)
        window.windowIconChanged.connect(lambda _i: self._sync_icon())
        theme_signals.changed.connect(self.restyle)
        self._sync_icon()
        self.restyle()

    def _add(self, kind: str, action, layout) -> _CaptionButton:
        button = _CaptionButton(kind, self)
        button.clicked.connect(action)
        layout.addWidget(button)
        self.buttons.append(button)
        return button

    # Dragging normally never reaches Qt: Windows is told the bar is the caption (WM_NCHITTEST) and moves the
    # window itself. A press can still arrive here as an ordinary click -- when the window was holding on to
    # the mouse (a release it never saw), Windows skips the caption question and the drag used to do nothing.
    # So a press on the bar starts the move from here too, and a double-click still maximizes.
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.is_caption_at_local(event.position().toPoint()):
            self._start_move()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if (event.button() == Qt.LeftButton and self.max_button is not None
                and self.is_caption_at_local(event.position().toPoint())):
            self._toggle_max()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _start_move(self) -> None:
        handle = self._window.windowHandle()
        if handle is not None:
            handle.startSystemMove()           # Windows' own move: snapping and Win+arrows keep working

    def _toggle_max(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def sync_state(self) -> None:
        if self.max_button is not None:
            maximized = self._window.isMaximized()
            self.max_button.setText(FLUENT_ICONS["restore" if maximized else "max"])
            self.max_button.setAccessibleName("Restore" if maximized else "Maximize")

    def _sync_icon(self) -> None:
        icon: QIcon = self._window.windowIcon()
        if icon.isNull():
            icon = QApplication.windowIcon()
        self.icon.setPixmap(icon.pixmap(16, 16) if not icon.isNull() else QIcon().pixmap(16, 16))
        self.icon.setVisible(not icon.isNull())

    def restyle(self) -> None:
        c = COLORS
        self.setStyleSheet(f"""
            QWidget#titleBar {{ background: {c['panel']}; border-bottom: 1px solid {c['border']}; }}
            QLabel#titleText {{ color: {c['text']}; font-weight: 600; background: transparent; }}
            QPushButton {{ background: transparent; border: none; border-radius: 0; color: {c['text']};
                           font-family: "{icon_font_family()}"; font-size: 10px;
                           padding: 0; margin: 0; min-width: 46px; max-width: 46px;
                           min-height: {BAR_HEIGHT}px; max-height: {BAR_HEIGHT}px; }}
            QPushButton:hover {{ background: {c['panel_alt']}; }}
            QPushButton:pressed {{ background: {c['border']}; }}
            QPushButton#caption_close:hover {{ background: #c42b1c; color: white; }}
            QPushButton#caption_close:pressed {{ background: #a02316; color: white; }}
        """)

    def is_caption_at(self, global_pos: QPoint) -> bool:
        """True if the point is on the bar but not on one of its buttons (so dragging it moves the window)."""
        return self.is_caption_at_local(self.mapFromGlobal(global_pos))

    def is_caption_at_local(self, local: QPoint) -> bool:
        """The same, for a point in the bar's own (logical) coordinates."""
        if not self.rect().contains(local):
            return False
        child = self.childAt(local)
        while child is not None and child is not self:
            if isinstance(child, QAbstractButton):
                return False
            child = child.parentWidget()
        return True


# ---------------------------------------------------------------------------
# Windows' side: hit testing and the client area
# ---------------------------------------------------------------------------

class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_REGISTERED: "weakref.WeakValueDictionary[int, QWidget]" = weakref.WeakValueDictionary()


def _resize_border(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    try:
        dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
        return (user32.GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi)
                + user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi))
    except (AttributeError, OSError):
        return 8


class _FrameFilter(QAbstractNativeEventFilter):
    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        window = _REGISTERED.get(int(msg.hWnd or 0))
        if window is None:
            return False, 0
        try:
            if msg.message == WM_NCCALCSIZE and msg.wParam:
                return self._calc_size(msg)
            if msg.message == WM_MOUSEMOVE and not (msg.wParam & MK_ANY_BUTTON):
                _release_stale_capture(msg.hWnd)
            if msg.message == WM_NCHITTEST:
                return self._hit_test(msg, window)
        except Exception:  # noqa: BLE001 -- never break a window over its frame
            return False, 0
        return False, 0

    @staticmethod
    def _calc_size(msg):
        user32 = ctypes.windll.user32
        if user32.IsZoomed(msg.hWnd):
            # A maximized window reaches past the screen by its frame; fit it to the work area instead
            # (which also leaves the taskbar and the status bar's reserved space alone).
            params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            monitor = user32.MonitorFromWindow(msg.hWnd, 2)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                params.rgrc[0] = info.rcWork
        return True, 0                                   # the whole window is client area: no native caption

    @staticmethod
    def _hit_test(msg, window):
        user32 = ctypes.windll.user32
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        rect = wintypes.RECT()
        user32.GetWindowRect(msg.hWnd, ctypes.byref(rect))
        if not user32.IsZoomed(msg.hWnd) and _resizable(window):
            b = _resize_border(int(msg.hWnd))
            left, right = x < rect.left + b, x >= rect.right - b
            top, bottom = y < rect.top + b, y >= rect.bottom - b
            edge = {(True, False, True, False): HTTOPLEFT, (False, True, True, False): HTTOPRIGHT,
                    (True, False, False, True): HTBOTTOMLEFT, (False, True, False, True): HTBOTTOMRIGHT,
                    (True, False, False, False): HTLEFT, (False, True, False, False): HTRIGHT,
                    (False, False, True, False): HTTOP, (False, False, False, True): HTBOTTOM}.get(
                (left, right, top, bottom))
            if edge:
                return True, edge
        bar = getattr(window, "_neon_titlebar", None)
        if bar is not None and bar.isVisible():
            # The point Windows asks about, not Qt's idea of the cursor: physical screen pixels -> the window's
            # own logical pixels (monitors with different scaling disagree otherwise).
            point = wintypes.POINT(x, y)
            user32.ScreenToClient(msg.hWnd, ctypes.byref(point))
            ratio = window.devicePixelRatioF() or 1.0
            local = QPoint(int(point.x / ratio), int(point.y / ratio)) - bar.pos()
            if bar.is_caption_at_local(local):
                return True, HTCAPTION
        return False, 0


def _release_stale_capture(hwnd) -> None:
    """The mouse moves with no button down, yet this window still holds on to it: a release was missed
    (a dialog opened mid-click, the window was hidden...). Holding it kept the text cursor from the last
    widget and routed title-bar presses to that widget instead of dragging, so let it go."""
    user32 = ctypes.windll.user32
    user32.GetCapture.restype = wintypes.HWND
    captured = user32.GetCapture()
    if captured and int(captured) == int(hwnd or 0):
        user32.ReleaseCapture()


def _resizable(window: QWidget) -> bool:
    return window.minimumSize() != window.maximumSize()


_FILTER: dict = {"filter": None}


def _ensure_filter() -> None:
    if _FILTER["filter"] is None and IS_WINDOWS:
        _FILTER["filter"] = _FrameFilter()
        QApplication.instance().installNativeEventFilter(_FILTER["filter"])


def _native_styles(window: QWidget) -> None:
    """Give a frameless window back the styles DWM needs for its shadow, corners, snapping and animations."""
    user32 = ctypes.windll.user32
    hwnd = wintypes.HWND(int(window.winId()))
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style |= WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
    if getattr(window, "_neon_minimize", False):
        style |= WS_MINIMIZEBOX
    if getattr(window, "_neon_maximize", False):
        style |= WS_MAXIMIZEBOX
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


# ---------------------------------------------------------------------------
# Putting it on a window
# ---------------------------------------------------------------------------

class _Watcher(QObject):
    """Keeps the bar across the top, the max/restore glyph right, and the native styles on the current hwnd."""

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        bar = getattr(obj, "_neon_titlebar", None)
        if bar is None:
            return False
        if kind in (QEvent.Resize, QEvent.Show):
            bar.setGeometry(0, 0, obj.width(), BAR_HEIGHT)
            bar.raise_()
        if kind == QEvent.WindowStateChange:
            bar.sync_state()
        if kind == QEvent.Show and IS_WINDOWS:
            _REGISTERED[int(obj.winId())] = obj
            _native_styles(obj)
        if kind == QEvent.WinIdChange and IS_WINDOWS:
            _REGISTERED[int(obj.winId())] = obj
        return False


_WATCHER: dict = {"watcher": None}
_WINDOWS: "weakref.WeakSet[QWidget]" = weakref.WeakSet()


def install(window: QWidget, minimize: bool = False, maximize: bool = False) -> None:
    """Give `window` NEON's title bar if the setting is on (else the native one, themed). Call at the end of
    the window's __init__, before it is first shown."""
    window._neon_minimize, window._neon_maximize = minimize, maximize
    _WINDOWS.add(window)
    _apply(window, custom_enabled())


def _apply(window: QWidget, custom: bool) -> None:
    has_bar = getattr(window, "_neon_titlebar", None) is not None
    if custom == has_bar:
        return
    visible = window.isVisible()
    geometry = window.geometry()
    client_size = _client_size(window) if IS_WINDOWS and window.testAttribute(Qt.WA_WState_Created) else None
    if custom:
        _ensure_filter()
        if _WATCHER["watcher"] is None:
            _WATCHER["watcher"] = _Watcher()
        bar = TitleBar(window, window._neon_minimize, window._neon_maximize)
        window._neon_titlebar = bar
        window._neon_margins = window.contentsMargins()
        m = window._neon_margins
        window.setContentsMargins(m.left(), m.top() + BAR_HEIGHT, m.right(), m.bottom())
        window.setWindowFlag(Qt.FramelessWindowHint, True)
        window.installEventFilter(_WATCHER["watcher"])
        bar.setGeometry(0, 0, window.width(), BAR_HEIGHT)
        bar.show()                                    # a child made after its window is shown stays hidden otherwise
        bar.raise_()
    else:
        bar = window._neon_titlebar
        window._neon_titlebar = None
        window.removeEventFilter(_WATCHER["watcher"])
        bar.hide()
        bar.deleteLater()
        window.setContentsMargins(window._neon_margins)
        window.setWindowFlag(Qt.FramelessWindowHint, False)
        for hwnd, w in list(_REGISTERED.items()):
            if w is window:
                _REGISTERED.pop(hwnd, None)
    if visible:                                       # changing the flags hid it: put it back where it was
        window.setGeometry(geometry)
        window.show()
        enable_dark_title_bar(window)
    if not custom and IS_WINDOWS and window.testAttribute(Qt.WA_WState_Created):
        _refresh_native_frame(window, client_size)


def _client_size(window: QWidget) -> tuple[int, int]:
    rect = wintypes.RECT()
    ctypes.windll.user32.GetClientRect(wintypes.HWND(int(window.winId())), ctypes.byref(rect))
    return rect.right, rect.bottom


def _refresh_native_frame(window: QWidget, client_size: tuple[int, int] | None) -> None:
    """Back to Windows' own title bar: our WM_NCCALCSIZE answer ("it's all client area") stays in force until
    Windows is told the frame changed, which left the window with no title bar at all and nothing to drag.
    The window then grows by the frame, so the content keeps its size."""
    user32 = ctypes.windll.user32
    hwnd = wintypes.HWND(int(window.winId()))
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    if not client_size or user32.IsZoomed(hwnd):
        return
    outer, inner = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    user32.GetClientRect(hwnd, ctypes.byref(inner))
    extra_w, extra_h = (outer.right - outer.left) - inner.right, (outer.bottom - outer.top) - inner.bottom
    user32.SetWindowPos(hwnd, None, 0, 0, client_size[0] + extra_w, client_size[1] + extra_h,
                        SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)


def refresh_all() -> None:
    """The setting changed: switch every open window."""
    custom = custom_enabled()
    for window in list(_WINDOWS):
        try:
            _apply(window, custom)
        except RuntimeError:                           # deleted on the C++ side
            _WINDOWS.discard(window)
