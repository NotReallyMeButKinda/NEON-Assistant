"""Custom wake words, memory expiry and editing, scheduled routines, plugin hooks, persona voices,
the noise filter, high contrast, and the status bar's media buttons / menu / opaque notification card."""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer, Qt

import memory_store
import notifications
import persona
import plugins
import routines
import settings_schema
import stt
import wakeword
import controller as controller_module
from controller import Assistant
from tests import common
from tests.common import backend, pump, pump_until
from ui import status_bar as sb
from ui import theme

sb.StatusBar._register_appbar = lambda self: None          # never reserve real screen space in a test


# ---------------------------------------------------------------------------
# Wake words
# ---------------------------------------------------------------------------

class ScoreModel:
    """Scores each frame by its first sample / 1000."""

    def predict(self, frame):
        return {"x": float(frame[0]) / 1000.0}

    def reset(self):
        pass


def _frames(*scores):
    return np.concatenate([np.full(wakeword.FRAME, int(s * 1000), dtype=np.int16) for s in scores])


class CustomWakeWordTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_presets_are_offered_and_names_are_slugged(self):
        models = {key: (phrase, kind) for key, phrase, kind in wakeword.list_models(self.dir)}
        self.assertEqual(models["custom:hey_nova"], ("hey nova", "preset"))
        self.assertEqual(models["custom:hey_dan"], ("hey dan", "preset"))
        self.assertEqual(models["hey_jarvis"][1], "builtin")
        self.assertEqual(wakeword.slug("Hey, Nova!"), "hey_nova")
        self.assertEqual(wakeword.phrase_of("custom:hey_dan"), "hey dan")

    def test_a_trained_model_is_listed_described_and_deletable(self):
        weights = {"w1": np.zeros((wakeword.WINDOW * 96, 2), np.float32), "b1": np.zeros(2, np.float32),
                   "w2": np.zeros(2, np.float32), "b2": np.zeros(1, np.float32),
                   "mean": np.zeros(wakeword.WINDOW * 96, np.float32), "scale": np.ones(wakeword.WINDOW * 96, np.float32)}
        path = wakeword.custom_path("custom:open_sesame", self.dir)
        path.parent.mkdir(parents=True)
        np.savez(path, meta=np.array(json.dumps({"phrase": "open sesame", "recordings": 3})), **weights)
        listed = [m for m in wakeword.list_models(self.dir) if m[0] == "custom:open_sesame"]
        self.assertEqual(listed, [("custom:open_sesame", "open sesame", "custom")])
        self.assertEqual(wakeword.custom_info("custom:open_sesame", self.dir)["recordings"], 3)
        score = wakeword.Classifier(weights).score(np.zeros(wakeword.WINDOW * 96))
        self.assertAlmostEqual(float(score[0]), 0.5)                     # all-zero network: exactly undecided
        self.assertTrue(wakeword.delete_custom("custom:open_sesame", self.dir))
        self.assertFalse(wakeword.trained("custom:open_sesame", self.dir))

    def test_a_custom_model_needs_two_sure_frames_in_a_row(self):
        det = wakeword.Detector("custom:hey_nova", threshold=lambda: 0.5, loader=ScoreModel)
        det.load()
        self.assertFalse(det.feed(_frames(0.9, 0.1, 0.9, 0.1)))            # isolated flukes
        self.assertTrue(det.feed(_frames(0.9, 0.9)))
        builtin = wakeword.Detector("hey_jarvis", threshold=lambda: 0.5, loader=ScoreModel)
        builtin.load()
        self.assertTrue(builtin.feed(_frames(0.9)))                        # openWakeWord's own: one frame

    def test_an_untrained_custom_phrase_that_isnt_a_preset_fails_clearly(self):
        det = wakeword.Detector("custom:open_sesame", directory=self.dir)
        for name in ("melspectrogram.onnx", "embedding_model.onnx"):      # no download in a test
            (self.dir / name).write_bytes(b"")
        with self.assertRaisesRegex(FileNotFoundError, "hasn't been trained"):
            det.load()

    def test_the_setting_accepts_custom_models_and_rejects_junk(self):
        clean, problems = settings_schema.sanitize({"wake_model": "custom:hey_nova"}, backend.DEFAULT_CONFIG)
        self.assertEqual((clean["wake_model"], problems), ("custom:hey_nova", []))
        clean, problems = settings_schema.sanitize({"wake_model": "custom:../../evil"}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["wake_model"], backend.DEFAULT_CONFIG["wake_model"])
        self.assertTrue(problems)

    def test_retraining_changes_the_signature_so_the_loop_reloads(self):
        cfg = {"wake_engine": "openwakeword", "wake_model": "custom:hey_nova"}
        self.assertEqual(wakeword.signature(cfg)[:2], ("openwakeword", "custom:hey_nova"))

    def test_training_helpers(self):
        import wake_training
        near = wake_training.confusables("hey nova")
        self.assertIn("nova", near)
        self.assertIn("hey", near)
        self.assertIn("hey now", near)
        self.assertNotIn("hey nova", near)
        speech = np.concatenate([np.zeros(8000), np.full(4000, 3000.0), np.zeros(8000)])
        trimmed = wake_training.trim(speech)
        self.assertTrue(4000 <= len(trimmed) < 7000, len(trimmed))      # the speech plus ~0.14 s either side
        self.assertGreater(len(wake_training.shift_pitch(np.ones(1000), 0.8)), 1000)   # lower = longer
        with self.assertRaises(wake_training.TrainingError):
            wake_training.train("x", directory=self.dir)                  # too short to be a phrase


