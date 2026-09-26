"""
units.py -- "convert 5 miles to kilometres", "how many cups in a litre", "what's 180 F in C".

Entirely local: a table of factors, no network and no language model, so a conversion answers in
well under a millisecond and works with the Wi-Fi off. Temperature and number bases are the two
things a plain multiply can't do, so they get their own paths.

    spoken_conversion(text) -> (value, from_alias, to_alias) | ("base", number, base) | None
    handle_conversion(text) -> the sentence to say, or None if this wasn't a conversion

Adding a unit means one line in `UNITS`: (dimension, how many base units it is, singular, plural).
The base unit of each dimension is whichever one has a factor of exactly 1.
"""

from __future__ import annotations

import re

from mathcalc import _words_to_digits

# alias -> (dimension, factor to the dimension's base unit, singular name, plural name)
# The first alias listed for a unit is the one spoken back.
_U: dict[str, tuple] = {}


def _unit(dimension: str, factor: float, singular: str, plural: str, *aliases: str) -> None:
    for alias in (singular, plural, *aliases):
        _U[alias.lower()] = (dimension, factor, singular, plural)


# ---- length (base: metre) ---------------------------------------------------------------------
_unit("length", 1e-9, "nanometre", "nanometres", "nanometer", "nanometers", "nm")
_unit("length", 1e-6, "micrometre", "micrometres", "micrometer", "micrometers", "micron", "microns", "um")
_unit("length", 0.001, "millimetre", "millimetres", "millimeter", "millimeters", "mm")
_unit("length", 0.01, "centimetre", "centimetres", "centimeter", "centimeters", "cm")
_unit("length", 1.0, "metre", "metres", "meter", "meters", "m")
_unit("length", 1000.0, "kilometre", "kilometres", "kilometer", "kilometers", "km", "kms", "klick", "klicks")
_unit("length", 0.0254, "inch", "inches", "in", "ins", '"')
_unit("length", 0.3048, "foot", "feet", "ft", "fts")
_unit("length", 0.9144, "yard", "yards", "yd", "yds")
_unit("length", 1609.344, "mile", "miles", "mi")
_unit("length", 1852.0, "nautical mile", "nautical miles", "nmi")
_unit("length", 9.4607304725808e15, "light year", "light years", "lightyear", "lightyears", "ly")
_unit("length", 0.201168, "rod", "rods")
_unit("length", 201.168, "furlong", "furlongs")

# ---- mass (base: kilogram) --------------------------------------------------------------------
_unit("mass", 1e-6, "milligram", "milligrams", "milligramme", "milligrammes", "mg")
_unit("mass", 0.001, "gram", "grams", "gramme", "grammes", "g")
_unit("mass", 1.0, "kilogram", "kilograms", "kilogramme", "kilogrammes", "kg", "kgs", "kilo", "kilos")
_unit("mass", 1000.0, "tonne", "tonnes", "metric ton", "metric tons", "t")
_unit("mass", 907.18474, "short ton", "short tons", "us ton", "us tons")
_unit("mass", 0.028349523125, "ounce", "ounces", "oz")
_unit("mass", 0.45359237, "pound", "pounds", "lb", "lbs")
_unit("mass", 6.35029318, "stone", "stones", "st")

# ---- volume (base: litre) ---------------------------------------------------------------------
_unit("volume", 0.001, "millilitre", "millilitres", "milliliter", "milliliters", "ml", "cc")
_unit("volume", 1.0, "litre", "litres", "liter", "liters", "l")
_unit("volume", 0.00492892159375, "teaspoon", "teaspoons", "tsp")
_unit("volume", 0.01478676478125, "tablespoon", "tablespoons", "tbsp", "tbs")
_unit("volume", 0.0295735295625, "fluid ounce", "fluid ounces", "fl oz", "floz", "fluid oz")
_unit("volume", 0.2365882365, "cup", "cups")
_unit("volume", 0.473176473, "pint", "pints", "pt")
_unit("volume", 0.946352946, "quart", "quarts", "qt")
_unit("volume", 3.785411784, "gallon", "gallons", "gal")
_unit("volume", 4.54609, "imperial gallon", "imperial gallons", "uk gallon", "uk gallons")
_unit("volume", 0.56826125, "imperial pint", "imperial pints", "uk pint", "uk pints")
_unit("volume", 1000.0, "cubic metre", "cubic metres", "cubic meter", "cubic meters", "m3")

