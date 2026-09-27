"""
commands.py -- the spoken-command parsers that need nothing but the words: renaming, websites, media
keys, Pear Desktop requests, calendar and help questions.

Each `spoken_*` function takes what was said and returns what it means (or None / False), with no side
effects and no settings, so they are easy to test and reuse. assistant.py re-exports them, so existing
callers (`backend.spoken_website(...)`) keep working. Parsers that depend on the assistant's state (the
notification queue, pending confirmations, timers) stay next to that state in assistant.py.
"""

from __future__ import annotations

import re

from mathcalc import _words_to_digits

_RENAME_PATTERNS = [re.compile(p) for p in (
    r"call yourself (?P<n>.+)",
    r"(?:your|ur) (?:new )?name (?:is|will be|should be) (?P<n>.+)",
    r"you(?:'re| are) (?:now )?(?:called|named) (?P<n>.+)",
    r"(?:change|set) your name to (?P<n>.+)",
    r"rename yourself(?: to)? (?P<n>.+)",
    r"(?:go|be) by (?P<n>.+)",
    r"be called (?P<n>.+)",
    r"(?:i(?:'ll| will| want to|'m going to) )(?:call|name) you (?P<n>.+)",
)]
_NOT_NAMES = {"back", "later", "tomorrow", "tonight", "now", "soon", "again", "if", "when",
              "up", "out", "a", "the", "that", "it", "this"}


# "Call me Alex", "my name is Sam", "what's my name", "forget my name": the user's own name (not mine).
_ME_WORDS = r"(?:call me|my name is|my name's|you can call me|please call me|i go by|i'm called|i am called)"
_NOT_USER_NAMES = {"a", "an", "the", "later", "back", "tomorrow", "tonight", "today", "when", "if", "crazy",
                   "maybe", "sometime", "soon", "now", "please", "anytime", "whenever", "that", "it", "this",
                   "on", "at", "in", "after", "before", "about", "ishmael"}
_SET_USER = re.compile(rf"^(?:(?:hey|ok|okay|actually|just|from now on|and)\s+)*{_ME_WORDS}\s+(?P<name>[a-z][a-z' -]{{0,30}}?)"
                       r"(?:\s+(?:from now on|please|instead))?$")
_ASK_USER = re.compile(r"^(?:what(?:'s| is) my name|whats my name|do you know my name|who am i|what do you call me)$")
_FORGET_USER = re.compile(r"^(?:forget my name|stop calling me by (?:my )?name|don'?t call me by (?:my )?name)$")


def spoken_user_name(text: str) -> tuple[str, str] | None:
    """('set', name) | ('ask', '') | ('forget', '') for what the user says about their own name, else None."""
    t = " ".join(re.sub(r"[?!.,]+", " ", str(text).lower()).split())
    if _ASK_USER.fullmatch(t):
        return "ask", ""
    if _FORGET_USER.fullmatch(t):
        return "forget", ""
    m = _SET_USER.fullmatch(t)
    if m:
        words = m.group("name").split()
        if 1 <= len(words) <= 3 and words[0] not in _NOT_USER_NAMES and not any(w in ("me", "you") for w in words):
            return "set", " ".join(words)
    return None


