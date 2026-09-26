"""
browser_commands.py -- what can be said about the page open in the browser, and how its text is cut
down to what matters for a question. No I/O here: assistant.handle_browser asks browser_bridge for the
page and the local AI for the answer.

    "summarize this page"                    -> ("summarize", "")
    "read this article"                      -> ("read", "")
    "what does this page say about shipping" -> ("ask", "what does this page say about shipping")
    "what's the price on this page"          -> ("ask", ...)
    "find refund policy on this page"        -> ("find", "refund policy")
    "what tabs do I have open"               -> ("tabs", "")
    "switch to the github tab"               -> ("switch", "github")
    "close this tab" / "close the youtube tab" -> ("close_tab", "" / "youtube")
"""

from __future__ import annotations

import re

_PAGE = r"(?:page|article|website|web ?site|site|tab|post|thread|web ?page|listing|story|blog post|recipe|video page)"
_THIS = r"(?:this|the|that|my)(?: current| open)?"

_PATTERNS: list[tuple[str, re.Pattern]] = [(kind, re.compile(p)) for kind, p in (
    ("summarize", rf"^(?:summari[sz]e|sum up|give me (?:a |the )?(?:summary|tldr|rundown|gist) of|tldr(?: of)?|"
                  rf"what(?:'s| is) the (?:gist|summary) of) {_THIS} {_PAGE}$"),
    ("summarize", rf"^what(?:'s| is) {_THIS} {_PAGE} about$"),
    ("summarize", rf"^what am i (?:reading|looking at)(?: in the browser)?$"),
    ("read", rf"^read (?:me |out )?{_THIS} {_PAGE}(?: (?:to me|out loud|aloud))?$"),
    ("read", rf"^read (?:me )?the (?:page|article) (?:out loud|aloud)$"),
    ("find", rf"^(?:find|search for|look for|highlight|search) (?P<q>.+?) (?:on|in) {_THIS} {_PAGE}$"),
    ("ask", rf"^what does {_THIS} {_PAGE} say about (?P<q>.+)$"),
    ("ask", rf"^(?:on|in|according to|from) {_THIS} {_PAGE}[, ]+(?P<q>.+)$"),
    ("ask", rf"^(?P<q>(?:what|when|who|where|which|how|is|are|does|do|can|did|was|were)\b.+?) "
            rf"(?:on|in|according to|from|for) {_THIS} {_PAGE}$"),
    ("tabs", r"^(?:what|which) tabs (?:do i have|are|have i got)(?: open)?(?: right now)?$"),
    ("tabs", r"^(?:list|read|tell me) (?:all )?(?:of )?my (?:open )?tabs$"),
    ("switch", r"^(?:switch|go|change|jump|flip)(?: back)? to (?:the |my )?(?P<q>.+?) tab$"),
    ("close_tab", r"^close (?:this|the current|that|the) tab$"),
    ("close_tab", r"^close (?:the |my )?(?P<q>.+?) tab$"),
)]


def spoken_browser_command(text: str) -> tuple[str, str] | None:
    """(kind, argument) for something said about the browser, else None. Whole utterances only."""
    t = " ".join(re.sub(r"[?!.,]+(?=\s|$)", "", str(text).lower()).split())
    for kind, pattern in _PATTERNS:
        m = pattern.match(t)
        if m:
            arg = (m.groupdict().get("q") or "").strip()
            if kind == "ask":
                arg = t                       # the model gets the whole question, page words and all
            return kind, arg
    return None


# ---------------------------------------------------------------------------
# Cutting a page down to what matters
# ---------------------------------------------------------------------------

CHUNK_CHARS = 900
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""the a an and or of to in on for is are was were be been this that it its what when who where
which how does do did can could would should will with from about page article site tab say says my me i you
your there their they them at as by than then so if not no yes any some""".split())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def keywords(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """The page's text in pieces of about `size` characters, split at paragraph or sentence ends."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    out, current = [], ""
    for para in paragraphs:
        while len(para) > size:
            cut = para.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 3 else size
            out.append((current + " " + para[:cut]).strip() if current else para[:cut].strip())
            current, para = "", para[cut:].strip()
        if len(current) + len(para) + 1 > size and current:
            out.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        out.append(current)
    return out


def relevant_text(question: str, text: str, budget: int = 12_000) -> str:
    """The parts of the page most likely to answer `question`, in page order, within `budget` characters.
    The opening chunk is always kept (it usually says what the page is)."""
    pieces = chunks(text)
    if sum(len(p) for p in pieces) <= budget:
        return "\n\n".join(pieces)
    wanted = keywords(question)
    scored = []
    for index, piece in enumerate(pieces):
        words = keywords(piece)
        hits = len(wanted & words)
        scored.append((hits + (0.5 if index == 0 else 0.0), -index, index))
    keep, used = set(), 0
    for _score, _neg, index in sorted(scored, reverse=True):
        if used + len(pieces[index]) > budget:
            continue
        keep.add(index)
        used += len(pieces[index])
    return "\n\n[...]\n\n".join(pieces[i] for i in sorted(keep))


def structured_text(page: dict) -> str:
    """The page's machine-readable facts (JSON-LD: prices, release dates, ratings) as lines."""
    facts = page.get("structured") or []
    lines = []
    for item in facts[:12]:
        if isinstance(item, dict):
            parts = [f"{k}: {v}" for k, v in item.items() if v not in (None, "", [], {})]
            if parts:
                lines.append("- " + "; ".join(parts)[:400])
    return "\n".join(lines)


def page_header(page: dict) -> str:
    title = str(page.get("title") or "").strip()
    url = str(page.get("url") or "").strip()
    desc = str(page.get("description") or "").strip()
    return "\n".join(x for x in (f"Title: {title}" if title else "", f"Address: {url}" if url else "",
                                 f"Description: {desc}" if desc else "") if x)


def best_tab(tabs: list[dict], name: str) -> dict | None:
    """The tab whose title or address best matches what was said ("the github tab")."""
    wanted = keywords(name)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for tab in tabs:
        title = str(tab.get("title") or "")
        url = str(tab.get("url") or "")
        words = keywords(title) | keywords(re.sub(r"https?://(www\.)?", "", url).replace(".", " ").replace("/", " "))
        score = len(wanted & words) / len(wanted)
        if name.lower() in title.lower():
            score += 0.5
        if score > best_score:
            best, best_score = tab, score
    return best if best_score >= 0.5 else None


def spoken_host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/:]+)", str(url))
    return m.group(1) if m else ""