# ---- temperature is not a simple factor: handled by _TEMPERATURE below -------------------------

# ---- data (base: byte) ------------------------------------------------------------------------
_unit("data", 0.125, "bit", "bits", "b")
_unit("data", 1.0, "byte", "bytes")
_unit("data", 1000.0, "kilobyte", "kilobytes", "kb")
_unit("data", 1e6, "megabyte", "megabytes", "mb", "meg", "megs")
_unit("data", 1e9, "gigabyte", "gigabytes", "gb", "gig", "gigs")
_unit("data", 1e12, "terabyte", "terabytes", "tb")
_unit("data", 1e15, "petabyte", "petabytes", "pb")
_unit("data", 1024.0, "kibibyte", "kibibytes", "kib")
_unit("data", 1024.0 ** 2, "mebibyte", "mebibytes", "mib")
_unit("data", 1024.0 ** 3, "gibibyte", "gibibytes", "gib")
_unit("data", 1024.0 ** 4, "tebibyte", "tebibytes", "tib")

# ---- speed (base: metre per second) -----------------------------------------------------------
_unit("speed", 1.0, "metre per second", "metres per second", "meters per second", "m/s", "mps")
_unit("speed", 1 / 3.6, "kilometre per hour", "kilometres per hour", "kilometers per hour",
      "km/h", "kph", "kmh", "kilometres an hour")
_unit("speed", 0.44704, "mile per hour", "miles per hour", "mph", "miles an hour")
_unit("speed", 0.514444, "knot", "knots", "kt", "kn")
_unit("speed", 0.3048, "foot per second", "feet per second", "ft/s", "fps")

# ---- time (base: second) ----------------------------------------------------------------------
_unit("time", 0.001, "millisecond", "milliseconds", "ms")
_unit("time", 1.0, "second", "seconds", "sec", "secs", "s")
_unit("time", 60.0, "minute", "minutes", "min", "mins")
_unit("time", 3600.0, "hour", "hours", "hr", "hrs", "h")
_unit("time", 86400.0, "day", "days", "d")
_unit("time", 604800.0, "week", "weeks", "wk", "wks")
_unit("time", 2629746.0, "month", "months", "mo")
_unit("time", 31556952.0, "year", "years", "yr", "yrs")

# ---- area (base: square metre) ----------------------------------------------------------------
_unit("area", 1.0, "square metre", "square metres", "square meter", "square meters", "sqm", "m2")
_unit("area", 1e6, "square kilometre", "square kilometres", "square kilometers", "km2")
_unit("area", 0.09290304, "square foot", "square feet", "sqft", "ft2")
_unit("area", 4046.8564224, "acre", "acres")
_unit("area", 10000.0, "hectare", "hectares", "ha")
_unit("area", 2589988.110336, "square mile", "square miles", "sqmi", "mi2")

# ---- pressure (base: pascal) ------------------------------------------------------------------
_unit("pressure", 1.0, "pascal", "pascals", "pa")
_unit("pressure", 1000.0, "kilopascal", "kilopascals", "kpa")
_unit("pressure", 100000.0, "bar", "bars")
_unit("pressure", 6894.757293168, "psi", "psi", "psis", "pound per square inch", "pounds per square inch")
_unit("pressure", 101325.0, "atmosphere", "atmospheres", "atm")
_unit("pressure", 100.0, "millibar", "millibars", "mbar", "hpa", "hectopascal", "hectopascals")

# ---- energy (base: joule) ---------------------------------------------------------------------
_unit("energy", 1.0, "joule", "joules", "j")
_unit("energy", 1000.0, "kilojoule", "kilojoules", "kj")
_unit("energy", 4.184, "calorie", "calories", "cal")
_unit("energy", 4184.0, "kilocalorie", "kilocalories", "kcal", "food calorie", "food calories")
_unit("energy", 3600.0, "watt hour", "watt hours", "wh")
_unit("energy", 3.6e6, "kilowatt hour", "kilowatt hours", "kwh")
_unit("energy", 1055.05585262, "btu", "btus")

