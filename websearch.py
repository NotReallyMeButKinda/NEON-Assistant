"""
websearch.py -- finding facts for a spoken question.

A question is turned into its topic first ("when did geometry dash come out" -> "geometry dash"):
Wikipedia's search matches every word it's given, so the whole question found pages about "coming
out" instead of the game. Then, for the answer:

  * the Wikipedia article about the topic: its opening section, plus Wikidata's structured facts
    (release / publication dates per platform, developer, director, birth date...), which is where
    exact dates live -- an article's short summary often leaves them out;
  * web results from the chosen provider, for anything newer or more specific than an encyclopedia:
        duckduckgo   no key needed (its HTML page)
        brave        Brave Search API; the key is kept in Windows Credential Manager
        searxng      your own SearXNG instance (its URL), JSON format enabled
        off          Wikipedia only

Everything here is plain HTTP with the standard library; each call returns [] / None on failure.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html import unescape

import neon_log
import secrets_store

log = neon_log.get("websearch")

USER_AGENT = "neon-assistant/1.0 (personal voice assistant; contact: user)"
BROWSER_HEADERS = {
    # DuckDuckGo's HTML page soft-blocks requests that don't look like a browser (an empty page, no error).
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://duckduckgo.com/",
}
BRAVE_SECRET = "brave_search_key"
PROVIDERS = ("duckduckgo", "brave", "searxng", "off")
LEAD_CHARS = 3500           # how much of the article's opening section is given to the AI
MAX_RETRY_WAIT = 5          # seconds: a longer "retry after" isn't worth keeping you waiting


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: float = 7.0, retry: bool = True) -> bytes | None:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # Wikimedia answers a burst of requests with 429 and a short Retry-After: wait it out once.
        wait = exc.headers.get("Retry-After", "") if exc.code == 429 else ""
        if retry and wait.isdigit() and int(wait) <= MAX_RETRY_WAIT:
            time.sleep(int(wait) + 0.2)
            return _get(url, headers, timeout, retry=False)
        log.info("search request failed (%s): HTTP %s", url.split("?")[0], exc.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.info("search request failed (%s): %s", url.split("?")[0], exc)
        return None


def _get_json(url: str, headers: dict | None = None, timeout: float = 7.0):
    raw = _get(url, headers, timeout)
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The question's topic
# ---------------------------------------------------------------------------

_LEAD = re.compile(
    r"^(?:(?:hey|ok|okay|please|so|um|uh)\s+)*"
    r"(?:(?:can|could|would) you\s+(?:please\s+)?(?:tell me|look up|search(?: for)?|find(?: out)?|google)\s+"
    r"|(?:tell me|look up|search(?: for)?|find(?: out)?|google|i want to know|do you know)\s+)?"
    r"(?:about\s+)?")
_QUESTION = re.compile(
    r"^(?:when|what|who|where|which|why|how(?: many| much| old| long| tall| big| far)?)"
    r"(?:'s|\s+(?:is|was|are|were|did|does|do|has|have|had|will|would|can|could))?\s+"
    r"(?:the\s+|a\s+|an\s+)?", re.I)
# What is being asked about the topic -- dropped for the search, kept for the AI.
_ASPECT = re.compile(
    r"\s+(?:(?:first\s+|initially\s+|originally\s+)?(?:come|came|comes) out|(?:get|got|was|were|is|be) "
    r"released|release(?:d)?|released on \w+|come out on \w+|launch(?:ed)?|premiere(?:d)?|air(?:ed)?|"
    r"born|die(?:d)?|start(?:ed)?|end(?:ed)?|found(?:ed)?|made|invented|created|built|written|directed|"
    r"developed|published)(?:\s+(?:on|in|for)\s+[\w ]+)?$"
    r"|\s+(?:release date|launch date|air date|premiere date|birthday|birth date|age|height|population|"
    r"net worth|box office|budget|runtime|developer|publisher|director|author|creator|founder|ceo|"
    r"capital|price|cost)$", re.I)
_OF = re.compile(r"^(?:the\s+)?(?:release date|launch date|premiere|air date|birthday|age|height|population|"
                 r"net worth|box office|budget|runtime|developer|publisher|director|author|creator|founder|"
                 r"ceo|capital|price|cost|date)\s+(?:of|for)\s+", re.I)


def topic_of(question: str) -> str:
    """"when did geometry dash come out?" -> "geometry dash"; "who directed oppenheimer" ->
    "oppenheimer"; a plain topic is returned as it is."""
    text = " ".join(str(question).replace("?", " ").split()).strip(" .!")
    lowered = _LEAD.sub("", text.lower(), count=1)
    text = text[len(text) - len(lowered):] if lowered else text
    text = _QUESTION.sub("", text, count=1)
    text = re.sub(r"^(?:directed|wrote|made|created|invented|founded|developed|published|sang|painted)\s+",
                  "", text, flags=re.I)
    text = _OF.sub("", text, count=1)
    for _ in range(2):
        text = _ASPECT.sub("", text)
    kinds = r"(?:movie|film|video game|game|tv show|show|series|book|novel|song|album)"
    if len(text.split()) > 1:                      # "the movie inception", "inception the movie"
        text = re.sub(rf"^(?:the\s+)?{kinds}\s+", "", text, flags=re.I)
        text = re.sub(rf"\s+(?:the\s+)?{kinds}$", "", text, flags=re.I)
    return text.strip(" .,'\"") or " ".join(str(question).split())


# ---------------------------------------------------------------------------
# Wikipedia + Wikidata
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    extract: str                      # the opening section, plain text
    url: str = ""
    image: str = ""                   # a picture for the article, if Wikipedia has a free one
    facts: list[str] = field(default_factory=list)   # "Publication date: 13 August 2013 (Android)"

    def as_dict(self) -> dict:
        return {"title": self.title, "extract": self.extract, "url": self.url, "image": self.image,
                "facts": list(self.facts)}


def wikipedia_titles(query: str, limit: int = 5) -> list[tuple[str, str]]:
    """(title, snippet) of Wikipedia's search results for `query`."""
    data = _get_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "utf8": 1, "srlimit": limit}))
    items = (data or {}).get("query", {}).get("search", [])
    return [(i.get("title", ""), unescape(re.sub(r"<[^>]+>", "", i.get("snippet", ""))).strip()) for i in items]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in ("the", "a", "an", "of")}


