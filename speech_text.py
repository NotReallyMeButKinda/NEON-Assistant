"""
speech_text.py -- the last step before text reaches a voice: numbers and symbols become words.

Voices guess at digits and symbols, and guess differently ("2008" came out as "two thousand and eight" or
"twenty zero eight", "$5" as "dollar five"). So everything that isn't a letter, a comma or a sentence
mark is spelled out here, the way a person would read it aloud:

    2008 -> two thousand eight          1999 -> nineteen ninety-nine      2025 -> twenty twenty-five
    1,250 -> one thousand two hundred fifty                               21st -> twenty-first
    $5.50 -> five dollars and fifty cents   3:30 pm -> three thirty p m   2008-2012 -> two thousand eight to twenty twelve
    40% -> forty percent   A & B -> A and B   #1 -> number one   (like this) -> like this

Only what is *spoken* changes; the chat and the captions keep the original text.
"""

from __future__ import annotations

import re

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = [(10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")]
_ORDINAL_WORDS = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
                  "nine": "ninth", "twelve": "twelfth"}


def number_words(n: int) -> str:
    """12 -> 'twelve', 1234 -> 'one thousand two hundred thirty-four', -5 -> 'minus five'."""
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {number_words(rest)}" if rest else "")
    for size, name in _SCALES:
        if n >= size:
            big, rest = divmod(n, size)
            return f"{number_words(big)} {name}" + (f" {number_words(rest)}" if rest else "")
    return str(n)


def year_words(n: int) -> str:
    """How years are said: 1999 -> nineteen ninety-nine, 1905 -> nineteen oh five, 1900 -> nineteen hundred,
    2000 -> two thousand, 2008 -> two thousand eight, 2025 -> twenty twenty-five."""
    if 2000 <= n <= 2009:
        return number_words(n)
    high, low = divmod(n, 100)
    if low == 0:
        return f"{number_words(high)} hundred"
    if low < 10:
        return f"{number_words(high)} oh {number_words(low)}"
    return f"{number_words(high)} {number_words(low)}"


def ordinal_words(n: int) -> str:
    words = number_words(n)
    head, sep, last = words.rpartition("-" if "-" in words.split(" ")[-1] else " ")
    last = last or words
    if last in _ORDINAL_WORDS:
        last = _ORDINAL_WORDS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return f"{head}{sep}{last}" if head else last


def _digits(text: str) -> str:
    return " ".join(_ONES[int(d)] for d in text)


def _plain(text: str) -> str:
    """An integer as spoken: '0042' keeps its zeros, 4-digit years are years, long runs are digits."""
    digits = text.replace(",", "")
    if len(digits) > 1 and digits.startswith("0"):
        return _digits(digits)                       # 007, zip codes, pins
    if len(digits) > 15:
        return _digits(digits)
    return number_words(int(digits))


def _is_year(text: str, before: str, after: str) -> bool:
    if "," in text or not re.fullmatch(r"\d{4}", text):
        return False
    n = int(text)
    if not 1100 <= n <= 2099:
        return False
    # "1500 meters", "2000 people": a unit or a counted noun after it means a quantity, not a year
    return not re.match(r"\s*(?:m|km|kg|g|mb|gb|tb|mhz|ghz|w|kw|v|ms|hz|ft|lb|lbs|people|dollars|euros|pounds|"
                        r"points|votes|units|copies|times|calories|miles|meters|metres|words|rpm|x|by|p)\b|x\d",
                        after, re.I) \
        and not before.endswith(("$", "£", "€", "x"))


_CURRENCY = {"$": ("dollar", "dollars", "cent", "cents"), "£": ("pound", "pounds", "penny", "pence"),
             "€": ("euro", "euros", "cent", "cents"), "¥": ("yen", "yen", "", "")}
_MONEY = re.compile(r"([$£€¥])\s?(\d[\d,]*)(?:\.(\d{1,2}))?(?:\s?(k|m|bn|million|billion|thousand))?\b", re.I)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s?([ap])\.?m\b)?", re.I)
_AMPM = re.compile(r"\b(\d{1,2})\s?([ap])\.?m\b", re.I)
_MONTH_NAMES = (r"(January|February|March|April|May|June|July|August|September|October|November|December|"
                r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?")
_MONTH_DAY = re.compile(_MONTH_NAMES + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!:)")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_NAMES + r"\b")
_DOMAIN = re.compile(r"\b([a-z0-9-]+)\.(com|org|net|io|ai|dev|app|gg|tv|me|co|uk|ca|de|fr|gov|edu)\b", re.I)
_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = ("January February March April May June July August September October November December").split()


def _iso_date(m: "re.Match") -> str:
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0)
    return f"{_MONTHS[month - 1]} {ordinal_words(day)}, {year_words(year)}"
_RANGE = re.compile(r"\b(\d{1,4})\s?[-–—]\s?(\d{1,4})\b")
# Phone numbers are read digit by digit, in their groups: 555-1234, (555) 123-4567, +1 555 123 4567
_PHONE = re.compile(r"(?<![\d.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])?\d{3}[.-]\d{4}(?![\d.])")


def _phone(m: "re.Match") -> str:
    groups = re.findall(r"\d+", m.group(0))
    return ", ".join(_digits(g) for g in groups)
_DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d.])")
_VERSION = re.compile(r"\b\d+(?:\.\d+){2,}\b")
_NUMBER = re.compile(r"\d[\d,]*(?<!,)")
_SCALE_WORDS = {"k": "thousand", "m": "million", "bn": "billion"}