# ---- angle (base: degree) ---------------------------------------------------------------------
_unit("angle", 1.0, "degree", "degrees", "deg")
_unit("angle", 57.29577951308232, "radian", "radians", "rad")
_unit("angle", 360.0, "turn", "turns", "revolution", "revolutions")

# Temperature: (to celsius, from celsius). Its aliases live here, not in _U.
_TEMPERATURE = {
    "celsius": (lambda c: c, lambda c: c, "degrees Celsius"),
    "fahrenheit": (lambda f: (f - 32) * 5 / 9, lambda c: c * 9 / 5 + 32, "degrees Fahrenheit"),
    "kelvin": (lambda k: k - 273.15, lambda c: c + 273.15, "kelvin"),
}
_TEMP_ALIASES = {
    "c": "celsius", "celsius": "celsius", "centigrade": "celsius", "degrees c": "celsius",
    "degree celsius": "celsius", "degrees celsius": "celsius", "celcius": "celsius",
    "f": "fahrenheit", "fahrenheit": "fahrenheit", "degrees f": "fahrenheit",
    "degrees fahrenheit": "fahrenheit", "farenheit": "fahrenheit",
    "k": "kelvin", "kelvin": "kelvin", "kelvins": "kelvin", "degrees kelvin": "kelvin",
}

def _alias_pattern() -> str:
    names = sorted(set(_U) | set(_TEMP_ALIASES), key=len, reverse=True)
    return "|".join(re.escape(n) for n in names)


_ALIASES_RE = _alias_pattern()
_NUM = r"-?\d+(?:[.,]\d+)?(?:e-?\d+)?"
_LEAD = r"(?:(?:please|hey|ok|okay|so|and|can you|could you|tell me|what'?s|what is|how much is)\s+)*"

_CONVERT = re.compile(
    rf"^{_LEAD}(?:convert|change|turn)\s+(?P<n>{_NUM})\s*(?P<a>{_ALIASES_RE})\b\s*"
    rf"(?:in ?to|into|to|in|as)\s+(?P<b>{_ALIASES_RE})\b[.?!]?$")
_HOW_MANY = re.compile(
    rf"^{_LEAD}how many\s+(?P<b>{_ALIASES_RE})\s+(?:are\s+|is\s+)?(?:there\s+)?(?:in|to|make|makes|make up)\s+"
    rf"(?:a|an|one|(?P<n>{_NUM}))\s*(?P<a>{_ALIASES_RE})\b[.?!]?$")
_PLAIN = re.compile(
    rf"^{_LEAD}(?P<n>{_NUM})\s*(?P<a>{_ALIASES_RE})\b\s*(?:in ?to|into|to|in|as|equals|=)\s+"
    rf"(?P<b>{_ALIASES_RE})\b[.?!]?$")

_BASES = {"binary": 2, "base two": 2, "base 2": 2, "octal": 8, "base eight": 8, "base 8": 8,
          "hex": 16, "hexadecimal": 16, "base sixteen": 16, "base 16": 16,
          "decimal": 10, "base ten": 10, "base 10": 10}
_BASE_RE = re.compile(
    rf"^{_LEAD}(?:convert|change|write|put|express)?\s*(?P<n>0x[0-9a-f]+|0b[01]+|0o[0-7]+|-?\d+)\s*"
    rf"(?:in ?to|into|to|in|as)\s+(?P<base>{'|'.join(sorted(_BASES, key=len, reverse=True))})\b[.?!]?$")


def _lookup(alias: str):
    """('temperature', key) or (dimension, (factor, singular, plural)), or None."""
    a = alias.strip().lower()
    if a in _TEMP_ALIASES:
        return "temperature", _TEMP_ALIASES[a]
    entry = _U.get(a)
    if entry is None:
        return None
    dimension, factor, singular, plural = entry
    return dimension, (factor, singular, plural)