class NoiseFilterTests(unittest.TestCase):
    def test_steady_noise_is_skipped_and_speech_like_bursts_are_not(self):
        rng = np.random.default_rng(0)
        fan = rng.normal(0, 400, 32000).astype(np.int16).tobytes()
        bursts = np.concatenate([rng.normal(0, a, 1600) for a in (50, 3000, 200, 2500, 80, 3500) * 3])
        self.assertTrue(stt.looks_like_steady_noise(fan))
        self.assertFalse(stt.looks_like_steady_noise(bursts.astype(np.int16).tobytes()))

    def test_the_wake_loop_never_transcribes_steady_noise(self):
        transcriber = stt.Transcriber({"stt_engine": "google", "wake_skip_noise": True})
        transcriber._google_text = lambda pcm: self.fail("sent noise to speech recognition")
        fan = np.random.default_rng(1).normal(0, 400, 32000).astype(np.int16).tobytes()
        self.assertIsNone(transcriber.transcribe(fan, role="wake"))


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryExpiryTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_expiry_phrases(self):
        cases = {"remember until friday that my locker is 12": ("locker", "12"),
                 "remember for 2 hours that I parked on level 3": ("", "I parked on level 3"),
                 "remember that I parked on level B4 just for today": ("", "I parked on level B4"),
                 "remember my gate code is 4821 until tomorrow": ("gate code", "4821")}
        for text, (key, value) in cases.items():
            command, expires = memory_store._parse_memory_command(text)
            self.assertEqual(command, ("remember", key, value), text)
            self.assertGreater(expires, time.time(), text)

    def test_a_trailing_duration_that_is_part_of_the_fact_is_kept(self):
        command, expires = memory_store._parse_memory_command("remember that the meeting is for two hours")
        self.assertEqual((command, expires), (("remember", "meeting", "for two hours"), 0.0))

    def test_expiry_times(self):
        now = datetime(2026, 9, 23, 19, 30)                  # a Wednesday evening
        at = lambda how, when: datetime.fromtimestamp(memory_store.expiry_time(how, when, now))  # noqa: E731
        self.assertEqual(at("for", "2 hours"), datetime(2026, 9, 23, 21, 30))
        self.assertEqual(at("until", "friday"), datetime(2026, 9, 25, 23, 59, 59))
        self.assertEqual(at("until", "5 pm"), datetime(2026, 9, 24, 17, 0))            # already past: tomorrow
        self.assertEqual(at("for", "today"), datetime(2026, 9, 23, 23, 59, 59))
        self.assertEqual(memory_store.expiry_time("until", "lunchtime", now), 0.0)

    def test_expired_memories_are_forgotten_and_the_reply_says_until_when(self):
        reply = memory_store.handle_memory_command("remember for 2 hours that I parked on level 3")
        self.assertIn("until", reply)
        memory_store.remember("old spot", "B2", expires=time.time() - 1)
        self.assertEqual([m["value"] for m in memory_store.all_memories()], ["I parked on level 3"])
        self.assertEqual(memory_store.search("old spot"), [])

    def test_edit_and_forget_by_id(self):
        a = memory_store.remember("wifi password", "hunter2")
        b = memory_store.remember("wifi name", "Home")
        self.assertTrue(memory_store.update(a["id"], value="hunter3"))
        self.assertTrue(memory_store.forget_id(b["id"]))
        self.assertEqual([(m["key"], m["value"]) for m in memory_store.all_memories()], [("wifi password", "hunter3")])

    def test_meaning_based_recall_when_words_dont_match(self):
        memory_store.remember("wifi password", "hunter2")
        memory_store.remember("gate code", "4821")
        vectors = {"Your wifi password is hunter2.": [1.0, 0.0], "Your gate code is 4821.": [0.0, 1.0],
                   "how do i get online": [0.9, 0.1]}
        memory_store.set_embedder(lambda texts: [vectors.get(t, [0.5, 0.5]) for t in texts], "fake")
        try:
            self.assertEqual(memory_store.search("wifi password")[0]["value"], "hunter2")       # words still first
            hits = memory_store.search("how do i get online")
            self.assertEqual([h["value"] for h in hits], ["hunter2"])
            self.assertNotIn("vec", hits[0])
            memory_store.set_embedder(lambda texts: None, "down")
            self.assertEqual(memory_store.search("how do i get online"), [])                 # unavailable: no guess
        finally:
            memory_store.set_embedder(None)

    def test_the_browser_edits_forgets_and_sets_expiry(self):
        from ui.settings_widgets import MemoryList
        memory_store.remember("wifi password", "hunter2")
        memory_store.remember("gate code", "4821")
        browser = MemoryList(memory_store)
        self.assertEqual(browser.table.rowCount(), 2)
        self.assertEqual(browser.table.item(0, 0).text(), "gate code")                   # newest first
        browser.table.item(0, 1).setText("1234")
        self.assertEqual(memory_store.search("gate code")[0]["value"], "1234")
        browser.table.item(0, 3).setText("2 hours")
        self.assertGreater([m for m in memory_store.all_memories() if m["key"] == "gate code"][0]["expires"], time.time())
        browser.table.selectRow(1)
        browser._forget_selected()
        self.assertEqual([m["key"] for m in memory_store.all_memories()], ["gate code"])


