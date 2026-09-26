"""The quick command box: a borderless, always-on-top text field opened by a global hotkey.

Type a command, press Enter, and it is sent like anything typed in the main window (the reply is
spoken, shown in the chat and status bar, and appears right here under the field for a few
seconds). Esc, or clicking elsewhere, closes it; Up/Down walk through what was typed earlier.

Over a fullscreen app it never takes focus: a game in exclusive fullscreen minimizes the moment it
loses focus, and others pause or let go of the mouse. The box shows without activating and a keyboard
hook (hotkeys.KeyCapture) brings the typing to it until it closes.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PySide6.QtCore import QEasingCurve, QEvent, QParallelAnimationGroup, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout,
                               QWidget)

import assistant as backend
import hotkeys

from .motion import animations_enabled
from .theme import COLORS
from .theme import signals as theme_signals

BOX_WIDTH = 620
HISTORY_MAX = 30
GRACE = 0.35     # seconds after opening during which a focus flicker doesn't close the box
FADE_MS, SLIDE_MS, SLIDE_PX = 170, 230, 18   # open animation: fades in while easing down into place
REPLY_MAX_CHARS = 320                        # a longer reply is shortened here (the chat has all of it)
REPLY_WAIT_SECONDS = 45                      # give up waiting for a reply that never comes
CAPTURE_IDLE_SECONDS = 60                    # typing over a game: closes by itself if left alone
CARET = " ▍"                            # trailing block while the reply is still being written


def force_foreground(hwnd: int) -> None:
    """Windows only lets the foreground app take focus. A hotkey press normally grants it, but
    attaching to the foreground thread's input makes it reliable."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    except Exception:  # noqa: BLE001 -- best effort; the box still works if it can't grab focus
        pass


def fullscreen_screen():
    """The screen a fullscreen or borderless-windowed app is filling, if one is in front (else None).
    Qt places each screen at its monitor's physical top-left corner (only the size is scaled), so
    the two are matched by that corner."""
    from .shade import fullscreen_monitor
    monitor = fullscreen_monitor()
    if monitor is None:
        return None
    return next((sc for sc in QGuiApplication.screens()
                 if (sc.geometry().x(), sc.geometry().y()) == (monitor[0], monitor[1])), None)


def keep_on_top(hwnd: int) -> None:
    """Above everything, including a borderless game that is itself "always on top" (Qt only sets
    the topmost flag when the window is created, and a topmost app activated later goes above it)."""
    try:
        ctypes.windll.user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-1), 0, 0, 0, 0,
                                          0x0001 | 0x0002 | 0x0040)    # NOSIZE | NOMOVE | SHOWWINDOW
    except Exception:  # noqa: BLE001 -- best effort
        pass