def _money(m: "re.Match") -> str:
    one, many, sub_one, sub_many = _CURRENCY[m.group(1)]
    whole = int(m.group(2).replace(",", ""))
    scale = (m.group(4) or "").lower()
    scale = _SCALE_WORDS.get(scale, scale)
    if scale:
        cents = f" point {_digits(m.group(3))}" if m.group(3) else ""
        return f"{number_words(whole)}{cents} {scale} {many}"
    text = f"{number_words(whole)} {one if whole == 1 else many}"
    if m.group(3) and sub_one:
        cents = int(m.group(3).ljust(2, "0"))
        if cents:
            text += f" and {number_words(cents)} {sub_one if cents == 1 else sub_many}"
    return text


def _time(m: "re.Match") -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 24 or minute > 59:
        return m.group(0)
    suffix = f" {m.group(3).lower()} m" if m.group(3) else ""
    if minute == 0:
        return number_words(hour) + (suffix or " o'clock")
    spoken_minute = f"oh {number_words(minute)}" if minute < 10 else number_words(minute)
    return f"{number_words(hour)} {spoken_minute}{suffix}"


def _range(m: "re.Match") -> str:
    a, b = m.group(1), m.group(2)
    as_years = _is_year(a, "", "") and (_is_year(b, "", "") or len(b) == 2)
    if as_years:
        second = year_words(int(b)) if len(b) == 4 else year_words(int(a[:2] + b))
        return f"{year_words(int(a))} to {second}"
    return f"{_plain(a)} to {_plain(b)}"


def _number(m: "re.Match", text: str) -> str:
    raw = m.group(0)
    before, after = text[max(0, m.start() - 2):m.start()], text[m.end():m.end() + 12]
    if _is_year(raw, before, after):
        return year_words(int(raw))
    return _plain(raw)


# Symbols said as words. Commas, periods, ! and ? stay (they shape the sentence); apostrophes stay inside words.
_SYMBOL_WORDS = [
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"#\s?(?=\d)"), "number "),
    (re.compile(r"#(?=\w)"), "hashtag "),
    (re.compile(r"(?<=\w)@(?=\w)"), " at "),
    (re.compile(r"@(?=\w)"), "at "),
    (re.compile(r"(?<=\d)\s*°\s*([CF])\b"), lambda m: " degrees " + ("celsius" if m.group(1) == "C" else "fahrenheit")),
    (re.compile(r"\s*°"), " degrees"),
    (re.compile(r"~\s?(?=\d)"), "about "),
    (re.compile(r"\s*\+\s*(?=\d)"), " plus "),
    (re.compile(r"(?<=\d)\+"), " plus"),
    (re.compile(r"\s*<=\s*"), " at most "), (re.compile(r"\s*>=\s*"), " at least "),
    (re.compile(r"\s+<\s+"), " less than "), (re.compile(r"\s+>\s+"), " greater than "),
    (re.compile(r"(?<=\d)\s?x\s?(?=\d)"), " by "),          # 1920x1080
    (re.compile(r"(?<=\d)x\b"), " times"),                   # 2x
    (re.compile(r"\s*%"), " percent"),
]
# Everything else that isn't a letter, a digit, whitespace, an apostrophe or , . ! ? becomes a pause or goes.
_PAUSE = re.compile(r"\s*[;:|—–]\s*|\s+-\s+")
_DROP = re.compile(r"[()\[\]{}\"“”«»*_^`\\<>=~#@$£€¥+]")


def words_for_speech(text: str) -> str:
    """`text` with numbers, prices, times and symbols spelled out and brackets removed."""
    t = str(text)
    t = _ISO_DATE.sub(_iso_date, t)
    t = _PHONE.sub(_phone, t)
    t = _MONTH_DAY.sub(lambda m: f"{m.group(1)} {ordinal_words(int(m.group(2)))}"
                       if 1 <= int(m.group(2)) <= 31 else m.group(0), t)
    t = _DAY_MONTH.sub(lambda m: f"the {ordinal_words(int(m.group(1)))} of {m.group(2)}"
                       if 1 <= int(m.group(1)) <= 31 else m.group(0), t)
    t = re.sub(r"\b(the)\s+the\b", r"\1", t, flags=re.I)             # "on the 3rd of May"
    t = _DOMAIN.sub(r"\1 dot \2", t)
    t = re.sub(r"\bdegrees\s+([CF])\b", lambda m: "degrees " + ("celsius" if m.group(1) == "C" else "fahrenheit"), t)
    t = _MONEY.sub(_money, t)
    t = _TIME.sub(_time, t)
    t = _AMPM.sub(lambda m: f"{number_words(int(m.group(1)))} {m.group(2).lower()} m", t)
    t = _VERSION.sub(lambda m: " point ".join(_plain(p) for p in m.group(0).split(".")), t)
    t = _ORDINAL.sub(lambda m: ordinal_words(int(m.group(1))), t)
    t = _RANGE.sub(_range, t)
    for pattern, spoken in _SYMBOL_WORDS:
        t = pattern.sub(spoken, t)
    t = _DECIMAL.sub(lambda m: f"{_plain(m.group(1))} point {_digits(m.group(2)[:4])}", t)
    t = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", t)     # PS5 -> PS 5, 4K -> 4 K
    t = _NUMBER.sub(lambda m: _number(m, t), t)
    t = _PAUSE.sub(", ", t)
    t = _DROP.sub(" ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"([,.!?])(?:\s*,)+", r"\1", t)                          # ", ," and ". ," from removed symbols
    t = re.sub(r"^[\s,]+", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()