# ---------------------------------------------------------------------------
# Routines on a schedule
# ---------------------------------------------------------------------------

class RoutineTriggerTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(routines.parse_trigger("weekdays at 9:00"),
                         {"kind": "time", "days": {0, 1, 2, 3, 4}, "hour": 9, "minute": 0})
        self.assertEqual(routines.parse_trigger("mon, wed and fri at 8pm")["days"], {0, 2, 4})
        self.assertEqual(routines.parse_trigger("every day at nine pm")["hour"], 21)
        self.assertEqual(routines.parse_trigger("when Discord starts"), {"kind": "app", "app": "discord"})
        self.assertEqual(routines.parse_trigger("at startup"), {"kind": "startup"})
        self.assertIsNone(routines.parse_trigger("sometime soonish"))
        self.assertEqual(routines.describe_trigger(routines.parse_trigger("weekends at 10")), "on weekends at 10:00")

    def test_the_scheduler_runs_each_routine_once_when_due(self):
        clock = {"now": datetime(2026, 9, 23, 8, 59)}                       # a Wednesday
        apps = {"now": {"explorer"}}
        config = {"routines": [{"name": "work mode", "steps": ["say: hi"], "when": "weekdays at 9:00"},
                               {"name": "game night", "steps": ["say: gg"], "when": "when discord starts"},
                               {"name": "hello", "steps": ["say: hello"], "when": "at startup"},
                               {"name": "manual", "steps": ["say: only by voice"]}]}
        ran = []
        scheduler = routines.Scheduler(lambda: config, ran.append, lambda: set(apps["now"]), now=lambda: clock["now"])
        self.assertEqual(scheduler.check(), ["hello"])                      # startup, once
        clock["now"] = datetime(2026, 9, 23, 9, 0)
        self.assertEqual(scheduler.check(), ["work mode"])
        self.assertEqual(scheduler.check(), [])                             # not twice in the same minute
        apps["now"] = {"explorer", "discord"}
        self.assertEqual(scheduler.check(), ["game night"])
        self.assertEqual(scheduler.check(), [])                             # still running: not again
        self.assertEqual(ran, ["hello", "work mode", "game night"])

    def test_the_when_survives_normalizing(self):
        kept = routines.normalize([{"name": "A", "steps": ["x"], "when": " weekdays  at 9 "}])
        self.assertEqual(kept, [{"name": "a", "steps": ["x"], "when": "weekdays at 9"}])


# ---------------------------------------------------------------------------
# Plugins, personas, themes
# ---------------------------------------------------------------------------

