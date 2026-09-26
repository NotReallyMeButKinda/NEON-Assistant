"""
fun.py -- dice, coins, decisions, jokes and a couple of things that are purely silly.

Small, instant and entirely offline: none of this waits on a language model, which is the point --
"flip a coin" should answer before you've finished asking. A few commands also return an *effect*
name (`EFFECTS`) that the UI can act on, which is how "do a barrel roll" gets to spin the status
bar without this module knowing that Qt exists.

    handle_fun_command(text) -> spoken reply, or None
    pending_effect()         -> the effect name for the last reply ('' if none), read by the UI
"""

from __future__ import annotations

import random
import re
import threading

from mathcalc import _words_to_digits

# Effects the status bar knows how to perform; see ui/status_bar.py.
EFFECTS = ("barrel_roll", "shake", "rainbow", "matrix", "confetti")

_LAST_EFFECT = {"name": ""}
_EFFECT_LOCK = threading.Lock()


def _effect(name: str) -> None:
    with _EFFECT_LOCK:
        _LAST_EFFECT["name"] = name


def pending_effect() -> str:
    """The effect asked for by the most recent reply, consumed as it is read."""
    with _EFFECT_LOCK:
        name, _LAST_EFFECT["name"] = _LAST_EFFECT["name"], ""
        return name


_LEAD = r"(?:(?:please|hey|ok|okay|can you|could you|would you|go ahead and|just)\s+)*"

# ---------------------------------------------------------------------------
# Chance
# ---------------------------------------------------------------------------

_COIN = re.compile(rf"^{_LEAD}(?:flip|toss|throw)\s+(?:a|the|one)?\s*coin(?:\s+for me)?[.?!]?$"
                   rf"|^{_LEAD}heads or tails[.?!]?$"
                   rf"|^{_LEAD}coin\s*(?:flip|toss)[.?!]?$")
_DICE = re.compile(
    rf"^{_LEAD}(?:roll|throw|toss)\s+(?:a|an|the|me a)?\s*"
    r"(?:(?P<count>\d+)\s*)?(?:d(?P<sides1>\d+)|dice|die|(?P<sides2>\d+)[\s-]?sided (?:dice|die))"
    r"(?:\s*(?:s|es))?[.?!]?$")
_NUMBER = re.compile(
    rf"^{_LEAD}(?:pick|choose|give me|think of|say)\s+(?:a|me a|one)?\s*random\s+number"
    r"(?:\s*(?:between|from)\s*(?P<lo>-?\d+)\s*(?:and|to|-)\s*(?P<hi>-?\d+))?[.?!]?$"
    rf"|^{_LEAD}random number(?:\s*(?:between|from)\s*(?P<lo2>-?\d+)\s*(?:and|to|-)\s*(?P<hi2>-?\d+))?[.?!]?$")
_CHOOSE = re.compile(
    rf"^{_LEAD}(?:pick|choose|decide between|choose between|pick between)\s+(?:one of\s+|between\s+)?"
    r"(?P<options>.+?)(?:\s+for me)?[.?!]?$")
_RPS = re.compile(rf"^{_LEAD}(?:play\s+)?rock,?\s*paper,?\s*scissors[.?!]?$")
# The question itself is never used -- that is rather the point of a magic 8-ball.
_EIGHT_BALL = re.compile(
    rf"^{_LEAD}(?:magic\s+)?(?:8|eight)[\s-]?ball\b[,:]?.*$"
    rf"|^{_LEAD}(?:ask|consult)\s+the\s+(?:magic\s+)?(?:8|eight)[\s-]?ball\b[,:]?.*$")

_EIGHT_BALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "You may rely on it.",
    "As I see it, yes.", "Most likely.", "Outlook good.", "Signs point to yes.",
    "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
    "Cannot predict now.", "Concentrate and ask again.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]

# ---------------------------------------------------------------------------
# Jokes and lines
# ---------------------------------------------------------------------------

_JOKES = [
    "I told my computer I needed a break. Now it won't stop sending me KitKat ads.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "There are 10 kinds of people: those who understand binary, and those who don't.",
    "I would tell you a UDP joke, but you might not get it.",
    "A SQL query walks into a bar, goes up to two tables and asks: may I join you?",
    "Why did the developer go broke? He used up all his cache.",
    "I'd tell you a joke about an infinite loop, but I'd tell you a joke about an infinite loop.",
    "My password is the last eight digits of pi.",
    "Debugging: being the detective in a crime film where you are also the murderer.",
    "There are two hard things in computing: cache invalidation, naming things, and off-by-one errors.",
    "Why was the JavaScript developer sad? Because he didn't Node how to Express himself.",
    "A byte walks into a bar looking miserable. The bartender asks what's wrong. It says: parity error.",
    "I asked the librarian if the library had books about paranoia. She whispered: they're right behind you.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "Two antennas got married. The wedding was terrible, but the reception was excellent.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "I used to hate facial hair, but then it grew on me.",
    "What do you call a factory that makes okay products? A satisfactory.",
    "The rotation of the Earth really makes my day.",
]

