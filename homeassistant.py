"""
homeassistant.py -- smart-home control through Home Assistant (optional: Settings > Smart home).

"Turn off the kitchen lights", "set the thermostat to 70", "is the front door locked", "what's the temperature
in the bedroom": NEON hands the sentence to Home Assistant's own Assist (POST /api/conversation/process), which
already knows your devices, rooms and their names, and says what Assist answers. Nothing is installed on this
PC; Home Assistant's REST API and a long-lived access token are all it needs. The token lives in Windows
Credential Manager, never in the settings file.

A sentence is only sent when it is clearly about the home (`home_request`): a control verb or a question
together with one of your Home Assistant device names, or a device word (lights, thermostat, blinds...).
Everything about the PC itself ("lock the PC", "turn up the volume") stays with NEON. Unlocking, opening a
garage or gate and disarming an alarm ask first (`needs_confirmation`).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

import neon_log
import secrets_store

log = neon_log.get("homeassistant")

TOKEN_SECRET = "home_assistant_token"
TIMEOUT = 8.0
NAMES_SECONDS = 600.0            # how long the list of device names is reused before it's read again


class HomeAssistantError(Exception):
    """Home Assistant can't be reached or refused; the message is ready to say."""


# ---------------------------------------------------------------------------
# The token and the connection
# ---------------------------------------------------------------------------

def token() -> str:
    return secrets_store.get_secret(TOKEN_SECRET) or ""


def set_token(value: str) -> bool:
    value = str(value or "").strip()
    return secrets_store.set_secret(TOKEN_SECRET, value) if value else secrets_store.delete_secret(TOKEN_SECRET)