def convert(value: float, from_alias: str, to_alias: str) -> tuple[float, str, str]:
    """(converted value, singular name, plural name) of `to_alias`.
    Raises ValueError when the units are unknown or belong to different dimensions."""
    a, b = _lookup(from_alias), _lookup(to_alias)
    if a is None:
        raise ValueError(f"I don't know the unit '{from_alias}'.")
    if b is None:
        raise ValueError(f"I don't know the unit '{to_alias}'.")
    if a[0] != b[0]:
        raise ValueError(f"{from_alias} and {to_alias} measure different things.")
    if a[0] == "temperature":
        to_c, _f, _n = _TEMPERATURE[a[1]]
        _t, from_c, name = _TEMPERATURE[b[1]]
        return from_c(to_c(value)), name, name
    (factor_a, _s, _p), (factor_b, singular, plural) = a[1], b[1]
    return value * factor_a / factor_b, singular, plural


def pretty(value: float) -> str:
    """A number a person would actually say: no floating-point dust, no pointless zeros."""
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    magnitude = abs(value)
    if magnitude and (magnitude >= 1e15 or magnitude < 1e-4):
        mantissa, _e, exponent = f"{value:.4g}".partition("e")
        return f"{mantissa} times ten to the power of {int(exponent)}"
    digits = 0 if magnitude >= 1000 else 2 if magnitude >= 1 else 4
    text = f"{value:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _to_base(number: int, base: int) -> str:
    if base == 10:
        return str(number)
    digits = {2: "0b", 8: "0o", 16: "0x"}[base]
    body = {2: bin, 8: oct, 16: hex}[base](abs(number))[2:]
    return ("-" if number < 0 else "") + digits + (body.upper() if base == 16 else body)


def spoken_conversion(text: str):
    """('units', value, from_alias, to_alias) | ('base', number, base) | None."""
    t = re.sub(r"\s+", " ", str(text).lower().replace("°", " ")).strip(" .?!")
    if not t:
        return None
    t = _words_to_digits(t)
    t = re.sub(r"\b(?:degrees?|deg)\s+(c|f|k|celsius|centigrade|fahrenheit|kelvin)\b", r"\1", t)

    m = _BASE_RE.match(t)
    if m:
        raw = m.group("n")
        try:
            number = int(raw, 0) if raw.lower().startswith(("0x", "0b", "0o")) else int(raw)
        except ValueError:
            return None
        return "base", number, _BASES[m.group("base")], raw

    for pattern in (_CONVERT, _HOW_MANY, _PLAIN):
        m = pattern.match(t)
        if not m:
            continue
        a, b = m.group("a"), m.group("b")
        raw = m.group("n")
        # A bare "5 m in feet" is fine; a bare "in to minutes" is not a conversion at all.
        if pattern is _PLAIN and raw is None:
            continue
        value = float((raw or "1").replace(",", ""))
        if _lookup(a) is None or _lookup(b) is None:
            continue
        return "units", value, a, b
    return None


def handle_conversion(text: str) -> str | None:
    """The spoken answer for a conversion, or None if `text` wasn't one."""
    parsed = spoken_conversion(text)
    if parsed is None:
        return None
    if parsed[0] == "base":
        _kind, number, base, spoken = parsed
        name = {2: "binary", 8: "octal", 16: "hexadecimal", 10: "decimal"}[base]
        return f"{spoken} in {name} is {_to_base(number, base)}."
    _kind, value, a, b = parsed
    try:
        result, singular, plural = convert(value, a, b)
    except ValueError as exc:
        return str(exc)
    shown = pretty(result)
    name = singular if shown in ("1", "-1") else plural
    return f"{pretty(value)} {_spoken_source(a, value)} is {shown} {name}."


def _spoken_source(alias: str, value: float) -> str:
    """How to read the unit that was given ('5 mi' -> 'miles')."""
    found = _lookup(alias)
    if found is None:
        return alias
    if found[0] == "temperature":
        return _TEMPERATURE[found[1]][2]
    _factor, singular, plural = found[1]
    return singular if abs(value) == 1 else plural


def dimensions() -> dict[str, list[str]]:
    """dimension -> the canonical unit names in it (used by the help text and the tests)."""
    out: dict[str, list[str]] = {"temperature": ["celsius", "fahrenheit", "kelvin"]}
    for dimension, _factor, singular, _plural in _U.values():
        out.setdefault(dimension, [])
        if singular not in out[dimension]:
            out[dimension].append(singular)
    return out