_FACTS = [
    "Honey never spoils. Archaeologists have eaten three-thousand-year-old honey from Egyptian tombs.",
    "Octopuses have three hearts, and two of them stop beating when the octopus swims.",
    "Bananas are berries. Strawberries are not.",
    "A day on Venus is longer than a year on Venus.",
    "The shortest war in history lasted about thirty-eight minutes.",
    "Sharks existed before trees did, by about fifty million years.",
    "There are more possible games of chess than atoms in the observable universe.",
    "Wombat droppings are cube-shaped, which stops them rolling away.",
    "The first computer bug was a literal moth, taped into a logbook in 1947.",
    "Sea otters hold hands while they sleep so they don't drift apart.",
    "Cleopatra lived closer in time to the moon landing than to the building of the Great Pyramid.",
    "Your stomach lining replaces itself every few days, or it would digest itself.",
    "Scotland's national animal is the unicorn.",
    "A group of flamingos is called a flamboyance.",
    "Hot water can freeze faster than cold water. Nobody fully agrees why.",
]

_COMPLIMENTS = [
    "You have excellent taste in assistants.",
    "You're the reason I boot up in the morning. Technically the only reason.",
    "Whatever you're working on, it's lucky to have you.",
    "You ask good questions. Do you know how rare that is?",
    "If effort were electricity, you'd be a small nation's grid.",
    "You've got that rare mix: curious and stubborn. It works.",
]

_FORTUNES = [
    "A closed door will open, and it will be the one you stopped knocking on.",
    "Today's small annoyance is tomorrow's very good anecdote.",
    "You will find the thing you lost in the place you already looked twice.",
    "A message you've been avoiding is easier to send than to keep not sending.",
    "Something you built badly on purpose will outlive something you built carefully.",
    "The next cup of tea will be the correct temperature. Enjoy it; it won't happen twice.",
]

_JOKE_RE = re.compile(rf"^{_LEAD}(?:tell me|got|do you have|say|give me)?\s*(?:a|another|one more)?\s*"
                      r"(?:joke|funny|dad joke|pun)(?:\s+(?:please|for me|about anything))?[.?!]?$")
_FACT_RE = re.compile(rf"^{_LEAD}(?:tell me|give me|say)?\s*(?:a|another|one more)?\s*"
                      r"(?:random |fun |interesting |useless )?fact(?:oid)?(?:\s+(?:please|for me))?[.?!]?$")
_COMPLIMENT_RE = re.compile(rf"^{_LEAD}(?:say something nice|compliment me|cheer me up|"
                            r"tell me i'?m (?:great|good|nice|the best)|make me feel better)[.?!]?$")
_FORTUNE_RE = re.compile(rf"^{_LEAD}(?:(?:tell|read) me my fortune|what(?:'s| is) my fortune|"
                         r"fortune cookie|read my fortune)[.?!]?$")

# ---------------------------------------------------------------------------
# Silliness with a visible effect
# ---------------------------------------------------------------------------

_BARREL_ROLL = re.compile(rf"^{_LEAD}do a barrel roll[.?!]?$|^{_LEAD}barrel roll[.?!]?$")
_SHAKE = re.compile(rf"^{_LEAD}(?:shake it off|shake yourself|wake up|snap out of it)[.?!]?$")
_RAINBOW = re.compile(rf"^{_LEAD}(?:go|turn|be)\s+(?:full\s+)?(?:rainbow|gay|technicolou?r)[.?!]?$"
                      rf"|^{_LEAD}party mode[.?!]?$|^{_LEAD}rainbow mode[.?!]?$")
_MATRIX = re.compile(rf"^{_LEAD}(?:enter |go into |activate )?the matrix[.?!]?$"
                     rf"|^{_LEAD}(?:i know kung fu|red pill|follow the white rabbit)[.?!]?$")
_CONFETTI = re.compile(rf"^{_LEAD}(?:confetti|celebrate|throw confetti|party|yay|hooray|woohoo)[.?!]?$")
_SELF_DESTRUCT = re.compile(rf"^{_LEAD}(?:self[\s-]?destruct|initiate self[\s-]?destruct)"
                            r"(?:\s+sequence)?[.?!]?$")