class PluginHookTests(unittest.TestCase):
    def test_hooks_are_called_and_a_hook_only_plugin_loads(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "logger.py").write_text(
            "SEEN = []\n"
            "def on_reply(heard, reply):\n    SEEN.append((heard, reply))\n"
            "def on_timer(label, reminder):\n    raise RuntimeError('a broken hook is contained')\n",
            encoding="utf-8")
        plugins.set_folder(folder)
        try:
            count, problems = plugins.load()
            self.assertEqual((count, problems), (1, []))
            module = plugins._LOADED[0]["module"]
            self.assertEqual(plugins.emit("on_reply", "what time is it", "It's noon."), 1)
            self.assertEqual(plugins.emit("on_timer", "tea", False), 1)
            self.assertEqual(plugins.emit("on_notification", {}), 0)
            self.assertTrue(pump_until(lambda: module.SEEN, 3))
            self.assertEqual(module.SEEN, [("what time is it", "It's noon.")])
        finally:
            plugins._LOADED.clear()
            plugins.set_folder(Path(tempfile.mkdtemp()))


class PersonaVoiceTests(unittest.TestCase):
    def test_voice_choice(self):
        installed = ["en_US-amy-medium", "en_US-joe-medium"]
        self.assertEqual(persona.voice_for("pirate", {"persona_auto_voice": True}, installed), "en_US-joe-medium")
        self.assertEqual(persona.voice_for("pirate", {"persona_auto_voice": False}, installed), "")
        self.assertEqual(persona.voice_for("butler", {"persona_auto_voice": True}, installed), "")    # none installed
        picked = {"persona_voices": {"pirate": "en_US-amy-medium"}}
        self.assertEqual(persona.voice_for("pirate", picked, installed), "en_US-amy-medium")
        gone = {"persona_voices": {"pirate": "en_GB-alan-medium"}, "persona_auto_voice": False}
        self.assertEqual(persona.voice_for("pirate", gone, installed), "")                 # never a missing voice
        self.assertEqual(persona.voice_for("default", {"persona_auto_voice": True}, installed), "")


