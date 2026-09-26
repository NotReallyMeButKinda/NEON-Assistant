"""
ytmusic.py -- control Pear Desktop (YouTube Music) through its local HTTP API server.

Endpoints follow the server's OpenAPI document (/api/v1/...). Auth: `POST /auth/{id}` hands
out a bearer token (the app may ask the user to approve the id the first time); if the
server's authStrategy is NONE no token is needed and none is requested.

Every public action returns a sentence ready to speak. Two exceptions carry the failure modes:
    Unavailable  the server isn't reachable (app closed, API server off, feature disabled) --
                 callers fall back to something else (media keys) or say so
    YTMError     the server answered but refused / couldn't do it; str() is speakable
`Client.run(name, *args)` wraps both: it returns None when unavailable, else the sentence.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import secrets_store

DEFAULT_URL = "http://127.0.0.1:26538"
DEFAULT_CLIENT_ID = "neon-assistant"
SECRET_NAME = "ytm-tokens"       # the Windows Credential Manager entry holding the access tokens
DOWN_BACKOFF = 8.0          # seconds to skip the API after a failed connection

_MESSAGES = {
    "play": "Playing.", "pause": "Paused.", "toggle": "Toggled play and pause.",
    "next": "Skipping to the next track.", "previous": "Going back to the previous track.",
    "stop": "Paused.", "mute": "Toggled mute.",
}
_POSTS = {"play": "play", "pause": "pause", "toggle": "toggle-play", "next": "next",
          "previous": "previous", "stop": "pause", "mute": "toggle-mute"}


class Unavailable(Exception):
    """Pear Desktop can't be reached (or the integration is switched off)."""


