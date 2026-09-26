"""Custom-painted widgets: state orb, speaking waveform, chat bubbles."""

from __future__ import annotations

import math
import random
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea,
                               QVBoxLayout, QWidget)

from .theme import COLORS, STATE_COLORS
from .theme import signals as theme_signals


class StateOrb(QWidget):
    """Glowing orb that pulses (listening), breathes (thinking) or ripples
    (speaking). Draws a vector mic icon and emits `clicked` when `clickable`."""

    clicked = Signal()

    def __init__(self, size: int = 56, clickable: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._state = "idle"
        self._phase = 0.0
        self._level = 0.0
        self._level_driven = False
        self._muted = False
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_muted(self, muted: bool) -> None:
        """Muted mic: drawn red with a slash through it until unmuted."""
        self._muted = muted
        self.update()

    def set_level(self, level: float) -> None:
        """Real audio loudness (0..1) while speaking; the glow follows it."""
        if self._state != "speaking":
            return
        self._level_driven = True
        self._level = max(level, self._level * 0.8)
        self.update()

    def set_state(self, state: str) -> None:
        self._state = state if state in STATE_COLORS else "idle"
        self._level_driven = False
        self._level = 0.0
        if self._state == "idle":
            self._timer.stop()
            self._phase = 0.0
        elif not self._timer.isActive():
            self._timer.start(33)
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.09) % (2 * math.pi)
        self.update()

    def mousePressEvent(self, event) -> None:
        if self._clickable and event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        muted_idle = self._muted and self._state in ("idle", "listening", "error")
        color = QColor(STATE_COLORS["muted"] if muted_idle else STATE_COLORS[self._state])
        c = QPointF(self.width() / 2, self.height() / 2)
        base = min(self.width(), self.height()) / 2
        wave = (math.sin(self._phase) + 1) / 2  # 0..1
        if self._level_driven:
            wave = min(1.0, self._level * 1.4)
        animated = self._state != "idle"

        glow_r = base * (0.75 + 0.25 * wave) if animated else base * 0.8
        grad = QRadialGradient(c, glow_r)
        glow = QColor(color)
        glow.setAlpha(int(90 + 90 * wave) if animated else 70)
        grad.setColorAt(0.35, glow)
        glow.setAlpha(0)
        grad.setColorAt(1.0, glow)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(c, glow_r, glow_r)

        if self._state == "speaking":  # expanding ripple
            ripple = QColor(color)
            ripple.setAlphaF(1 - wave)
            p.setPen(QPen(ripple, 1.5))
            p.setBrush(Qt.NoBrush)
            rr = base * (0.5 + 0.5 * wave)
            p.drawEllipse(c, rr, rr)

        core = base * 0.6
        p.setPen(QPen(color, 2))
        p.setBrush(QColor(COLORS["panel_alt"]))
        p.drawEllipse(c, core, core)

        if self._clickable:  # vector mic icon
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            w, h = core * 0.42, core * 0.75
            p.drawRoundedRect(
                QRectF(c.x() - w / 2, c.y() - h / 2 - core * 0.1, w, h), w / 2, w / 2)
            p.setPen(QPen(color, 1.6))
            p.setBrush(Qt.NoBrush)
            arc = QRectF(c.x() - core * 0.42, c.y() - core * 0.5, core * 0.84, core * 0.8)
            p.drawArc(arc, 200 * 16, 140 * 16)
            p.drawLine(QPointF(c.x(), c.y() + core * 0.3), QPointF(c.x(), c.y() + core * 0.55))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(c, core * 0.4, core * 0.4)

        if muted_idle:  # slash through the icon
            p.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() - core * 0.75, c.y() - core * 0.75),
                       QPointF(c.x() + core * 0.75, c.y() + core * 0.75))