class ContrastThemeTests(unittest.TestCase):
    def tearDown(self):
        theme.FOLLOW["high_contrast"] = True
        theme.windows_high_contrast = self._real if hasattr(self, "_real") else theme.windows_high_contrast
        theme.apply_theme("neon")

    def test_windows_high_contrast_swaps_the_theme_but_keeps_the_choice(self):
        self._real = theme.windows_high_contrast
        theme.windows_high_contrast = lambda: True
        self.assertEqual(theme.effective_theme("ember"), "contrast")
        self.assertEqual(theme.effective_theme("paper"), "contrast_light")
        theme.FOLLOW["high_contrast"] = False
        self.assertEqual(theme.effective_theme("ember"), "ember")

    def test_high_contrast_pairs_are_readable(self):
        for name in ("contrast", "contrast_light"):
            theme.apply_theme(name, "#123456")                               # a custom accent is ignored here
            c = theme.COLORS
            for fg, bg in ((c["text"], c["bg"]), (c["accent"], c["bg"]), (c["muted"], c["bg"])):
                la, lb = theme._luminance(fg), theme._luminance(bg)
                ratio = (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
                self.assertGreaterEqual(ratio, 7.0, f"{name}: {fg} on {bg}")


# ---------------------------------------------------------------------------
# The status bar and the test notification
# ---------------------------------------------------------------------------

class StatusBarAdditionsTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        backend.CONFIG.update(bar_height=44)
        self.ctl = Assistant()
        self.bar = sb.StatusBar(self.ctl)
        self.bar.resize(1500, 44)
        self.bar.show()
        pump(80)

    def tearDown(self):
        self.bar.hide()
        for timer in self.bar.findChildren(QTimer):
            timer.stop()

    def test_the_notification_card_is_opaque(self):
        self.assertTrue(self.bar.strip.testAttribute(Qt.WA_StyledBackground))
        self.bar.area.ticker.show_caption("You", "You: some caption underneath", "", animate=False)
        self.bar.strip.show_notification({"app": "Mail", "title": "Hi", "count": 1, "ask": "none"}, animate=False)
        pump(100)
        image = self.bar.grab().toImage()
        spot = image.pixelColor(self.bar.strip.x() + self.bar.strip.width() - 40, self.bar.height() // 2)
        self.assertEqual(spot.name(), theme.COLORS["panel_alt"])              # the card's own colour, not the text

    def test_state_label_fits_every_state_at_the_largest_text(self):
        backend.CONFIG["bar_text_size"] = 20
        self.bar.apply_config()
        label = self.bar.state_label
        for state in ("listening", "thinking", "speaking"):
            self.bar._on_state(state)
            self.assertGreaterEqual(label.width() + 1, label.minimumSizeHint().width(), state)

    def test_media_buttons_follow_the_song_and_send_actions(self):
        sent = []
        backend_media, backend.media_control = backend.media_control, lambda action: sent.append(action)
        self.bar._refresh_music = lambda: None
        try:
            backend.CONFIG.update(bar_show_music=True, ytm_enabled=True)
            self.bar._show_music({"artist": "A", "title": "B", "songDuration": 100, "elapsedSeconds": 10,
                                  "isPaused": True})
            self.assertTrue(self.bar.media_box.isVisible())
            self.assertEqual(self.bar.play_btn.shape, "play")
            self.bar.play_btn.click()
            self.assertEqual(self.bar.play_btn.shape, "pause")                # flips at once
            self.bar.next_btn.click()
            self.bar.prev_btn.click()
            self.assertTrue(pump_until(lambda: len(sent) == 3, 3))
            self.assertEqual(sorted(sent), ["next", "playpause", "previous"])
            backend.CONFIG["bar_show_media_buttons"] = False
            self.bar.apply_config()
            self.assertFalse(self.bar.media_buttons.isVisibleTo(self.bar))
        finally:
            backend.media_control = backend_media

    def test_the_right_click_menu_switches_elements(self):
        menu = self.bar.build_menu()
        action = next(a for a in menu.actions() if a.text() == "Clock")
        self.assertFalse(action.isChecked())
        action.setChecked(True)
        self.assertTrue(backend.cfg_bool("bar_show_clock"))
        self.assertTrue(json.loads(backend.CONFIG_PATH.read_text(encoding="utf-8"))["bar_show_clock"])
        self.assertTrue(pump_until(lambda: self.bar.clock_label.isVisible(), 2))


class TestNotificationTests(unittest.TestCase):
    def test_it_is_simulated_and_never_touches_windows(self):
        common.use_temp_config()
        ctl = Assistant()
        shown = []
        ctl.notification.connect(shown.append)
        real = notifications.send_test_toast
        notifications.send_test_toast = lambda *a, **k: self.fail("sent a real Windows toast")
        backend.CONFIG.update(notify_ask="none", notify_sound="off")
        try:
            reply = ctl.test_notification()
            self.assertIn("simulated", reply)
            self.assertTrue(pump_until(lambda: shown, 5))
            self.assertEqual((shown[0]["app"], shown[0]["id"]), ("Neon", notifications.TEST_ID))
        finally:
            notifications.send_test_toast = real

    def test_the_watcher_never_dismisses_a_simulated_one(self):
        watcher = notifications.NotificationWatcher(lambda n: None)
        watcher._cmd_file = Path(tempfile.mkdtemp()) / "cmd.txt"
        watcher.remove(notifications.TEST_ID)
        self.assertFalse(watcher._cmd_file.exists())


class ReadAloudTests(unittest.TestCase):
    def test_the_setting_and_rules_choose_read(self):
        note = notifications.Notification(1, "Discord", "Alex", "are you coming?")
        self.assertEqual(notifications.decide(note, {"notify_ask": "read"}).action, "read")
        rule = {"notify_ask": "speech", "notify_rules": [{"app": "discord", "mode": "read"}]}
        self.assertEqual(notifications.decide(note, rule).action, "read")
        quiet = {"notify_ask": "read", "notify_quiet_enabled": True, "notify_quiet_start": "00:00",
                 "notify_quiet_end": "23:59"}
        self.assertEqual(notifications.decide(note, quiet, now=datetime(2026, 9, 23, 12)).action, "show")
        clean, _ = settings_schema.sanitize({"notify_ask": "read"}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["notify_ask"], "read")

    def test_what_is_said(self):
        one = backend.read_aloud_text([{"app": "Discord", "title": "Alex", "body": "are you coming?"}])
        self.assertEqual(one, "Discord says: Alex. are you coming?")
        many = backend.read_aloud_text([{"app": f"App{i}", "title": "t", "body": ""} for i in range(5)])
        self.assertTrue(many.startswith("App2 says: t.") and many.endswith("And 2 more."))
        long = backend.read_aloud_text([{"app": "Mail", "title": "x", "body": "word " * 200}])
        self.assertLess(len(long), 350)

    def test_attachment_names_are_said_as_what_they_are(self):
        said = notifications.spoken_file_names
        self.assertEqual(said("IMG_20260924_183012.jpg"), "an image")
        self.assertEqual(said("sent 8f3a2c1b9e7d.png"), "sent an image")
        self.assertEqual(said("Screenshot 2026-09-24 at 10.10.10.png"), "a screenshot")
        self.assertEqual(said("PTT-20260924-WA0001.opus"), "an audio clip")
        self.assertEqual(said("beach_trip.jpg"), "an image called beach trip")
        self.assertEqual(said("budget2026.xlsx"), "a spreadsheet called budget")
        for text in ("version 1.2.3 is out", "see example.com", "e.g. this"):
            self.assertEqual(said(text), text)
        line = backend.read_aloud_text([{"app": "Discord", "title": "Alex", "body": "IMG_4521.HEIC"}])
        self.assertEqual(line, "Discord says: Alex. an image.")
        message = backend.notification_message({"app": "Discord", "title": "Alex", "body": "8f3a2c1b9e7d.png"},
                                               ai=lambda *_a: "")
        self.assertEqual(message, "Alex says an image")

    def test_a_new_notification_is_spoken_without_a_question(self):
        common.use_temp_config()
        backend.CONFIG.update(notify_ask="read", notify_sound="off")
        ctl = Assistant()
        said = []

        class Speaker:
            is_speaking = False

            def say(self, text):
                said.append(text)
                import threading
                done = threading.Event()
                done.set()
                return done
        ctl._speaker = Speaker()
        ctl._ask_about_notifications = lambda batch: self.fail("asked instead of reading")
        ctl._on_new_notification(notifications.Notification(7, "Discord", "Alex", "are you coming?"))
        self.assertTrue(pump_until(lambda: said, 5))
        self.assertEqual(said, ["Discord says: Alex. are you coming?"])


class SummarizeModeTests(unittest.TestCase):
    """notify_ask = "summarize": the spoken summary straight away, without "want a summary?"."""

    def test_the_setting_and_rules_choose_summarize(self):
        note = notifications.Notification(1, "Discord", "Alex", "are you coming?")
        self.assertEqual(notifications.decide(note, {"notify_ask": "summarize"}).action, "summarize")
        rule = {"notify_ask": "none", "notify_rules": [{"app": "discord", "mode": "summarize"}]}
        self.assertEqual(notifications.decide(note, rule).action, "summarize")
        clean, _ = settings_schema.sanitize({"notify_ask": "summarize"}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["notify_ask"], "summarize")

    def test_a_new_notification_is_summarized_without_a_question(self):
        common.use_temp_config()
        backend.CONFIG.update(notify_ask="summarize", notify_sound="off",
                              notify_offer_card=False)    # the board offer has its own tests (test_notify_strip)
        saved = backend.ollama_answer
        prompts, said, cards = [], [], []
        backend.ollama_answer = lambda prompt, system=None, **_kw: prompts.append(prompt) or "Alex asks if you're coming."
        try:
            ctl = Assistant()

            class Speaker:
                is_speaking = False

                def say(self, text):
                    said.append(text)
                    import threading
                    done = threading.Event()
                    done.set()
                    return done
            ctl._speaker = Speaker()
            ctl.notification.connect(cards.append)
            ctl._ask_about_notifications = lambda batch: self.fail("asked instead of summarizing")
            ctl._on_new_notification(notifications.Notification(7, "Discord", "Alex", "are you coming?", aumid="x!y"))
            self.assertTrue(pump_until(lambda: said and cards, 5))      # the card arrives by a queued signal
        finally:
            backend.ollama_answer = saved
        self.assertEqual(said, ["Alex asks if you're coming."])
        self.assertIn("Discord: Alex - are you coming?", prompts[0])
        self.assertEqual((cards[0]["ask"], cards[0]["can_open"], cards[0]["aumid"]), ("none", True, "x!y"))


class OpenNotificationAppTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.ctl = Assistant()
        self.saved = (controller_module.os.startfile, backend.launch_app)
        self.started, self.launched = [], []
        controller_module.os.startfile = self.started.append
        backend.launch_app = lambda name: self.launched.append(name) or f"Launching {name}."

    def tearDown(self):
        controller_module.os.startfile, backend.launch_app = self.saved

    def test_it_opens_the_sender_by_its_app_id(self):
        self.ctl.open_notification_app({"app": "Discord", "aumid": "com.squirrel.Discord.Discord"})
        self.assertEqual(self.started, ["shell:AppsFolder\\com.squirrel.Discord.Discord"])
        self.assertEqual(self.launched, [])

    def test_without_an_app_id_it_finds_the_app_by_name(self):
        self.ctl.open_notification_app({"app": "Steam", "aumid": ""})
        self.assertTrue(pump_until(lambda: self.launched, 3))
        self.assertEqual((self.started, self.launched), ([], ["Steam"]))

    def test_it_opens_the_notifications_own_link_first(self):
        self.ctl.open_notification_app({"app": "Snipping Tool", "aumid": "Microsoft.ScreenSketch!App",
                                        "launch": "ms-screensketch:edit?x=1"})
        self.assertEqual(self.started, ["ms-screensketch:edit?x=1"])
        self.started.clear()
        self.ctl.open_notification_app({"app": "Evil", "aumid": "e!a", "launch": "file:///C:/Windows/System32/calc.exe"})
        self.assertEqual(self.started, ["shell:AppsFolder\\e!a"])                       # a blocked link: the app

    def test_the_link_comes_from_windows_notification_database(self):
        import sqlite3
        db = Path(tempfile.mkdtemp()) / "wpndatabase.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE Notification (Id INTEGER, Type TEXT, Payload BLOB)")
        rows = [(1, "toast", b'<toast activationType="protocol" launch="ms-screensketch:edit?a=1"><visual/></toast>'),
                (2, "toast", b'<toast launch="discord-args"><visual/></toast>'),              # handled in-app
                (3, "toast", b'<toast activationType="protocol" launch="shell:startup"/>'),     # blocked
                (4, "tile", b'<tile/>')]
        con.executemany("INSERT INTO Notification VALUES (?, ?, ?)", rows)
        con.commit()
        con.close()
        got = [notifications.launch_uri(i, db) for i in (1, 2, 3, 4, 99, -1)]
        self.assertEqual(got, ["ms-screensketch:edit?a=1", "", "", "", "", ""])
        self.assertEqual(notifications.launch_uri(1, Path(tempfile.mkdtemp()) / "missing.db"), "")

    def test_the_watcher_passes_the_app_id_on(self):
        note = notifications.parse_toast({"id": 3, "app": "Discord", "aumid": "com.squirrel.Discord.Discord",
                                          "texts": ["Alex", "hi"]})
        self.assertEqual(note.as_dict()["aumid"], "com.squirrel.Discord.Discord")


class WebSearchTests(unittest.TestCase):
    """Questions are searched by their topic, and the AI gets the article's exact facts."""

    def setUp(self):
        import websearch
        common.use_temp_config()
        self.ws = websearch
        self.saved = {k: getattr(websearch, k) for k in ("wikipedia_titles", "_article", "web_results")}
        self.saved_backend = {k: getattr(backend, k) for k in ("ollama_answer",)}
        self.searched = []

        def titles(query, limit=5):
            self.searched.append(query)
            return [("List of rhythm games", ""), ("Geometry Dash", ""), ("Geometry", "")]
        websearch.wikipedia_titles = titles
        articles = {"Geometry Dash": websearch.Article(
            "Geometry Dash", "Geometry Dash is a 2013 rhythm game. It is hard. Very.", "https://w/GD", "",
            ["Release date: 13 August 2013 (iOS, Android); 22 December 2014 (Microsoft Windows)"])}
        websearch._article = lambda title: articles.get(title)
        websearch.web_results = lambda q, provider, count=5, searxng_url="": (
            [] if provider == "off" else [("GD wiki", "Released in 2013 by RobTop.", "https://x")])
        self.prompts = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.ws, k, v)
        for k, v in self.saved_backend.items():
            setattr(backend, k, v)
        backend.SEARCH_HOOKS["article"] = None

    def test_the_question_is_searched_by_its_topic(self):
        for question, topic in (("when did geometry dash come out", "geometry dash"),
                                ("who directed oppenheimer", "oppenheimer"),
                                ("what is the release date of hollow knight silksong", "hollow knight silksong"),
                                ("when did the movie inception come out", "inception"),
                                ("how old is taylor swift", "taylor swift"),
                                ("Geometry Dash", "Geometry Dash")):
            self.assertEqual(self.ws.topic_of(question), topic, question)

    def test_the_ai_gets_the_articles_facts_and_web_results(self):
        backend.ollama_answer = lambda prompt, system=None, **_kw: self.prompts.append(prompt) or "13 August 2013."
        self.assertEqual(backend.web_search_and_answer("when did geometry dash come out"), "13 August 2013.")
        self.assertEqual(self.searched, ["geometry dash"])
        prompt = self.prompts[0]
        self.assertIn("Encyclopedia article: Geometry Dash", prompt)          # the best-named title, not "List of..."
        self.assertIn("Release date: 13 August 2013 (iOS, Android)", prompt)
        self.assertIn("GD wiki: Released in 2013 by RobTop.", prompt)

    def test_provider_off_is_wikipedia_only(self):
        backend.CONFIG["search_provider"] = "off"
        backend.ollama_answer = lambda prompt, system=None, **_kw: self.prompts.append(prompt) or "ok"
        backend.web_search_and_answer("geometry dash")
        self.assertNotIn("Web results", self.prompts[0])

    def test_without_the_ai_it_reads_the_articles_opening(self):
        backend.ollama_answer = lambda prompt, system=None, **_kw: None
        self.assertEqual(backend.web_search_and_answer("geometry dash"), "Geometry Dash is a 2013 rhythm game. It is hard.")

    def test_the_article_goes_to_the_popup_only_when_it_is_on(self):
        shown = []
        backend.SEARCH_HOOKS["article"] = shown.append
        backend.ollama_answer = lambda prompt, system=None, **_kw: "ok"
        backend.web_search_and_answer("geometry dash")
        self.assertEqual(shown, [])
        backend.CONFIG["wiki_popup"] = True
        backend.web_search_and_answer("geometry dash")
        self.assertEqual(shown[0]["title"], "Geometry Dash")

    def test_a_rate_limited_request_is_retried_once(self):
        import io
        import urllib.error
        import urllib.request
        calls = []

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, None)
            return io.BytesIO(b"ok")
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            self.assertEqual(self.ws._get("https://example.org/x"), b"ok")
            self.assertEqual(len(calls), 2)
        finally:
            urllib.request.urlopen = real

    def test_wikidata_dates_read_naturally(self):
        date = self.ws._date
        self.assertEqual(date({"time": "+2013-08-13T00:00:00Z", "precision": 11}), "13 August 2013")
        self.assertEqual(date({"time": "+2013-08-00T00:00:00Z", "precision": 10}), "August 2013")
        self.assertEqual(date({"time": "+1850-00-00T00:00:00Z", "precision": 9}), "1850")


