"""Playing a whole album or playlist through Pear Desktop, against a fake Pear queue that behaves like the real
one: songs added a moment after they're asked for, and "after the current song" sometimes one slot too late."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ytmusic


def card(title, kind_word, artist, browse_id=None, page="MUSIC_PAGE_TYPE_ALBUM", video_id=None):
    endpoint = ({"browseEndpoint": {"browseId": browse_id, "browseEndpointContextSupportedConfigs": {
        "browseEndpointContextMusicConfig": {"pageType": page}}}} if browse_id else
        {"watchEndpoint": {"videoId": video_id}})
    return {"musicCardShelfRenderer": {"title": {"runs": [{"text": title, "navigationEndpoint": endpoint}]},
                                       "subtitle": {"runs": [{"text": kind_word}, {"text": " • "},
                                                             {"text": artist}]},
                                       "contents": [row("Some song", "Song", artist, video_id="zzzzzzzzzzz")]}}


def row(title, kind_word, artist, browse_id=None, page="MUSIC_PAGE_TYPE_ALBUM", video_id=None):
    item = {"flexColumns": [
        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": title}]}}},
        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": kind_word}, {"text": " • "},
                                                                         {"text": artist}]}}}]}
    if browse_id:
        item["navigationEndpoint"] = {"browseEndpoint": {"browseId": browse_id, "browseEndpointContextSupportedConfigs": {
            "browseEndpointContextMusicConfig": {"pageType": page}}}}
    else:
        item["playlistItemData"] = {"videoId": video_id}
    return {"musicResponsiveListItemRenderer": item}


def search_page(*items):
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": [{"musicShelfRenderer": {"contents": list(items)}}]}}}}]}}}


def vid(n):
    return f"track{n:06d}"                             # 11 characters, like a real video id


class FakePear:
    """Pear Desktop's API: a queue, a search answer, the current song. Added songs land late (after
    `delay` queue reads), and with `misplace`, one slot further than asked."""

    def __init__(self, searches, queue, delay=2, misplace=False, videos=None):
        self.searches = searches                      # list of responses, one per search call
        self.videos = videos or {}                    # audio id -> its music video's id (shown first in the queue)
        self.queue = [[v, i == 0] for i, v in enumerate(queue)]
        self.delay, self.misplace = delay, misplace
        self.pending: list[list] = []
        self.calls: list[tuple] = []

    def _land(self):
        still = []
        for item in self.pending:
            item[0] -= 1
            if item[0] > 0:
                still.append(item)
                continue
            current = next(i for i, (_v, sel) in enumerate(self.queue) if sel)
            at = min(len(self.queue), current + 1 + (1 if self.misplace and len(self.queue) > current + 1 else 0))
            self.queue.insert(at, [item[1], False])
        self.pending = still

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        if path == "/api/v1/search":
            return self.searches.pop(0)
        if method == "POST" and path == "/api/v1/queue":
            self.pending.append([self.delay, body["videoId"]])
            return None
        if method == "GET" and path == "/api/v1/queue":
            self._land()
            return {"items": [self._item(v, sel) for v, sel in self.queue]}
        if method == "PATCH" and path == "/api/v1/queue":
            for i, item in enumerate(self.queue):
                item[1] = i == body["index"]
            return None
        if method == "PATCH" and path.startswith("/api/v1/queue/"):
            item = self.queue.pop(int(path.rsplit("/", 1)[1]))
            self.queue.insert(body["toIndex"], item)
            return None
        if method == "GET" and path == "/api/v1/song":
            self._land()
            current = next(v for v, sel in self.queue if sel)
            return {"videoId": current, "title": current, "artist": "", "isPaused": False}
        if method == "POST" and path == "/api/v1/play":
            return None
        raise AssertionError(f"unexpected {method} {path}")

    def _item(self, video_id, selected):
        if video_id not in self.videos:
            return {"playlistPanelVideoRenderer": {"videoId": video_id, "selected": selected}}
        return {"playlistPanelVideoWrapperRenderer": {                  # the video first, the audio as counterpart
            "primaryRenderer": {"playlistPanelVideoRenderer": {"videoId": self.videos[video_id], "selected": selected}},
            "counterpart": [{"counterpartRenderer": {"playlistPanelVideoRenderer": {"videoId": video_id}}}]}}

    def ids(self):
        return [v for v, _sel in self.queue]


class AlbumTests(unittest.TestCase):
    def client(self, pear):
        client = ytmusic.Client({"ytm_enabled": True}, Path(tempfile.mkdtemp()) / "token")
        client._request = pear.request
        return client

    def play(self, pear, query, tracks):
        client = self.client(pear)
        with mock.patch.object(ytmusic, "collection_tracks", return_value=tracks) as lookup, \
                mock.patch.object(ytmusic.time, "sleep", lambda _s: None):
            reply = client.search_and_play(query)
            if client.fill_thread:
                client.fill_thread.join(10)
        return reply, lookup

    def test_an_album_top_result_plays_all_of_it_in_order(self):
        for misplace in (False, True):
            with self.subTest(misplace=misplace):
                tracks = [vid(n) for n in range(1, 9)]
                pear = FakePear([search_page(card("Nurture", "Album", "Porter Robinson", browse_id="MPREb_nurture"))],
                                ["nowplaying", "autonext001", "autonext002"], misplace=misplace)
                reply, lookup = self.play(pear, "nurture porter robinson", tracks)
                self.assertEqual(lookup.call_args[0][0]["browse_id"], "MPREb_nurture")
                self.assertEqual(reply, "Playing Nurture by Porter Robinson: 8 songs.")
                current = next(i for i, (_v, sel) in enumerate(pear.queue) if sel)
                self.assertEqual(pear.ids()[current:current + 8], tracks)          # all of it, in order, next
                self.assertEqual(pear.ids()[current - 1], "nowplaying")

    def test_songs_with_music_videos(self):
        tracks = [vid(n) for n in range(1, 5)]
        videos = {track: f"musicvid{n:03d}" for n, track in enumerate(tracks)}
        pear = FakePear([search_page(card("Stomach Book", "Album", "Stomach Book", browse_id="MPREb_sb"))],
                        ["nowplaying", "autonext001"], misplace=True, videos=videos)
        reply, _lookup = self.play(pear, "the album stomach book", tracks)
        self.assertEqual(reply, "Playing Stomach Book by Stomach Book: 4 songs.")
        current = next(i for i, (_v, sel) in enumerate(pear.queue) if sel)
        self.assertEqual(pear.ids()[current:current + 4], tracks)

    def test_saying_album_skips_a_song_with_the_same_name(self):
        tracks = [vid(n) for n in range(1, 4)]
        pear = FakePear([search_page(card("Look at the Sky", "Song", "Porter Robinson", video_id="songsongson"),
                                     row("Nurture", "Album", "Porter Robinson", browse_id="MPREb_nurture"))],
                        ["nowplaying"])
        reply, lookup = self.play(pear, "the album nurture by porter robinson", tracks)
        self.assertEqual(pear.calls[0][2], {"query": "nurture by porter robinson"})
        self.assertEqual(lookup.call_args[0][0]["browse_id"], "MPREb_nurture")
        self.assertIn("3 songs", reply)

    def test_saying_album_searches_albums_when_none_is_in_the_results(self):
        tracks = [vid(n) for n in range(1, 3)]
        pear = FakePear([search_page(card("Worlds", "Song", "Someone", video_id="songsongson")),
                         search_page(row("Worlds", "Album", "Porter Robinson", browse_id="MPREb_worlds"))],
                        ["nowplaying"])
        reply, lookup = self.play(pear, "worlds album", tracks)
        self.assertEqual(pear.calls[1][2], {"query": "worlds", "params": ytmusic.ALBUMS_FILTER})
        self.assertEqual(lookup.call_args[0][0]["browse_id"], "MPREb_worlds")
        self.assertEqual(reply, "Playing Worlds by Porter Robinson: 2 songs.")

    def test_a_song_is_still_just_that_song(self):
        pear = FakePear([search_page(card("Shelter", "Song", "Porter Robinson", video_id="shelterxxxx"))], ["nowplaying"])
        reply, lookup = self.play(pear, "shelter", [])
        lookup.assert_not_called()
        self.assertEqual(reply, "Playing shelterxxxx.")
        self.assertEqual(pear.ids(), ["nowplaying", "shelterxxxx"])

    def test_if_the_tracks_cant_be_listed_it_plays_the_first(self):
        pear = FakePear([search_page(card("Nurture", "Album", "Porter Robinson", browse_id="MPREb_nurture"),
                                     row("Look at the Sky", "Song", "Porter Robinson", video_id="lookattheks"))],
                        ["nowplaying"])
        reply, _lookup = self.play(pear, "nurture", [])
        self.assertEqual(pear.ids(), ["nowplaying", "zzzzzzzzzzz"])  # the first song in the response, as before
        self.assertTrue(reply.startswith("Playing"))

    def test_a_new_request_stops_the_old_album(self):
        client = self.client(FakePear([], ["nowplaying"]))
        client._fill = 5
        pear = FakePear([], ["nowplaying"])
        client._request = pear.request
        client._queue_rest([vid(1), vid(2), vid(3)], 4)             # an older request's fill: does nothing
        self.assertEqual(pear.calls, [])


class ParsingTests(unittest.TestCase):
    def test_kind_words(self):
        self.assertEqual(ytmusic.wanted_kind("the album nurture by porter robinson"), ("album", "nurture by porter robinson"))
        self.assertEqual(ytmusic.wanted_kind("tummy ache album"), ("album", "tummy ache"))
        self.assertEqual(ytmusic.wanted_kind("my chill playlist"), ("playlist", "my chill"))
        self.assertEqual(ytmusic.wanted_kind("shelter"), ("", "shelter"))

    def test_album_page_tracks_skip_the_carousels(self):
        page = {"contents": [row("Track 1", "Song", "A", video_id=vid(1)), row("Track 2", "Song", "A", video_id=vid(2)),
                             {"musicCarouselShelfRenderer": {"contents": [{"musicTwoRowItemRenderer": {
                                 "navigationEndpoint": {"watchEndpoint": {"videoId": vid(9)}}}}]}}]}
        self.assertEqual(ytmusic.track_ids(page), [vid(1), vid(2)])


if __name__ == "__main__":
    unittest.main()


class OpenPearTests(unittest.TestCase):
    """Pear Desktop closed: "Should I open it and play ...?", and after a yes, open it and try again."""

    def setUp(self):
        from tests import common
        import assistant as backend
        common.use_temp_config()
        backend.CONFIG["ytm_enabled"] = True
        self.backend = backend
        self.started: list[str] = []
        self.calls: list[tuple] = []

        def run(method, *args):
            self.calls.append((method, args))
            return "Playing Worlds by Porter Robinson: 12 songs." if self.started else None
        patches = [mock.patch.object(backend.YTM, "run", run),
                   mock.patch.object(backend.YTM, "wait_until_ready", lambda seconds: True),
                   mock.patch.object(backend, "_startfile", self.started.append),
                   mock.patch.object(backend, "_APP_CATALOGUE",
                                     {"YouTube Music": {"launch_path": "C:/Start/YouTube Music.lnk"}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def say(self, text):
        class Agent:
            def run(self, text):
                return {"results": [{"status": f"TOOL:{text}"}]}

            def reset(self):
                pass
        return self.backend.handle_utterance(Agent(), text)

    def test_yes_opens_it_and_plays(self):
        self.assertEqual(self.say("play the album worlds by porter robinson"),
                         "YouTube Music isn't open. Should I open it and play the album worlds by porter robinson?")
        self.assertEqual(self.started, [])
        self.assertEqual(self.say("yes"), "Playing Worlds by Porter Robinson: 12 songs.")
        self.assertEqual(self.started, ["C:/Start/YouTube Music.lnk"])
        self.assertEqual(self.calls[-1], ("search_and_play", ("the album worlds by porter robinson",)))

    def test_no_leaves_it(self):
        self.say("play shelter on youtube music")
        self.assertEqual(self.say("no"), "Okay, I'll leave it.")
        self.assertEqual(self.started, [])

    def test_without_pear_installed_it_just_says_so(self):
        with mock.patch.object(self.backend, "_APP_CATALOGUE", {}):
            self.assertEqual(self.say("play shelter on youtube music"), self.backend._YTM_UNREACHABLE)

    def test_waiting_for_it_to_start(self):
        client = ytmusic.Client({"ytm_enabled": True}, Path(tempfile.mkdtemp()) / "token")
        answers = iter([ytmusic.Unavailable("refused"), ytmusic.Unavailable("refused"), {"items": []}])

        def request(method, path, body=None, timeout=5.0):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        client._request = request
        with mock.patch.object(ytmusic.time, "sleep", lambda _s: None):
            self.assertTrue(client.wait_until_ready(30))
