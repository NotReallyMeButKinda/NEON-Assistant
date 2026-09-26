import json
import unittest

import settings_schema as schema

from tests import common
from tests.common import backend


class SchemaTests(unittest.TestCase):
    def test_types_are_coerced_and_bad_values_fall_back(self):
        bad = {"tts_speed": "fast", "bar_height": 500, "tts_engine": "PIPER", "notify_ask": "loud",
               "bar_show_clock": "yes", "notify_seconds": "20", "avoid_apps": "Edge, Zen", "state_colors": "oops"}
        clean, problems = schema.sanitize({**backend.DEFAULT_CONFIG, **bad}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["tts_speed"], backend.DEFAULT_CONFIG["tts_speed"])      # not a number -> default
        self.assertEqual(clean["bar_height"], 80)                                      # clamped to the range
        self.assertEqual(clean["tts_engine"], "piper")                                 # case-insensitive choice
        self.assertEqual(clean["notify_ask"], "speech")                                # unknown choice -> default
        self.assertIs(clean["bar_show_clock"], True)
        self.assertEqual(clean["notify_seconds"], 20.0)
        self.assertEqual(clean["avoid_apps"], ["Edge", "Zen"])
        self.assertEqual(clean["state_colors"], {})
        self.assertTrue(problems)

    def test_unknown_keys_survive(self):
        clean, _ = schema.sanitize({**backend.DEFAULT_CONFIG, "future_setting": {"a": 1}}, backend.DEFAULT_CONFIG)
        self.assertEqual(clean["future_setting"], {"a": 1})

    def test_hotkey_migration(self):
        old = {"hotkey_enabled": True, "hotkey": "Ctrl+Alt+V", "hotkey_action": "toggle_wake",
               "mute_hotkey_enabled": True, "mute_hotkey": "Ctrl+Alt+M"}
        new = schema.migrate(old)
        self.assertEqual(new["config_version"], schema.CONFIG_VERSION)
        self.assertEqual(new["hotkey_wake"], "Ctrl+Alt+V")
        self.assertEqual(new["hotkey_mute"], "Ctrl+Alt+M")
        self.assertFalse(any(k in new for k in ("hotkey", "hotkey_action", "mute_hotkey")))

    def test_current_version_is_left_alone(self):
        self.assertEqual(schema.migrate({"config_version": schema.CONFIG_VERSION, "hotkey": "keep"})["hotkey"], "keep")
        self.assertEqual(schema.migrate({"config_version": "garbage"})["config_version"], schema.CONFIG_VERSION)


class OfflineDefaultsTests(unittest.TestCase):
    """New installs start offline; an existing install keeps doing what it did."""

    ONLINE_SWITCHES = ("stt_online_fallback", "location_from_ip", "app_descriptions_online")

    def test_a_new_install_is_offline(self):
        d = backend.DEFAULT_CONFIG
        self.assertEqual((d["stt_engine"], d["wake_engine"], d["wake_model"]),
                         ("whisper", "openwakeword", "custom:hey_nova"))
        self.assertFalse(any(d[k] for k in self.ONLINE_SWITCHES))

    def test_an_existing_install_keeps_its_online_behaviour(self):
        new = schema.migrate({"config_version": 2, "stt_engine": "google"})
        self.assertTrue(all(new[k] is True for k in self.ONLINE_SWITCHES))
        self.assertEqual(new["stt_engine"], "google")
        kept = schema.migrate({"config_version": 2, "location_from_ip": False})
        self.assertIs(kept["location_from_ip"], False)

    def test_no_ip_lookup_when_it_is_off(self):
        common.use_temp_config()
        saved = backend._get_json, dict(backend._LOCATION)
        backend._get_json = lambda url: self.fail(f"went online: {url}")
        try:
            backend._LOCATION.update(lat=None, lon=None, city=None)
            self.assertFalse(backend.refresh_location())
            data, why = backend._weather_data()
            self.assertIsNone(data)
            self.assertIn("Settings", why)
        finally:
            backend._get_json = saved[0]
            backend._LOCATION.update(saved[1])

    def test_no_app_lookups_when_they_are_off(self):
        common.use_temp_config()
        saved = backend.discover_shortcuts, backend.load_app_cache, backend.save_app_cache, backend.fetch_app_description
        backend.discover_shortcuts = lambda: {"Some Editor": "C:/x.lnk"}
        backend.load_app_cache = lambda: {}
        backend.save_app_cache = lambda cache: None
        backend.fetch_app_description = lambda *a: self.fail("looked an app up online")
        try:
            backend.build_app_catalogue(background=False)
            self.assertIn("Some Editor", backend._APP_CATALOGUE)
        finally:
            (backend.discover_shortcuts, backend.load_app_cache, backend.save_app_cache,
             backend.fetch_app_description) = saved


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.path = common.use_temp_config()

    def test_defaults_ship_every_hotkey_unmapped(self):
        for key in ("hotkey_talk", "hotkey_wake", "hotkey_window", "hotkey_quick", "hotkey_mute", "hotkey_hold"):
            self.assertEqual(backend.DEFAULT_CONFIG[key], "")
        self.assertFalse(backend.DEFAULT_CONFIG["onboarding_done"])

    def test_corrupt_file_is_kept_aside_not_overwritten(self):
        self.path.write_text('{"assistant_name": "Dan", ', encoding="utf-8")
        cfg = backend.load_config()
        self.assertEqual(cfg["assistant_name"], backend.DEFAULT_CONFIG["assistant_name"])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("*.corrupt-*"))), 1)
        self.assertIn("kept it", backend.CONFIG_LOAD_ERROR)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        backend.save_config({"x": 1})
        self.assertEqual(json.loads(self.path.read_text()), {"x": 1})
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_persist_keys_writes_only_the_changed_keys(self):
        backend.CONFIG["bar_height"] = 60              # an unsaved edit sitting in memory
        backend.persist_keys({"tts_speed": 1.3})
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk["tts_speed"], 1.3)
        self.assertNotEqual(on_disk.get("bar_height"), 60)
        self.assertEqual(backend.CONFIG["tts_speed"], 1.3)


if __name__ == "__main__":
    unittest.main()
