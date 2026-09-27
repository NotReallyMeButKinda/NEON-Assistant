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
import threading
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

# Albums and playlists: Pear Desktop's API can search and queue single songs, but not list an album's
# tracks, so those come from YouTube Music itself (the same public request its web page makes).
INNERTUBE = "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false"
INNERTUBE_NEXT = "https://music.youtube.com/youtubei/v1/next?prettyPrint=false"
INNERTUBE_CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20250915.01.00", "hl": "en", "gl": "US"}
ALBUMS_FILTER = "EgWKAQIYAWoMEA4QChADEAQQCRAF"      # YouTube Music's "Albums" search filter
MAX_TRACKS = 100
_KIND_WORDS = {"album", "single", "ep", "playlist", "song", "video", "artist", "episode", "podcast", "profile"}
_PAGE_KINDS = {"MUSIC_PAGE_TYPE_ALBUM": "album", "MUSIC_PAGE_TYPE_PLAYLIST": "playlist",
               "MUSIC_PAGE_TYPE_ARTIST": "artist", "MUSIC_PAGE_TYPE_USER_CHANNEL": "artist"}
_ALBUM_WORDS = re.compile(r"\b(?:the\s+)?(?:album|record|lp|ep)\b", re.I)
_PLAYLIST_WORDS = re.compile(r"\b(?:the\s+)?playlist\b", re.I)

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


class _Row(tuple):
    """(video id, is the current song), plus `ids`: every video id of the entry. A song with a music video is
    one entry with two ids (the video's and the audio track's), and either may be the one that was queued."""
    ids: frozenset = frozenset()


