"""Text plumbing: HTML stripping, wikitext parsing, fuzzy matching.

Everything here is pure and total — no network, no Discord, and no
function ever raises on odd input; it just returns the best it could
parse.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date

try:  # pragma: no cover - exercised via the fallback test
    from rapidfuzz import fuzz
    from rapidfuzz.distance import JaroWinkler

    def _jaro_winkler(a: str, b: str) -> float:
        return JaroWinkler.similarity(a, b)

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAVE_RAPIDFUZZ = False


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def clean_summary(text: str | None, limit: int = 600) -> str:
    """Wikipedia/TV summaries cleaned to plain prose and clipped."""
    text = strip_html(text)
    if not text:
        return ""
    for noise in (
        "(Source: Crunchyroll)",
        "[Written by MAL Rewrite]",
    ):
        text = text.replace(noise, "")
    text = _WS_RE.sub(" ", text).strip()
    return shorten(text, limit)


def shorten(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    clipped = text[: max(1, width - 1)].rsplit(" ", 1)[0]
    return clipped + "…"


# ---------------------------------------------------------------------------
# Wikitext
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")
_TEMPLATE_NAME_RE = re.compile(r"^\{\{\s*([^|}]+)")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_RE = re.compile(r"<ref[^>]*?/>|<ref[^>]*?>.*?</ref>", re.DOTALL | re.IGNORECASE)


def wikitext_to_text(value: str) -> str:
    """Flatten one infobox parameter value into readable text."""
    if not value:
        return ""
    value = _COMMENT_RE.sub(" ", value)
    value = _REF_RE.sub(" ", value)
    # Resolve [[link|label]] to the label; bare [[link]] to the link text.
    value = _LINK_RE.sub(lambda m: m.group(1).strip(), value)
    # {{plainlist|* a * b}} and {{ubl|...}} and friends → comma list
    value = re.sub(r"\{\{\s*(?:plainlist|ubl|unbulleted list|bulleted list|hlist|flatlist)\s*\|",
                   "", value, flags=re.IGNORECASE)
    value = re.sub(r"\{\{\s*(?:film date|start date|end date)\s*\|", "", value, flags=re.IGNORECASE)
    value = value.replace("{{nowrap|", "").replace("}}", " ")
    # A bullet per line is IMDb/Wikipedia's way of writing a list; render
    # it as a comma list.
    value = re.sub(r"^\s*\*\s*", "", value, flags=re.MULTILINE)
    value = re.sub(r"\n\s*", ", ", value)
    value = re.sub(r"<br\s*/?>", ", ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = _html.unescape(value)
    value = value.replace("|", ", ")
    return _WS_RE.sub(" ", value).strip(" ,;-")


def parse_infobox(wikitext: str, template: str = "Infobox film") -> dict[str, str]:
    """Extract ``{{Infobox film|key=value|...}}`` parameters into a dict.

    Handles nested templates inside values ({{Film date|...}}, {{Plain
    list|...}}) by tracking brace depth. Total: returns whatever it could
    read, never raises.
    """
    if not wikitext:
        return {}
    # Find the template start (allow leading whitespace/newlines).
    pattern = re.compile(r"\{\{\s*" + re.escape(template), re.IGNORECASE)
    match = pattern.search(wikitext)
    if not match:
        return {}
    depth = 2  # we're inside the opening {{
    index = match.end()
    body: list[str] = []
    length = len(wikitext)
    while index < length and depth > 0:
        char = wikitext[index]
        if char == "{" and wikitext.startswith("{{", index):
            depth += 2
            body.append("{{")
            index += 2
            continue
        if char == "}" and wikitext.startswith("}}", index):
            depth -= 2
            if depth <= 0:
                break
            body.append("}}")
            index += 2
            continue
        body.append(char)
        index += 1

    return _parse_template_params("".join(body))


def _parse_template_params(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    # Depth-aware split on top-level pipes. Pipes hide inside nested
    # templates ({{Film date|…}}) AND inside wiki links
    # ([[Studio Pierrot|Pierrot]]) — both must stay in one piece.
    depth = 0
    link_depth = 0
    current: list[str] = []
    parts: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("{{", i):
            depth += 2
            current.append("{{")
            i += 2
            continue
        if body.startswith("}}", i):
            depth -= 2
            current.append("}}")
            i += 2
            continue
        if body.startswith("[[", i):
            link_depth += 1
            current.append("[[")
            i += 2
            continue
        if body.startswith("]]", i) and link_depth > 0:
            link_depth -= 1
            current.append("]]")
            i += 2
            continue
        char = body[i]
        if char == "|" and depth == 0 and link_depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    parts.append("".join(current))

    for part in parts:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        value = wikitext_to_text(value)
        if value:
            params.setdefault(key, value)
    return params


def parse_film_date(value: str) -> tuple[int, int, int] | None:
    """Parse ``{{Film date|2014|11|7|United States|...}}`` → first date.

    Returns (year, month, day) of the first complete triple found.
    """
    numbers = re.findall(r"\d+", value or "")
    for i in range(0, len(numbers) - 2):
        year, month, day = (int(numbers[i]), int(numbers[i + 1]), int(numbers[i + 2]))
        if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            try:
                date(year, month, day)
                return year, month, day
            except ValueError:
                continue
    return None


def parse_runtime(value: str) -> int | None:
    """Extract runtime minutes from text like '169 minutes' or '24 min. each'."""
    match = re.search(r"(\d+)\s*(?:min|minutes)", value or "", re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)\s*(?:h|hr|hour)", value or "", re.IGNORECASE)
    if match:
        hours = int(match.group(1))
        rest = re.search(r"(\d+)\s*m\b", value or "", re.IGNORECASE)
        return hours * 60 + (int(rest.group(1)) if rest else 0)
    return None


def parse_number(value: str) -> int | None:
    match = re.search(r"[\d,]{1,12}", value or "")
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

_ROMAJI_MARKERS = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f]")


def looks_japanese(text: str) -> bool:
    return bool(text and _ROMAJI_MARKERS.search(text))


_ARTICLES = ("the ", "a ", "an ")


def normalize_title(text: str) -> str:
    """Lowercase, strip punctuation/articles — for comparisons only."""
    text = (text or "").strip().casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    text = _WS_RE.sub(" ", text).strip()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article):]
            break
    return text


def split_season_suffix(query: str) -> tuple[str, int | None]:
    """Pull a trailing 'season 2' / 's2' off a query.

    'part 2' and 'cour 2' are deliberately *not* split off — they are part
    of many real titles ("Dune Part Two", "Attack on Titan Final Season
    Part 2") and guessing would mangle the lookup.
    """
    match = re.search(
        r"\b(?:season|series)\s*(\d{1,2})\s*$",
        query.strip(),
        re.IGNORECASE,
    )
    if match:
        base = query[: match.start()].strip(" -,")
        if base:
            return base, int(match.group(1))
    match = re.search(r"\bs(\d{1,2})\s*$", query.strip(), re.IGNORECASE)
    if match:
        base = query[: match.start()].strip(" -,")
        if base:
            return base, int(match.group(1))
    return query.strip(), None


# ---------------------------------------------------------------------------
# Fuzzy matching (rapidfuzz when available, bounded fallback otherwise)
# ---------------------------------------------------------------------------


def similarity(a: str, b: str) -> float:
    """0–100 string similarity, typo-tolerant.

    ``fuzz.ratio`` treats a swapped pair of letters ("naurto") as two
    edits; Jaro-Winkler recognises transpositions, so a confident J-W
    score lifts near-misses that are obviously just typos.
    """
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if _HAVE_RAPIDFUZZ:
        score = float(fuzz.ratio(a, b))
        try:
            jw = float(_jaro_winkler(a, b))
        except Exception:  # pragma: no cover - older rapidfuzz
            jw = 0.0
        if jw >= 0.9:
            score = max(score, jw * 100.0)
        return score
    return _fallback_ratio(a, b)


def title_match_score(query: str, candidate: str, alt_titles: list[str] | None = None) -> float:
    """Best similarity between a query and a title (incl. alternates)."""
    best = similarity(query, candidate)
    for alt in (alt_titles or []):
        best = max(best, similarity(query, alt))
    return best


def _fallback_ratio(a: str, b: str) -> float:
    """A bounded Damerau-Levenshtein-ish ratio for minimal installs."""
    if a == b:
        return 100.0
    distance = _bounded_edit_distance(a, b, 12)
    if distance is None:
        return 0.0
    longest = max(len(a), len(b))
    return max(0.0, 100.0 * (longest - distance) / longest)


def _bounded_edit_distance(a: str, b: str, cap: int) -> int | None:
    if abs(len(a) - len(b)) > cap:
        return None
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        best = i
        for j, char_b in enumerate(b, 1):
            cost = 0 if char_a == char_b else 1
            value = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            best = min(best, value)
        if best > cap:
            return None
        previous = current
    return previous[-1]


def is_likely_match(query: str, candidate: str, threshold: float = 80.0) -> bool:
    return title_match_score(query, candidate) >= threshold
