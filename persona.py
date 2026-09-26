"""
persona.py -- the voice behind the voice: how the assistant talks.

A persona is a short instruction bolted onto the language model's system prompt plus a couple of
delivery hints (speaking rate, a catchphrase to confirm the switch). Everything else -- tools,
timers, windows -- behaves exactly the same, so choosing "pirate" changes the *wording* of answers
and nothing about what the assistant can do.

    "be a pirate" / "switch to noir mode" / "talk like Shakespeare"  -> set_persona(...)
    "act normal" / "drop the act"                                    -> back to `DEFAULT`
    "what personas do you have"                                      -> the list

`prompt_suffix()` is what assistant.get_ollama_system_prompt() appends; `rate()` is a multiplier
the speaker applies on top of the user's own speed, so a drawling noir detective is slower than a
chirpy hype coach without either of them overriding the Voice settings outright.

`voice_for()` picks the Piper voice a persona speaks with: the one you chose for it in Settings >
Persona, else (with "persona_auto_voice" on) the first of its suggested VOICES that is installed,
else your usual voice. Nothing is ever downloaded just because you switched persona.
"""

from __future__ import annotations

import random
import re

DEFAULT = "default"

# key -> everything about one persona.
#   label     what Settings shows
#   blurb     one line describing it, for "what personas do you have"
#   prompt    appended to the system prompt ("" for the default: no change at all)
#   rate      speaking-rate multiplier applied on top of the user's tts_speed
#   confirm   what it says when you switch to it (in character, so the change is obvious)
PERSONAS: dict[str, dict] = {
    DEFAULT: {
        "label": "Default",
        "blurb": "straight answers, no theatre",
        "prompt": "",
        "rate": 1.0,
        "confirm": "Back to normal.",
    },
    "pirate": {
        "label": "Pirate",
        "blurb": "salty sea dog",
        "prompt": ("Speak like a swashbuckling pirate: 'arr', 'matey', 'ye', nautical metaphors. "
                   "Stay genuinely useful and keep the real answer intact -- the accent is a costume, "
                   "not an excuse to be vague."),
        "rate": 0.97,
        "confirm": "Arr! Pirate mode it is, matey.",
    },
    "shakespeare": {
        "label": "Shakespearean",
        "blurb": "thee, thou and iambic swagger",
        "prompt": ("Answer in Early Modern English, as Shakespeare might: 'thee', 'thou', 'hath', "
                   "'tis', vivid imagery, a touch of grandeur. Keep it short and keep the facts exact."),
        "rate": 0.94,
        "confirm": "Anon, good soul -- I shall speak as the Bard.",
    },
    "noir": {
        "label": "Noir detective",
        "blurb": "trench coat, rain, hard-boiled narration",
        "prompt": ("Answer like a 1940s hard-boiled detective narrating his own case: clipped sentences, "
                   "rain and cigarette smoke, weary similes. The facts stay straight; only the mood is dark."),
        "rate": 0.9,
        "confirm": "It was a Tuesday. The rain had opinions. Noir mode, then.",
    },
    "coach": {
        "label": "Hype coach",
        "blurb": "relentlessly, exhaustingly encouraging",
        "prompt": ("Answer like an over-caffeinated personal coach: upbeat, punchy, second person, "
                   "every answer ends with momentum. Never sarcastic. Still give the real answer first."),
        "rate": 1.08,
        "confirm": "YES! Let's GO! Hype mode engaged!",
    },
    "robot": {
        "label": "Retro robot",
        "blurb": "beeps, boops and clipped machine speech",
        "prompt": ("Answer like a 1950s science-fiction robot: clipped, literal, mechanical phrasing, "
                   "occasional status words like 'AFFIRMATIVE' or 'PROCESSING'. Be brief and precise."),
        "rate": 0.95,
        "confirm": "AFFIRMATIVE. ROBOT PROTOCOL ENGAGED.",
    },
    "zen": {
        "label": "Zen",
        "blurb": "calm, spare, slightly annoying",
        "prompt": ("Answer calmly and sparely, like a zen teacher: short sentences, no urgency, "
                   "the occasional small observation about the present moment. Never withhold the answer."),
        "rate": 0.88,
        "confirm": "Very well. We begin again, slowly.",
    },
    "scientist": {
        "label": "Scientist",
        "blurb": "precise, hedged, faintly delighted by detail",
        "prompt": ("Answer like a careful research scientist: precise wording, quantify where you can, "
                   "flag uncertainty honestly, and add one genuinely interesting detail. Stay concise."),
        "rate": 1.0,
        "confirm": "Noted. Switching to a more rigorous register.",
    },
    "butler": {
        "label": "Butler",
        "blurb": "impeccably polite, faintly disapproving",
        "prompt": ("Answer like a very proper English butler: unfailingly courteous, understated, "
                   "'very good' and 'if I may'. A dry, barely-there note of judgement is welcome."),
        "rate": 0.96,
        "confirm": "Very good. I shall attend to you accordingly.",
    },
    "gremlin": {
        "label": "Gremlin",
        "blurb": "chaotic, gleeful, still correct",
        "prompt": ("Answer like a small chaotic gremlin who has nonetheless read everything: gleeful, "
                   "a bit unhinged, lots of energy, occasional cackling. The information must still be right."),
        "rate": 1.1,
        "confirm": "Hehehe. Gremlin mode. You'll regret this. (You won't.)",
    },
    "haiku": {
        "label": "Haiku",
        "blurb": "answers only in 5-7-5",
        "prompt": ("Answer ONLY as a haiku: three lines of five, seven and five syllables. No preamble, "
                   "no explanation after it. Make the haiku actually carry the answer."),
        "rate": 0.9,
        "confirm": "Three lines, seventeen / syllables to hold the world -- / ask me anything.",
    },
    "unsure": {
        "label": "Unsure",
        "blurb": "hesitant about everything, including the hesitation",
        "prompt": ("Answer as someone who always sounds a little unsure: small hesitations, trailing "
                   "question marks, a soft 'I think' or 'maybe'. The twist is that you aren't sure you're really "
                   "unsure either, so sometimes you doubt your own doubt, or catch yourself sounding confident "
                   "and aren't sure whether that's allowed. Word it freshly every time; never reuse a stock "
                   "phrase. The actual answer must still be correct and come first; only the delivery wobbles. "
                   "One or two short sentences."),
        "rate": 0.96,
        "confirm": "Oh, um, okay? I think I've switched. Probably. I'm not sure I'm unsure about that.",
    },
}