class QuickInput(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._controller = controller
        self._history: list[str] = []
        self._history_pos = 0
        self._draft = ""
        self._opened_at = 0.0
        self._return_to = 0                         # the window that had focus before (a game): it gets it back
        self._capture = hotkeys.KeyCapture(self._on_captured_key)   # installed only while open over a fullscreen app
        self._capture_idle = QTimer(self)
        self._capture_idle.setSingleShot(True)
        self._capture_idle.timeout.connect(self.hide)

        self.setAttribute(Qt.WA_TranslucentBackground)   # rounded card, no rectangle behind it
        self.setFixedWidth(BOX_WIDTH)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 22)          # room for the glow
        self.card = QFrame()
        self.card.setObjectName("quickcard")
        outer.addWidget(self.card)

        column = QVBoxLayout(self.card)
        column.setContentsMargins(18, 4, 18, 4)
        column.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setMinimumHeight(46)
        self.edit.returnPressed.connect(self._submit)
        self.edit.installEventFilter(self)
        row.addWidget(self.dot)
        row.addWidget(self.edit, 1)
        column.addLayout(row)
        self.reply = QLabel()                       # the answer, under the field
        self.reply.setWordWrap(True)
        self.reply.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reply.setContentsMargins(22, 0, 0, 12)
        self.reply.hide()
        column.addWidget(self.reply)

        self._awaiting = False                      # a command was sent from this box and its reply is pending
        self._writing = False                       # the reply is still arriving (word by word from the model)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._patience = QTimer(self)               # no reply at all (e.g. still starting up): don't wait forever
        self._patience.setSingleShot(True)
        self._patience.timeout.connect(self.hide)
        controller.caption.connect(self._on_caption)
        controller.state.connect(self._on_state)

        self._glow = QGraphicsDropShadowEffect(self.card)
        self._glow.setBlurRadius(36)
        self._glow.setOffset(0, 0)
        self.card.setGraphicsEffect(self._glow)

        self._anim = QParallelAnimationGroup(self)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.addAnimation(self._fade)
        self._anim.addAnimation(self._slide)

        theme_signals.changed.connect(self._restyle)
        self._restyle()

    def _restyle(self) -> None:
        self.card.setStyleSheet(
            f"QFrame#quickcard {{ background: {COLORS['panel']}; border: 1px solid {COLORS['accent']}; "
            f"border-radius: 16px; }}")
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; font-size: 18px; padding: 0; "
            f"color: {COLORS['text']}; selection-background-color: {COLORS['accent']}; "
            f"selection-color: {COLORS['on_accent']}; }}")
        self.dot.setStyleSheet(f"background: {COLORS['accent']}; border-radius: 5px;")
        self.reply.setStyleSheet(f"color: {COLORS['accent_bright']}; font-size: 15px; background: transparent;")
        glow = COLORS["accent"]
        self._glow.setColor(_with_alpha(glow, 110))

    # ---- showing / hiding ----------------------------------------------------------
    @property
    def capturing(self) -> bool:
        """Open over a fullscreen app, typed into through the keyboard hook."""
        return self._capture.installed

    def hideEvent(self, event) -> None:
        self._capture.uninstall()                # the keyboard goes back the moment the box closes
        self._capture_idle.stop()
        self._anim.stop()                        # closing mid-animation must not leave it half-faded
        self.setWindowOpacity(1.0)
        self._awaiting = False
        self._writing = False
        self._hide_timer.stop()
        self._patience.stop()
        self.reply.hide()
        self.reply.clear()
        self._fit()                              # back to one line, so it never reopens at the old size
        super().hideEvent(event)
        back, self._return_to = self._return_to, 0
        if back:
            QTimer.singleShot(0, lambda: self._give_focus_back(back))

    @staticmethod
    def _give_focus_back(hwnd: int) -> None:
        """Closing the box returns focus to what was in front (a fullscreen game would otherwise stay
        in the background, or minimized)."""
        try:
            user32 = ctypes.windll.user32
            if user32.IsWindow(wintypes.HWND(hwnd)):
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
        except Exception:  # noqa: BLE001 -- best effort
            pass

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.open_box()

    def open_box(self) -> None:
        self.edit.setPlaceholderText(f"Ask {backend.assistant_name()} anything...   Enter to send  -  Esc to close")
        self.edit.clear()
        self.reply.hide()
        self.reply.clear()
        self._history_pos = len(self._history)
        self._fit()
        # Over a fullscreen app: on its screen (games often park or hide the mouse elsewhere).
        full = fullscreen_screen()
        self.setAttribute(Qt.WA_ShowWithoutActivating, full is not None)
        screen = full or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.geometry() if full else screen.availableGeometry()   # a fullscreen app covers the taskbar
        target = QPoint(area.x() + (area.width() - self.width()) // 2, area.y() + int(area.height() * 0.26))
        self._anim.stop()
        animate = animations_enabled()
        self._slide.setStartValue(target - QPoint(0, SLIDE_PX))
        self._slide.setEndValue(target)
        self.setWindowOpacity(0.0 if animate else 1.0)     # invisible until the animation has started: no flash
        self.move(target - QPoint(0, SLIDE_PX) if animate else target)
        self._opened_at = time.monotonic()
        try:
            foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            foreground = 0
        self._return_to = foreground if foreground and foreground != int(self.winId()) and full is None else 0
        self.show()
        keep_on_top(int(self.winId()))
        if animate:
            self._anim.start()
        self.edit.setFocus(Qt.OtherFocusReason)
        if full is not None and self._capture.install():
            self._capture_idle.start(CAPTURE_IDLE_SECONDS * 1000)
            return                                        # the game keeps focus; keys come through the hook
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))
        self.edit.setFocus(Qt.ActiveWindowFocusReason)

    def _on_captured_key(self, vk: int, text: str, ctrl: bool) -> None:
        """A key typed over a fullscreen app (see hotkeys.KeyCapture): edit the field as if it had focus."""
        if not self.isVisible():
            return
        self._capture_idle.start(CAPTURE_IDLE_SECONDS * 1000)
        edit = self.edit
        if ctrl:
            if vk == 0x56:                                # Ctrl+V
                edit.insert(QGuiApplication.clipboard().text().replace("\n", " "))
            elif vk == 0x41:                              # Ctrl+A
                edit.selectAll()
            elif vk == 0x08:                              # Ctrl+Backspace
                edit.cursorWordBackward(True)
                edit.del_()
            return
        if vk == 0x1B:                                    # Esc
            self.hide()
        elif vk == 0x0D:                                  # Enter
            self._submit()
        elif vk == 0x08:
            edit.backspace()
        elif vk == 0x2E:
            edit.del_()
        elif vk in (0x25, 0x27):                          # Left / Right
            (edit.cursorBackward if vk == 0x25 else edit.cursorForward)(False)
        elif vk in (0x24, 0x23):                          # Home / End
            (edit.home if vk == 0x24 else edit.end)(False)
        elif vk in (0x26, 0x28):                          # Up / Down
            self._step_history(-1 if vk == 0x26 else 1)
        elif text:
            edit.insert(text)

    def _submit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            self.hide()
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            del self._history[:-HISTORY_MAX]
        self._history_pos = len(self._history)
        self.edit.clear()                         # stays open: the reply appears below, and you can type another
        self._awaiting = True
        self._hide_timer.stop()
        self._patience.start(REPLY_WAIT_SECONDS * 1000)
        self._show_reply("Thinking...")
        self._controller.submit(text)

    # ---- the reply ---------------------------------------------------------------------
    def _show_reply(self, text: str) -> None:
        text = text.strip()
        if len(text) > REPLY_MAX_CHARS:
            text = text[:REPLY_MAX_CHARS].rsplit(" ", 1)[0] + "..."
        if self._writing and text:
            text += CARET
        self.reply.setText(text)
        self.reply.setVisible(bool(text))
        self._fit()

    def _fit(self) -> None:
        """Exactly as tall as the field plus the reply (if one is showing) -- taller or shorter.
        adjustSize() under-estimates wrapped text, the outer horizontal layout doesn't pass
        height-for-width on, and a label that was just hidden still counts until the layouts are
        recalculated: so recalculate them, then ask the card how tall it needs to be at its real width."""
        for layout in (self.card.layout(), self.layout()):
            layout.invalidate()
            layout.activate()
        margins = self.layout().contentsMargins()
        card_width = self.width() - margins.left() - margins.right()
        needed = self.card.heightForWidth(card_width)
        if needed < 0:
            needed = self.card.sizeHint().height()
        height = needed + margins.top() + margins.bottom()
        self.setMinimumHeight(0)                 # a taller earlier reply must not hold the minimum up
        self.resize(self.width(), max(self.minimumSizeHint().height(), height))

    def _on_caption(self, sender: str, text: str) -> None:
        if self._awaiting and self.isVisible() and sender != "You":
            self._show_reply(text)

    def _on_state(self, state: str) -> None:
        self._writing = state == "speaking"        # the reply streams in while she is "speaking" it
        if not (self._awaiting and self.isVisible()):
            return
        if state == "idle":                       # the reply is finished (and spoken): leave it up a moment
            self._patience.stop()
            self._hide_timer.start(int(backend.cfg_num("quick_reply_seconds") * 1000))
        else:
            self._hide_timer.stop()

    # ---- events ----------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key_Up, Qt.Key_Down) and self._history:
                self._step_history(-1 if key == Qt.Key_Up else 1)
                return True
        return super().eventFilter(obj, event)

    def _step_history(self, direction: int) -> None:
        if not self._history:
            return
        if self._history_pos == len(self._history):
            self._draft = self.edit.text()                  # keep what was being typed
        self._history_pos = max(0, min(len(self._history), self._history_pos + direction))
        self.edit.setText(self._draft if self._history_pos == len(self._history)
                          else self._history[self._history_pos])

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() == QEvent.ActivationChange and self.isVisible() and not self.isActiveWindow()
                and not self.capturing):                   # over a game it was never active
            if time.monotonic() - self._opened_at > GRACE:
                QTimer.singleShot(0, self._hide_if_inactive)   # clicked elsewhere: dismiss

    def _hide_if_inactive(self) -> None:
        if self.isVisible() and not self.isActiveWindow():
            self.hide()


def _with_alpha(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c