class Waveform(QWidget):
    """Little equalizer bars; animates only while active."""

    def __init__(self, bars: int = 7, parent=None):
        super().__init__(parent)
        self.setFixedSize(bars * 6, 22)
        self._levels = [0.15] * bars
        self._level_driven = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_level(self, level: float) -> None:
        """Feed real loudness; bars scroll left as new samples arrive."""
        if level <= 0.0 and not self._level_driven:
            return
        self._level_driven = True
        self._timer.stop()  # real data replaces the random animation
        self._levels = self._levels[1:] + [max(0.12, min(1.0, level * 1.6))]
        self.update()

    def set_active(self, active: bool, color: str | None = None) -> None:
        # `color` is accepted for compatibility; the bars always use the current theme's accent2.
        if active:
            if not self._timer.isActive() and not self._level_driven:
                self._timer.start(70)  # placeholder until real levels arrive (or for SAPI)
        else:
            self._timer.stop()
            self._level_driven = False
            self._levels = [0.15] * len(self._levels)
            self.update()

    def _tick(self) -> None:
        self._levels = [0.2 + 0.8 * random.random() for _ in self._levels]
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(COLORS["accent2"]))
        bw = self.width() / len(self._levels)
        for i, lvl in enumerate(self._levels):
            h = self.height() * lvl
            p.drawRoundedRect(QRectF(i * bw + 1, (self.height() - h) / 2, bw - 2, h), 2, 2)


class Spectrum(QWidget):
    """A frequency bar graph of the assistant's voice.

    Fed from `controller.spectrum`, which is the real FFT of what is playing (see tts.py), so the
    bars move with the vowels rather than jiggling at random. Bars fall back to zero on their own,
    which keeps it alive-looking between the 30 Hz updates and settles it when speech stops."""

    FALL = 0.82                      # per frame, so a peak decays over ~0.3 s

    def __init__(self, bands: int = 9, height: int = 22, parent=None):
        super().__init__(parent)
        self._bands = bands
        self.setFixedSize(bands * 7 + 2, height)
        self._levels = [0.0] * bands
        self._active = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fall)

    def set_levels(self, levels) -> None:
        values = [max(0.0, min(1.0, float(v))) for v in (levels or [])][:self._bands]
        values += [0.0] * (self._bands - len(values))
        self._levels = [max(new, old * self.FALL) for new, old in zip(values, self._levels)]
        if any(v > 0.01 for v in self._levels) and not self._timer.isActive():
            self._timer.start(33)
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if not active:
            self._levels = [0.0] * self._bands
            self._timer.stop()
            self.update()

    def _fall(self) -> None:
        self._levels = [v * self.FALL for v in self._levels]
        if all(v <= 0.01 for v in self._levels):
            self._timer.stop()
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        hot, cool = QColor(COLORS["accent"]), QColor(COLORS["accent2"])
        width = self.width() / max(1, self._bands)
        for i, level in enumerate(self._levels):
            # Low bands in the accent colour, high bands in the secondary: the mix makes the
            # shape of the sound readable at a glance in a 22-pixel-tall strip.
            t = i / max(1, self._bands - 1)
            colour = QColor(int(hot.red() + (cool.red() - hot.red()) * t),
                            int(hot.green() + (cool.green() - hot.green()) * t),
                            int(hot.blue() + (cool.blue() - hot.blue()) * t))
            colour.setAlpha(120 + int(135 * min(1.0, level * 1.4)))
            p.setBrush(colour)
            h = max(2.0, self.height() * min(1.0, level))
            p.drawRoundedRect(QRectF(i * width + 1, (self.height() - h) / 2, width - 2, h), 1.5, 1.5)