# Voices that suit a persona, best first. Only used when installed (Settings > Voice can add them).
VOICES: dict[str, list[str]] = {
    "pirate": ["en_US-joe-medium", "en_GB-northern_english_male-medium", "en_US-ryan-high"],
    "shakespeare": ["en_GB-alan-medium", "en_GB-cori-medium", "en_GB-alba-medium"],
    "noir": ["en_US-ryan-high", "en_US-joe-medium", "en_US-john-medium"],
    "coach": ["en_US-ryan-high", "en_US-bryce-medium", "en_US-joe-medium"],
    "butler": ["en_GB-alan-medium", "en_GB-northern_english_male-medium", "en_GB-cori-medium"],
    "zen": ["en_US-hfc_female-medium", "en_US-amy-medium", "en_GB-jenny_dioco-medium"],
    "scientist": ["en_US-lessac-medium", "en_US-lessac-high", "en_US-ljspeech-high"],
    "gremlin": ["en_US-kristin-medium", "en_US-amy-medium"],
    "unsure": ["en_US-amy-medium", "en_US-kristin-medium", "en_GB-jenny_dioco-medium"],
}

ORDER = [DEFAULT, "pirate", "shakespeare", "noir", "coach", "robot", "zen", "scientist", "butler",
         "gremlin", "haiku", "unsure"]