def _all_video_ids(node) -> frozenset:
    found, stack = set(), [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            vid = cur.get("videoId")
            if isinstance(vid, str) and re.fullmatch(r"[\w-]{11}", vid):
                found.add(vid)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return frozenset(found)


def _has(row, video_id: str) -> bool:
    return video_id == row[0] or video_id in getattr(row, "ids", ())


def _queue_rows(data) -> list[tuple[str | None, bool]]:
    """(video id, is the current song) for each queue entry, in order (each also carries `.ids`)."""
    rows = []
    for item in (data or {}).get("items", []) if isinstance(data, dict) else []:
        renderer = item.get("playlistPanelVideoRenderer") if isinstance(item, dict) else None
        if renderer is None and isinstance(item, dict):   # entries with a counterpart video are wrapped
            wrapper = item.get("playlistPanelVideoWrapperRenderer") or {}
            renderer = wrapper.get("primaryRenderer", {}).get("playlistPanelVideoRenderer")
        renderer = renderer or {}
        row = _Row((renderer.get("videoId") or _first_video_id(item), bool(renderer.get("selected"))))
        row.ids = _all_video_ids(item)
        rows.append(row)
    return rows


def _runs_text(node) -> list[str]:
    return [str(r.get("text", "")) for r in (node or {}).get("runs", []) if isinstance(r, dict)]


def _result(renderer: dict, card: bool) -> dict | None:
    """One search result as {"kind", "title", "artist", "video_id", "browse_id"}: kind is song / album /
    playlist / artist."""
    if card:                                          # the "Top result" card
        title_runs = (renderer.get("title") or {}).get("runs") or [{}]
        endpoint = title_runs[0].get("navigationEndpoint") or {}
        title = str(title_runs[0].get("text", ""))
        details = _runs_text(renderer.get("subtitle"))
    else:                                             # a row in a list of results
        endpoint = renderer.get("navigationEndpoint") or {}
        columns = [c.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text")
                   for c in renderer.get("flexColumns", [])]
        title = "".join(_runs_text(columns[0])) if columns else ""
        details = _runs_text(columns[1]) if len(columns) > 1 else []
    parts = [p.strip() for p in details if p.strip() and p.strip() != "\u2022"]
    if parts and parts[0].lower() in _KIND_WORDS:
        parts = parts[1:]
    artist = parts[0] if parts else ""
    browse = endpoint.get("browseEndpoint") or {}
    if browse.get("browseId"):
        page = (browse.get("browseEndpointContextSupportedConfigs") or {}).get(
            "browseEndpointContextMusicConfig", {}).get("pageType", "")
        playlist = _first_key(renderer, "watchPlaylistEndpoint").get("playlistId") or             (browse["browseId"][2:] if browse["browseId"].startswith("VL") else None)
        return {"kind": _PAGE_KINDS.get(page, "other"), "title": title, "artist": artist, "video_id": None,
                "browse_id": browse["browseId"], "playlist_id": playlist}
    video = (endpoint.get("watchEndpoint") or {}).get("videoId") or \
        (renderer.get("playlistItemData") or {}).get("videoId") or _first_video_id(renderer)
    if video:
        return {"kind": "song", "title": title, "artist": artist, "video_id": video, "browse_id": None,
                "playlist_id": None}
    return None


def _first_key(node, key: str) -> dict:
    """The first dict under `key` anywhere in node (document order), or {}."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get(key), dict):
                return cur[key]
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return {}


def _results(data) -> list[dict]:
    """A YouTube Music search response's results, in the order they're shown (the top result first)."""
    found, stack = [], [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, card in (("musicCardShelfRenderer", True), ("musicResponsiveListItemRenderer", False)):
                if isinstance(node.get(key), dict):
                    result = _result(node[key], card)
                    if result:
                        found.append(result)
                    if card:                          # the card's own list (more by the artist...) comes after it
                        stack.append(node[key].get("contents"))
                    break
            else:
                stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return found


def _innertube(url: str, payload: dict, timeout: float):
    body = json.dumps(dict(payload, context={"client": INNERTUBE_CLIENT})).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Origin": "https://music.youtube.com",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def collection_tracks(pick: dict, timeout: float = 10.0, attempts: int = 3) -> list[str]:
    """The video ids of an album's or playlist's tracks, in order; [] if YouTube Music can't say.

    First what its own Play button asks for (the "next" request for the album's playlist, which has been
    reliable), then the album / playlist page (which now and then comes back empty, so it's retried)."""
    if pick.get("playlist_id"):
        for attempt in range(attempts):
            data = _innertube(INNERTUBE_NEXT, {"playlistId": pick["playlist_id"], "isAudioOnly": True,
                                               "enablePersistentPlaylistPanel": True}, timeout)
            tracks = queue_ids(data)
            if tracks:
                return tracks
            time.sleep(0.3 * (attempt + 1))
    if pick.get("browse_id"):
        for attempt in range(attempts):
            tracks = track_ids(_innertube(INNERTUBE, {"browseId": pick["browse_id"]}, timeout))
            if tracks:
                return tracks
            time.sleep(0.3 * (attempt + 1))
    return []


def queue_ids(data) -> list[str]:
    """The songs of a "next" (watch playlist) answer: its playlist panel, in order."""
    ids, stack = [], [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            panel = node.get("playlistPanelVideoRenderer")
            if isinstance(panel, dict):
                if panel.get("videoId") and panel["videoId"] not in ids:
                    ids.append(panel["videoId"])
                continue
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return ids[:MAX_TRACKS]


def track_ids(page) -> list[str]:
    """The tracks of an album or playlist page: its list rows (not the "more from" carousels)."""
    ids, stack = [], [page]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            row = node.get("musicResponsiveListItemRenderer")
            if isinstance(row, dict):
                vid = (row.get("playlistItemData") or {}).get("videoId")
                if vid and vid not in ids:
                    ids.append(vid)
                continue
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return ids[:MAX_TRACKS]


def wanted_kind(query: str) -> tuple[str, str]:
    """("album" / "playlist" / "", the query without those words): "play the album Nurture by Porter
    Robinson" asks for the album even if a song by that name is the top result."""
    for kind, words in (("album", _ALBUM_WORDS), ("playlist", _PLAYLIST_WORDS)):
        if words.search(query):
            cleaned = " ".join(words.sub(" ", query).split())
            return kind, re.sub(r"^(?:by|from)\s+", "", cleaned) or query
    return "", query


def _describe(song: dict) -> str:
    title, artist = song.get("title") or "an unknown track", song.get("artist")
    return f"{title} by {artist}" if artist else title


class Client:
    def __init__(self, cfg: dict, token_path: Path, secret_name: str = SECRET_NAME):
        self._cfg = cfg               # read live, so Settings changes apply immediately
        self._token_path = token_path  # legacy plain-text store: migrated into Credential Manager on first use
        self._secret = secret_name
        self._down_until = 0.0        # after a failed connection, don't retry for a moment (see _request)
        self._fill = 0                # bumped by every "play ...": an album still being queued stops
        self.fill_thread: threading.Thread | None = None

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
        """Search YouTube Music and play the top result. A song is queued right after the current one and
        skipped to; an album or playlist plays from its first track, with the rest queued after it in order."""
        self._fill += 1                               # a new request: stop queueing the last album
        kind, query = wanted_kind(query)
        data = self._request("POST", "/api/v1/search", {"query": query}, timeout=12)
        results = _results(data)
        pick = next((r for r in results if r["kind"] == kind), None) if kind else (results[0] if results else None)
        if kind == "album" and pick is None:
            data = self._request("POST", "/api/v1/search", {"query": query, "params": ALBUMS_FILTER}, timeout=12)
            pick = next((r for r in _results(data) if r["kind"] == "album"), None)
        if pick and pick["kind"] in ("album", "playlist"):
            tracks = collection_tracks(pick)
            if tracks:
                return self._play_collection(pick, tracks)
        video_id = pick["video_id"] if pick and pick["video_id"] else _first_video_id(data)
        if not video_id:
            raise YTMError(f"I couldn't find {query} on YouTube Music.")
        return self._play_now(video_id, query)

    def _play_collection(self, pick: dict, tracks: list[str]) -> str:
        name = pick["title"] + (f" by {pick['artist']}" if pick["artist"] and pick["kind"] == "album" else "")
        started = self._play_now(tracks[0], name)
        if not started.startswith("Playing"):
            return started
        if len(tracks) > 1:
            fill = self._fill
            self.fill_thread = threading.Thread(target=self._queue_rest, args=(tracks, fill),
                                                name="Nova-YTMQueue", daemon=True)
            self.fill_thread.start()
        count = f"{len(tracks)} songs" if len(tracks) != 1 else "1 song"
        return f"Playing {name}: {count}."

    def _queue_rows(self) -> list[tuple[str | None, bool]]:
        return _queue_rows(self._request("GET", "/api/v1/queue"))

    def _queue_rest(self, tracks: list[str], fill: int) -> None:
        """Put tracks[1:] right after the one playing, in order. Pear Desktop adds each one a moment after it's
        asked, and not always where it was asked, so each is found once it lands and moved into place."""
        try:
            for position, video_id in enumerate(tracks[1:], 1):
                if fill != self._fill:
                    return                            # another "play ..." came in
                before = self._queue_rows()
                self._request("POST", "/api/v1/queue",
                              {"videoId": video_id, "insertPosition": "INSERT_AFTER_CURRENT_VIDEO"})
                after = before
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and len(after) <= len(before):
                    time.sleep(0.15)
                    after = self._queue_rows()
                landed = _new_index(before, after, video_id)
                current = next((i for i, (_vid, selected) in enumerate(after) if selected), -1)
                if landed is None or current < 0:
                    continue
                anchor = next((i for i in range(current, -1, -1) if _has(after[i], tracks[0])), current)
                target = anchor + position
                if landed != target and target < len(after):
                    self._request("PATCH", f"/api/v1/queue/{landed}", {"toIndex": target})
                    time.sleep(0.1)
        except (Unavailable, YTMError):
            return

    def _play_now(self, video_id: str, query: str) -> str:
        """Queue one song right after the current one and skip to it."""
        before = self._queue_rows()
        self._request("POST", "/api/v1/queue", {"videoId": video_id, "insertPosition": "INSERT_AFTER_CURRENT_VIDEO"})
        # Pear adds it a moment later, and where it lands isn't reliable (a live test put it *behind* the up-next
        # track), so wait for it to appear and jump straight to it instead of skipping with "next".
        index = None
        deadline = time.monotonic() + 4
        while index is None and time.monotonic() < deadline:
            time.sleep(0.2)
            index = _new_index(before, self._queue_rows(), video_id)
        if index is None:
            return f"I couldn't find {query} in the queue after adding it."
        rows = self._queue_rows()
        current = next((i for i, (_vid, selected) in enumerate(rows) if selected), -1)
        if 0 <= current and index > current + 1:          # landed late: right after the current song, then play
            self._request("PATCH", f"/api/v1/queue/{index}", {"toIndex": current + 1})
            index = current + 1
        self._request("PATCH", "/api/v1/queue", {"index": index})
        # Playing when the queue has moved to it. (Not by the song's id: a song with a music video plays
        # under its audio track's id, which may not be the one that was queued.)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.4)
            rows = self._queue_rows()
            if 0 <= index < len(rows) and rows[index][1] and _has(rows[index], video_id):
                song = self.song() or {}
                if song.get("isPaused"):
                    self._request("POST", "/api/v1/play")
                return f"Playing {_describe(song) if song else query}."
        return f"I added {query} to your queue, but it didn't start playing."

    def wait_until_ready(self, seconds: float) -> bool:
        """After starting Pear Desktop: True once its API answers with its queue (its window has loaded)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._down_until = 0.0                    # don't let the unreachable back-off skip the check
            try:
                self._request("GET", "/api/v1/queue", timeout=3)
                time.sleep(1.5)                       # the page answers a moment before it can search
                return True
            except (Unavailable, YTMError):
                time.sleep(1.0)
        return False

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


def _new_index(before: list, after: list, video_id: str) -> int | None:
    """Where the song just added sits in `after` (the queue was `before` it): the one occurrence of it whose
    removal gives back the old queue."""
    old = [vid for vid, _sel in before]
    for i, row in enumerate(after):
        if _has(row, video_id) and [v for v, _s in after[:i] + after[i + 1:]] == old:
            return i
    hits = [i for i, row in enumerate(after) if _has(row, video_id)]
    return hits[-1] if len(hits) > sum(1 for row in before if _has(row, video_id)) else None