def _best_title(topic: str, titles: list[str]) -> list[str]:
    """Titles ordered so the one naming the topic comes first ("Geometry Dash" before "List of ...")."""
    want = _words(topic)

    def score(i_title):
        i, title = i_title
        have = _words(re.sub(r"\s*\(.*?\)", "", title))
        overlap = len(want & have) / max(1, len(want | have))
        return (-(overlap + (0.5 if want and want <= _words(title) else 0)), i)
    return [t for _i, t in sorted(enumerate(titles), key=score)]


def article_for(question: str) -> Article | None:
    """The Wikipedia article the question is about, with its facts; None if there isn't one."""
    topic = topic_of(question)
    titles = [t for t, _s in wikipedia_titles(topic)]
    if not titles and topic != question:
        titles = [t for t, _s in wikipedia_titles(question)]
    for title in _best_title(topic, titles)[:3]:
        article = _article(title)
        if article is not None:
            return article
    return None


def _article(title: str) -> Article | None:
    summary = _get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}")
    if not summary or summary.get("type") == "disambiguation":
        return None
    title = summary.get("title") or title
    pages = (_get_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "prop": "extracts|pageimages", "exintro": 1, "explaintext": 1, "redirects": 1,
         "piprop": "thumbnail", "pithumbsize": 800, "titles": title, "format": "json"})) or {}
    ).get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {}) if pages else {}
    extract = (page.get("extract") or summary.get("extract") or "").strip()
    if not extract:
        return None
    image = (page.get("thumbnail") or {}).get("source") or _any_image(page.get("title") or title)
    url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    facts = wikidata_facts(summary.get("wikibase_item") or "")
    return Article(title, extract[:LEAD_CHARS], url, image, facts)