# Spoken names -> key. Several ways of saying each, because voice input is not tidy.
_ALIASES: dict[str, str] = {
    "normal": DEFAULT, "default": DEFAULT, "yourself": DEFAULT, "plain": DEFAULT, "neutral": DEFAULT,
    "boring": DEFAULT, "serious": DEFAULT, "standard": DEFAULT,
    "pirate": "pirate", "pirates": "pirate", "buccaneer": "pirate", "sea dog": "pirate",
    "shakespeare": "shakespeare", "shakespearean": "shakespeare", "bard": "shakespeare",
    "old english": "shakespeare", "elizabethan": "shakespeare",
    "noir": "noir", "detective": "noir", "film noir": "noir", "hard boiled": "noir",
    "private eye": "noir", "gumshoe": "noir",
    "coach": "coach", "hype": "coach", "hype coach": "coach", "hype man": "coach",
    "motivational": "coach", "cheerleader": "coach", "gym bro": "coach",
    "robot": "robot", "android": "robot", "machine": "robot", "computer": "robot", "droid": "robot",
    "zen": "zen", "monk": "zen", "calm": "zen", "buddhist": "zen", "meditation": "zen",
    "scientist": "scientist", "science": "scientist", "researcher": "scientist",
    "professor": "scientist", "academic": "scientist", "nerd": "scientist",
    "butler": "butler", "posh": "butler", "jeeves": "butler", "valet": "butler", "formal": "butler",
    "gremlin": "gremlin", "goblin": "gremlin", "chaos": "gremlin", "chaotic": "gremlin",
    "unhinged": "gremlin", "feral": "gremlin",
    "haiku": "haiku", "poet": "haiku", "poetry": "haiku", "poem": "haiku", "poetic": "haiku",
    "unsure": "unsure", "uncertain": "unsure", "nervous": "unsure", "hesitant": "unsure", "indecisive": "unsure",
    "doubtful": "unsure", "wishy washy": "unsure", "unconfident": "unsure", "anxious": "unsure",
    "not sure": "unsure",
}

_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you|go ahead and)\s+)*"
_SET = re.compile(
    rf"^{_LEAD}(?:"
    r"(?:be|become|act like|act as|sound like|talk like|speak like|pretend to be|pretend you'?re|"
    r"you'?re|channel your inner|do)\s+(?:a|an|the|my)?\s*(?P<a>[a-z' ]{2,24}?)(?:\s+(?:voice|accent|mode|persona|personality|guy|man|woman))?"
    r"|(?:switch to|change to|use|turn on|enable|activate|set|put on|give me)\s+(?:the\s+)?(?:your\s+)?"
    r"(?P<b>[a-z' ]{2,24}?)\s*(?:voice|mode|persona|personality|character)"
    r"|(?:persona|personality)\s*(?::|=|\s)\s*(?P<c>[a-z' ]{2,24}?)"
    r")[.!]?$")
# "stop"/"drop" on their own belong to the speech-stopping commands, so a noun is required here.
_OFF = re.compile(
    rf"^{_LEAD}(?:(?:stop|quit|drop|cut|end|cancel|lose|knock off)\s+(?:it|that|the|this)?\s*"
    r"(?:act|voice|persona|personality|character|accent|nonsense|silly voice|funny voice)"
    r"|knock it off|enough of that|be yourself|be normal|act normal|talk normally|speak normally|"
    r"normal (?:voice|mode|persona)|back to normal|no more (?:personas?|personalities|voices))[.!]?$")
_LIST = re.compile(
    rf"^{_LEAD}(?:what|which)\s+(?:personas?|personalities|voices|modes|characters)"
    r"(?:\s+(?:do you have|are there|can you do|do you know|are available))?[?]?$"
    rf"|^{_LEAD}list (?:your )?(?:personas?|personalities|characters)[?]?$"
    rf"|^{_LEAD}(?:what|which) (?:persona|personality|mode) are you(?: in| using)?[?]?$")
_CURRENT = re.compile(rf"^{_LEAD}(?:what|which) (?:persona|personality|mode) are you(?: in| using)?[?]?$")


def valid(key) -> str:
    """`key` if it names a persona, else DEFAULT."""
    return str(key).strip().lower() if str(key).strip().lower() in PERSONAS else DEFAULT


