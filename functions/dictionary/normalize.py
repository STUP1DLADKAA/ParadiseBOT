"""Turning messy human input into a clean search term.

Two jobs, both pure and both heavily tested:

1. **Normalisation** — ``  "Hello!"  `` → ``hello`` without changing what the
   person actually meant. Quotes, markdown, stray punctuation and whitespace
   go; hyphens, apostrophes and multi-word expressions stay.
2. **Natural-language triggers** — deciding that ``what does hello mean?`` is
   a dictionary request while ``I need to define a function`` is just chat.
   The bar for triggering is deliberately high: Paradise interrupting a
   conversation is much worse than missing one phrasing.

Plus the small amount of English morphology needed to recover from
``running`` → ``run`` and to offer ``promises`` as a form of ``promise``.
"""

from __future__ import annotations

import re
import unicodedata

from .models import WordForm

MAX_TERM_LENGTH = 48
MAX_WORDS_QUOTED = 4
MAX_TRIGGER_MESSAGE_LENGTH = 160

# Quote characters people actually type, including the smart ones phones add.
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "\u201c": "\u201d",  # “ ”
    "\u2018": "\u2019",  # ‘ ’
    "\u00ab": "\u00bb",  # « »
}
_QUOTE_OPENERS = "".join(_QUOTE_PAIRS)

# Leading characters that mean "this is a command for some other bot".
_COMMAND_PREFIXES = ("/", "!", "?", ".", "-", "$", ";", ">", "+", "=", "~", "&", "%")

# Never look these up, however the sentence is phrased.
_STOPWORD_TERMS = frozenset(
    {
        "it", "this", "that", "these", "those", "he", "she", "they", "them",
        "we", "us", "you", "u", "me", "i", "him", "her", "his", "hers", "their",
        "something", "anything", "everything", "nothing", "someone", "anyone",
        "everyone", "nobody", "somebody", "anybody", "everybody", "what",
        "who", "why", "how", "when", "where", "which", "the", "a", "an",
        "yes", "no", "ok", "okay", "lol", "lmao", "bruh", "bro", "idk", "wtf",
        "thing", "stuff", "word", "words", "here", "there", "now", "then",
        "yourself", "myself", "itself", "himself", "herself", "themselves",
    }
)

_URLISH = re.compile(r"https?://|www\.|discord\.gg/", re.IGNORECASE)
_MENTIONISH = re.compile(r"<[@#!&:a-zA-Z0-9_]+?\d+>|@everyone|@here")
_MARKDOWN_EDGES = re.compile(r"^[*_~|]+|[*_~|]+$")
_WORD_RE = re.compile(r"^[^\W\d_][\w'\u2019\u2010-\u2015-]*$", re.UNICODE)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def strip_wrapping_quotes(raw: str) -> tuple[str, bool]:
    """Remove one layer of matching quotes. Returns (text, was_quoted)."""
    text = raw.strip()
    quoted = False
    while len(text) >= 2:
        opener = text[0]
        closer = _QUOTE_PAIRS.get(opener)
        if closer is None or not text.endswith(closer):
            break
        # A closing quote that also appears inside means we'd be joining two
        # separate quotes — leave it alone.
        inner = text[1:-1]
        if closer in inner:
            break
        text = inner.strip()
        quoted = True
    return text, quoted


def normalize_query(raw: str) -> str:
    """Clean a raw search term without changing the intended word.

    Returns ``""`` when nothing usable is left.
    """
    if not raw:
        return ""
    # NFC keeps composed accents stable so the cache key is predictable.
    text = unicodedata.normalize("NFC", str(raw))
    text = text.replace("\u200b", " ").replace("\u00a0", " ")
    # Quotes, markdown and stray punctuation can be nested in any order
    # ('"hello",' / **"hello"** / “hello”.) — peel until nothing changes.
    for _ in range(4):
        before = text
        text, _ = strip_wrapping_quotes(text)
        text = _MARKDOWN_EDGES.sub("", text).strip()
        # Trailing sentence punctuation is never part of a headword.
        text = text.strip(" \t.,;:!?\u2026")
        if text == before:
            break
    text = " ".join(text.split())
    return text[:MAX_TERM_LENGTH].strip()