class ChatBubble(QWidget):
    def __init__(self, sender: str, text: str, parent=None):
        super().__init__(parent)
        user = sender == "You"
        self.user = user

        self.frame = QFrame()
        self.frame.setObjectName("bubble")
        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(14, 8, 14, 8)
        inner.setSpacing(2)

        who = QLabel(sender)
        self.who = who
        body = QLabel(text)
        self.body = body
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet("font-size: 14px;")
        stamp = QLabel(f"{datetime.now():%H:%M}")
        self.stamp = stamp
        stamp.setAlignment(Qt.AlignRight)
        inner.addWidget(who)
        inner.addWidget(body)
        inner.addWidget(stamp)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        if user:
            row.addStretch(1)
            row.addWidget(self.frame)
        else:
            row.addWidget(self.frame)
            row.addStretch(1)
        self.restyle()

    def restyle(self) -> None:
        """(Re)apply the current theme's colors."""
        accent = COLORS["accent2"] if self.user else COLORS["accent_bright"]
        bg = COLORS["bubble_user"] if self.user else COLORS["bubble_bot"]
        border = COLORS["bubble_user_border"] if self.user else COLORS["bubble_bot_border"]
        self.frame.setStyleSheet(
            f"QFrame#bubble {{ background: {bg}; border: 1px solid {border}; border-radius: 14px; }}")
        self.who.setStyleSheet(f"color: {accent}; font-weight: 700; font-size: 11px;")
        self.stamp.setStyleSheet(f"color: {COLORS['muted']}; font-size: 10px;")

    def set_width(self, width: int) -> None:
        """Every bubble is the same width (half the chat), however short its text."""
        self.frame.setFixedWidth(width)

    def append_text(self, chunk: str) -> None:
        self.body.setText(self.body.text() + chunk)


class ChatView(QScrollArea):
    """Scrollable list of bubbles and dim system lines."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(holder)
        self._layout.setContentsMargins(6, 6, 10, 6)
        self._layout.setSpacing(10)
        self._layout.addStretch(1)
        self.setWidget(holder)
        self._bubbles: list[ChatBubble] = []
        self._system_lines: list[QLabel] = []
        self._streaming: ChatBubble | None = None
        self.verticalScrollBar().rangeChanged.connect(self._stick_to_bottom)
        theme_signals.changed.connect(self.restyle)

    def restyle(self) -> None:
        for bubble in self._bubbles:
            bubble.restyle()
        for line in self._system_lines:
            line.setStyleSheet(f"color: {COLORS['muted']}; font-size: 11px; font-style: italic;")
            self._fit_line(line, self._bubble_width())

    @staticmethod
    def _fit_line(line: QLabel, width: int) -> None:
        """A wrapped label added with an alignment gets its one-line height from the layout (it
        ignores heightForWidth), clipping everything past the first line or two; pin it instead."""
        line.setFixedWidth(width)
        line.ensurePolished()                           # the style sheet's font decides the height
        line.setFixedHeight(line.heightForWidth(width))

    def _stick_to_bottom(self, _min, maximum) -> None:
        self.verticalScrollBar().setValue(maximum)

    def add_chunk(self, sender: str, chunk: str) -> None:
        """Append to the reply that is currently streaming in (starting it if needed)."""
        if self._streaming is None:
            bubble = ChatBubble(sender, chunk.lstrip())
            bubble.set_width(self._bubble_width())
            self._bubbles.append(bubble)
            self._layout.insertWidget(self._layout.count() - 1, bubble)
            self._streaming = bubble
        else:
            self._streaming.append_text(chunk)

    def end_stream(self) -> None:
        if self._streaming is not None:
            self._streaming.body.setText(self._streaming.body.text().strip())
        self._streaming = None

    def add_message(self, sender: str, text: str) -> None:
        if sender == "system":
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {COLORS['muted']}; font-size: 11px; font-style: italic;")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            self._fit_line(lbl, self._bubble_width())   # same half-width column as the bubbles
            self._system_lines.append(lbl)
            self._layout.insertWidget(self._layout.count() - 1, lbl, 0, Qt.AlignHCenter)
            return
        bubble = ChatBubble(sender, text)
        bubble.set_width(self._bubble_width())
        self._bubbles.append(bubble)
        self._layout.insertWidget(self._layout.count() - 1, bubble)

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bubbles.clear()
        self._system_lines.clear()
        self._streaming = None

    def _bubble_width(self) -> int:
        return max(240, self.viewport().width() // 2)   # every chat box is half the chat window wide

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w = self._bubble_width()
        for b in self._bubbles:
            b.set_width(w)
        for line in self._system_lines:
            self._fit_line(line, w)