def base_url(cfg: dict) -> str:
    url = str(cfg.get("ha_url") or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def _call(cfg: dict, method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT):
    url, key = base_url(cfg), token()
    if not url:
        raise HomeAssistantError("Home Assistant's address isn't set. Add it in Settings, under Smart home.")
    if not key:
        raise HomeAssistantError("I don't have a Home Assistant access token yet. Add one in Settings, "
                                 "under Smart home.")
    request = urllib.request.Request(url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise HomeAssistantError("Home Assistant didn't accept the access token. Make a new one in your "
                                     "Home Assistant profile and paste it into Settings.") from None
        raise HomeAssistantError(f"Home Assistant answered with an error ({exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HomeAssistantError("I can't reach Home Assistant. Is it running, and is the address in "
                                 "Settings right?") from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise HomeAssistantError("Home Assistant sent something I couldn't read.") from None


def check(cfg: dict) -> str:
    """Settings' Test button: a sentence saying whether it works, and what it found."""
    try:
        _call(cfg, "GET", "/api/")
        info = _call(cfg, "GET", "/api/config")
        states = _call(cfg, "GET", "/api/states")
    except HomeAssistantError as exc:
        return str(exc)
    where = info.get("location_name") or "Home Assistant"
    return f"Connected to {where} (version {info.get('version', '?')}), {len(states)} devices and sensors."


# ---------------------------------------------------------------------------
# Knowing what's in the home
# ---------------------------------------------------------------------------

_NAMES = {"names": set(), "at": 0.0}
_NAMES_LOCK = threading.Lock()
# Device words that make a sentence about the home even when no device name matches.
# Not "switch" (a verb too: "switch to noir mode"), "alarm" ("set an alarm" is a timer), "door" alone ("open the
# door" says nothing about which) or plain "temperature" (the weather): those count only as part of a device
# name, or as "temperature inside / in the bedroom".
DEVICE_WORDS = ("lights", "light", "lamp", "lamps", "bulb", "bulbs", "fan", "fans", "smart plug", "plug", "outlet",
                "thermostat", "heating", "heater", "air conditioning", "air conditioner", "ac", "a c",
                "blinds", "blind", "curtains", "curtain", "shades", "shutters", "garage", "garage door", "gate",
                "door lock", "front door", "back door", "vacuum", "robot vacuum", "alarm system",
                "security system", "sprinklers", "humidifier", "dehumidifier", "purifier", "kettle", "scene")
_INDOORS = re.compile(r"\b(?:temperature|humidity)\b.*\b(?:inside|indoors|in here|in the house|at home|upstairs|"
                      r"downstairs|in the \w+(?: room)?)\b")
# Things that belong to the PC, never to Home Assistant.
PC_WORDS = re.compile(r"\b(?:pc|computer|laptop|screen|monitor|display|volume|sound|mic|microphone|music|song|"
                      r"playlist|wifi|wi-fi|bluetooth|dark mode|night light|notifications?|wake word|dictation|"
                      r"status bar|window|windows|tab|browser|timer|alarm clock|keyboard|mouse|caps lock|num lock|"
                      r"bitwarden|vault|password|persona|mode|voice)\b")


def device_names(cfg: dict) -> set[str]:
    """The friendly names of Home Assistant's entities, lower-case (read at most every NAMES_SECONDS; empty
    if Home Assistant can't be reached)."""
    with _NAMES_LOCK:
        if time.monotonic() - _NAMES["at"] < NAMES_SECONDS and _NAMES["at"]:
            return set(_NAMES["names"])
    try:
        states = _call(cfg, "GET", "/api/states", timeout=4.0)
    except HomeAssistantError:
        states = []
    names = set()
    for state in states if isinstance(states, list) else []:
        name = str((state.get("attributes") or {}).get("friendly_name") or "").strip().lower()
        if 2 < len(name) <= 60:
            names.add(" ".join(re.sub(r"[^\w' ]", " ", name).split()))
    with _NAMES_LOCK:
        _NAMES.update(names=names, at=time.monotonic() if names else 0.0)
    return names


def forget_names() -> None:
    with _NAMES_LOCK:
        _NAMES.update(names=set(), at=0.0)


# ---------------------------------------------------------------------------
# What was said
# ---------------------------------------------------------------------------

_LEAD = r"^(?:(?:please|hey|ok|okay|can you|could you|would you|go ahead and)\s+)*"
_CONTROL = re.compile(_LEAD + r"(?:turn|switch|flip|toggle|dim|brighten|set|put|make|open|close|shut|lock|unlock|"
                              r"raise|lower|start|stop|pause|arm|disarm|activate|run|enable|disable|increase|"
                              r"decrease|cool|warm|heat)\b")
_QUESTION = re.compile(_LEAD + r"(?:is|are|was|what(?:'s| is| are)|whats|how (?:warm|cold|hot|humid)|which|did i "
                               r"(?:leave|lock|close))\b")
_SENSITIVE = re.compile(r"\b(?:unlock|disarm|open (?:the |my )?(?:garage|gate|front door|back door|door|lock))\b")


def _clean(text: str) -> str:
    return " ".join(re.sub(r"[?!.,]+", " ", str(text).lower()).split())


def could_be_home(text: str) -> bool:
    """A cheap first look (no network): a control verb or a question, and nothing about the PC."""
    t = _clean(text)
    return bool(t) and bool(_CONTROL.match(t) or _QUESTION.match(t)) and not PC_WORDS.search(t)


def home_request(text: str, names: set[str] | None = None) -> bool:
    """True when `text` is a smart-home command or question for Home Assistant: a control verb or a question,
    and either one of your device names or a device word, and nothing that belongs to the PC."""
    t = _clean(text)
    if not t or not (_CONTROL.match(t) or _QUESTION.match(t)):
        return False
    if PC_WORDS.search(t):
        return False
    padded = f" {t} "
    if any(f" {name} " in padded for name in names or ()):
        return True
    return bool(_INDOORS.search(t)) or any(re.search(rf"\b{re.escape(word)}\b", t) for word in DEVICE_WORDS)


def needs_confirmation(text: str) -> bool:
    """Unlocking, opening a garage / gate / door and disarming an alarm are asked about first."""
    return bool(_SENSITIVE.search(_clean(text)))


# ---------------------------------------------------------------------------
# Asking Home Assistant
# ---------------------------------------------------------------------------

_NOT_UNDERSTOOD = ("Home Assistant didn't understand that. If it's a device, make sure it's exposed to Assist "
                   "in Home Assistant's voice assistant settings.")


def ask(cfg: dict, text: str) -> str:
    """Hand `text` to Home Assistant's Assist and return what to say. Raises HomeAssistantError."""
    body = {"text": str(text).strip(), "language": str(cfg.get("ha_language") or "en")}
    data = _call(cfg, "POST", "/api/conversation/process", body)
    response = (data or {}).get("response") or {}
    speech = (((response.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
    kind = response.get("response_type", "")
    code = (response.get("data") or {}).get("code", "")
    log.info("Home Assistant: %r -> %s %s", text, kind, code)
    if kind == "error" and code == "no_intent_match":
        return _NOT_UNDERSTOOD                      # Assist's own "Sorry, I couldn't understand" doesn't say why
    if kind == "error" and code == "no_valid_targets":
        return speech or _NOT_UNDERSTOOD            # it names what it didn't find: say that
    if kind == "error":
        return speech or "Home Assistant couldn't do that."
    return speech or "Done."