class MessageModeTests(unittest.TestCase):
    """notify_ask = "message": just 'Alex says <their words>', picked out by the AI but never a summary."""

    NOTE = {"app": "Discord", "title": "Alex (#general, Gaming Server)",
            "body": "Alex: are you coming tonight? we meet at eight"}

    @staticmethod
    def ai(reply):
        return lambda prompt, system: reply

    def test_the_ai_picks_out_sender_and_message(self):
        reply = '{"sender": "Alex", "message": "are you coming tonight? we meet at eight"}'
        self.assertEqual(backend.notification_message(self.NOTE, self.ai(reply)),
                         "Alex says are you coming tonight? we meet at eight")

    def test_a_summary_or_invented_name_is_not_trusted(self):
        summary = '{"sender": "Alex", "message": "Alex is asking whether you will join them later"}'
        self.assertEqual(backend.notification_message(self.NOTE, self.ai(summary)),
                         "Alex (#general, Gaming Server) says Alex: are you coming tonight? we meet at eight")
        made_up = '{"sender": "Bob", "message": "are you coming tonight?"}'
        self.assertEqual(backend.notification_message(self.NOTE, self.ai(made_up)),
                         "Discord says are you coming tonight?")

    def test_without_the_ai(self):
        for reply in (None, "sorry, I can't", '{"sender": 3}'):
            self.assertEqual(backend.notification_message({"app": "Signal", "title": "Mum", "body": "call me"},
                                                          self.ai(reply)), "Mum says call me")
        self.assertEqual(backend.notification_message({"app": "Windows", "title": "Updates ready", "body": ""},
                                                      self.ai(None)), "Windows says Updates ready")

    def test_the_setting_and_rules_choose_message(self):
        note = notifications.Notification(1, "Discord", "Alex", "hi")
        self.assertEqual(notifications.decide(note, {"notify_ask": "message"}).action, "message")
        rule = {"notify_ask": "speech", "notify_rules": [{"app": "discord", "mode": "message"}]}
        self.assertEqual(notifications.decide(note, rule).action, "message")
        clean, _ = settings_schema.sanitize({"notify_ask": "message"}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["notify_ask"], "message")

    def test_a_new_notification_is_spoken_as_just_the_message(self):
        common.use_temp_config()
        backend.CONFIG.update(notify_ask="message", notify_sound="off")
        ctl = Assistant()
        said = []

        class Speaker:
            is_speaking = False

            def say(self, text):
                said.append(text)
                import threading
                done = threading.Event()
                done.set()
                return done
        ctl._speaker = Speaker()
        ctl._ask_about_notifications = lambda batch: self.fail("asked instead of reading")
        original = backend.ollama_generate
        backend.ollama_generate = lambda prompt, system=None, timeout=None: '{"sender": "Alex", "message": "you coming?"}'
        try:
            ctl._on_new_notification(notifications.Notification(7, "Discord", "Alex", "Alex: you coming?"))
            self.assertTrue(pump_until(lambda: said, 5))
        finally:
            backend.ollama_generate = original
        self.assertEqual(said, ["Alex says you coming?"])


if __name__ == "__main__":
    unittest.main()