def is_lookupable(term: str, *, allow_phrase: bool = True) -> bool:
    """Is this something a dictionary could plausibly hold?"""
    if not term or len(term) > MAX_TERM_LENGTH:
        return False
    if any(char.isdigit() for char in term):
        return False
    words = term.split()
    if not words:
        return False
    if len(words) > 1 and not allow_phrase:
        return False
    if len(words) > MAX_WORDS_QUOTED:
        return False
    return all(_WORD_RE.match(word) for word in words)


def cache_key(term: str, language: str = "en") -> str:
    return f"{language.lower()}:{term.casefold()}"


def display_word(term: str) -> str:
    """How the word is shown when a provider gave us nothing prettier."""
    return term.strip()


# ---------------------------------------------------------------------------
# Natural-language triggers
# ---------------------------------------------------------------------------

# Every pattern must match the *whole* message. The term is captured raw and
# validated afterwards, so quoting rules live in one place.
_POLITE = r"(?:(?:hey|yo|ok|okay)[,\s]+)?(?:(?:can|could|would)\s+(?:you|u|someone|somebody)\s+)?(?:(?:pls|plz|please)\s+)?"
_TAIL = r"(?:\s+(?:pls|plz|please|for\s+me|thanks|ty))?[\s.!?\u2026]*"
_TERM = r"(?P<term>.+?)"

_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # dictionary hello / define hello / definition of hello / meaning of hello
    re.compile(
        rf"^{_POLITE}(?:dictionary|define|definition|defn|meaning|definitions)"
        rf"(?:\s+(?:of|for))?(?:\s+the\s+word)?\s+{_TERM}{_TAIL}$",
        re.IGNORECASE,
    ),
    # what does hello mean? / what do hello mean
    re.compile(
        rf"^{_POLITE}what\s+(?:does|do|did)\s+{_TERM}\s+means?{_TAIL}$",
        re.IGNORECASE,
    ),
    # what is the meaning of hello? / what's the definition of "hello"
    re.compile(
        rf"^{_POLITE}what(?:'s|\u2019s|\s+is|\s+are)\s+(?:the\s+)?(?:meaning|definition|dictionary\s+definition)"
        rf"\s+(?:of|for)(?:\s+the\s+word)?\s+{_TERM}{_TAIL}$",
        re.IGNORECASE,
    ),
    # hello meaning / "hello" definition
    re.compile(
        rf"^{_TERM}\s+(?:meaning|definition){_TAIL}$",
        re.IGNORECASE,
    ),
)


def looks_like_command(content: str) -> bool:
    return content.strip().startswith(_COMMAND_PREFIXES)


def match_text_request(content: str) -> str | None:
    """Extract the word from a plain-chat dictionary request.

    Returns the normalised term, or ``None`` when the message is just a
    message. Case-insensitive, quote-tolerant, and conservative on purpose.
    """
    if not content:
        return None
    text = " ".join(str(content).split())
    if not text or len(text) > MAX_TRIGGER_MESSAGE_LENGTH:
        return None
    if looks_like_command(text) or "\n" in str(content).strip():
        return None
    if _URLISH.search(text) or _MENTIONISH.search(text) or "```" in text:
        return None

    for pattern in _TRIGGER_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        raw = match.group("term").strip()
        if not raw:
            continue
        _, quoted = strip_wrapping_quotes(raw)
        term = normalize_query(raw)
        if not term:
            continue
        # Unquoted requests must name exactly one word: "define hello" yes,
        # "define what we do next" no.
        if not is_lookupable(term, allow_phrase=quoted):
            continue
        if term.casefold() in _STOPWORD_TERMS:
            continue
        if not quoted and len(term) < 2:
            continue
        return term
    return None


# ---------------------------------------------------------------------------
# Morphology
# ---------------------------------------------------------------------------

_VOWELS = "aeiou"

