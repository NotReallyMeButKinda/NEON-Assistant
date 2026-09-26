"""Home Assistant (homeassistant.py): what counts as a smart-home request, and the whole round trip against a
fake Home Assistant on a free local port that speaks the same REST API (/api/, /api/config, /api/states,
/api/conversation/process). No real Home Assistant, token or Credential Manager entry is involved."""

import http.server
import json
import socket
import threading
import unittest

import homeassistant as ha
import system_control

from tests import common
from tests.common import backend
from tests.test_finish_pass import Patch

TOKEN = "test-token-abc"
STATES = [{"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Lights"}},
          {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door Lock"}},
          {"entity_id": "sensor.bedroom_temp", "state": "21", "attributes": {"friendly_name": "Bedroom Temperature"}},
          {"entity_id": "media_player.tv", "state": "on", "attributes": {"friendly_name": "TV"}}]
ANSWERS = {"turn off the kitchen lights": ("action_done", "Turned off the lights", ""),
           "unlock the front door": ("action_done", "Unlocked the front door", ""),
           "is the front door locked": ("query_answer", "Yes, Front Door Lock is locked", ""),
           "what's the bedroom temperature": ("query_answer", "Bedroom Temperature is 21 degrees", ""),
           "turn on the garden lights": ("error", "Sorry, I am not aware of any area called garden", "no_valid_targets")}


class FakeHomeAssistant(http.server.BaseHTTPRequestHandler):
    heard: list = []

    def _answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._answer(401, {"message": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._authorised():
            return
        FakeHomeAssistant.heard.append(("GET", self.path))
        if self.path == "/api/":
            self._answer(200, {"message": "API running."})
        elif self.path == "/api/config":
            self._answer(200, {"location_name": "Home", "version": "2026.9.1"})
        elif self.path == "/api/states":
            self._answer(200, STATES)
        else:
            self._answer(404, {})

    def do_POST(self):
        if not self._authorised():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakeHomeAssistant.heard.append(("POST", self.path, body.get("text")))
        kind, speech, code = ANSWERS.get(body.get("text", "").lower().rstrip("?."),
                                         ("error", "Sorry, I couldn't understand that", "no_intent_match"))
        self._answer(200, {"response": {"response_type": kind, "language": "en",
                                        "speech": {"plain": {"speech": speech}},
                                        "data": {"code": code} if code else {"targets": []}},
                           "conversation_id": "x"})

    def log_message(self, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DetectionTests(unittest.TestCase):
    NAMES = {"kitchen lights", "front door lock", "bedroom temperature", "tv", "desk lamp"}

    def test_home_sentences(self):
        for text in ("turn off the kitchen lights", "turn on the desk lamp", "set the thermostat to 70",
                     "is the front door locked", "dim the lights to 30 percent", "open the garage", "close the blinds",
                     "turn off the tv", "start the vacuum", "what's the temperature inside", "activate the movie scene",
                     "what's the humidity in the bedroom"):
            self.assertTrue(ha.home_request(text, self.NAMES), text)

    def test_sentences_that_stay_with_neon(self):
        for text in ("set an alarm for 7am", "switch to noir mode", "what's the temperature", "lock bitwarden",
                     "lock the pc", "turn up the volume", "turn off the screen", "set a timer for 5 minutes",
                     "open chrome", "close discord", "what's the weather", "turn on dark mode", "pause the music",
                     "open the door", "hello there"):
            self.assertFalse(ha.home_request(text, self.NAMES), text)

    def test_what_is_asked_first(self):
        for text in ("unlock the front door", "open the garage", "disarm the alarm system", "open the gate"):
            self.assertTrue(ha.needs_confirmation(text), text)
        for text in ("lock the front door", "close the garage", "turn on the lights"):
            self.assertFalse(ha.needs_confirmation(text), text)


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        self.port = free_port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), FakeHomeAssistant)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        FakeHomeAssistant.heard = []
        patch = Patch(self)
        patch(ha, "token", lambda: TOKEN)
        patch(system_control, "perform", lambda *a, **k: "(the PC did it)")
        backend.CONFIG.update(ha_enabled=True, ha_url=f"127.0.0.1:{self.port}")
        ha.forget_names()

    def say(self, text):
        return backend.handle_utterance(None, text)

    def sent(self):
        return [h[2] for h in FakeHomeAssistant.heard if h[0] == "POST"]

    def test_commands_and_questions_go_to_assist(self):
        self.assertEqual(self.say("Turn off the kitchen lights."), "Turned off the lights")
        self.assertEqual(self.say("is the front door locked?"), "Yes, Front Door Lock is locked")
        self.assertEqual(self.say("what's the bedroom temperature"), "Bedroom Temperature is 21 degrees")
        self.assertEqual(len(self.sent()), 3)
        gets = [h for h in FakeHomeAssistant.heard if h == ("GET", "/api/states")]
        self.assertEqual(len(gets), 1)                                        # the device names are reused

    def test_unlocking_asks_first(self):
        self.assertEqual(self.say("unlock the front door"), "Unlock the front door?")
        self.assertEqual(self.sent(), [])                                    # nothing sent before the yes
        self.assertEqual(self.say("yes"), "Unlocked the front door")
        self.assertEqual(self.sent(), ["unlock the front door"])
        self.say("unlock the front door")
        self.assertEqual(self.say("no"), "Okay, I'll leave it.")
        self.assertEqual(len(self.sent()), 1)
        backend.CONFIG["ha_confirm"] = False
        self.assertEqual(self.say("unlock the front door"), "Unlocked the front door")

    def test_pc_commands_and_chat_never_reach_it(self):
        for text in ("lock the pc", "turn up the volume", "switch to noir mode"):
            self.say(text)
        self.assertEqual(self.sent(), [])
        self.assertEqual([h for h in FakeHomeAssistant.heard if h[0] == "GET"], [])   # no network at all

    def test_not_understood_and_switched_off(self):
        self.assertIn("not aware of any area called garden", self.say("turn on the garden lights"))
        self.assertIn("exposed to Assist", ha.ask(backend.CONFIG, "frobnicate the widget"))
        backend.CONFIG["ha_enabled"] = False
        self.assertIsNone(backend.handle_home("turn off the kitchen lights"))

    def test_connection_problems_are_said_plainly(self):
        self.assertEqual(ha.check(backend.CONFIG), "Connected to Home (version 2026.9.1), 4 devices and sensors.")
        Patch(self)(ha, "token", lambda: "wrong")
        self.assertIn("didn't accept the access token", ha.check(backend.CONFIG))
        Patch(self)(ha, "token", lambda: TOKEN)
        backend.CONFIG["ha_url"] = f"127.0.0.1:{free_port()}"                  # nothing listens there
        self.assertIn("can't reach Home Assistant", self.say("turn off the kitchen lights"))
        Patch(self)(ha, "token", lambda: "")
        self.assertIn("access token", ha.check(backend.CONFIG))

    def test_the_ai_can_send_free_form_requests(self):
        self.assertEqual(backend._intent_home({"request": "turn off the kitchen lights"}, "kill the kitchen lights"),
                         "Turned off the lights")
        self.assertIn("smart_home", backend.INTENT_GROUPS["home"])
        backend.CONFIG["ha_enabled"] = False
        self.assertIn("smart_home", backend._unavailable_intents())


if __name__ == "__main__":
    unittest.main()