def _any_image(title: str) -> str:
    """The article's picture when it has no freely licensed one: game covers and film posters are
    "non-free". Only used as a fallback -- asked first, it prefers a logo over a photo."""
    pages = (_get_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800, "pilicense": "any",
         "titles": title, "format": "json"})) or {}).get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {}) if pages else {}
    return (page.get("thumbnail") or {}).get("source") or ""


# Wikidata properties worth reading out, in this order.
_PROPERTIES = [
    ("P577", "Release date"), ("P571", "Founded or created"), ("P580", "Started"), ("P582", "Ended"),
    ("P569", "Born"), ("P570", "Died"), ("P19", "Birthplace"), ("P178", "Developer"), ("P123", "Publisher"),
    ("P57", "Director"), ("P162", "Producer"), ("P58", "Screenwriter"), ("P161", "Cast"), ("P50", "Author"),
    ("P175", "Performer"), ("P86", "Composer"), ("P136", "Genre"), ("P400", "Platform"), ("P2047", "Length"),
    ("P2130", "Budget"), ("P2142", "Box office"), ("P112", "Founded by"), ("P169", "CEO"),
    ("P159", "Headquarters"), ("P17", "Country"), ("P36", "Capital"), ("P1082", "Population"),
    ("P6", "Head of government"), ("P348", "Latest version"), ("P106", "Occupation"), ("P856", "Website"),
]
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
MAX_VALUES = 6              # per property (a cast list, a dozen platforms...)


def _date(value: dict) -> str:
    m = re.match(r"([+-])0*(\d+)-(\d\d)-(\d\d)", value.get("time", ""))
    if not m:
        return ""
    sign, year, month, day = m.groups()
    year = f"{year} BC" if sign == "-" else year
    precision = int(value.get("precision", 11))
    if precision >= 11 and int(day):
        return f"{int(day)} {_MONTHS[int(month) - 1]} {year}"
    if precision == 10 and int(month):
        return f"{_MONTHS[int(month) - 1]} {year}"
    return year


def wikidata_facts(qid: str) -> list[str]:
    """Readable facts for a Wikidata item: "Release date: 13 August 2013 (Android)"."""
    if not re.fullmatch(r"Q\d+", qid or ""):
        return []
    data = _get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
    claims = ((data or {}).get("entities", {}).get(qid) or {}).get("claims", {})
    raw: list[tuple[str, list[tuple[object, list[str]]]]] = []
    wanted_ids: set[str] = set()
    for prop, label in _PROPERTIES:
        values = []
        for claim in claims.get(prop, [])[:MAX_VALUES]:
            if claim.get("rank") == "deprecated":
                continue
            snak = claim.get("mainsnak", {})
            value = (snak.get("datavalue") or {}).get("value")
            if value is None:
                continue
            notes = [q.get("datavalue", {}).get("value", {}).get("id", "")
                     for key in ("P400", "P291") for q in claim.get("qualifiers", {}).get(key, [])]
            notes = [n for n in notes if n]
            if isinstance(value, dict) and "id" in value:
                wanted_ids.add(value["id"])
            wanted_ids.update(notes)
            values.append((value, notes))
        if values:
            raw.append((label, values))
    labels = _labels(sorted(wanted_ids))
    facts = []
    for label, values in raw:
        shown = []
        for value, notes in values:
            text = _value_text(value, labels)
            if not text:
                continue
            where = ", ".join(labels.get(n, "") for n in notes if labels.get(n))
            shown.append(f"{text} ({where})" if where else text)
        if shown:
            facts.append(f"{label}: " + "; ".join(dict.fromkeys(shown)))
    return facts


def _value_text(value, labels: dict) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if "time" in value:
        return _date(value)
    if "id" in value:
        return labels.get(value["id"], "")
    if "amount" in value:
        amount = value["amount"].lstrip("+")
        unit = value.get("unit", "")
        unit_id = unit.rsplit("/", 1)[-1] if unit.startswith("http") else ""
        return f"{amount} {labels.get(unit_id, '')}".strip()
    return ""


