"""
settings_schema.py -- validation and versioned migration for assistant_config.json.

DEFAULT_CONFIG in assistant.py is the source of truth for keys and types; this module adds what a
plain dict can't say: legal ranges and choices. `sanitize()` runs on load, so a hand-edited or
half-corrupt file (a number saved as text, an unknown engine name, "false" as a string) can't
crash a feature later -- the bad value is replaced by the default and reported.

CONFIG_VERSION / MIGRATIONS: renamed or reshaped settings are converted once, in order, and the
version is stamped into the file. Add a function to MIGRATIONS when you change a setting's shape.
"""

from __future__ import annotations

import re

import persona

CONFIG_VERSION = 4

# key -> ("choice", (allowed...)) | ("range", low, high) | ("pattern", regex the whole value must match)
CONSTRAINTS: dict[str, tuple] = {
    "tts_engine": ("choice", ("piper", "sapi")),
    "stt_engine": ("choice", ("google", "whisper")),
    "wake_engine": ("choice", ("stt", "openwakeword")),
    "wake_model": ("pattern", r"(hey_jarvis|alexa|hey_mycroft|hey_rhasspy|custom:[a-z0-9_]{2,40})"),
    "whisper_device": ("choice", ("auto", "cpu", "cuda")),
    "wake_whisper_model": ("choice", ("", "tiny.en", "base.en", "small.en", "tiny", "base", "small")),
    "whisper_model": ("choice", ("tiny.en", "base.en", "small.en", "tiny", "base", "small")),
    "ack_type": ("choice", ("sound", "speech", "off")),
    "temperature_unit": ("choice", ("auto", "celsius", "fahrenheit")),
    "time_format": ("choice", ("12h", "24h")),
    "bar_clock_format": ("choice", ("auto", "12h", "24h")),
    "bar_position": ("choice", ("top", "bottom")),
    "search_provider": ("choice", ("duckduckgo", "brave", "searxng", "off")),
    "wiki_popup_side": ("choice", ("right", "left")),
    "board_remind_time": ("pattern", r"([01]?\d|2[0-3]):[0-5]\d"),
    "notify_ask": ("choice", ("speech", "summarize", "read", "message", "card", "none")),
    "copilot_key_action": ("choice", ("talk", "wake", "window", "quick", "mute", "hold", "dictation")),
    "tts_speed": ("range", 0.4, 3.0), "tts_volume": ("range", 0.0, 2.0),
    "tts_noise": ("range", 0.0, 2.0), "tts_noise_w": ("range", 0.0, 2.0),
    "sapi_rate": ("range", 50, 400), "ack_volume": ("range", 0.0, 1.0), "ack_delay": ("range", 0.0, 10.0),
    "energy_multiplier": ("range", 1.0, 20.0), "silence_duration": ("range", 0.2, 10.0),
    "listen_timeout": ("range", 0.5, 60.0), "phrase_time_limit": ("range", 2.0, 120.0),
    "wake_phrase_limit": ("range", 1.0, 60.0), "ollama_timeout": ("range", 3.0, 600.0),
    "web_search_results": ("range", 1, 20), "app_match_threshold": ("range", 0.05, 1.0),
    "bar_height": ("range", 30, 80), "bar_caption_seconds": ("range", 0.0, 60.0),
    "shade_seconds": ("range", 1.0, 60.0),
    "bar_text_size": ("range", 10, 20), "notify_seconds": ("range", 2.0, 120.0),
    "bar_monitor": ("range", 0, 8), "wake_threshold": ("range", 0.1, 0.95), "whisper_threads": ("range", 0, 32), "calendar_alert_minutes": ("range", 1, 120), "quick_reply_seconds": ("range", 1.0, 60.0),
    "persona": ("choice", tuple(persona.ORDER)), "conversation_seconds": ("range", 2.0, 30.0),
    "browser_port": ("range", 1024, 65535), "panel_widget_port": ("range", 1024, 65535), "bitwarden_lock_minutes": ("range", 1.0, 240.0),
    "listen_sound_volume": ("range", 0.0, 1.0), "thinking_after": ("range", 0.5, 30.0),
}

