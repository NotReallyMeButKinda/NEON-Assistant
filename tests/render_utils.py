"""Helpers for the visual tests: a clip audit and screenshot comparison.

clip_problems(window)  finds widgets that are too small for their own text (cut-off labels, buttons
                       squeezed below their minimum size, wrapped text without the height it needs).
check_screenshot(...)  renders a window offscreen and compares it to a stored image in tests/baselines/.
                       A different picture fails the test and leaves `<name>.actual.png` and
                       `<name>.diff.png` (differences in red) in tests/_renders/ to look at.
                       After a deliberate visual change, run with NEON_UPDATE_BASELINES=1 to accept it.

Baselines are rendered with this machine's fonts, so they belong to the machine they were made on;
regenerate them (NEON_UPDATE_BASELINES=1) after moving to another one.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QRadioButton,
                               QSpinBox, QWidget)

TESTS = Path(__file__).resolve().parent
BASELINES = TESTS / "baselines"
RENDERS = TESTS / "_renders"
UPDATE = os.environ.get("NEON_UPDATE_BASELINES") == "1"
# Baselines belong to the machine they were drawn on (fonts, scaling). CI sets this to keep the clipped-text
# audit but skip the pixel comparison.
SKIP_SCREENSHOTS = os.environ.get("NEON_SKIP_SCREENSHOTS") == "1"
PIXEL_TOLERANCE = 24          # per-channel difference that counts as "different" (anti-aliasing noise is below it)
MAX_DIFFERENT = 0.003         # fraction of pixels allowed to differ (a blinking caret, a hover tint)


def _describe(widget: QWidget) -> str:
    name = widget.objectName() or type(widget).__name__
    text = getattr(widget, "text", None)
    shown = text() if callable(text) else ""
    return f"{type(widget).__name__}({name!r}{', ' + repr(shown[:40]) if shown else ''})"


def clip_problems(root: QWidget) -> list[str]:
    """Visible widgets under `root` that can't show all of their text or content."""
    problems = []
    for w in root.findChildren(QWidget):
        if not w.isVisible() or w.width() <= 1 or w.height() <= 1:
            continue
        if isinstance(w, QLabel):
            if not (w.text() or "").strip() or w.pixmap() is not None and not w.text():
                continue
            if w.wordWrap():
                need = w.heightForWidth(w.width())
                if need > 0 and w.height() + 2 < need:
                    problems.append(f"{_describe(w)} is {w.height()} px tall but its wrapped text needs {need}")
            else:
                hint = w.minimumSizeHint()
                if w.width() + 1 < hint.width():
                    problems.append(f"{_describe(w)} is {w.width()} px wide but its text needs {hint.width()}")
                if w.height() + 1 < hint.height():
                    problems.append(f"{_describe(w)} is {w.height()} px tall but its text needs {hint.height()}")
        elif isinstance(w, QAbstractButton):
            # Judged by the text itself: a one-glyph icon button (the gear) is padded by the style sheet's
            # minimum size, but nothing is cut off.
            text = w.text().replace("&", "")
            if text.strip():
                indicator = 22 if isinstance(w, (QCheckBox, QRadioButton)) else 8
                need = w.fontMetrics().horizontalAdvance(text) + indicator
                if w.width() < need:
                    problems.append(f"{_describe(w)} is {w.width()} px wide but its text needs about {need}")
        elif isinstance(w, (QComboBox, QSpinBox, QDoubleSpinBox)):
            hint = w.minimumSizeHint()
            if w.width() + 1 < hint.width() or w.height() + 1 < hint.height():
                problems.append(f"{_describe(w)} is {w.width()}x{w.height()} but needs at least "
                                f"{hint.width()}x{hint.height()}")
    return problems


def _pixels(image: QImage) -> np.ndarray:
    image = image.convertToFormat(QImage.Format_RGBA8888)
    raw = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
    return raw[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def _diff(actual: QImage, expected: QImage) -> tuple[float, QImage]:
    """(fraction of pixels that differ, an image with the differences in red)."""
    a, b = _pixels(actual), _pixels(expected)
    different = (np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_TOLERANCE).any(axis=2)
    marked = b.copy()
    marked[different] = (255, 0, 0, 255)
    out = QImage(marked.data, marked.shape[1], marked.shape[0], marked.shape[1] * 4, QImage.Format_RGBA8888).copy()
    return float(different.mean()), out


def check_screenshot(name: str, widget: QWidget) -> str | None:
    """None if `widget` looks like its baseline (or the baseline was just written / updated);
    otherwise a message saying how it differs."""
    if SKIP_SCREENSHOTS:
        return None
    image = widget.grab().toImage()
    BASELINES.mkdir(exist_ok=True)
    path = BASELINES / f"{name}.png"
    if UPDATE or not path.exists():
        image.save(str(path))
        return None
    expected = QImage(str(path))
    if image.size() != expected.size():
        RENDERS.mkdir(exist_ok=True)
        image.save(str(RENDERS / f"{name}.actual.png"))
        return (f"{name}: is {image.width()}x{image.height()}, the baseline is "
                f"{expected.width()}x{expected.height()}")
    fraction, marked = _diff(image, expected)
    if fraction > MAX_DIFFERENT:
        RENDERS.mkdir(exist_ok=True)
        image.save(str(RENDERS / f"{name}.actual.png"))
        marked.save(str(RENDERS / f"{name}.diff.png"))
        return f"{name}: {fraction:.2%} of the pixels differ (see tests/_renders/{name}.diff.png)"
    return None