_IRREGULAR_BASES = {
    # Only the handful that would otherwise send people to a dead end.
    "went": "go", "gone": "go", "did": "do", "done": "do", "said": "say",
    "made": "make", "took": "take", "taken": "take", "came": "come",
    "saw": "see", "seen": "see", "knew": "know", "known": "know",
    "got": "get", "gotten": "get", "gave": "give", "given": "give",
    "found": "find", "thought": "think", "told": "tell", "became": "become",
    "left": "leave", "felt": "feel", "brought": "bring", "began": "begin",
    "kept": "keep", "held": "hold", "wrote": "write", "written": "write",
    "stood": "stand", "heard": "hear", "let": "let", "meant": "mean",
    "met": "meet", "ran": "run", "paid": "pay", "sat": "sit", "spoke": "speak",
    "lay": "lie", "led": "lead", "grew": "grow", "lost": "lose", "fell": "fall",
    "sent": "send", "built": "build", "understood": "understand",
    "drew": "draw", "broke": "break", "broken": "break", "spent": "spend",
    "cut": "cut", "rose": "rise", "driven": "drive", "drove": "drive",
    "bought": "buy", "wore": "wear", "chose": "choose", "chosen": "choose",
    "ate": "eat", "eaten": "eat", "children": "child", "people": "person",
    "men": "man", "women": "woman", "feet": "foot", "teeth": "tooth",
    "geese": "goose", "mice": "mouse", "better": "good", "best": "good",
    "worse": "bad", "worst": "bad", "was": "be", "were": "be", "been": "be",
    "am": "be", "are": "be", "is": "be", "had": "have", "has": "have",
}

_ES_ENDINGS = ("ches", "shes", "sses", "xes", "zes", "oes", "ses")


def base_forms(word: str) -> tuple[str, ...]:
    """Plausible dictionary head-forms for an inflected word.

    ``running`` → ``run``, ``promises`` → ``promise``, ``happier`` → ``happy``.
    These are *candidates*: the caller looks each one up and only uses a hit,
    so a wrong guess costs a request, never a wrong answer.
    """
    term = word.strip().casefold()
    if len(term) < 3 or not term.isalpha():
        return ()

    irregular = _IRREGULAR_BASES.get(term)
    candidates: list[str] = [irregular] if irregular else []

    def add(*values: str) -> None:
        for value in values:
            if len(value) >= 2 and value != term and value not in candidates:
                candidates.append(value)

    # Plurals / third person singular
    if term.endswith("ies") and len(term) > 4:
        add(term[:-3] + "y")
    if term.endswith(_ES_ENDINGS) and len(term) > 4:
        add(term[:-2])
    if term.endswith("ves") and len(term) > 4:
        add(term[:-3] + "f", term[:-3] + "fe")
    if term.endswith("s") and not term.endswith(("ss", "us", "is", "as")):
        add(term[:-1])

    # Past tense / past participle
    if term.endswith("ied") and len(term) > 4:
        add(term[:-3] + "y")
    if term.endswith("ed") and len(term) > 3:
        stem = term[:-2]
        add(stem, stem + "e")
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
            add(stem[:-1])

    # Present participle / gerund
    if term.endswith("ing") and len(term) > 4:
        stem = term[:-3]
        add(stem, stem + "e")
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
            add(stem[:-1])
        if term.endswith("ying") and len(term) > 5:
            add(term[:-4] + "ie")

    # Comparatives / superlatives / adverbs
    if term.endswith("ier") and len(term) > 4:
        add(term[:-3] + "y")
    if term.endswith("iest") and len(term) > 5:
        add(term[:-4] + "y")
    if term.endswith("est") and len(term) > 5:
        add(term[:-3], term[:-2])
    if term.endswith("er") and len(term) > 4:
        add(term[:-2], term[:-1])
    if term.endswith("ily") and len(term) > 4:
        add(term[:-3] + "y")
    if term.endswith("ly") and len(term) > 4:
        add(term[:-2])

    return tuple(candidates[:6])


