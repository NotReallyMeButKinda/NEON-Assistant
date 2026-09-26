"""
intent.py -- understanding natural speech when the exact-wording parsers don't match.

Two layers:

1. `normalize_utterance` -- a cheap, rule-based clean-up that runs before every parser: speech-to-text
   fillers ("um", "uh"), politeness wrappers ("could you please ... for me") and a leading wake name are
   dropped, so "um, nova, could you please pause the music for me" reaches the media parser as
   "pause the music". It only removes a politeness prefix when a command verb follows, so questions like
   "can you hear me" keep their meaning.

2. `classify` -- when no parser matched, the local AI picks one intent from CATALOGUE and fills its slots
   (Ollama's structured output keeps the reply to a JSON schema). Most intents are *templates*: their
   slots are rendered into a canonical sentence the ordinary parsers already accept ("shut the music up
   for a sec" -> "pause the music"), and that sentence is routed again, so every command keeps a
   single implementation. A few intents are *calls* (open an app, weather, a web lookup) that
   assistant.py runs directly because no parser exists for them.

Nothing here talks to Ollama itself: `classify` is handed a `ask_json(prompt, schema, system)` callable,
which keeps this module pure and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. Cleaning up what was said
# ---------------------------------------------------------------------------

_FILLERS = re.compile(r"\b(?:u+m+|u+h+|e+r+m*|hmm+|mm+)\b[,.]?\s*")
_LEAD_NOISE = re.compile(r"^(?:(?:okay|ok|so|well|alright|right|oh|hey|yo)\b[,.]?\s+)+")
_POLITE = re.compile(
    r"^(?:(?:please|kindly|can you|could you|would you|will you|can u|could u|would you mind|"
    r"do you mind|i want you to|i'd like you to|i would like you to|i need you to|"
    r"go ahead and|just|quickly|maybe|possibly|actually)\b[,]?\s+)+")
_TAIL = re.compile(
    r"(?:[,]?\s+(?:please|for me|for me please|thanks|thank you|thank you very much|real quick|"
    r"quickly|if you can|if you could|if possible|would you|could you|will you|okay|ok|now please))+$")
# A politeness prefix is only dropped when one of these follows ("could you pause" -> "pause"); "can you
# hear me" or "would you rather..." stay questions.
COMMAND_VERBS = frozenset("""
    open close quit exit kill launch start play pause resume stop skip set turn mute unmute lock show
    take grab remind remember forget add move mark put delete remove read summarize summarise explain
    convert run minimize minimise maximize maximise restore tell give flip roll pick copy clear empty
    shut restart reboot sign log search look find check switch change make create go navigate visit
    pull type cancel rewind like dislike shuffle rename call be enable disable save unlock