def _labels(ids: list[str]) -> dict[str, str]:
    """English labels for Wikidata ids (50 per request, the API's limit)."""
    out: dict[str, str] = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        data = _get_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": "|".join(chunk), "props": "labels", "languages": "en",
             "format": "json"}))
        for qid, entity in ((data or {}).get("entities") or {}).items():
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            if label:
                out[qid] = label
    return out


# ---------------------------------------------------------------------------
# Web results
# ---------------------------------------------------------------------------

def web_results(query: str, provider: str, count: int = 5, searxng_url: str = "") -> list[tuple[str, str, str]]:
    """(title, snippet, url) from the chosen provider; [] when it's off, unconfigured or unreachable."""
    provider = provider if provider in PROVIDERS else "duckduckgo"
    if provider == "brave":
        return _brave(query, count)
    if provider == "searxng":
        return _searxng(query, count, searxng_url)
    if provider == "duckduckgo":
        return _duckduckgo(query, count)
    return []


def _duckduckgo(query: str, count: int) -> list[tuple[str, str, str]]:
    raw = _get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), BROWSER_HEADERS)
    if not raw:
        return []
    html = raw.decode("utf-8", "replace")
    links = list(re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S))
    results = []
    for i, link in enumerate(links):
        # everything up to the next result belongs to this one (its snippet is a few divs further on)
        block = html[link.end():links[i + 1].start() if i + 1 < len(links) else len(html)]
        url = unescape(link.group(1))
        if "y.js?" in url or "ad_domain" in url:
            continue                                     # adverts
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', block, re.S)
        target = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg")
        results.append((_text(link.group(2)), _text(snippet.group(1)) if snippet else "",
                        target[0] if target else url))
        if len(results) >= count:
            break
    return results


def _brave(query: str, count: int) -> list[tuple[str, str, str]]:
    key = brave_key()
    if not key:
        return []
    data = _get_json("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": count}), {"Accept": "application/json", "X-Subscription-Token": key,
                                        "User-Agent": USER_AGENT})
    items = ((data or {}).get("web") or {}).get("results") or []
    return [(_text(i.get("title", "")), _text(" ".join([i.get("description", "")] + list(i.get("extra_snippets") or [])[:2])),
             i.get("url", "")) for i in items[:count]]


def _searxng(query: str, count: int, base: str) -> list[tuple[str, str, str]]:
    base = str(base or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return []
    data = _get_json(f"{base}/search?" + urllib.parse.urlencode({"q": query, "format": "json"}))
    items = (data or {}).get("results") or []
    return [(_text(i.get("title", "")), _text(i.get("content", "")), i.get("url", "")) for i in items[:count]]


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", str(html))).split())


def brave_key() -> str:
    return secrets_store.get_secret(BRAVE_SECRET) or ""


def set_brave_key(key: str) -> bool:
    key = str(key or "").strip()
    return secrets_store.set_secret(BRAVE_SECRET, key) if key else secrets_store.delete_secret(BRAVE_SECRET)


# ---------------------------------------------------------------------------
# Everything together, for the AI
# ---------------------------------------------------------------------------

@dataclass
class Findings:
    question: str
    article: Article | None
    results: list[tuple[str, str, str]]

    def empty(self) -> bool:
        return self.article is None and not self.results

    def sources_text(self) -> str:
        parts = []
        if self.article is not None:
            a = self.article
            parts.append(f"Encyclopedia article: {a.title}\n" + ("Facts:\n" + "\n".join(f"- {f}" for f in a.facts) + "\n"
                                                                 if a.facts else "") + a.extract)
        if self.results:
            parts.append("Web results:\n" + "\n".join(f"- {t}: {s}" for t, s, _u in self.results))
        return "\n\n".join(parts)


def find(question: str, provider: str = "duckduckgo", count: int = 5, searxng_url: str = "") -> Findings:
    """The encyclopedia article and the web results, fetched side by side (each is a few round trips;
    together they took as long as both)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="websearch") as pool:
        article = pool.submit(article_for, question)
        results = pool.submit(web_results, question, provider, count, searxng_url)
        return Findings(question, article.result(), results.result())