def spoken_rename(text: str) -> str | None:
    """'call yourself Jarvis' / 'your name is Max' -> 'Jarvis' / 'Max', else None.
    Whole-utterance matches only, so a question like "what's your name" is untouched."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    t = re.sub(r"^(?:(?:hey|ok|okay|please|so|from now on|now)\s+)+", "", t)
    t = re.sub(r"\s+(?:from now on|please)$", "", t)
    for pattern in _RENAME_PATTERNS:
        m = pattern.fullmatch(t)
        if m:
            words = m.group("n").split()
            if 1 <= len(words) <= 3 and words[0] not in _NOT_NAMES:
                return m.group("n")
    return None


_TLDS = frozenset(
    "com org net edu gov mil int info biz name pro mobi xyz online site tech store shop blog dev app "
    "io ai co me tv gg fm ly to cc ws us uk ca au nz ie de fr es it nl be se no dk fi ru ua cz at ch "
    "jp kr cn in br mx ar za eu asia cloud page link live news club top wiki art design studio agency "
    "games game lol wtf email world life space fun icu vip work network digital media".split())
_SITE_SPACED_TLDS = "com org net io ai edu gov"    # "youtube com" (no dot heard) is still a site
_OPEN_SITE = re.compile(
    r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)*"
    r"(?:open up|open|launch|go to|goto|visit|browse to|navigate to|take me to|pull up|load|head to)\s+"
    r"(?:(?:the|my|a)\s+)?(?:(?:website|web ?site|site|web ?page|page|url|link|address)\s+)?"
    r"(?:(?:for|called|named|at)\s+)?(?P<target>.+?)(?:\s+(?:please|now))?$")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")


def spoken_website(text: str) -> str | None:
    """'open github.com' / 'go to youtube dot com' / 'visit example.org slash docs' ->
    'github.com' etc. (host plus optional path), or None when the words after the verb don't
    end in a known top-level domain (then it's an app request as usual)."""
    t = re.sub(r"[!?,;]+", " ", text.lower()).strip().rstrip(".").strip()
    m = _OPEN_SITE.match(t)
    if not m:
        return None
    target = m.group("target").strip()
    target = re.sub(r"\s+dot\s+", ".", target)              # "github dot com"
    target = re.sub(r"\s+(?:forward )?slash\s+", "/", target)
    target = re.sub(r"\s*\.\s*", ".", target)               # "github . com"
    if "." not in target.split("/", 1)[0] and re.fullmatch(rf"[a-z0-9-]+ (?:{_SITE_SPACED_TLDS.replace(' ', '|')})", target):
        target = target.replace(" ", ".")                     # "youtube com"
    host, _, path = target.partition("/")
    if not _HOST.fullmatch(host) or host.rsplit(".", 1)[-1] not in _TLDS:
        return None
    return host + (f"/{path}" if path else "")


_MEDIA_PHRASE = re.compile(
    r"^(?:please |hey |can you |could you )*"
    r"(?P<verb>pause|resume|play|unpause|stop|skip|next|previous|mute|unmute|"
    r"volume up|volume down|turn (?:it |the volume |the music )?(?:up|down)|louder|quieter)"
    r"(?: (?:the |this |that )?(?:music|song|songs|track|tune|playback|audio|volume|it))?"
    r"(?: please)?$")


def spoken_media_action(text: str) -> str | None:
    """Maps a bare spoken media command to a media_control action, else None.
    Only whole-utterance matches, so 'play some jazz' still reaches the LLM."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    t = re.sub(r"^(?:next|previous|last) (?:song|track)$", lambda m: m.group(0).split()[0], t)
    m = _MEDIA_PHRASE.match(t)
    if not m:
        return None
    verb = m.group("verb")
    if verb.startswith("turn"):
        return "volume up" if verb.endswith("up") else "volume down"
    return {"unpause": "play"}.get(verb, verb)


_YTM_NAME = r"(?:youtube music|you tube music|yt music|ytm|pear(?: desktop)?)"
_SEEK = re.compile(
    r"(?:skip|go|jump|fast forward|rewind|move)\s*((?:forward|ahead|back|backwards?)\s+)?"
    r"(\d+)\s*(seconds?|secs?|minutes?|mins?)(?:\s+(forward|ahead|back|backwards?))?")

def spoken_music_command(text: str) -> tuple[str, object, bool] | None:
    """Maps a spoken music request to (YTM client method, argument, must_answer_if_unreachable),
    or None. Whole-utterance matches only, so ordinary conversation isn't hijacked."""
    t = re.sub(r"[.!?,]", "", text.lower()).strip()
    t = re.sub(r"^(?:(?:please|hey|can you|could you|go ahead and)\s+)+", "", t)
    t = re.sub(r"\s+please$", "", t)

    if re.fullmatch(r"(?:what(?:'s| is)|whats) (?:playing|this song|this track|the song|currently playing)"
                    r"(?: right now)?|what song is (?:this|playing)(?: right now)?|"
                    r"who (?:sings|is singing|plays) this(?: song)?|what am i listening to", t):
        return "now_playing", None, False
    if re.fullmatch(r"(?:what(?:'s| is)|whats) (?:playing |coming |up )?next|what song is next|"
                    r"what(?:'s| is) the next (?:song|track)|what(?:'s| is) up next", t):
        return "up_next", None, False
    if re.fullmatch(r"(?:i )?(?:like|love|thumbs up|heart) (?:this|the current|that)(?: song| track)?", t):
        return "like", None, False
    if re.fullmatch(r"(?:i )?(?:dislike|hate|thumbs down|(?:don't|do not) like) "
                    r"(?:this|the current|that)(?: song| track)?", t):
        return "dislike", None, False
    if re.fullmatch(r"shuffle(?: the)?(?: queue| music| songs| playlist)?", t):
        return "shuffle", None, False

    digits = _words_to_digits(t)
    m = _SEEK.fullmatch(digits)
    if m:
        direction = (m.group(1) or m.group(4) or "").strip()
        verb = digits.split()[0]
        if direction:
            forward = direction.startswith(("forward", "ahead"))
        elif verb in ("skip", "fast"):      # "skip 30 seconds", "fast forward 30 seconds"
            forward = True
        elif verb == "rewind":
            forward = False
        else:                               # a bare "go 30 seconds" has no direction: not ours
            forward = None
        if forward is not None:
            amount = int(m.group(2)) * (60 if m.group(3).startswith("min") else 1)
            return "seek", (amount if forward else -amount), True
    m = re.fullmatch(r"(?:set |change |turn )?(?:the )?(?:music |youtube music )?volume (?:to|at) "
                     r"(\d+)\s*(?:percent|%)?", digits)
    if m:
        return "set_volume", int(m.group(1)), True

    m = (re.fullmatch(rf"play (?P<q>.+?) (?:on|in|using|from|with) {_YTM_NAME}", t)
         or re.fullmatch(r"play (?:the )?(?:song|track|tune|songs|music) (?P<q>.+)", t)
         # albums and playlists keep the word: ytmusic.wanted_kind() looks for it
         or re.fullmatch(r"play (?P<q>(?:the )?(?:album|playlist) .+)", t)
         or re.fullmatch(r"play (?P<q>.+ (?:album|playlist)(?: by .+)?)", t))
    query = re.sub(r"^by\s+", "", m.group("q").strip()) if m else ""   # "play music by toto"
    if query:
        return "search_and_play", query, True
    return None


_CALENDAR_Q = re.compile(
    r"(?:what(?:'s| is) (?:my |the )?next (?:meeting|event|appointment)|when(?:'s| is) my next (?:meeting|event|appointment)"
    r"|what(?:'s| is) (?:on )?my (?:calendar|schedule)(?: today)?|do i have (?:any )?(?:meetings|events|appointments)(?: today| soon)?"
    r"|what(?:'s| is) coming up)")
_HELP_Q = re.compile(r"(?:help|help me|what can you do|what do you do|what commands (?:do you know|can i use)|what are you able to do)")


def spoken_calendar_query(text: str) -> bool:
    return bool(_CALENDAR_Q.fullmatch(re.sub(r"[.!?,]", "", text.lower()).strip()))


def spoken_help_query(text: str) -> bool:
    return bool(_HELP_Q.fullmatch(re.sub(r"[.!?,]", "", text.lower()).strip()))