_TRUE = {"1", "true", "yes", "on"}


def _coerce(value, default):
    """`value` as the same type as `default`; raises ValueError / TypeError if it can't be."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in _TRUE
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        if value is None or isinstance(value, (dict, list)):
            raise TypeError("expected text")
        return str(value)
    if isinstance(default, list):
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        if not isinstance(value, list):
            raise TypeError("expected a list")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise TypeError("expected an object")
        return value
    return value


def sanitize(config: dict, defaults: dict) -> tuple[dict, list[str]]:
    """(cleaned copy of `config`, human-readable list of what was fixed)."""
    clean, problems = dict(config), []
    for key, default in defaults.items():
        if key not in config:
            continue
        value = config[key]
        try:
            value = _coerce(value, default)
            rule = CONSTRAINTS.get(key)
            if rule and rule[0] == "choice":
                match = next((c for c in rule[1] if str(value).strip().lower() == c.lower()), None)
                if match is None:
                    raise ValueError(f"must be one of {', '.join(rule[1])}")
                value = match
            elif rule and rule[0] == "pattern":
                value = str(value).strip().lower()
                if not re.fullmatch(rule[1], value):
                    raise ValueError("not a known value")
            elif rule and rule[0] == "range":
                lo, hi = rule[1], rule[2]
                clamped = min(max(value, lo), hi)
                if clamped != value:
                    problems.append(f"{key}: {value} is outside {lo}..{hi}, using {clamped}")
                    value = type(default)(clamped)
        except (ValueError, TypeError) as exc:
            problems.append(f"{key}: {value!r} isn't valid ({exc}); using the default")
            value = default
        clean[key] = value
    return clean, problems


# ---- migrations: version N -> N+1, applied in order ------------------------------------------

_OLD_HOTKEY_KEYS = ("hotkey_enabled", "hotkey", "hotkey_action", "mute_hotkey_enabled", "mute_hotkey")


def _v1_to_v2(data: dict) -> dict:
    """One hotkey + an 'action' choice became an independent bind per action."""
    if not any(k in data for k in _OLD_HOTKEY_KEYS):
        return data
    data = dict(data)

    def on(key: str) -> bool:
        value = data.get(key, True)
        return value.strip().lower() in _TRUE if isinstance(value, str) else bool(value)

    target = {"toggle_wake": "hotkey_wake", "toggle_window": "hotkey_window"}.get(
        str(data.get("hotkey_action", "talk")), "hotkey_talk")
    if on("hotkey_enabled") and data.get("hotkey"):
        data.setdefault(target, data["hotkey"])
    if on("mute_hotkey_enabled") and data.get("mute_hotkey"):
        data.setdefault("hotkey_mute", data["mute_hotkey"])
    for key in _OLD_HOTKEY_KEYS:
        data.pop(key, None)
    return data


def _v2_to_v3(data: dict) -> dict:
    """Three new switches for things that go online. New installs start with them off (offline first);
    an existing install keeps doing what it did until the user turns them off."""
    data = dict(data)
    for key in ("stt_online_fallback", "location_from_ip", "app_descriptions_online"):
        data.setdefault(key, True)
    return data


def _v3_to_v4(data: dict) -> dict:
    """llama3.2 (3B) made up niche facts; qwen3:8b follows instructions and structured output far better.
    Only the old default is swapped: a model the user chose themselves is kept."""
    data = dict(data)
    if str(data.get("ollama_model", "")).strip() in ("llama3.2", "llama3.2:latest"):
        data["ollama_model"] = "qwen3:8b"
    return data


MIGRATIONS = {1: _v1_to_v2, 2: _v2_to_v3, 3: _v3_to_v4}


def migrate(data: dict) -> dict:
    """Bring a loaded config up to CONFIG_VERSION."""
    version = data.get("config_version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is not None:
            data = step(data)
        version += 1
    data = dict(data)
    data["config_version"] = CONFIG_VERSION
    return data
