"""The Wikipedia pop-up: the article a search found, at the chosen edge, closed from its outer corner."""

import unittest

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QPixmap

from tests import common
from tests.common import pump, pump_until
from tests.render_utils import check_screenshot, clip_problems

import assistant as backend  # noqa: E402
from ui import theme  # noqa: E402
from ui import wiki_popup  # noqa: E402

ARTICLE = {"title": "Geometry Dash", "url": "https://en.wikipedia.org/wiki/Geometry_Dash",
           "image": "https://upload.wikimedia.org/gd.png",
           "extract": "Geometry Dash is a 2013 rhythm video game developed by Swedish game developer Robert Topala "
                      "and published by his company RobTop Games. It was released for iOS and Android in August "
                      "2013, Windows Phone in June 2014, and Steam in December 2014.\nThe player controls an icon "
                      "and must navigate through levels while avoiding obstacles."}


class FakeController(QObject):
    wiki_article = Signal(dict)


def png_bytes() -> bytes:
    pixmap = QPixmap(600, 340)
    pixmap.fill(QColor("#3a7bd5"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(data)


class WikiPopupTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        theme.apply_theme("ember", "", None)
        backend.CONFIG.update(bar_animate=False, wiki_popup=True)
        self.fetched = []
        self._real_fetch = wiki_popup.fetch_image
        wiki_popup.fetch_image = lambda url: self.fetched.append(url) or png_bytes()    # never the network
        self.ctl = FakeController()
        self.popup = wiki_popup.WikiPopup(self.ctl)

    def tearDown(self):
        self.popup.hide()
        wiki_popup.fetch_image = self._real_fetch

    def show(self, article=ARTICLE):
        self.ctl.wiki_article.emit(article)
        pump(50)

    def test_shows_the_article_and_its_picture(self):
        self.show()
        self.assertTrue(self.popup.isVisible())
        self.assertEqual(self.popup.title.text(), "Geometry Dash")
        self.assertIn("RobTop Games", self.popup.text.text())
        self.assertTrue(pump_until(lambda: self.popup.image.isVisible(), 3))
        self.assertEqual(self.fetched, [ARTICLE["image"]])

    def test_the_close_button_is_on_the_chosen_side(self):
        for side, on_right in (("right", True), ("left", False)):
            backend.CONFIG["wiki_popup_side"] = side
            self.show()
            button = self.popup.close_btn.geometry().center().x()
            self.assertEqual(button > self.popup.width() / 2, on_right, side)
            area = self.popup._screen().availableGeometry()
            expected = (area.right() - wiki_popup.CARD_WIDTH - wiki_popup.MARGIN if on_right
                        else area.x() + wiki_popup.MARGIN)
            self.assertEqual(self.popup.x(), expected, side)
            self.popup.close_btn.click()
            self.assertFalse(self.popup.isVisible())

    def test_picture_and_text_are_optional(self):
        backend.CONFIG.update(wiki_popup_image=False, wiki_popup_text=False)
        self.show()
        pump(100)
        self.assertEqual(self.fetched, [])
        self.assertFalse(self.popup.image.isVisible())
        self.assertFalse(self.popup.scroll.isVisible())

    def test_a_late_picture_from_an_older_article_is_ignored(self):
        self.show()
        self.popup._image_url = "https://upload.wikimedia.org/newer.png"
        self.popup._got_image(ARTICLE["image"], png_bytes())
        self.assertIsNone(self.popup._pixmap if not self.popup.image.isVisible() else None)

    def test_its_look(self):
        self.show()
        pump_until(lambda: self.popup.image.isVisible(), 3)
        pump(60)
        self.assertEqual(clip_problems(self.popup), [])
        self.assertIsNone(check_screenshot("wiki-popup", self.popup))


if __name__ == "__main__":
    unittest.main()
