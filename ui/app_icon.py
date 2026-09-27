"""
app_icon.py -- NEON's glowing dot (the orb): the tray icon, the window icon, the .exe icon and the
browser extension's icon all come from paint_orb(), so they always look the same.

    python -m ui.app_icon        -> rewrites icons/neon.ico, icons/neon.png and browser_extension/icons/neon-*.png
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient

ROOT = Path(__file__).resolve().parent.parent
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
EXTENSION_SIZES = (16, 32, 48, 96)


def paint_orb(size: int, colors: dict) -> QPixmap:
    """A soft glow, a dark disc with an accent ring, a bright dot in the middle."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c, r = size / 2, size / 2
    glow = QRadialGradient(c, c, r)
    edge = QColor(colors["accent"])
    edge.setAlpha(150)
    glow.setColorAt(0.45, edge)
    edge.setAlpha(0)
    glow.setColorAt(1.0, edge)
    p.setPen(Qt.NoPen)
    p.setBrush(glow)
    p.drawEllipse(QRectF(0, 0, size, size))
    ring = r * 0.62
    p.setBrush(QColor(colors["bg"]))
    p.setPen(QPen(QColor(colors["accent"]), max(1.2, size * 0.07)))
    p.drawEllipse(QRectF(c - ring, c - ring, 2 * ring, 2 * ring))
    dot = r * 0.3
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(colors["accent_bright"]))
    p.drawEllipse(QRectF(c - dot, c - dot, 2 * dot, 2 * dot))
    p.end()
    return pm


def make_icon(colors: dict) -> QIcon:
    """The orb at several sizes, so the taskbar and the tray both get a crisp one."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        icon.addPixmap(paint_orb(size, colors))
    return icon


def _png_bytes(pm: QPixmap) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    pm.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path, colors: dict) -> Path:
    """A Windows .ico holding PNG-compressed images at every size Explorer asks for."""
    images = [(size, _png_bytes(paint_orb(size, colors))) for size in ICO_SIZES]
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    for size, png in images:
        dim = 0 if size >= 256 else size             # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + b"".join(png for _, png in images))
    return path


def write_extension_icons(folder: Path, colors: dict) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for size in EXTENSION_SIZES:
        target = folder / f"neon-{size}.png"
        paint_orb(size, colors).save(str(target), "PNG")
        written.append(target)
    return written


def default_colors() -> dict:
    """The default (Neon) theme: the icons on disk can't follow the user's theme, so they use this."""
    from ui.theme import THEMES
    return THEMES["neon"]


def write_all() -> list[Path]:
    import os
    from PySide6.QtWidgets import QApplication
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # nothing is shown
    app = QApplication.instance() or QApplication([])   # QPixmap needs one  # noqa: F841
    colors = default_colors()
    png = ROOT / "icons" / "neon.png"                   # Linux .desktop entries and the Arch package
    paint_orb(256, colors).save(str(png), "PNG")
    return [write_ico(ROOT / "icons" / "neon.ico", colors), png,
            *write_extension_icons(ROOT / "browser_extension" / "icons", colors)]


if __name__ == "__main__":
    for written in write_all():
        print(f"Wrote {written}")