""".split())


_QUESTION_WORDS = frozenset("what what's whats when where who whose why how which is are was were do does did "
                            "can could will would should".split())


def normalize_utterance(text: str, names: tuple[str, ...] = ()) -> str:
    """What was said, minus fillers, politeness and a leading wake name. Returns `text` unchanged
    (same object) when there is nothing to remove, so callers can compare cheaply."""
    raw = " ".join(str(text).split())
    t = _FILLERS.sub("", raw).strip(" ,")
    for name in sorted({n.strip().lower() for n in names if n and n.strip()}, key=len, reverse=True):
        m = re.match(rf"^(?:hey\s+|ok\s+|okay\s+)?{re.escape(name)}\b(?P<comma>[,.!])?\s*", t, flags=re.I)
        # Without a comma the name only goes when a command or question follows ("nova pause the music"),
        # so an assistant called Max doesn't turn "max volume" into "volume".
        if m and (m.group("comma") or t[m.end():].split(" ", 1)[0].lower() in COMMAND_VERBS | _QUESTION_WORDS):
            t = t[m.end():]
            break
    t = _LEAD_NOISE.sub("", t)
    m = _POLITE.match(t.lower())
    if m:
        rest = t[m.end():]
        if rest.split(" ", 1)[0].lower().strip(",.") in COMMAND_VERBS:
            t = rest
    tail = _TAIL.search(t.lower())
    if tail and tail.start() > 0:
        t = t[:tail.start()]
    t = t.strip(" ,")
    if not t or t.lower() == raw.lower():
        return text
    return t


# Questions whose answer is a checkable fact about the world. The local AI's memory is least reliable
# exactly here (release dates of games and films, who made what), so these always get a web lookup.
_ABOUT_US = re.compile(r"\b(?:you|your|yours|yourself|my|mine|me|i|i'm|we|our|us)\b")
_FACT_PATTERNS = [re.compile(p) for p in (
    r"\bwhen (?:did|does|do|will|is|was|were|are|'s)\b.*\b(?:come|came|coming) out\b",
    r"\bwhen (?:did|does|do|will|is|was|were|are)\b.*\b(?:release[sd]?|launch(?:ed|es)?|premiere[sd]?|air(?:ed|s)?|"
    r"drop(?:ped|s)?|born|die[sd]?|founded|invented|discovered|start(?:ed|s)?|end(?:ed|s)?|happen(?:ed|s)?|"
    r"built|made|written|published|formed|open(?:ed|s)?|announced|win|won|sign(?:ed)?)\b",
    r"\b(?:release|launch|premiere|air|opening) date\b",
    r"\bis\b.+\b(?:out yet|released yet|out already)\b",
    r"^(?:who|which company|which studio) (?:directed|wrote|developed|made|created|invented|founded|composed|"
    r"produced|published|voiced|voices|plays|played|starred|stars|won|sang|sings|owns|runs|discovered|designed|"
    r"painted|built|killed|married|coached|coaches|scored)\b",
    r"^who (?:is|was) the (?:ceo|founder|president|prime minister|director|author|developer|creator|"
    r"voice(?: actor)?|lead|singer|drummer|guitarist|king|queen|mayor|governor|owner|coach|captain)\b",
    r"^how (?:old|tall|long|big|far|high|deep|heavy|many|much)\b.*\b(?:is|was|are|were|did|does|do|has|have)\b",
    r"^(?:in )?what year\b",
    r"\bpopulation of\b",
    r"^(?:what|which) (?:company|studio|country|team|year|actor|actress|platforms?|console|engine|"
    r"record label|network|channel)\b",
    r"^what (?:is|was) the (?:capital|currency|population|height|price|budget|box office|runtime|"
    r"sales|rating|score|age|net worth)\b",
    r"\bhow many (?:copies|units|people|episodes|seasons|games|films|movies|albums|awards|oscars|grammys)\b",
)]


def looks_like_fact_question(text: str) -> bool:
    """True for a question about a checkable fact of the world ("when does GTA 6 come out", "who directed
    Dune"); False for anything about the user or the assistant ("when did I set that timer")."""
    t = " ".join(re.sub(r"[?!.,]", " ", str(text).lower()).split())
    if not t or _ABOUT_US.search(t):
        return False
    return any(p.search(t) for p in _FACT_PATTERNS)


# ---------------------------------------------------------------------------
# 2. The intent catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Intent:
    name: str
    description: str
    template: str = ""            # a sentence the ordinary parsers accept, with {slot} placeholders
    slots: tuple[str, ...] = ()
    call: bool = False            # run by assistant.py directly (no parser exists for it)
    examples: tuple[str, ...] = field(default=())


def _t(name, description, template, *slots, examples=()):
    return Intent(name, description, template, tuple(slots), False, tuple(examples))


def _c(name, description, *slots, examples=()):
    return Intent(name, description, "", tuple(slots), True, tuple(examples))


CATALOGUE: list[Intent] = [
    # --- apps, sites, windows
    _c("open_app", "open / launch / start a program or game on the PC", "app",
       examples=("fire up discord", "get steam going")),
    _c("open_website", "open a website in the browser", "site", examples=("take me to reddit",)),
    _t("close_window", "close a named app or window (not a browser tab)", "close {window}", "window",
       examples=("get rid of spotify", "shut notepad")),
    _t("close_current_window", "close the app or window the user is looking at", "close this window",
       examples=("close whatever this is",)),
    _t("minimize_window", "minimize a window ('' for the current one, 'all' for everything)",
       "minimize {window}", "window"),
    _t("maximize_window", "maximize / make a window full size", "maximize {window}", "window"),
    _t("restore_window", "bring a minimized window back", "restore {window}", "window"),
    # --- music and media
    _t("media_pause", "pause the music or video", "pause the music", examples=("shut the music up for a sec",)),
    _t("media_play", "resume / continue playing", "resume the music"),
    _t("media_next", "skip to the next song", "next song", examples=("i don't like this one, next",)),
    _t("media_previous", "go back to the previous song", "previous song"),
    _t("media_louder", "make the music a bit louder", "volume up"),
    _t("media_quieter", "make the music a bit quieter", "volume down"),
    _t("play_music", "play a song, artist, album or genre", "play {query} on youtube music", "query",
       examples=("put on some lofi", "i wanna hear daft punk")),
    _t("now_playing", "which song is playing", "what's playing"),
    _t("like_song", "like / thumbs up the current song", "like this song"),
    _t("seek_forward", "skip ahead N seconds in the song", "skip ahead {seconds} seconds", "seconds"),
    _t("seek_back", "go back N seconds in the song", "rewind {seconds} seconds", "seconds"),
    # --- the PC
    _t("set_volume", "set the PC volume to a percentage", "set the volume to {percent} percent", "percent"),
    _t("get_volume", "what the volume is at", "what's the volume"),
    _t("mute_pc", "mute the PC's sound", "mute the pc"),
    _t("unmute_pc", "unmute the PC's sound", "unmute the pc"),
    _t("lock_pc", "lock the PC", "lock the pc"),
    _t("sleep_pc", "put the PC to sleep", "put the pc to sleep"),
    _t("shutdown_pc", "shut the PC down", "shut down the pc"),
    _t("restart_pc", "restart the PC", "restart the pc"),
    _t("sign_out", "sign out of Windows", "sign out"),
    _t("empty_bin", "empty the recycle bin", "empty the recycle bin"),
    _t("screenshot", "take a screenshot", "take a screenshot"),
    _t("show_desktop", "show the desktop", "show the desktop"),
    _t("uptime", "how long the PC has been on", "what's the uptime"),
    _t("disk_space", "free disk space", "how much disk space do i have left"),
    _t("brightness", "set screen brightness", "set the brightness to {percent} percent", "percent"),
    # --- timers and reminders
    _t("timer_set", "start a countdown timer ('duration' like '10 minutes')", "set a timer for {duration}",
       "duration", examples=("give me 10 minutes on the clock",)),
    _t("reminder", "remind the user of something after a while", "remind me in {duration} to {what}",
       "duration", "what", examples=("in an hour tell me to call mom",)),
    _t("timer_status", "how long is left on the timers", "how much time is left"),
    _t("timer_cancel", "cancel the timers / reminders", "cancel the timer"),
    # --- memory and clipboard
    _t("remember", "store a fact the user tells you to remember", "remember that {fact}", "fact",
       examples=("don't let me forget the wifi password is hunter2",)),
    _t("recall", "look up something the user asked you to remember earlier",
       "what do you remember about {topic}", "topic", examples=("what was that gate code i gave you",)),
    _t("forget", "forget a stored fact", "forget {topic}", "topic"),
    _t("clipboard_last", "what the user last copied", "what did i copy"),
    _t("copy_reply", "copy your last answer to the clipboard", "copy that"),
    # --- the board (to-do cards)
    _t("board_add", "add a card / task to the to-do board ('card' is the whole task, like 'buy milk'; 'column' "
       "only if the user named one)", "add {card} to {column}", "card", "column",
       examples=("i need to buy milk, put it on my list",)),
    _t("board_move", "move a card to another column", "move {card} to {column}", "card", "column"),
    _t("board_done", "mark a card as done", "mark {card} as done", "card"),
    _t("board_show", "open the board window", "show the board"),
    _t("board_list", "read what's on the board", "what's on my board"),
    _t("board_due", "what cards are due ('when' is today / tomorrow / this week)", "what's due {when}", "when"),
    # --- notifications and calendar
    _t("notifications_read", "read out the latest notification", "read the last notification"),
    _t("notifications_summary", "summarize notifications", "summarize my notifications"),
    _t("notifications_clear", "clear / dismiss notifications", "clear notifications"),
    _t("notifications_pause", "stop reading notifications for a while", "pause notifications for {duration}",
       "duration"),
    _t("calendar", "what's on the user's calendar / next meeting", "what's on my calendar"),
    # --- small utilities
    _t("convert_units", "convert an amount between units", "convert {amount} {from_unit} to {to_unit}",
       "amount", "from_unit", "to_unit"),
    _t("run_routine", "run one of the user's routines by name", "run {name}", "name"),
    _t("summarize_selection", "summarize the text the user highlighted in another app",
       "summarize the selected text"),
    _t("explain_selection", "explain the text the user highlighted", "explain the selected text"),
    _t("joke", "tell a joke", "tell me a joke"),
    _t("coin", "flip a coin", "flip a coin"),
    _t("dice", "roll a die", "roll a dice"),
    _t("help", "what the assistant can do", "what can you do"),
    # --- lookups and conversation
    _c("weather", "the weather, temperature or forecast, now or another day: hot, cold, rain, snow, what to wear "
       "('location' is a place name only, blank = here)", "location", examples=("is it gonna be cold tomorrow",)),
    _c("time", "what time it is now, here or in a place ('location' is a place name only)", "location"),
    _c("fact_lookup", "a question about real-world facts that must be looked up: release dates, people, "
       "films, games, sports, places, history, prices, news, anything that could be wrong from memory",
       "query", examples=("when does the new zelda come out", "who voices mario in the movie")),
    _c("chat", "conversation, opinions, advice, jokes, creative writing, explanations of general concepts",
       examples=("what should i name my cat", "explain how rainbows work")),
]
BY_NAME = {i.name: i for i in CATALOGUE}


def register(intent: Intent) -> None:
    """Add (or replace) an intent -- for features that load later, like the browser and Bitwarden."""
    BY_NAME[intent.name] = intent
    for n, existing in enumerate(CATALOGUE):
        if existing.name == intent.name:
            CATALOGUE[n] = intent
            return
    CATALOGUE.insert(len(CATALOGUE) - 4, intent)          # before the lookups and chat


def template(name, description, template_text, *slots, examples=()) -> Intent:
    return _t(name, description, template_text, *slots, examples=examples)


def call(name, description, *slots, examples=()) -> Intent:
    return _c(name, description, *slots, examples=examples)


# ---------------------------------------------------------------------------
# 3. Asking the model
# ---------------------------------------------------------------------------

SYSTEM = ("You map what a user said to a voice assistant onto exactly one intent from a list, and fill in its "
          "slots with short values taken from what they said. Speech-to-text mistakes are common: read past "
          "them. Slot values are plain words (numbers as digits). Leave a slot empty when it wasn't said. "
          "Choose fact_lookup for any question whose answer is a real-world fact (dates, names, numbers, "
          "people, films, games); choose chat only for conversation and general explanations.")


def _offered(exclude: set) -> list[Intent]:
    return [i for i in CATALOGUE if i.name not in exclude]


def _catalogue_text(exclude: set = frozenset()) -> str:
    lines = []
    for i in _offered(exclude):
        slots = f"({', '.join(i.slots)})" if i.slots else ""
        ex = f"  e.g. {' / '.join(repr(e) for e in i.examples)}" if i.examples else ""
        lines.append(f"- {i.name}{slots}: {i.description}{ex}")
    return "\n".join(lines)


def _schema(exclude: set = frozenset()) -> dict:
    offered = _offered(exclude)
    slot_names = sorted({s for i in offered for s in i.slots})
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": [i.name for i in offered]},
            "slots": {"type": "object", "properties": {s: {"type": "string"} for s in slot_names}},
        },
        "required": ["intent", "slots"],
    }