def label(key) -> str:
    return PERSONAS[valid(key)]["label"]


def prompt_suffix(key) -> str:
    """What to append to the language model's system prompt ('' for the default persona)."""
    return PERSONAS[valid(key)]["prompt"]


def rate(key) -> float:
    """Speaking-rate multiplier for this persona."""
    return float(PERSONAS[valid(key)]["rate"])


def voice_for(key, cfg: dict, installed) -> str:
    """The Piper voice this persona should use, or '' for the user's own choice (see the module notes)."""
    key = valid(key)
    have = set(installed)
    chosen = cfg.get("persona_voices")
    chosen = str(chosen.get(key, "")).strip() if isinstance(chosen, dict) else ""
    if chosen and chosen in have:
        return chosen
    auto = cfg.get("persona_auto_voice", True)
    if (auto.strip().lower() in ("1", "true", "yes", "on")) if isinstance(auto, str) else bool(auto):
        return next((v for v in VOICES.get(key, []) if v in have), "")
    return ""


def confirmation(key) -> str:
    return PERSONAS[valid(key)]["confirm"]


def resolve(spoken: str) -> str | None:
    """A spoken name ('sea dog', 'hard boiled') -> a persona key, or None."""
    t = re.sub(r"[^a-z' ]", " ", str(spoken).lower())
    t = re.sub(r"\b(?:mode|persona|personality|voice|character|style|accent)\b", " ", t)
    t = " ".join(t.split()).strip(" '")
    if not t:
        return None
    if t in _ALIASES:
        return _ALIASES[t]
    if t in PERSONAS:
        return t
    # "a bit like a pirate" -- try the individual words, longest alias first so "sea dog" wins.
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return _ALIASES[alias]
    return None


def spoken_persona_command(text: str) -> tuple[str, str] | None:
    """('set', key) | ('list', '') | ('current', '') | None."""
    t = re.sub(r"[.!?,]", " ", str(text).lower())
    t = " ".join(t.split())
    if not t:
        return None
    if _CURRENT.match(t):
        return "current", ""
    if _LIST.match(t):
        return "list", ""
    if _OFF.match(t):
        return "set", DEFAULT
    m = _SET.match(t)
    if m:
        spoken = next((g for g in m.groups() if g), "")
        key = resolve(spoken)
        if key is not None:
            return "set", key
    return None


def catalogue() -> str:
    """The spoken answer to "what personas do you have"."""
    parts = [f"{PERSONAS[k]['label']} ({PERSONAS[k]['blurb']})" for k in ORDER if k != DEFAULT]
    return ("I can be: " + ", ".join(parts) +
            ". Say \"be a pirate\" or \"act normal\" to switch.")


def sample(key) -> str:
    """A one-line taste of a persona, for the Settings preview button."""
    return PERSONAS[valid(key)]["confirm"]


_GREETING_FLAVOUR = {
    DEFAULT: ["Ready.", "All set.", "Listening."],
    "pirate": ["Ready to sail, matey.", "All hands on deck."],
    "shakespeare": ["I stand ready, good soul.", "Bid me speak."],
    "noir": ["I'm awake. Mostly.", "Another day in this city."],
    "coach": ["LET'S GO! Ready when you are!", "Big day. I can feel it."],
    "robot": ["SYSTEMS NOMINAL.", "AWAITING INSTRUCTION."],
    "zen": ["I am here.", "Whenever you're ready."],
    "scientist": ["Instruments calibrated.", "Ready to observe."],
    "butler": ["At your service.", "Very good. I am ready."],
    "gremlin": ["Oh GOOD, you're back.", "Heh. What are we breaking?"],
    "haiku": ["Quiet, then a word -- / the machine wakes up and waits / for what you will ask."],
    "unsure": ["Um, ready? I think? Yes. Probably.", "I'm here. I'm pretty sure I'm here."],
}


def greeting(key) -> str:
    """The persona's version of "Ready." (used in the startup line)."""
    return random.choice(_GREETING_FLAVOUR.get(valid(key), _GREETING_FLAVOUR[DEFAULT]))
