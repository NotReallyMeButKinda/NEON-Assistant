"""The Wikipedia pop-up: when a search finds an article, its picture and opening section appear in a
small card at the left or right edge of the screen (Settings > AI & chat). No title bar; the close
button sits in the top corner on the same side as the card, so it's the corner nearest the edge.

It never takes focus from what you're doing, and a new search replaces what it shows.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

import assistant as backend
import websearch

from . import wayland_place
from .motion import animations_enabled
from .status_bar import list_monitors
from .theme import COLORS
from .theme import signals as theme_signals

CARD_WIDTH = 400
MARGIN = 20                 # from the screen's edge
IMAGE_MAX_HEIGHT = 260
FADE_MS = 180
RADIUS = 16


def fetch_image(url: str) -> bytes | None:
    """The article's picture (Wikimedia asks for an identifying User-Agent, which websearch sends)."""
    return websearch._get(url, timeout=8.0) if url.startswith("https://") else None


class WikiPopup(QWidget):
    image_ready = Signal(str, bytes)          # (url, data), from the download thread

    def __init__(self, controller):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(CARD_WIDTH)
        self._article: dict = {}
        self._image_url = ""
        self._pixmap: QPixmap | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 18)
        outer.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("wikiTitle")
        self.title.setWordWrap(True)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("wikiClose")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("Close")
        self.close_btn.setAccessibleName("Close the article")
        self.close_btn.clicked.connect(self.close_popup)
        outer.addLayout(self.header)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.hide()
        outer.addWidget(self.image)

        self.text = QLabel()
        self.text.setObjectName("wikiText")
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.text)
        self.scroll.viewport().setAutoFillBackground(False)
        self.text.setAutoFillBackground(False)             # setWidget turns it on
        outer.addWidget(self.scroll, 1)

        self.link = QLabel()
        self.link.setObjectName("wikiLink")
        self.link.setTextFormat(Qt.RichText)
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.linkActivated.connect(lambda _href: self._open_article())
        outer.addWidget(self.link, 0, Qt.AlignLeft)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        self.image_ready.connect(self._got_image)
        controller.wiki_article.connect(self.show_article)
        theme_signals.changed.connect(self._restyle)
        self._restyle()
        wayland_place.prepare(self, "NEON Wikipedia card")

    # ---- content ---------------------------------------------------------------------------
    def show_article(self, article: dict) -> None:
        self._article = dict(article or {})
        if not self._article.get("title"):
            return
        self._arrange_header()
        self.title.setText(self._article["title"])
        want_text = backend.cfg_bool("wiki_popup_text")
        self.text.setText(str(self._article.get("extract") or "").replace("\n", "\n\n") if want_text else "")
        self.scroll.setVisible(want_text)
        self.link.setVisible(bool(self._article.get("url")))
        self._pixmap = None
        self.image.clear()
        self.image.hide()
        self._image_url = str(self._article.get("image") or "") if backend.cfg_bool("wiki_popup_image") else ""
        if self._image_url:
            url = self._image_url
            threading.Thread(target=lambda: self._download(url), name="Nova-WikiImage", daemon=True).start()
        self._place()
        wayland_place.settle(self)
        if not self.isVisible():
            self.setWindowOpacity(0.0 if animations_enabled() else 1.0)
            self.show()
            if animations_enabled():
                self._fade.stop()
                self._fade.setStartValue(0.0)
                self._fade.setEndValue(1.0)
                self._fade.start()
        self.raise_()

    def _arrange_header(self) -> None:
        """The close button in the corner on the card's side of the screen."""
        while self.header.count():
            self.header.takeAt(0)
        if self._side() == "left":
            self.header.addWidget(self.close_btn, 0, Qt.AlignTop)
            self.header.addWidget(self.title, 1)
        else:
            self.header.addWidget(self.title, 1)
            self.header.addWidget(self.close_btn, 0, Qt.AlignTop)

    def _download(self, url: str) -> None:
        data = fetch_image(url)
        if data:
            try:
                self.image_ready.emit(url, data)
            except RuntimeError:
                pass                                       # closed at exit

    def _got_image(self, url: str, data: bytes) -> None:
        if url != self._image_url:
            return                                         # a newer article is showing
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        width = CARD_WIDTH - 36
        scaled = pixmap.scaled(width, IMAGE_MAX_HEIGHT, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._pixmap = scaled
        self.image.setPixmap(_rounded(scaled, 10))
        self.image.show()
        self._place()

    def _open_article(self) -> None:
        url = str(self._article.get("url") or "")
        if url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(url))

    def close_popup(self) -> None:
        self._image_url = ""
        self.hide()

    # ---- where it sits -------------------------------------------------------------------
    @staticmethod
    def _side() -> str:
        return "left" if str(backend.CONFIG.get("wiki_popup_side", "right")) == "left" else "right"

    @staticmethod
    def _screen():
        """The status bar's monitor (matched by its top-left corner, which Qt keeps physical)."""
        monitors = list_monitors()
        if monitors:
            m = monitors[min(len(monitors) - 1, max(0, int(backend.cfg_num("bar_monitor"))))]
            for screen in QGuiApplication.screens():
                if (screen.geometry().x(), screen.geometry().y()) == (m[0], m[1]):
                    return screen
        return QGuiApplication.primaryScreen()

    def _place(self) -> None:
        area = self._screen().availableGeometry()
        self.layout().invalidate()
        self.layout().activate()
        text_height = self.text.heightForWidth(CARD_WIDTH - 60) if self.scroll.isVisible() else 0
        fixed = self.sizeHint().height() - (self.scroll.sizeHint().height() if self.scroll.isVisible() else 0)
        height = min(int(area.height() * 0.75), fixed + max(0, text_height) + 8)
        self.resize(CARD_WIDTH, max(120, height))
        x = area.x() + MARGIN if self._side() == "left" else area.right() - CARD_WIDTH - MARGIN
        self.move(x, area.y() + int(area.height() * 0.12))

    # ---- look ------------------------------------------------------------------------------
    def _restyle(self) -> None:
        c = COLORS
        self.setStyleSheet(f"""
            QLabel#wikiTitle {{ color: {c['accent']}; font-size: 20px; font-weight: 800; background: transparent; }}
            QLabel#wikiText {{ color: {c['text']}; font-size: 14px; background: transparent; }}
            QPushButton#wikiClose {{ background: {c['panel_alt']}; color: {c['text']}; border: 1px solid {c['border']};
                                     border-radius: 15px; font-size: 13px; padding: 0; }}
            QPushButton#wikiClose:hover {{ background: {c['accent']}; color: {c['on_accent']}; }}
            QLabel#wikiLink {{ background: transparent; }}
            QScrollArea {{ background: transparent; }}
        """)
        self.link.setText(f'<a href="open" style="color: {c["accent_bright"]}; text-decoration: none;">'
                          "Read more on Wikipedia</a>")
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS, RADIUS)
        fill = QColor(COLORS["panel"])
        fill.setAlpha(245)
        p.fillPath(path, fill)
        p.setPen(QColor(COLORS["accent"]))
        p.drawPath(path)
        p.end()


def _rounded(pixmap: QPixmap, radius: float) -> QPixmap:
    out = QPixmap(pixmap.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(out.rect()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return out
