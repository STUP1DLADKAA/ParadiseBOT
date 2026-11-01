"""The free toolkit: deterministic, offline, zero-setup tools for mentions.

Every function here is pure local computation — no network, no API key,
no AI. They exist so "``@Paradise roll 2d20``" or "``@Paradise base64
encode hi``" costs nothing, answers instantly and works even when every
provider is down.

Each tool is reachable through :func:`run_tool`, which never raises: a
tool that cannot make sense of its arguments returns a helpful line
instead, because refusing a genuine request is not in character.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import re
import secrets
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from functions.helpers import ParadiseError
from functions.time import resolve_timezone

#: The friend group's home timezone — used to interpret bare clock times
#: ("8 pm") when the asker doesn't name a zone. Discord renders every
#: timestamp in each viewer's own zone anyway, so this only fixes what
#: the *input* means.
_HOME_ZONE = "Asia/Kolkata"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    """What a tool produced: a chat line and an optional PNG attachment."""

    text: str
    image: bytes | None = None
    filename: str = ""

    @property
    def has_image(self) -> bool:
        return self.image is not None


#: Tools that render an image and therefore prefer an embed/attachment.
IMAGE_TOOLS = frozenset({"qr", "color"})

#: Every tool key the router may send here.
TOOL_KEYS = frozenset(
    {
        "reverse", "mock", "count", "cipher", "base64", "hash", "morse",
        "emojify", "spoiler", "uuid", "password", "rng", "dice",
        "eightball", "rps", "lorem", "qr", "color", "countdown",
        "teams", "meettime", "weekday", "timestamp", "json", "sort", "dedupe",
        "readability",
    }
)


# ---------------------------------------------------------------------------
# Text tools
# ---------------------------------------------------------------------------


def reverse_text(text: str) -> str:
    return text.strip()[::-1]


def mock_text(text: str) -> str:
    """Alternating case, starting low — the classic condescending meme."""
    out = []
    upper = False
    for char in text.strip():
        if char.isalpha():
            out.append(char.upper() if upper else char.lower())
            upper = not upper
        else:
            out.append(char)
    return "".join(out)


def count_text(text: str) -> str:
    value = text.strip()
    words = len(value.split())
    chars = len(value)
    chars_no_space = len(value.replace(" ", ""))
    sentences = len([s for s in value.replace("!", ".").replace("?", ".").split(".") if s.strip()])
    return (
        f"📊 **{words}** words · **{chars}** characters "
        f"(**{chars_no_space}** without spaces) · **{sentences}** sentences"
    )


_MORSE = {
    "a": ".-", "b": "-...", "c": "-.-.", "d": "-..", "e": ".", "f": "..-.",
    "g": "--.", "h": "....", "i": "..", "j": ".---", "k": "-.-",
    "l": ".-..", "m": "--", "n": "-.", "o": "---", "p": ".--.",
    "q": "--.-", "r": ".-.", "s": "...", "t": "-", "u": "..-", "v": "...-",
    "w": ".--", "x": "-..-", "y": "-.--", "z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "!": "-.-.--",
    "'": ".----.", "/": "-..-.", "(": "-.--.", ")": "-.--.-",
    ":": "---...", "=": "-...-", "+": ".-.-.", "-": "-....-",
}
_MORSE_REV = {code: char for char, code in _MORSE.items()}


def morse_encode(text: str) -> str:
    cleaned = text.strip().casefold()
    words = []
    for word in cleaned.split():
        try:
            words.append(" ".join(_MORSE[char] for char in word))
        except KeyError:
            raise ParadiseError(
                "morse only knows letters, numbers and basic punctuation."
            ) from None
    return " / ".join(words)


def morse_decode(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        raise ParadiseError("give me some morse first (dots and dashes).")
    words = []
    for group in cleaned.split("/"):
        chars = []
        for code in group.split():
            char = _MORSE_REV.get(code)
            if char is None:
                raise ParadiseError("that doesn't look like morse to me.")
            chars.append(char)
        words.append("".join(chars))
    return " ".join(words)


_EMOJI_LETTERS = {
    char: f":regional_indicator_{char}:" for char in string.ascii_lowercase
}
_EMOJI_WORDS = {
    "love": "❤️", "heart": "❤️", "fire": "🔥", "lit": "🔥", "cool": "🧊",
    "ok": "👌", "yes": "✅", "no": "❌", "hi": "👋", "hello": "👋",
    "bye": "👋", "funny": "😂", "lol": "😂", "cry": "😭", "sad": "😢",
    "happy": "😊", "angry": "😡", "star": "⭐", "good": "👍",
    "bad": "👎", "coffee": "☕", "pizza": "🍕", "game": "🎮",
    "bot": "🤖", "cat": "🐱", "dog": "🐶", "sun": "☀️", "moon": "🌙",
    "money": "💰", "win": "🏆", "party": "🎉", "paradise": "🌴",
}


def emojify(text: str) -> str:
    words = []
    for word in text.strip().casefold().split():
        if word in _EMOJI_WORDS:
            words.append(_EMOJI_WORDS[word])
            continue
        chars = []
        for char in word:
            if char in _EMOJI_LETTERS:
                chars.append(_EMOJI_LETTERS[char])
            elif char.isdigit():
                chars.append(f"{char}\ufe0f\u20e3")
            else:
                chars.append(char)
        words.append("".join(chars))
    return " ".join(words)


def spoiler_text(text: str) -> str:
    return f"|| {text.strip()} ||"


# ---------------------------------------------------------------------------
# Cipher / encoding tools
# ---------------------------------------------------------------------------


def caesar(text: str, shift: int) -> str:
    """Shift letters by *shift* (negative shifts back). Pure, lossless."""
    result = []
    for char in text:
        if "a" <= char <= "z":
            result.append(chr((ord(char) - 97 + shift) % 26 + 97))
        elif "A" <= char <= "Z":
            result.append(chr((ord(char) - 65 + shift) % 26 + 65))
        else:
            result.append(char)
    return "".join(result)


def base64_encode(text: str) -> str:
    return base64.b64encode(text.strip().encode("utf-8")).decode("ascii")


def base64_decode(text: str) -> str:
    cleaned = "".join(text.strip().split())
    try:
        raw = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=False)
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ParadiseError("that wasn't valid base64 (or it wasn't UTF-8 inside).") from None


def hash_text(text: str) -> str:
    raw = text.strip().encode("utf-8")
    return (
        f"🔐 sha256: `{hashlib.sha256(raw).hexdigest()}`\n"
        f"md5: `{hashlib.md5(raw).hexdigest()}`"
    )


# ---------------------------------------------------------------------------
# Generator tools
# ---------------------------------------------------------------------------


def make_uuids(count: int = 1) -> str:
    import uuid as _uuid

    count = max(1, min(count, 5))
    return "\n".join(f"`{str(_uuid.uuid4())}`" for _ in range(count))


def make_password(length: int = 16, count: int = 1) -> str:
    length = max(8, min(length, 64))
    count = max(1, min(count, 3))
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+?"
    lines = []
    for _ in range(count):
        # Guaranteed one lower, one upper, one digit, one symbol, then random.
        pool = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$%^&*()-_=+?"),
        ]
        pool += [secrets.choice(alphabet) for _ in range(length - 4)]
        secrets.SystemRandom().shuffle(pool)
        lines.append("`" + "".join(pool) + "`")
    return (
        f"🔒 fresh password{'s' if count > 1 else ''} — seen once, never stored:\n"
        + "\n".join(lines)
    )


def random_number(low: int = 1, high: int = 100) -> str:
    if low > high:
        low, high = high, low
    if high - low > 1_000_000_000:
        raise ParadiseError("keep the range under a billion, gambler.")
    return f"🎲 **{secrets.randbelow(high - low + 1) + low}** (from {low} to {high})"


def dice_roll(notation: str) -> str:
    """Roll dice: '2d20+3', 'd6', '4d6', 'roll a d20'. Cryptographically random."""
    cleaned = notation.strip().casefold()
    # Tolerate any wrapper words: "roll a d20", "throw 2 d6 for me".
    match = re.search(r"(\d*)\s*d\s*(\d+)(?:\s*\+\s*(\d+))?", cleaned)
    if match is None:
        if cleaned and cleaned not in {
            "dice", "roll", "a dice", "roll dice", "roll a dice",
            "roll the dice", "roll the dices",
        }:
            raise ParadiseError(
                "dice look like `2d6+3` — count, 'd', sides, optional bonus."
            )
        count, sides, bonus = 1, 6, 0
    else:
        count = int(match.group(1) or 1)
        sides = int(match.group(2))
        bonus = int(match.group(3) or 0)
    label = f"`{count}d{sides}" + (f"+{bonus}" if bonus else "") + "`"
    if not 1 <= count <= 25 or not 2 <= sides <= 1000 or abs(bonus) > 1000:
        raise ParadiseError("1–25 dice, 2–1000 sides, bonus up to ±1000.")
    rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
    total = sum(rolls) + bonus
    if count == 1 and bonus == 0:
        return f"🎲 {label} → **{total}**"
    detail = " + ".join(str(roll) for roll in rolls)
    if bonus:
        detail += f" {'+' if bonus > 0 else '-'} {abs(bonus)}"
    return f"🎲 {label} → {detail} = **{total}**"


_EIGHT_BALL_ANSWERS = (
    "obviously yes.", "yes. duh.", "the stars say yes.",
    "without a doubt.", "yes, but you won't like the why.",
    "ask again after coffee.", "hm. unclear. ask louder.",
    "I know, but I'm not telling.", "the answer is in another castle.",
    "no. and you knew that.", "absolutely not.", "my sources say no.",
    "doubtful. very doubtful.", "don't count on it.",
    "yes — if you actually start it.", "the vibes approve.",
    "even the dice feel bad for you.", "100%. zero research, pure confidence.",
    "yes, and I take zero responsibility.", "outcome hazy, try yelling.",
)


def eightball(question: str) -> str:
    prefix = ""
    if question.strip():
        prefix = f"🔮 *{question.strip()[:120]}*\n"
    return prefix + "**" + secrets.choice(_EIGHT_BALL_ANSWERS) + "**"


_RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
_RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}


def rock_paper_scissors(choice: str) -> str:
    mine = secrets.choice(tuple(_RPS_BEATS))
    mine_emoji = _RPS_EMOJI[mine]
    cleaned = choice.strip().casefold()
    for key in _RPS_BEATS:
        if cleaned.startswith(key) or (key in cleaned and len(cleaned) <= 12):
            yours = key
            yours_emoji = _RPS_EMOJI[yours]
            if yours == mine:
                return f"{yours_emoji} vs {mine_emoji} — a tie. spooky."
            if _RPS_BEATS[yours] == mine:
                return f"{yours_emoji} vs {mine_emoji} — you win. this time."
            return f"{yours_emoji} vs {mine_emoji} — I win. bow to your bot overlord."
    return (
        f"🗿 I picked **{mine}**. you didn't, so that's a forfeit. "
        "(say rock, paper or scissors with it)"
    )


_LOREM_WORDS = (
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing",
    "elit", "sed", "do", "eiusmod", "tempor", "incididunt", "ut", "labore",
    "et", "dolore", "magna", "aliqua", "enim", "ad", "minim", "veniam",
    "quis", "nostrud", "exercitation", "ullamco", "laboris", "nisi",
    "aliquip", "ex", "ea", "commodo", "consequat", "duis", "aute", "irure",
)


def lorem_ipsum(paragraphs: int = 1) -> str:
    paragraphs = max(1, min(paragraphs, 5))
    out = []
    for index in range(paragraphs):
        sentences = []
        for s in range(4):
            words = []
            target = 12 + secrets.randbelow(10)
            for w in range(target):
                word = secrets.choice(_LOREM_WORDS)
                if w == 0 or (index == 0 and s == 0 and w == 0):
                    word = word.capitalize()
                words.append(word)
            sentences.append(" ".join(words) + ("" if s == 3 else ","))
        first = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod."
        body = " ".join(sentences[:3])
        out.append(f"{first} {body}" if index == 0 else body)
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Image tools (offline: QR codes and colour swatches)
# ---------------------------------------------------------------------------


def qr_png(text: str) -> bytes:
    """Render *text* as a scannable QR PNG. Fully offline."""
    value = text.strip()
    # Be generous with classifier-style payloads: "qr code for hello" → "hello".
    value = re.sub(
        r"^(?:please\s+)?(?:(?:make|generate|create|draw|scan)\s+)?"
        r"(?:me\s+)?(?:a\s+|an\s+)?qr(?:\s+code)?\s*(?:for|of|with)?\s*",
        "", value, flags=re.IGNORECASE,
    ).strip() or text.strip()
    if not value:
        raise ParadiseError("give me something to encode — a link, some text, anything.")
    if len(value.encode("utf-8")) > 1500:
        raise ParadiseError("QR codes hold about 1,500 characters — that's too much.")
    try:
        import qrcode

        image = qrcode.make(value, box_size=8, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except ImportError:  # pragma: no cover - qrcode ships in requirements
        raise ParadiseError("the QR renderer isn't installed on this host.") from None


_NAMED_COLORS = {
    "red": (220, 53, 69), "green": (25, 135, 84), "blue": (13, 110, 253),
    "yellow": (255, 193, 7), "orange": (253, 126, 20), "purple": (102, 16, 242),
    "pink": (214, 51, 132), "black": (18, 18, 20), "white": (248, 249, 250),
    "gray": (108, 117, 125), "grey": (108, 117, 125), "cyan": (13, 202, 240),
    "magenta": (214, 51, 234), "lime": (158, 224, 44), "brown": (120, 72, 32),
    "navy": (10, 30, 90), "teal": (18, 162, 154), "gold": (255, 200, 40),
}


def _parse_color(value: str) -> tuple[int, int, int]:
    cleaned = value.strip().casefold().lstrip("#").replace(" ", "")
    if cleaned in _NAMED_COLORS:
        return _NAMED_COLORS[cleaned]
    if len(cleaned) == 3 and all(c in string.hexdigits for c in cleaned):
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) == 6 and all(c in string.hexdigits for c in cleaned):
        return tuple(int(cleaned[i : i + 2], 16) for i in (0, 2, 4))  # noqa: E501
    digits = [part for part in value.replace(",", " ").split() if part.lstrip("-").isdigit()]
    if len(digits) == 3 and all(0 <= int(d) <= 255 for d in digits):
        return tuple(int(d) for d in digits)  # type: ignore[return-value]
    raise ParadiseError(
        "give me a hex (`#5865F2`), rgb (`88 101 242`) or a common colour name."
    )


def color_card(value: str) -> tuple[str, bytes]:
    """Info line + a small swatch PNG for a colour. Offline."""
    rgb = _parse_color(value)
    hex_code = "%02X%02X%02X" % rgb
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (120, 60), rgb)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 119, 59), outline=(255, 255, 255), width=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
    except ImportError:  # pragma: no cover - Pillow ships in requirements
        png = b""
    line = (
        f"🎨 **#{hex_code}** · rgb({rgb[0]}, {rgb[1]}, {rgb[2]}) · "
        f"css: `rgb({rgb[0]} {rgb[1]} {rgb[2]})`"
    )
    return line, png


# ---------------------------------------------------------------------------
# Countdown
# ---------------------------------------------------------------------------


def _month_number(name: str) -> int | None:
    names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    return names.get(name[:3])


def countdown(target: str, today=None) -> str:
    """Days until a date: 'christmas', 'new year', '25 dec 2026', '2026-12-25'."""
    from datetime import date, datetime

    value = " ".join(str(target or "").strip().casefold().split())
    if not value:
        raise ParadiseError("countdown to what? try 'days until christmas'.")
    now = today or datetime.now()

    year = now.year
    if "new year" in value or value in {"new years", "new year's", "nye"}:
        day, month = 1, 1
        year = now.year + 1
        label = "New Year"
    elif "christmas" in value or value in {"xmas", "x-mas"}:
        day, month = 25, 12
        label = "Christmas"
    elif "diwali" in value:
        # Approximate — the lunar calendar shifts; good enough for hype.
        day, month = 8, 11
        label = "Diwali (approximate — the calendar shifts)"
    elif "holi" in value:
        day, month = 14, 3
        label = "Holi (approximate)"
    else:
        # "25 dec 2026" / "dec 25 2026" / "2026-12-25" / "25/12/2026"
        tokens = value.replace("-", " ").replace("/", " ").replace(",", " ").split()
        day = month = None
        year = now.year
        for token in tokens:
            if token[:3] in ("jan", "feb", "mar", "apr", "may", "jun", "jul",
                             "aug", "sep", "oct", "nov", "dec"):
                month = _month_number(token)
            elif len(token) == 4 and token.isdigit():
                year = int(token)
            elif token.isdigit() and day is None and 1 <= int(token) <= 31:
                day = int(token)
        if day is None or month is None:
            raise ParadiseError(
                "try a full date — 'days until 25 dec 2026' or 'days until christmas'."
            )
        label = f"{day:02d} {_MONTH_NAMES[month - 1]} {year}"

    try:
        target_date = date(year, month, day)
    except ValueError:
        raise ParadiseError("that date doesn't exist on any calendar I respect.") from None
    if target_date < now.date():
        # A past date this year probably means next year ("days until christmas"
        # asked in January).
        try:
            target_date = date(year + 1, month, day)
            label += f" ({year + 1})"
        except ValueError:
            pass
    delta_days = (target_date - now.date()).days
    emoji = "🎉" if delta_days == 0 else "⏳"
    weekday = target_date.strftime("%A, %d %B %Y")
    if delta_days == 0:
        return f"{emoji} **{label} is TODAY.** go go go."
    if delta_days == 1:
        return f"{emoji} **{label} — tomorrow.** ({weekday})"
    return f"{emoji} **{label}: {delta_days} days to go.** ({weekday})"


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


# ---------------------------------------------------------------------------
# Duration parsing (shared with the reminder route)
# ---------------------------------------------------------------------------

_DURATION_UNITS = {
    "s": 1, "sec": 60, "secs": 60, "second": 60, "seconds": 60,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(text: str) -> float | None:
    """'in 20 minutes' / '2h30m' / 'in 3 days' → seconds. None when unclear.

    Accepts compound shorthand ("1h30m") and plain English ("twenty
    minutes" is *not* accepted — numbers only, on purpose).
    """
    value = " ".join(str(text or "").strip().casefold().split())
    if not value:
        return None
    value = value.removeprefix("in ").removesuffix("from now").strip()
    if not value:
        return None

    # Compound shorthand: 2h30m, 90s, 1d12h
    shorthand = 0.0
    found_short = False
    for amount, unit in re.findall(r"(\d+)\s*(s|m|h|d|w)(?![a-z])", value):
        shorthand += int(amount) * _DURATION_UNITS[unit]
        found_short = True
    if found_short:
        return shorthand or None

    # Plain English: "20 minutes", "a day", "half an hour"
    match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
        value,
    )
    if match is not None:
        amount = float(match.group(1))
        return amount * _DURATION_UNITS[match.group(2)]

    word_units = {
        "a second": 1, "a minute": 60, "an hour": 3600, "a hour": 3600,
        "a day": 86400, "a week": 604800,
    }
    for phrase, seconds in word_units.items():
        if phrase in value:
            return seconds
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if sec == 0 else f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


# ---------------------------------------------------------------------------
# Group utilities: teams, meeting times, dates, timestamps
# ---------------------------------------------------------------------------

_TEAM_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def make_teams(args: str) -> str:
    """Split a list of names into random, evenly balanced teams."""
    value = " ".join(str(args or "").split())
    count = 2
    match = re.search(
        r"(?:(?P<num>\d+)|(?P<word>two|three|four|five|six))\s+teams",
        value, re.IGNORECASE,
    )
    if match:
        count = (
            int(match.group("num")) if match.group("num")
            else _TEAM_COUNT_WORDS[match.group("word").casefold()]
        )
    count = min(max(count, 2), 8)
    # Everything up to and including the "teams" keyword is lead-in
    # ("make two teams from …", "split into 3 teams: …") — drop it, then
    # read the names that follow.
    keyword = re.search(r"\bteams?\b[\s:,-]*", value, re.IGNORECASE)
    cleaned = value[keyword.end():] if keyword else value
    cleaned = re.sub(
        r"^(?:from|of|out of|with|for)?[\s:,]+", "", cleaned, flags=re.IGNORECASE
    )
    names = [n.strip(" .") for n in re.split(r"[,;]+|\s+and\s+", cleaned) if n.strip(" .")]
    if len(names) == 1 and " " in names[0]:
        names = names[0].split()
    names = [n for n in names if n and n.casefold() not in ("teams", "team")]
    if len(names) < 2:
        raise ParadiseError("give me the people — 'make two teams from taher, stupid, ash'.")
    random.shuffle(names)
    teams: list[list[str]] = [[] for _ in range(min(count, len(names)))]
    for index, name in enumerate(names):
        teams[index % len(teams)].append(name)
    lines = [f"⚔️ **{len(teams)} teams**, randomised and balanced:"]
    for number, team in enumerate(teams, 1):
        lines.append(f"**Team {number}:** {', '.join(team)}")
    lines.append("*(rerun to shuffle again — I take no responsibility for the result)*")
    return "\n".join(lines)


_MEETTIME_RE1 = re.compile(
    r"^what(?:'s| is)? (?:the )?time (?:is it )?in (?P<target>.+?) "
    r"(?:when|if) it(?:'?s| is) (?P<when>.+?) in (?P<source>.+?)\s*[?!.]*$",
    re.IGNORECASE,
)
_MEETTIME_RE2 = re.compile(
    r"^(?:if|when) it(?:'?s| is) (?P<when>.+?) in (?P<source>.+?)\s*"
    r"what(?:'s| is)?\s+(?:the\s+)?time\s+(?:is\s+it\s+)?in\s+"
    r"(?P<target>.+?)\s*[?!.]*$",
    re.IGNORECASE,
)
#: One compiled shape covering both meeting-time phrasings, for the router.
#: Group names are stripped so the two alternatives can share one pattern.
MEETTIME_SHAPE = re.compile(
    "(?:%s)|(?:%s)" % (
        re.sub(r"\(\?P<\w+>", "(?:", _MEETTIME_RE1.pattern),
        re.sub(r"\(\?P<\w+>", "(?:", _MEETTIME_RE2.pattern),
    ),
    re.IGNORECASE,
)

_WALL_TIME = re.compile(
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?",
    re.IGNORECASE,
)


def _parse_wall_time(when: str) -> tuple[int, int]:
    match = _WALL_TIME.search(when)
    if match is None:
        raise ParadiseError("what time exactly? try 'when it's 8 pm in india'.")
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ampm = (match.group("ampm") or "").replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            raise ParadiseError("that clock time doesn't exist.")
        if ampm.casefold().startswith("p") and hour < 12:
            hour += 12
        elif ampm.casefold().startswith("a") and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        raise ParadiseError("that clock time doesn't exist.")
    if not 0 <= minute < 60:
        raise ParadiseError("that clock time doesn't exist.")
    return hour, minute


def _zone_or_error(name: str) -> str:
    zone = resolve_timezone(name.strip())
    if zone is None:
        raise ParadiseError(f"I don't know the timezone '{name.strip()}'.")
    return zone


def meeting_time(text: str, *, now: datetime | None = None) -> str:
    """"what time is it in tokyo when it's 8 pm in india" → the answer."""
    value = " ".join(str(text or "").split())
    match = _MEETTIME_RE1.match(value) or _MEETTIME_RE2.match(value)
    if match is None:
        raise ParadiseError(
            "try 'what time is it in tokyo when it's 8 pm in india'."
        )
    hour, minute = _parse_wall_time(match.group("when"))
    source_zone = _zone_or_error(match.group("source"))
    target_zone = _zone_or_error(match.group("target"))
    now_source = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo(source_zone))
    moment = now_source.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now_source:  # that time already passed today — take tomorrow's
        moment += timedelta(days=1)
    target_moment = moment.astimezone(ZoneInfo(target_zone))
    clock = "%I:%M %p"
    src_abbr = moment.tzname() or source_zone
    tgt_abbr = target_moment.tzname() or target_zone
    if moment.date() == target_moment.date():
        day_note = "same day"
    elif target_moment.date() > moment.date():
        day_note = "next day over there"
    else:
        day_note = "previous day over there"
    return (
        f"🕒 {moment.strftime(clock).lstrip('0')} {src_abbr} in "
        f"{match.group('source').strip().title()} = "
        f"**{target_moment.strftime(clock).lstrip('0')} {tgt_abbr}** in "
        f"{match.group('target').strip().title()} — {day_note}."
    )


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_date_value(value: str, *, today: date | None = None) -> date:
    """'25 dec 2026' / 'dec 25 2026' / '2026-12-25' / '25/12/2026' / 'tomorrow'."""
    clean = " ".join(str(value or "").strip().casefold().split())
    if not clean:
        raise ParadiseError("which date?")
    today = today or date.today()
    if clean in ("today",):
        return today
    if clean in ("tomorrow",):
        return today + timedelta(days=1)
    if clean in ("yesterday",):
        return today - timedelta(days=1)
    # ISO (2026-11-01) and day-first numeric (25/12/2026, 25.12) forms,
    # where two bare numbers mean day and month.
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", clean)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            raise ParadiseError(
                "that date doesn't exist on any calendar I respect."
            ) from None
    match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{4}))?", clean)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            parsed = date(year, month, day)
        except ValueError:
            raise ParadiseError(
                "that date doesn't exist on any calendar I respect."
            ) from None
        if year == today.year and (parsed - today).days < -90:
            try:
                return date(year + 1, month, day)
            except ValueError:
                return parsed
        return parsed

    tokens = clean.replace("-", " ").replace("/", " ").replace(",", " ").split()
    day = month = None
    year = today.year
    for token in tokens:
        if _month_number(token):
            month = _month_number(token)
        elif len(token) == 4 and token.isdigit():
            year = int(token)
        elif token.isdigit() and day is None and 1 <= int(token) <= 31:
            day = int(token)
        elif token in _WEEKDAYS:
            delta = (_WEEKDAYS[token] - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    if day is None or month is None:
        raise ParadiseError("try a full date — '25 dec 2026' or 'friday'.")
    try:
        parsed = date(year, month, day)
    except ValueError:
        raise ParadiseError("that date doesn't exist on any calendar I respect.") from None
    if year == today.year and (parsed - today).days < -90:
        # "1 jan" said in November clearly means the next one.
        try:
            return date(year + 1, month, day)
        except ValueError:
            return parsed
    return parsed


def weekday_of(args: str, *, today: date | None = None) -> str:
    """'what day was 15 aug 1947' → the weekday of a date."""
    today = today or date.today()
    parsed = _parse_date_value(args, today=today)
    when = "today" if parsed == today else (
        "tomorrow" if parsed == today + timedelta(days=1) else parsed.strftime("%d %B %Y")
    )
    return f"📅 **{parsed.strftime('%A')}** — {when}."


def discord_timestamp(args: str, *, now: datetime | None = None) -> str:
    """A copy-ready `<t:…:F>` Discord timestamp for a date/time."""
    value = " ".join(str(args or "").split())
    if not value:
        raise ParadiseError("timestamp for when? try 'discord timestamp tomorrow 8 pm'.")
    # Optional explicit zone at the end ("… in tokyo", "… ist", "… america/new_york").
    zone_name = None
    zone_match = re.search(r"\b(?:in|at)\s+(?P<zone>[a-z_ /]+)$", value, re.IGNORECASE)
    if zone_match:
        candidate = resolve_timezone(zone_match.group("zone"))
        if candidate:
            zone_name = candidate
            value = value[: zone_match.start()].strip()
    # Clock time, if any. A bare number next to a month name ("25 dec") is a
    # date, not a clock — only an explicit "8 pm" / "18:30" counts as time.
    hour = minute = None
    time_match = None
    for candidate in _WALL_TIME.finditer(value):
        has_ampm = bool((candidate.group("ampm") or "").strip())
        has_colon = candidate.group("m") is not None
        if has_ampm or has_colon:
            time_match = candidate
            break
    if time_match is not None:
        try:
            hour, minute = _parse_wall_time(time_match.group(0))
            value = (value[: time_match.start()] + " " + value[time_match.end():]).strip()
        except ParadiseError:
            hour = minute = None
    # Remaining words are the day.
    day = _parse_date_value(value or "today")
    zone = ZoneInfo(zone_name or _HOME_ZONE)
    moment = datetime(
        day.year, day.month, day.day, hour if hour is not None else 0,
        minute or 0, tzinfo=zone,
    )
    stamp = int(moment.timestamp())
    human = moment.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    zone_note = "" if zone_name else " *(read your input as India time)*"
    return (
        f"⏱️ {human}:\n"
        f"full: `<t:{stamp}:F>` → <t:{stamp}:F>\n"
        f"relative: `<t:{stamp}:R>` → <t:{stamp}:R>{zone_note}"
    )


# ---------------------------------------------------------------------------
# Text utilities: json, sorting, dedupe, readability
# ---------------------------------------------------------------------------


def json_tool(args: str) -> str:
    """Validate (and pretty-print) a JSON payload."""
    payload = str(args or "").strip()
    mode = "format"
    verb = re.match(
        r"(validate|check|verify|format|pretty\s?-?\s?print|prettify|beautify|fix)\s+",
        payload, re.IGNORECASE,
    )
    if verb:
        mode = "validate" if verb.group(1).casefold() in (
            "validate", "check", "verify"
        ) else "format"
        payload = payload[verb.end():].strip()
    if not payload:
        raise ParadiseError("paste the JSON after it — 'validate json {\"a\": 1}'.")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        hint = ""
        if "Expecting" in error.msg and chr(39) in payload:
            hint = " — JSON wants double quotes, not single ones"
        elif "Expecting" in error.msg and re.search(r"[,{]\s*[a-z_]\s*:", payload):
            hint = " — keys need quotes around them"
        return f"not valid JSON: {error.msg} at position {error.pos}.{hint}"
    if mode == "validate":
        kind = type(parsed).__name__
        if isinstance(parsed, dict):
            kind = f"object with {len(parsed)} keys"
        elif isinstance(parsed, list):
            kind = f"array with {len(parsed)} items"
        return f"✅ valid JSON — {kind}."
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    if len(pretty) > 1600:
        pretty = pretty[:1600] + "\n… (trimmed — it's long)"
    return f"✅ valid JSON:\n```json\n{pretty}\n```"


def _split_list(args: str) -> list[str]:
    cleaned = re.sub(
        r"^(?:sort|dedupe|deduplicate|unique|remove\s+duplicates?)?\s*"
        r"(?:these|this|that|the|my)?\s*"
        r"(?:lines?|list|items?|numbers?|names?|values?|duplicates?)?"
        r"\s*(?:from|in)?\s*[:,-]?\s*",
        "", str(args or "").strip(), count=1, flags=re.IGNORECASE,
    )
    items = [t.strip() for t in re.split(r"[,;]+", cleaned) if t.strip()]
    if len(items) < 2:
        raise ParadiseError("give me a comma-separated list — 'sort b, c, a'.")
    return items


def _is_numberish(token: str) -> bool:
    try:
        float(token.strip().replace(",", ""))
    except ValueError:
        return False
    return True


def sort_list(args: str) -> str:
    """Sort a comma-separated list (numbers or words)."""
    items = _split_list(args)
    if all(_is_numberish(t) for t in items):
        ordered = [t for _, t in sorted(
            ((float(t.replace(",", "")), t) for t in items), key=lambda pair: pair[0]
        )]
    else:
        ordered = sorted(items, key=str.casefold)
    return "🔢 " + ", ".join(ordered)


def dedupe_list(args: str) -> str:
    """Remove duplicates from a comma-separated list, keeping order."""
    items = _split_list(args)
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    removed = len(items) - len(unique)
    if removed == 0:
        return f"🧹 no duplicates — all {len(unique)} were already unique."
    plural = "s" if removed > 1 else ""
    return f"🧹 {removed} duplicate{plural} gone:\n" + ", ".join(unique)


def _count_syllables(word: str) -> int:
    """Vowel-group heuristic; good enough for Flesch scoring."""
    clean = re.sub(r"[^a-z]", "", word.casefold())
    if not clean:
        return 0
    groups = re.findall(r"[aeiouy]+", clean)
    if clean.endswith("e") and len(groups) > 1 and not clean.endswith(("le", "ee", "ye")):
        groups = groups[:-1]
    return max(1, len(groups))


def readability_score(text: str) -> str:
    """Flesch reading ease + Flesch–Kincaid grade for a passage."""
    value = " ".join(str(text or "").split())
    if len(value.split()) < 8:
        raise ParadiseError("give me a proper paragraph — at least a sentence or two.")
    sentences = [s for s in re.split(r"[.!?]+", value) if s.strip()]
    words = re.findall(r"[\w'-]+", value)
    syllables = sum(_count_syllables(w) for w in words)
    if not sentences or not words:
        raise ParadiseError("that doesn't look like prose.")
    words_per_sentence = len(words) / len(sentences)
    syllables_per_word = syllables / len(words)
    ease = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
    grade = 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59
    ease = max(0.0, min(100.0, ease))
    if ease >= 90:
        verdict = "very easy — a kid could read it"
    elif ease >= 70:
        verdict = "plain English — most people cruise through it"
    elif ease >= 60:
        verdict = "plain English, slightly dense"
    elif ease >= 50:
        verdict = "fairly dense — high-school territory"
    elif ease >= 30:
        verdict = "dense — university territory"
    else:
        verdict = "very dense — legalese vibes, nobody's reading this for fun"
    return (
        f"📖 readability: **{ease:.0f}/100** — {verdict}.\n"
        f"grade level ~**{max(1, round(grade))}** • "
        f"{len(words)} words • {len(sentences)} sentences • "
        f"{words_per_sentence:.1f} words/sentence"
    )


def run_tool(key: str, args: str, *, author: str = "") -> ToolResult:
    """Run one toolkit tool. Returns a chat line; never raises for bad input."""
    args = " ".join(str(args or "").split())
    try:
        if key == "reverse":
            return ToolResult(f"🔄 `{reverse_text(args)}`" if args else "🔄 reverse *what*?")
        if key == "mock":
            return ToolResult(f"🧽 {mock_text(args)}" if args else "🧽 mock *what*?")
        if key == "count":
            return ToolResult(count_text(args) if args else "📏 count *what*?")
        if key == "cipher":
            return _run_cipher(args)
        if key == "base64":
            return _run_base64(args)
        if key == "hash":
            return ToolResult(hash_text(args) if args else "🔐 hash *what*?")
        if key == "morse":
            return _run_morse(args)
        if key == "emojify":
            return ToolResult(emojify(args) if args else "🔤 emojify *what*?")
        if key == "spoiler":
            return ToolResult(spoiler_text(args) if args else "🫥 spoiler *what*?")
        if key == "uuid":
            digits = [int(d) for d in re.findall(r"\d+", args)]
            return ToolResult(f"🆔\n{make_uuids(digits[0] if digits else 1)}")
        if key == "password":
            return _run_password(args)
        if key == "rng":
            return _run_rng(args)
        if key == "dice":
            return ToolResult(dice_roll(args))
        if key == "eightball":
            return ToolResult(eightball(args))
        if key == "rps":
            return ToolResult(rock_paper_scissors(args))
        if key == "lorem":
            count = 1
            digits = [t for t in args.split() if t.isdigit()]
            if digits:
                count = int(digits[0])
            return ToolResult(lorem_ipsum(count))
        if key == "qr":
            return ToolResult("📸 here's your QR code:", image=qr_png(args), filename="qr.png")
        if key == "color":
            line, png = color_card(args)
            return ToolResult(line, image=png or None, filename="color.png")
        if key == "countdown":
            return ToolResult(countdown(args))
        if key == "teams":
            return ToolResult(make_teams(args))
        if key == "meettime":
            return ToolResult(meeting_time(args))
        if key == "weekday":
            return ToolResult(weekday_of(args))
        if key == "timestamp":
            return ToolResult(discord_timestamp(args))
        if key == "json":
            return ToolResult(json_tool(args))
        if key == "sort":
            return ToolResult(sort_list(args))
        if key == "dedupe":
            return ToolResult(dedupe_list(args))
        if key == "readability":
            return ToolResult(readability_score(args))
    except ParadiseError as error:
        return ToolResult(f"• {error}")
    return ToolResult(
        "I have that tool on paper but not in code — try `@Paradise help`."
    )


def _run_cipher(args: str) -> ToolResult:
    cleaned = args.strip()
    if not cleaned:
        return ToolResult("🔐 try `rot13 hello` or `caesar 3 hello` (negative shifts decode).")
    match = re.match(r"^(?P<shift>-?\d+)\s+(?P<text>.+)$", cleaned)
    if match:
        shift = int(match.group("shift"))
        return ToolResult(f"🔐 caesar {shift:+d}:\n`{caesar(match.group('text'), shift)}`")
    return ToolResult(f"🔐 rot13:\n`{caesar(cleaned, 13)}`")


def _run_base64(args: str) -> ToolResult:
    cleaned = args.strip()
    if not cleaned:
        return ToolResult("📦 try `base64 encode hello` or `base64 decode aGVsbG8`.")
    match = re.match(r"^(?P<mode>encode|decode)\s+(?P<text>.+)$", cleaned, re.IGNORECASE)
    if match is None:
        # No explicit mode: base64-looking input decodes, everything else encodes.
        probe = "".join(cleaned.split())
        looks_encoded = bool(probe) and all(c in string.ascii_letters + string.digits + "+=/=" for c in probe)
        if looks_encoded and len(probe) % 4 == 0:
            return ToolResult(f"📦 decoded:\n`{base64_decode(cleaned)}`")
        return ToolResult(f"📦 encoded:\n`{base64_encode(cleaned)}`")
    if match.group("mode").casefold() == "decode":
        return ToolResult(f"📦 decoded:\n`{base64_decode(match.group('text'))}`")
    return ToolResult(f"📦 encoded:\n`{base64_encode(match.group('text'))}`")


def _run_morse(args: str) -> ToolResult:
    cleaned = args.strip()
    if not cleaned:
        return ToolResult("📡 try `morse encode sos` or `morse decode ... --- ...`.")
    if set(cleaned) <= {".", "-", " ", "/"}:
        return ToolResult(f"📡 decoded:\n`{morse_decode(cleaned)}`")
    if cleaned.casefold().startswith("encode "):
        return ToolResult(f"📡 {morse_encode(cleaned[7:])}")
    return ToolResult(f"📡 {morse_encode(cleaned)}")


def _run_password(args: str) -> ToolResult:
    length = 16
    count = 1
    for number in re.findall(r"\d+", args):
        value = int(number)
        if 8 <= value <= 64 and length == 16:
            length = value
        elif 1 <= value <= 3 and count == 1:
            count = value
    return ToolResult(make_password(length, count))


def _run_rng(args: str) -> ToolResult:
    numbers = [int(n) for n in re.findall(r"-?\d+", args)]
    if len(numbers) >= 2:
        return ToolResult(random_number(numbers[0], numbers[1]))
    return ToolResult(random_number())


__all__ = [
    "IMAGE_TOOLS", "TOOL_KEYS", "ToolResult", "base64_decode", "base64_encode",
    "caesar", "color_card", "count_text", "countdown", "dice_roll",
    "eightball", "emojify", "format_duration", "hash_text", "lorem_ipsum",
    "make_password", "make_uuids", "mock_text", "morse_decode", "morse_encode",
    "parse_duration", "qr_png", "random_number", "reverse_text",
    "rock_paper_scissors", "run_tool", "spoiler_text",
]