class YTMError(Exception):
    """Pear Desktop answered but the request failed; the message is speakable."""


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _first_video_id(node) -> str | None:
    """First video id in a (raw YouTube Music) search response, in document order, so the
    top result wins. The response shape isn't specified, hence the generic walk."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            vid = cur.get("videoId")
            if isinstance(vid, str) and re.fullmatch(r"[\w-]{11}", vid):
                return vid
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return None


def _queue_rows(data) -> list[tuple[str | None, bool]]:
    """(video id, is the current song) for each queue entry, in order."""
    rows = []
    for item in (data or {}).get("items", []) if isinstance(data, dict) else []:
        renderer = item.get("playlistPanelVideoRenderer") if isinstance(item, dict) else None
        if renderer is None and isinstance(item, dict):   # entries with a counterpart video are wrapped
            wrapper = item.get("playlistPanelVideoWrapperRenderer") or {}
            renderer = wrapper.get("primaryRenderer", {}).get("playlistPanelVideoRenderer")
        renderer = renderer or {}
        rows.append((renderer.get("videoId") or _first_video_id(item), bool(renderer.get("selected"))))
    return rows


def _describe(song: dict) -> str:
    title, artist = song.get("title") or "an unknown track", song.get("artist")
    return f"{title} by {artist}" if artist else title


class Client:
    def __init__(self, cfg: dict, token_path: Path, secret_name: str = SECRET_NAME):
        self._cfg = cfg               # read live, so Settings changes apply immediately
        self._token_path = token_path  # legacy plain-text store: migrated into Credential Manager on first use
        self._secret = secret_name
        self._down_until = 0.0        # after a failed connection, don't retry for a moment (see _request)

    # ---- config ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return _truthy(self._cfg.get("ytm_enabled", True))

    def _base(self) -> str:
        url = str(self._cfg.get("ytm_url") or DEFAULT_URL).strip().rstrip("/")
        if "://" not in url:
            url = f"http://{url}"
        return url.replace("//localhost", "//127.0.0.1", 1)   # Windows tries ::1 first: ~2 s wasted

    def _client_id(self) -> str:
        return str(self._cfg.get("ytm_client_id") or DEFAULT_CLIENT_ID).strip()

    # ---- token store: Windows Credential Manager (a plain file is only the legacy / fallback) --------
    def _token_key(self) -> str:
        return f"{self._base()}|{self._client_id()}"

    def _tokens(self) -> dict:
        data: dict = {}
        try:
            raw = secrets_store.get_secret(self._secret)
            data = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not data and self._token_path.exists():          # legacy file: move it into the credential store
            try:
                legacy = json.loads(self._token_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                legacy = {}
            if isinstance(legacy, dict) and legacy:
                data = legacy
                self._save_tokens(data)
        return data

    def _save_tokens(self, data: dict) -> None:
        try:
            saved = secrets_store.set_secret(self._secret, json.dumps(data))
        except OSError:
            saved = False
        if saved:
            try:
                self._token_path.unlink(missing_ok=True)     # no plain-text copy left next to the source
            except OSError:
                pass
            return
        try:                                                # Credential Manager refused: keep working with a file
            self._token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass                                            # not fatal: we just ask again next launch

    def _token(self) -> str:
        return str(self._tokens().get(self._token_key(), ""))

    def _store_token(self, token: str) -> None:
        data = self._tokens()
        data[self._token_key()] = token
        self._save_tokens(data)

    # ---- HTTP ---------------------------------------------------------------------
    def _open(self, method: str, path: str, body, timeout: float, token: str):
        data = json.dumps(body).encode("utf-8") if body is not None else (b"" if method != "GET" else None)
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self._base() + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _authenticate(self) -> str:
        path = f"/auth/{urllib.parse.quote(self._client_id(), safe='')}"
        try:
            reply = self._open("POST", path, None, timeout=60, token="")   # the user may have to approve
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise YTMError("Pear Desktop didn't approve my connection. Allow it in the app, "
                               "then ask again.") from exc
            raise YTMError(f"Pear Desktop wouldn't sign me in ({exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Unavailable(str(exc)) from exc
        token = reply.get("accessToken") if isinstance(reply, dict) else None
        if not token:
            raise YTMError("Pear Desktop didn't give me an access token.")
        self._store_token(token)
        return token

    def _request(self, method: str, path: str, body=None, timeout: float = 5.0):
        if not self.enabled:
            raise Unavailable("YouTube Music control is switched off")
        if time.monotonic() < self._down_until:
            # Every media command tries this API first; an unreachable host would otherwise cost
            # a full connect timeout each time before the media-key fallback runs.
            raise Unavailable("Pear Desktop was unreachable a moment ago")
        token = self._token()
        for attempt in (0, 1):
            try:
                reply = self._open(method, path, body, timeout, token)
                self._down_until = 0.0
                return reply
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and attempt == 0:
                    token = self._authenticate()
                    continue
                raise YTMError(f"Pear Desktop returned an error ({exc.code}).") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._down_until = time.monotonic() + DOWN_BACKOFF
                raise Unavailable(str(exc)) from exc
        raise YTMError("Pear Desktop wouldn't accept my access token.")

    # ---- reading state -------------------------------------------------------------
    def song(self) -> dict | None:
        data = self._request("GET", "/api/v1/song")
        return data if isinstance(data, dict) and data.get("videoId") else None

    def now_playing(self) -> str:
        song = self.song()
        if not song:
            return "Nothing is playing in YouTube Music right now."
        return f"{_describe(song)}." + (" It's paused." if song.get("isPaused") else "")

    def up_next(self) -> str:
        data = self._request("GET", "/api/v1/queue/next")
        if not isinstance(data, dict) or not data.get("title"):
            return "There's nothing next in the queue."
        return f"Up next is {_describe(data)}."

    # ---- controls ------------------------------------------------------------------
    def media(self, action: str) -> str:
        """play / pause / toggle / next / previous / stop / mute / volume_up / volume_down."""
        if action in ("volume_up", "volume_down", "mute"):
            # Live test: GET /volume always answered {state: 0, isMuted: false}, so a relative
            # step can't be computed and mute can't be verified. Let the system media keys do
            # these (callers treat Unavailable as "use the media keys").
            raise Unavailable("relative volume / mute aren't reliable through the API")
        endpoint = _POSTS.get(action)
        if endpoint is None:
            raise YTMError(f"I don't know how to {action} the music.")
        self._request("POST", f"/api/v1/{endpoint}")
        return _MESSAGES[action]

    def set_volume(self, percent: float) -> str:
        percent = max(0, min(100, round(percent)))
        self._request("POST", "/api/v1/volume", {"volume": percent})
        return f"Volume set to {percent} percent."

    def seek(self, seconds: float) -> str:
        """Skip forward (positive) or back (negative) within the current song."""
        amount = abs(seconds)
        self._request("POST", "/api/v1/go-forward" if seconds > 0 else "/api/v1/go-back", {"seconds": amount})
        return f"Skipping {'ahead' if seconds > 0 else 'back'} {_spoken_seconds(amount)}."

    def like(self) -> str:
        self._request("POST", "/api/v1/like")
        return "Liked it."

    def dislike(self) -> str:
        self._request("POST", "/api/v1/dislike")
        return "Disliked it."

    def shuffle(self) -> str:
        self._request("POST", "/api/v1/shuffle")
        return "Shuffled the queue."

    def search_and_play(self, query: str) -> str:
        """Search YouTube Music, queue the top result right after the current song and skip to it."""
        data = self._request("POST", "/api/v1/search", {"query": query}, timeout=12)
        video_id = _first_video_id(data)
        if not video_id:
            raise YTMError(f"I couldn't find {query} on YouTube Music.")
        self._request("POST", "/api/v1/queue", {"videoId": video_id, "insertPosition": "INSERT_AFTER_CURRENT_VIDEO"})
        # Where it landed isn't reliable (a live test put it *behind* the up-next track), so find
        # it in the queue and jump straight to that index instead of skipping with "next".
        time.sleep(0.5)
        rows = _queue_rows(self._request("GET", "/api/v1/queue"))
        current = next((i for i, (_vid, selected) in enumerate(rows) if selected), -1)
        hits = [i for i, (vid, _sel) in enumerate(rows) if vid == video_id]
        if not hits:
            return f"I couldn't find {query} in the queue after adding it."
        index = next((i for i in hits if i > current), hits[-1])
        self._request("PATCH", "/api/v1/queue", {"index": index})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.5)
            song = self.song()
            if song and song.get("videoId") == video_id:
                if song.get("isPaused"):
                    self._request("POST", "/api/v1/play")
                return f"Playing {_describe(song)}."
        return f"I added {query} to your queue, but it didn't start playing."

    # ---- plumbing --------------------------------------------------------------------
    def run(self, name: str, *args) -> str | None:
        """Calls the named action. None = Pear Desktop unavailable (caller decides what to do);
        a YTMError comes back as its message."""
        try:
            return getattr(self, name)(*args)
        except Unavailable:
            return None
        except YTMError as exc:
            return str(exc)

    def status(self) -> str:
        """For Settings' 'Test connection'."""
        if not self.enabled:
            return "YouTube Music control is switched off."
        self._down_until = 0.0       # an explicit test always tries for real
        try:
            song = self.song()
        except Unavailable:
            return f"Couldn't reach Pear Desktop at {self._base()}. Is it running with the API server enabled?"
        except YTMError as exc:
            return str(exc)
        return f"Connected. Now playing: {_describe(song)}." if song else "Connected. Nothing is playing."


def _spoken_seconds(seconds: float) -> str:
    seconds = round(seconds)
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"