@dataclass
class Understood:
    intent: str
    slots: dict
    command: str = ""             # the canonical sentence for template intents ("" for calls)

    @property
    def is_call(self) -> bool:
        return BY_NAME[self.intent].call


def _clean_slot(value) -> str:
    text = " ".join(str(value or "").split()).strip(" .,!?\"'")
    return text[:120]


def render(name: str, slots: dict) -> str | None:
    """The canonical sentence for a template intent, or None when a slot it needs is missing."""
    intent = BY_NAME.get(name)
    if intent is None or intent.call:
        return None
    values = {s: _clean_slot(slots.get(s)) for s in intent.slots}
    if name == "board_add" and re.sub(r"^(?:my|the)\s+", "", values.get("column", "").lower()) in (
            "", "list", "to do list", "todo list", "to-do list", "board", "to do board", "todo", "tasks", "task list"):
        values["column"] = "to do"
    if name in ("minimize_window", "maximize_window", "restore_window") and not values.get("window"):
        values["window"] = "this window"
    if name == "board_due" and not values.get("when"):
        values["when"] = "this week"
    if any(not values[s] for s in intent.slots):
        return None
    return intent.template.format(**values).lower()


def classify(text: str, ask_json, context: str = "", exclude: set = frozenset()) -> Understood | None:
    """What the user most likely meant, or None if the model is unavailable or gave nothing usable.
    `ask_json(prompt, schema, system)` returns a dict or None (assistant.ollama_json). `exclude` names
    intents that can't be done right now (the browser isn't connected...), so they aren't offered."""
    prompt = (f"Intents:\n{_catalogue_text(exclude)}\n\n"
              + (f"Recent conversation (for 'it' / 'that'):\n{context}\n\n" if context else "")
              + f"The user said: \"{text}\"\nReply with the intent and its slots.")
    data = ask_json(prompt, _schema(exclude), SYSTEM)
    if not isinstance(data, dict):
        return None
    name = str(data.get("intent") or "")
    if name not in BY_NAME or name in exclude:
        return None
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    slots = {k: _clean_slot(v) for k, v in slots.items() if _clean_slot(v)}
    intent = BY_NAME[name]
    if intent.call:
        return Understood(name, slots)
    command = render(name, slots)
    if command is None:
        return None
    return Understood(name, slots, command)
