"""
ui/effects.py -- the short, silly animations "do a barrel roll" and friends ask for.

One transparent child widget lies over the status bar and paints the effect; it is hidden (and its
timer stopped) the rest of the time, so nothing here costs anything while it is idle. Effects that
move the bar work on a *snapshot* of it rather than the live widgets, because the bar is a
registered Windows AppBar whose real rectangle is enforced on every move -- rotating the pixmap is
both smoother and the only thing that actually works.

    overlay.play("barrel_roll" | "shake" | "confetti" | "matrix")

`rainbow` isn't here: it is a theme change, so the status bar drives it directly.
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QWidget

from .theme import COLORS

FRAME_MS = 16                    # ~60 fps while an effect is running

DURATIONS = {                    # seconds
    "barrel_roll": 1.1,
    "shake": 0.8,
    "confetti": 2.6,
    "matrix": 4.0,
}
CONFETTI_COUNT = 70
# Kept to characters the default monospace font is guaranteed to have: a missing glyph draws
# as an empty box, which is a very un-Matrix thing to rain.
MATRIX_GLYPHS = "01234789<>/\\|=+*#$%&@?!~^_:;"


class EffectOverlay(QWidget):
    """Plays one effect at a time over its parent, then gets out of the way."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()
        self._effect = ""
        self._frame = 0
        self._frames = 0
        self._snapshot: QPixmap | None = None
        self._particles: list[dict] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---- lifecycle -------------------------------------------------------------------
    def play(self, effect: str) -> bool:
        """Start `effect`. False if it isn't one this overlay knows."""
        if effect not in DURATIONS:
            return False
        parent = self.parentWidget()
        if parent is None or parent.width() <= 0 or parent.height() <= 0:
            return False
        self.setGeometry(parent.rect())
        self._effect = effect
        self._frame = 0
        self._frames = max(1, int(DURATIONS[effect] * 1000 / FRAME_MS))
        self._snapshot = parent.grab() if effect in ("barrel_roll", "shake") else None
        self._particles = (self._make_confetti() if effect == "confetti"
                           else self._make_rain() if effect == "matrix" else [])
        self.raise_()
        self.show()
        self._timer.start(FRAME_MS)
        return True

    def stop(self) -> None:
        self._timer.stop()
        self._effect = ""
        self._snapshot = None
        self._particles = []
        self.hide()

    @property
    def running(self) -> bool:
        return bool(self._effect)

    def _tick(self) -> None:
        self._frame += 1
        if self._frame >= self._frames:
            self.stop()
            parent = self.parentWidget()
            if parent is not None:
                parent.update()
            return
        self.update()

    # ---- particles -------------------------------------------------------------------
    def _make_confetti(self) -> list[dict]:
        width, height = max(1, self.width()), max(1, self.height())
        palette = [COLORS["accent"], COLORS["accent2"], COLORS["success"], COLORS["accent_bright"],
                   COLORS["error"]]
        return [{"x": random.uniform(0, width), "y": random.uniform(-2 * height, height),
                 "vx": random.uniform(-0.4, 0.4), "vy": random.uniform(0.35, 1.1),
                 "spin": random.uniform(-0.25, 0.25), "angle": random.uniform(0, 6.28),
                 "size": random.uniform(3.0, 6.5), "color": QColor(random.choice(palette))}
                for _ in range(CONFETTI_COUNT)]

    def _make_rain(self) -> list[dict]:
        width = max(1, self.width())
        columns = max(6, width // 14)
        return [{"x": i * width / columns + random.uniform(-3, 3),
                 "y": random.uniform(-self.height() * 2, 0),
                 "vy": random.uniform(1.4, 4.0),
                 "glyph": random.choice(MATRIX_GLYPHS)}
                for i in range(columns)]

    # ---- painting --------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        if not self._effect:
            return
        progress = self._frame / self._frames
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        {"barrel_roll": self._paint_roll, "shake": self._paint_shake,
         "confetti": self._paint_confetti, "matrix": self._paint_matrix}[self._effect](painter, progress)

    def _paint_snapshot(self, painter: QPainter, transform: QTransform) -> None:
        if self._snapshot is None:
            return
        painter.fillRect(self.rect(), QColor(COLORS["bg"]))
        painter.setTransform(transform)
        ratio = self._snapshot.devicePixelRatio() or 1.0
        painter.drawPixmap(QPointF(-self._snapshot.width() / (2 * ratio),
                                   -self._snapshot.height() / (2 * ratio)), self._snapshot)

    def _paint_roll(self, painter: QPainter, progress: float) -> None:
        """A roll about the bar's long axis -- which is what a barrel roll actually is, and the only
        rotation that stays visible on something 1600 pixels wide and 44 tall. Spinning it in the
        plane of the screen shows an edge-on sliver for most of the animation and nothing else."""
        eased = 0.5 - 0.5 * math.cos(math.pi * progress)          # ease in and out
        squash = math.cos(2 * math.pi * eased)                    # 1 -> 0 -> -1 -> 0 -> 1
        transform = QTransform()
        transform.translate(self.width() / 2, self.height() / 2)
        transform.scale(1.0, squash if abs(squash) > 0.02 else 0.02)
        self._paint_snapshot(painter, transform)

    def _paint_shake(self, painter: QPainter, progress: float) -> None:
        decay = (1.0 - progress) ** 2
        offset = math.sin(progress * math.pi * 14) * 14 * decay
        transform = QTransform()
        transform.translate(self.width() / 2 + offset, self.height() / 2 + offset * 0.25)
        self._paint_snapshot(painter, transform)

    def _paint_confetti(self, painter: QPainter, progress: float) -> None:
        fade = min(1.0, 2.5 * (1.0 - progress))
        width = max(1, self.width())
        for bit in self._particles:
            bit["x"] += bit["vx"]
            bit["y"] += bit["vy"]
            bit["angle"] += bit["spin"]
            bit["vy"] += 0.012                      # a little gravity
            if bit["y"] > self.height() + 8:        # the bar is only ~44 px tall: keep it falling
                bit["y"] = random.uniform(-2 * self.height(), -4)
                bit["x"] = random.uniform(0, width)
                bit["vy"] = random.uniform(0.35, 1.1)
            colour = QColor(bit["color"])
            colour.setAlphaF(max(0.0, min(1.0, fade)))
            painter.save()
            painter.translate(bit["x"], bit["y"])
            painter.rotate(math.degrees(bit["angle"]))
            painter.setPen(Qt.NoPen)
            painter.setBrush(colour)
            painter.drawRect(QRectF(-bit["size"] / 2, -bit["size"] / 4, bit["size"], bit["size"] / 2))
            painter.restore()

    def _paint_matrix(self, painter: QPainter, progress: float) -> None:
        fade = min(1.0, 3.0 * (1.0 - progress))
        font = QFont("Consolas", max(8, self.height() // 3))
        painter.setFont(font)
        head = QColor(COLORS["success"])
        tail = QColor(COLORS["accent2"])
        for drop in self._particles:
            drop["y"] += drop["vy"]
            if drop["y"] > self.height() + 20:
                drop["y"] = random.uniform(-self.height(), -4)
                drop["glyph"] = random.choice(MATRIX_GLYPHS)
            for step in range(4):                   # a short trail behind each glyph
                colour = QColor(head if step == 0 else tail)
                colour.setAlphaF(max(0.0, fade * (0.85 - 0.22 * step)))
                painter.setPen(colour)
                painter.drawText(QPointF(drop["x"], drop["y"] - step * self.height() / 3),
                                 drop["glyph"] if step == 0 else random.choice(MATRIX_GLYPHS))