def candidate_forms(word: str, parts_of_speech: tuple[str, ...] = ()) -> tuple[WordForm, ...]:
    """Regular inflections to *offer for verification*.

    Nothing here is displayed until a provider confirms the form exists —
    English is far too irregular to trust generated spellings.
    """
    term = word.strip().casefold()
    if not term.isalpha() or len(term) < 2:
        return ()
    parts = {part.lower() for part in parts_of_speech}
    noun = "noun" in parts or not parts
    verb = "verb" in parts or not parts
    adjective = "adjective" in parts

    forms: list[WordForm] = []

    def add(label: str, value: str) -> None:
        if value and value != term and all(value != f.value for f in forms):
            forms.append(WordForm(label, value))

    if term.endswith("y") and len(term) > 2 and term[-2] not in _VOWELS:
        stem_y = term[:-1]
        plural = stem_y + "ies"
        third = stem_y + "ies"
        past = stem_y + "ied"
    elif term.endswith(("s", "x", "z", "ch", "sh")):
        plural = third = term + "es"
        past = term + "ed"
    elif term.endswith("e"):
        plural = term + "s"
        third = term + "s"
        past = term + "d"
    else:
        plural = third = term + "s"
        past = term + "ed"

    if term.endswith("e") and not term.endswith("ee"):
        gerund = term[:-1] + "ing"
    elif (
        len(term) > 2
        and term[-1] not in _VOWELS + "wxy"
        and term[-2] in _VOWELS
        and term[-3] not in _VOWELS
    ):
        gerund = term + term[-1] + "ing"
        past = term + term[-1] + "ed"
    else:
        gerund = term + "ing"

    if noun:
        add("plural", plural)
    if verb:
        add("third person", third)
        add("past tense", past)
        add("present participle", gerund)
    if adjective:
        if term.endswith("y") and len(term) > 2 and term[-2] not in _VOWELS:
            add("comparative", term[:-1] + "ier")
            add("superlative", term[:-1] + "iest")
        elif term.endswith("e"):
            add("comparative", term + "r")
            add("superlative", term + "st")
        else:
            add("comparative", term + "er")
            add("superlative", term + "est")
    return tuple(forms)


def common_prefix_probe(word: str) -> str:
    """A wildcard query that catches this word's regular inflections."""
    stem = word.strip().casefold()
    if stem.endswith("e") and len(stem) > 3:
        stem = stem[:-1]
    elif stem.endswith("y") and len(stem) > 3:
        stem = stem[:-1]
    return f"{stem[:12]}*"


# ---------------------------------------------------------------------------
# "Did you mean…"
# ---------------------------------------------------------------------------


def edit_distance(left: str, right: str, *, limit: int = 4) -> int:
    """Damerau-Levenshtein distance, capped at ``limit`` for speed."""
    left = left.casefold()
    right = right.casefold()
    if left == right:
        return 0
    if abs(len(left) - len(right)) > limit:
        return limit + 1

    previous_previous: list[int] = []
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        best = current[0]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + cost,   # substitution
            )
            if (
                i > 1
                and j > 1
                and left_char == right[j - 2]
                and left[i - 2] == right_char
            ):
                value = min(value, previous_previous[j - 2] + 1)  # transposition
            current[j] = value
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous_previous, previous = previous, current
    return previous[-1]


def common_prefix_length(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def rank_suggestions(
    query: str,
    candidates: list[tuple[str, float]],
    *,
    limit: int = 4,
    max_distance: int = 4,
) -> tuple[str, ...]:
    """Order "did you mean" candidates: closest spelling first.

    Ties on edit distance are broken by the longest shared prefix — typos
    cluster towards the end of a word, so ``recieve`` should offer
    ``receive`` (shares "rec") ahead of ``relieve`` (shares "re"), however
    much more common the second word is. Provider score only breaks a
    remaining tie.
    """
    target = normalize_query(query).casefold()
    scored: list[tuple[int, int, float, str]] = []
    seen: set[str] = {target}
    for word, score in candidates:
        clean = normalize_query(word)
        key = clean.casefold()
        if not clean or key in seen:
            continue
        distance = edit_distance(target, key, limit=max_distance)
        if distance > max_distance:
            continue
        seen.add(key)
        scored.append((distance, -common_prefix_length(target, key), -float(score or 0), clean))
    scored.sort()
    return tuple(word for _, _, _, word in scored[:limit])