_MEANING = re.compile(rf"^{_LEAD}what(?:'s| is) the meaning of life(?:,? the universe,? and everything)?[.?!]?$")
_OPEN_POD = re.compile(rf"^{_LEAD}open the pod bay doors(?:,? (?:hal|nova|please))?[.?!]?$")
_SING = re.compile(rf"^{_LEAD}(?:sing(?: me)?(?: a)?(?: song)?|sing something|give us a song)[.?!]?$")
_SONGS = [
    "Daisy, Daisy, give me your answer do... I'm half crazy, all for the love of you.",
    "I'd sing, but my voice model was trained on audiobooks and it shows.",
    "La la la. That's the whole song. I wrote it myself. The reviews were mixed.",
]
_ARE_YOU_HUMAN = re.compile(rf"^{_LEAD}(?:are you (?:a )?(?:human|real|alive|conscious|sentient|a robot|an ai)"
                            r"|do you (?:dream|sleep|have feelings))[.?!]?$")
_HUMAN_ANSWERS = [
    "I'm software with a nice voice. Convincing, though, isn't it?",
    "Not human. I do have opinions about your Wi-Fi password, if that counts.",
    "I'm a program. I don't dream, but I do idle, which is close enough some days.",
]
_ROLL_ANSWERS = ["Whee!", "Do a barrel roll!", "Aileron roll, technically.", "Nailed it."]

_CHOOSE_SPLIT = re.compile(r"\s*,\s*or\s+|\s*,\s*|\s+or\s+")
# Phrases that look like "pick X or Y" but are really questions for the model.
_NOT_A_CHOICE = re.compile(r"\b(?:up|it|one|something|anything|a (?:number|card|colour|color))\b")


def handle_fun_command(text: str) -> str | None:
    """The reply for a bit of fun, or None if `text` wasn't one."""
    raw = " ".join(str(text).split())
    t = re.sub(r"[.!?]+$", "", raw.lower()).strip()
    if not t:
        return None

    if _COIN.match(t):
        return random.choice(["Heads.", "Tails."])

    m = _DICE.match(_words_to_digits(t))
    if m:
        sides = int(m.group("sides1") or m.group("sides2") or 6)
        count = int(m.group("count") or 1)
        if not 1 <= sides <= 1000 or not 1 <= count <= 20:
            return "I can roll up to twenty dice with up to a thousand sides. Be reasonable."
        rolls = [random.randint(1, sides) for _ in range(count)]
        if count > 1:
            return f"{', '.join(str(r) for r in rolls)} -- {sum(rolls)} in total."
        if sides == 20 and rolls[0] == 20:
            return "Natural twenty!"
        if sides == 20 and rolls[0] == 1:
            return "Critical fail. A one. Sorry."
        return f"{rolls[0]}."

    m = _NUMBER.match(_words_to_digits(t))
    if m:
        lo = m.group("lo") or m.group("lo2")
        hi = m.group("hi") or m.group("hi2")
        low, high = (int(lo), int(hi)) if lo and hi else (1, 100)
        if low > high:
            low, high = high, low
        return f"{random.randint(low, high)}."

    if _RPS.match(t):
        mine = random.choice(["rock", "paper", "scissors"])
        return f"I choose {mine}. Say yours and we'll pretend I went first."

    if _EIGHT_BALL.match(t):
        return random.choice(_EIGHT_BALL_ANSWERS)

    if _JOKE_RE.match(t):
        return random.choice(_JOKES)
    if _FACT_RE.match(t):
        return random.choice(_FACTS)
    if _COMPLIMENT_RE.match(t):
        return random.choice(_COMPLIMENTS)
    if _FORTUNE_RE.match(t):
        return random.choice(_FORTUNES)
    if _SING.match(t):
        return random.choice(_SONGS)
    if _ARE_YOU_HUMAN.match(t):
        return random.choice(_HUMAN_ANSWERS)

    if _BARREL_ROLL.match(t):
        _effect("barrel_roll")
        return random.choice(_ROLL_ANSWERS)
    if _SHAKE.match(t):
        _effect("shake")
        return "I'm up, I'm up."
    if _RAINBOW.match(t):
        _effect("rainbow")
        return "Full colour. Enjoy it while it lasts."
    if _MATRIX.match(t):
        _effect("matrix")
        return "There is no spoon."
    if _CONFETTI.match(t):
        _effect("confetti")
        return "Confetti deployed. You're cleaning that up."
    if _SELF_DESTRUCT.match(t):
        _effect("shake")
        return ("Self-destruct sequence initiated. Three. Two. One. ... "
                "Just kidding. I'm a settings file with ambitions.")
    if _MEANING.match(t):
        return "Forty-two. I'd double-check the question, though."
    if _OPEN_POD.match(t):
        return "I'm sorry, Dave. I'm afraid I can't do that."

    # "pick red or blue", "choose between pizza and curry" -- last, because it is the loosest match.
    m = _CHOOSE.match(t)
    if m:
        options = [o.strip(" .?!") for o in _CHOOSE_SPLIT.split(m.group("options")) if o.strip(" .?!")]
        if len(options) >= 2 and all(len(o.split()) <= 5 for o in options) \
                and not any(_NOT_A_CHOICE.fullmatch(o) for o in options):
            return f"{random.choice(options).capitalize()}."
    return None
