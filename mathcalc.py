"""
mathcalc.py -- spoken maths: "what is twelve times fifteen", evaluated without eval().
"""

from __future__ import annotations

import ast
import math
import operator as op
import re


# ---------------------------------------------------------------------------
# Safe calculator (ast-based -- no eval())
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
}
_ALLOWED_UNARYOPS = {ast.USub: op.neg, ast.UAdd: op.pos}
_ALLOWED_NAMES = {"pi": math.pi, "e": math.e}
_ALLOWED_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "abs": abs, "round": round,
    "floor": math.floor, "ceil": math.ceil,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[node.func.id](*(_eval_node(a) for a in node.args))
        raise ValueError(f"function '{node.func.id}' isn't allowed")
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    raise ValueError("expression contains something that isn't a plain calculation")


def safe_calculate(expression: str) -> float:
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree)


_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | set(_SCALES) | {"and"}

# Spoken operator phrases -> symbols (longest first so "divided by" beats "by").
_SPOKEN_OPS = [
    (r"\bsquare root of\b", " sqrt "), (r"\bto the power of\b", " ** "), (r"\bto the\b", " ** "),
    (r"\bsquared\b", " ** 2 "), (r"\bcubed\b", " ** 3 "),
    (r"\bdivided by\b", " / "), (r"\bover\b", " / "), (r"\bmultiplied by\b", " * "),
    (r"\btimes\b", " * "), (r"\bplus\b", " + "), (r"\bminus\b", " - "), (r"\bnegative\b", " -"),
    (r"\bmod(?:ulo)?\b", " % "), (r"\bpoint\b", "."),
]


def _words_to_digits(text: str) -> str:
    """'two thousand twenty five' -> '2025'; leaves non-number words alone."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in _NUMBER_WORDS and tokens[i] != "and":
            total = current = 0
            while i < len(tokens) and tokens[i] in _NUMBER_WORDS:
                w = tokens[i]
                if w == "and":
                    # "and" only joins number words ("one hundred and five")
                    if i + 1 >= len(tokens) or tokens[i + 1] not in _NUMBER_WORDS:
                        break
                elif w in _UNITS:
                    current += _UNITS[w]
                elif w in _TENS:
                    current += _TENS[w]
                elif w == "hundred":
                    current = max(current, 1) * 100
                else:
                    total += max(current, 1) * _SCALES[w]
                    current = 0
                i += 1
            out.append(str(total + current))
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def normalize_spoken_math(text: str) -> str:
    """Turn spoken arithmetic into symbols ('what is two plus two' ->
    'what is 2 + 2'). Needle rejects a tool argument it can't find in the
    utterance, so the utterance itself has to contain the digits."""
    t = re.sub(r"(?<!\d),|,(?!\d)|[?!]", " ", text.lower())  # keep "1,000" intact
    t = _words_to_digits(t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s*of\s*(\d+(?:\.\d+)?)", r"\1 % of \2", t)
    t = t.replace("^", " ** ")
    t = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", t)
    for pattern, symbol in _SPOKEN_OPS:
        t = re.sub(pattern, symbol, t)
    t = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", t)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    return re.sub(r"\s+", " ", t).strip()


_FILLER = re.compile(
    r"\b(what is|what's|whats|calculate|compute|work out|how much is|how many is|"
    r"tell me|the answer to|equals?|please|hey|can you|could you|solve)\b")


def spoken_math_expression(text: str) -> str | None:
    """Returns a ready-to-evaluate expression if the whole utterance is just
    arithmetic (plus filler like 'what is'), else None."""
    t = normalize_spoken_math(text)
    t = _FILLER.sub(" ", t)
    t = re.sub(r"(\d+(?:\.\d+)?) % of (\d+(?:\.\d+)?)", r"(\1 / 100 * \2)", t)
    t = re.sub(r"\s+", " ", t).strip(" .=")
    if not re.search(r"\d", t) or not re.search(r"[-+*/%]|sqrt", t):
        return None
    if re.fullmatch(r"(?:sqrt|[\d\s.+\-*/%()])+", t) is None:
        return None
    t = re.sub(r"sqrt\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    return t


def calculate(expression: str) -> str:
    try:
        result = safe_calculate(expression)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as exc:
        return f"I couldn't calculate '{expression}' ({exc})."
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{expression} = {result}"
