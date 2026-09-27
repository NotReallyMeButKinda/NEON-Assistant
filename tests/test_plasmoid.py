"""The KDE Plasma panel widget (plasmoid/) against the real panel feed (panel_feed.py), over real HTTP on
127.0.0.1. The widget's own QML runs, with small stand-ins for Plasma's modules (tests/plasma_harness.py)."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from tests import common  # noqa: F401  (the offscreen Qt application)
from tests.common import pump_until
from tests import plasma_harness as harness
import panel_feed


def click(item, button=Qt.LeftButton):
    """A press and release in the middle of a MouseArea, delivered straight to it."""
    center = QPointF(item.width() / 2, item.height() / 2)
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        event = QMouseEvent(kind, center, center, button, button if kind == QMouseEvent.Type.MouseButtonPress
                            else Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(item, event)


class FeedTests(unittest.TestCase):
    def test_a_streamed_reply_grows_one_line(self):
        feed = panel_feed.Feed()
        feed.caption("You", "what time is it")
        feed.caption("Nova", "It's")
        feed.caption("Nova", "It's 3 pm.")
        self.assertEqual(feed.data["history"], [{"sender": "You", "text": "what time is it"},
                                                {"sender": "Nova", "text": "It's 3 pm."}])
        for n in range(20):
            feed.caption("You", f"line {n}")
        self.assertEqual(len(feed.data["history"]), panel_feed.HISTORY)

    def test_long_poll_waits_for_news(self):
        feed = panel_feed.Feed()
        seq = feed.snapshot()["seq"]
        started = time.monotonic()
        self.assertEqual(feed.snapshot(seq, wait=0.3)["seq"], seq)           # nothing new: waited, then answered
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        threading.Timer(0.1, lambda: feed.update(state="listening")).start()
        snap = feed.snapshot(seq, wait=5)
        self.assertEqual((snap["seq"], snap["state"]), (seq + 1, "listening"))

    def test_voice_level_is_throttled(self):
        feed = panel_feed.Feed()
        before = feed.seq
        for n in range(50):
            feed.level((n % 10) / 10)
        self.assertLess(feed.seq - before, 5)


class ServerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.server = panel_feed.PanelServer(panel_feed.Feed(), self.commands.append)
        self.assertTrue(self.server.start(0))
        self.url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()

    def post(self, body, headers=None):
        request = urllib.request.Request(self.url + "/command", data=body.encode(), method="POST",
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_commands_need_the_widget_header(self):
        self.assertEqual(self.post("talk"), 403)                              # a web page can't add the header
        self.assertEqual(self.post("talk", {"X-Neon-Widget": "1"}), 200)
        self.assertEqual(self.post("format c:", {"X-Neon-Widget": "1"}), 400)
        self.assertEqual(self.commands, ["talk"])

    def test_other_host_names_are_refused(self):
        request = urllib.request.Request(self.url + "/status", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_status_is_json(self):
        with urllib.request.urlopen(self.url + "/status", timeout=5) as reply:
            data = json.loads(reply.read())
        self.assertEqual(data["state"], "idle")
        self.assertNotIn("Access-Control-Allow-Origin", reply.headers)


class ControllerFeedTests(unittest.TestCase):
    def test_the_controller_feeds_it_and_commands_come_back_on_the_ui_thread(self):
        from controller import Assistant
        common.use_temp_config()
        ctl = Assistant()
        ran = []
        server = panel_feed.connect(ctl, lambda name: ran.append((name, threading.current_thread() is
                                                                  threading.main_thread())), 0)
        self.assertIsNotNone(server)
        try:
            ctl.state.emit("listening")
            ctl.caption.emit("You", "hello there")
            snap = server.feed.snapshot()
            self.assertEqual((snap["state"], snap["text"], snap["name"]), ("listening", "hello there", "Nova"))
            self.assertIn("listening", snap["colors"])
            request = urllib.request.Request(f"http://127.0.0.1:{server.port}/command", data=b"wake", method="POST",
                                             headers={"X-Neon-Widget": "1"})
            urllib.request.urlopen(request, timeout=5).close()
            self.assertTrue(pump_until(lambda: ran, 5))
            self.assertEqual(ran, [("wake", True)])
        finally:
            server.stop()
        before = server.feed.seq
        ctl.state.emit("thinking")                                             # unhooked once stopped
        self.assertEqual(server.feed.seq, before)


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.feed = panel_feed.Feed()
        self.feed.update(name="Nova", wake=True, colors={"listening": "#00ff88", "idle": "#8888ff"})
        self.commands: list[str] = []
        self.server = panel_feed.PanelServer(self.feed, self.commands.append)
        self.assertTrue(self.server.start(0))
        self.widget = harness.Widget(port=self.server.port, launchCommand="/opt/neon/run --minimized")
        self.root, self.compact = self.widget.root, self.widget.compact
        self.label = self.widget.find(self.compact, "captionLabel")
        self.compact.setWidth(300)
        self.compact.setHeight(24)
        self.assertTrue(pump_until(lambda: self.root.property("online"), 10), self.widget.errors)

    def tearDown(self):
        self.widget.close()
        self.server.stop()

    def caption(self):
        return self.label.property("text")

    def test_it_follows_neon(self):
        self.assertEqual(self.caption(), 'Say "hey nova"')
        self.feed.update(state="listening")
        self.assertTrue(pump_until(lambda: self.caption() == "Listening", 5), self.caption())
        orb = self.widget.find(self.compact, "panelOrb")
        self.assertEqual(orb.property("tint").name(), "#00ff88")               # NEON's own theme colour
        self.assertGreater(orb.property("pulseMs"), 0)
        self.feed.caption("You", "what time is it")
        self.assertTrue(pump_until(lambda: self.caption() == "what time is it", 5), self.caption())
        self.feed.update(state="speaking")
        self.feed.caption("Nova", "It's 3 pm.")
        self.assertTrue(pump_until(lambda: self.caption() == "It's 3 pm.", 5), self.caption())
        self.assertIn("It's 3 pm.", self.root.property("toolTipSubText"))
        self.assertEqual(self.widget.errors, [])

    def test_clicks_and_the_menu_drive_neon(self):
        click(self.compact)
        self.assertTrue(pump_until(lambda: self.commands == ["talk"], 5), self.commands)
        click(self.compact, Qt.MiddleButton)
        self.assertTrue(pump_until(lambda: self.commands[-1:] == ["mute"], 5), self.commands)
        actions = {a.property("text"): a for a in self.widget.attached().actions()}
        self.assertIn("Listen for the wake word", actions)
        self.assertTrue(actions["Listen for the wake word"].property("checked"))
        QMetaObject.invokeMethod(actions["Type a command…"], "trigger")
        self.assertTrue(pump_until(lambda: self.commands[-1:] == ["quick"], 5), self.commands)
        QMetaObject.invokeMethod(actions["Show recent lines"], "trigger")
        self.assertTrue(self.root.property("expanded"))

    def test_the_popup_shows_recent_lines_and_buttons(self):
        self.feed.caption("You", "<b>hi</b> & bye")
        self.feed.caption("Nova", "Hello!")
        history = self.widget.find(self.widget.full, "history")
        self.assertTrue(pump_until(lambda: history.property("count") == 2, 5))
        buttons = self.widget.find(self.widget.full, "buttons")
        mute = next(b for b in buttons.childItems() if b.property("text") == "Mute")
        QMetaObject.invokeMethod(mute, "click")
        self.assertTrue(pump_until(lambda: self.commands[-1:] == ["mute"], 5), self.commands)
        self.assertFalse(mute.property("checked"))                             # follows NEON, not the click
        self.feed.update(muted=True)
        self.assertTrue(pump_until(lambda: mute.property("checked"), 5))
        self.assertEqual(self.root.property("neonState"), "muted")

    def test_offline_then_back(self):
        port = self.server.port
        self.server.stop()
        self.assertTrue(pump_until(lambda: not self.root.property("online"), 10))
        self.assertEqual(self.caption(), "NEON isn't running")
        click(self.compact)                                                    # starts NEON instead
        launcher = self.widget.find(self.root, "launcher")
        self.assertEqual(launcher.property("ran").toVariant(), ["/opt/neon/run --minimized"])
        self.server = panel_feed.PanelServer(self.feed, self.commands.append)
        self.assertTrue(self.server.start(port))
        self.assertTrue(pump_until(lambda: self.root.property("online"), 10))

    def test_its_settings_page(self):
        page = harness.load_page("contents/ui/configGeneral.qml")
        defaults = harness.default_configuration()
        for key, value in defaults.items():                                    # Plasma fills cfg_* in...
            page.setProperty(f"cfg_{key}", value)
        page.setProperty("cfg_clickAction", "popup")
        combo = next(c for c in page.findChildren(QObject) if c.property("valueRole") == "value")
        self.assertEqual(combo.property("currentValue"), "popup")
        combo.setProperty("currentIndex", 0)
        combo.activated.emit(0)                                                 # ...the user picks...
        self.assertEqual(page.property("cfg_clickAction"), "talk")              # ...and it reads them back
        page.setProperty("cfg_port", 47900)
        self.assertEqual(page.property("cfg_port"), 47900)
        model = harness.load_page("contents/config/config.qml")
        sources = [c.property("source") for c in model.findChildren(QObject) if c.property("source")]
        self.assertEqual(sources, ["configGeneral.qml"])

    def test_the_package_is_complete(self):
        widget = harness.WIDGET
        meta = json.loads((widget / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["KPlugin"]["Id"], widget.name)
        self.assertEqual(meta["KPackageStructure"], "Plasma/Applet")
        for part in ("contents/ui/main.qml", "contents/ui/configGeneral.qml", "contents/config/main.xml",
                     "contents/config/config.qml"):
            self.assertTrue((widget / part).is_file(), part)
        config_page = (widget / "contents/ui/configGeneral.qml").read_text(encoding="utf-8")
        for key in harness.default_configuration():
            self.assertIn(f"cfg_{key}", config_page)                           # every setting has a control


if __name__ == "__main__":
    unittest.main()
