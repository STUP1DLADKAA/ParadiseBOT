"""Dynamic meme cards, vinyl fallback stickers, and native Discord sticker picker.

Everything here works with zero API keys and zero external HTTP dependencies:
- ``StickerPicker`` replies with real native Discord stickers (available custom
  guild stickers + Discord standard sticker packs) and gracefully falls back to
  a custom die-cut PNG sticker if none are available or allowed.
- ``render_meme_png`` turns chat messages (or standalone reactions) into crisp
  PNG meme cards in memory using Pillow and the bundled Inter font family.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import re
from collections import deque
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_DISCORD_TAG_RE = re.compile(r"<(?:@!?|@&|#|a?:([A-Za-z0-9_]+):)\d+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

MEME_TEMPLATES: tuple[str, ...] = (
    "nobody_quote",
    "breaking_news",
    "live_reaction",
    "brain_scan",
    "museum_frame",
    "impact_reaction",
)

_VERDICT_BADGES: tuple[str, ...] = (
    "PARADISE VERDICT: QUESTIONABLE AT BEST",
    "CERTIFIED GENERAL CHAT MOMENT",
    "ARCHIVED IN THE SERVER PERMANENT RECORD",
    "MY LAST TWO BRAIN CELLS JUST RETIRED",
    "SCIENTISTS ARE STILL STUDYING THIS MESSAGE",
    "BRO REALLY TYPED THIS AND HIT ENTER",
    "FACT CHECK: 12% LOGIC, 88% VIBES",
)

_NEWS_TICKERS: tuple[str, ...] = (
    "EXPERTS BAFFLED  |  SERVER IQ DROPS 14%  |  TOUCHING GRASS RECOMMENDED",
    "WITNESSES STUNNED  |  KEYBOARD CONFISCATION UNDER CONSIDERATION",
    "BREAKING  |  LOCAL BOT FORCED TO READ THIS WITH ITS OWN EYES",
    "DEVELOPING STORY  |  COUNCIL OF ELDERS CONVENING IN VOICE CHAT",
    "LIVE UPDATE  |  NOBODY ASKED YET EVERYBODY SAW IT",
)

_LIVE_CAPTIONS: tuple[str, ...] = (
    "BRO REALLY TYPED THAT OUT LOUD",
    "STARING AT MY SCREEN IN SILENCE",
    "HOLD ON... READING THIS TWICE",
    "MY HONEST UNFILTERED REACTION",
    "SCREENSHOTTED FOR FUTURE HISTORIANS",
    "EVEN MY ROUTER BLINKED AT THIS",
)

_IMPACT_PAIRS: tuple[tuple[str, str], ...] = (
    ("HOLD ON LET BRO COOK", "NEVERMIND THE KITCHEN BURNT DOWN"),
    ("NO THOUGHTS DETECTED", "HEAD COMPLETELY EMPTY"),
    ("ACCORDING TO MY CALCULATIONS", "THAT MADE ZERO SENSE"),
    ("ABSOLUTE CINEMA", "PEAK GENERAL CHAT LORE"),
    ("200 IQ CONFIDENCE", "2 IQ EXECUTION"),
    ("WAIT A SECOND", "RUN THAT SENTENCE BACK"),
    ("ME PRETENDING TO BE SMART", "WHILE THINKING ABOUT SOUP"),
    ("SILENCE MORTALS", "THE BOT HAS OPINIONS"),
)

_STICKER_BADGES: tuple[tuple[str, str, tuple[int, int, int]], ...] = (
    ("CERTIFIED", "BRUH", (235, 87, 87)),
    ("COOKED", "100%", (242, 153, 74)),
    ("200 IQ", "MAYBE", (86, 204, 242)),
    ("SUSPICIOUS", "HMMM", (155, 81, 224)),
    ("PEAK", "LORE", (39, 174, 96)),
    ("0 THOUGHTS", "SOUP ONLY", (242, 201, 76)),
    ("REAL", "NO CAP", (47, 128, 237)),
    ("EXCUSE ME", "WHAT??", (235, 87, 150)),
)

_FACES: tuple[str, ...] = (
    "squint",
    "nerd",
    "shock",
    "monocle",
    "deadpan",
)


@lru_cache(maxsize=32)
def _load_font(role: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    mapping = {
        "display": "InterDisplay-SemiBold.ttf",
        "bold": "Inter-SemiBold.ttf",
        "medium": "Inter-Medium.ttf",
        "regular": "Inter-Regular.ttf",
        "italic": "Inter-Italic.ttf",
    }
    filename = mapping.get(role, "Inter-Regular.ttf")
    candidate = _FONTS_DIR / filename
    try:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    except OSError:
        pass
    return ImageFont.load_default()


def clean_meme_text(raw: str, *, max_chars: int = 110) -> str:
    """Sanitize Discord text to clean printable ASCII/Latin glyphs for Pillow."""
    text = _DISCORD_TAG_RE.sub(lambda m: f":{m.group(1)}:" if m.group(1) else "@someone", raw or "")
    text = _URL_RE.sub("[link]", text)
    # Replace smart punctuation with clean ASCII equivalents
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2014": " - ",
        "\u2013": "-",
        "\u2026": "...",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    # Strip emojis / non-Latin glyphs that TrueType Inter doesn't carry
    cleaned_chars = [
        ch if (32 <= ord(ch) <= 126) else " "
        for ch in text
    ]
    compact = " ".join("".join(cleaned_chars).split()).strip()
    if not compact:
        compact = "unintelligible keyboard noises"
    if len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip() + "..."
    return compact


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int = 3,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines - 1:
                break
    remaining_idx = sum(len(line.split()) for line in lines) + len(current.split())
    if remaining_idx < len(words):
        tail = " ".join([current] + words[remaining_idx:])
        while len(tail) > 4:
            bbox = draw.textbbox((0, 0), tail + "...", font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                break
            tail = tail[:-1].rstrip()
        current = tail + "..."
    lines.append(current)
    return lines[:max_lines]


def _draw_mascot_face(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    mood: str,
    accent: tuple[int, int, int] = (88, 101, 242),
) -> None:
    """Draw an expressive cartoon Paradise face inside ``box``."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    r = min(x1 - x0, y1 - y0) // 2 - 12

    # Subtle glow ring + head circle
    draw.ellipse((cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6), fill=(35, 39, 52))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent, outline=(255, 255, 255), width=4)

    eye_dx = int(r * 0.36)
    eye_y = cy - int(r * 0.14)
    eye_r = max(8, int(r * 0.17))

    if mood == "squint":
        for ex in (cx - eye_dx, cx + eye_dx):
            draw.rounded_rectangle(
                (ex - eye_r - 4, eye_y - 5, ex + eye_r + 4, eye_y + 5),
                radius=4,
                fill=(255, 255, 255),
            )
            draw.ellipse((ex - 4, eye_y - 4, ex + 4, eye_y + 4), fill=(18, 20, 28))
            draw.line((ex - eye_r - 6, eye_y - 12, ex + eye_r + 6, eye_y - 9), fill=(18, 20, 28), width=5)
        draw.line((cx - int(r * 0.28), cy + int(r * 0.35), cx + int(r * 0.28), cy + int(r * 0.32)), fill=(18, 20, 28), width=6)

    elif mood == "nerd":
        for ex in (cx - eye_dx, cx + eye_dx):
            draw.ellipse((ex - eye_r - 5, eye_y - eye_r - 5, ex + eye_r + 5, eye_y + eye_r + 5), fill=(255, 255, 255), outline=(18, 20, 28), width=5)
            draw.ellipse((ex - 5, eye_y - 6, ex + 5, eye_y + 4), fill=(18, 20, 28))
        # Glasses bridge
        draw.line((cx - eye_dx + eye_r + 5, eye_y, cx + eye_dx - eye_r - 5, eye_y), fill=(18, 20, 28), width=5)
        # Goofy buck teeth grin
        mouth_y = cy + int(r * 0.30)
        draw.arc((cx - int(r * 0.36), mouth_y - 18, cx + int(r * 0.36), mouth_y + 22), start=0, end=180, fill=(18, 20, 28), width=5)
        draw.rectangle((cx - 12, mouth_y + 2, cx - 1, mouth_y + 16), fill=(255, 255, 255), outline=(18, 20, 28), width=2)
        draw.rectangle((cx + 1, mouth_y + 2, cx + 12, mouth_y + 16), fill=(255, 255, 255), outline=(18, 20, 28), width=2)

    elif mood == "shock":
        for ex in (cx - eye_dx, cx + eye_dx):
            draw.ellipse((ex - eye_r - 4, eye_y - eye_r - 6, ex + eye_r + 4, eye_y + eye_r + 6), fill=(255, 255, 255), outline=(18, 20, 28), width=3)
            draw.ellipse((ex - 4, eye_y - 4, ex + 4, eye_y + 4), fill=(18, 20, 28))
        mouth_y = cy + int(r * 0.36)
        draw.ellipse((cx - 16, mouth_y - 20, cx + 16, mouth_y + 20), fill=(18, 20, 28), outline=(255, 255, 255), width=3)

    elif mood == "monocle":
        # Left normal eye, right golden monocle + raised eyebrow
        lx, rx = cx - eye_dx, cx + eye_dx
        draw.ellipse((lx - eye_r, eye_y - eye_r, lx + eye_r, eye_y + eye_r), fill=(255, 255, 255))
        draw.ellipse((lx - 5, eye_y - 5, lx + 5, eye_y + 5), fill=(18, 20, 28))
        draw.ellipse((rx - eye_r - 6, eye_y - eye_r - 6, rx + eye_r + 6, eye_y + eye_r + 6), fill=(245, 248, 255), outline=(242, 201, 76), width=5)
        draw.ellipse((rx - 5, eye_y - 5, rx + 5, eye_y + 5), fill=(18, 20, 28))
        draw.line((rx + eye_r + 4, eye_y + 6, rx + eye_r + 8, cy + int(r * 0.55)), fill=(242, 201, 76), width=3)
        draw.arc((rx - eye_r - 6, eye_y - eye_r - 22, rx + eye_r + 6, eye_y - eye_r - 4), start=200, end=340, fill=(18, 20, 28), width=5)
        draw.arc((cx - int(r * 0.25), cy + int(r * 0.22), cx + int(r * 0.30), cy + int(r * 0.48)), start=10, end=155, fill=(18, 20, 28), width=5)

    else:  # deadpan
        for ex in (cx - eye_dx, cx + eye_dx):
            draw.ellipse((ex - 7, eye_y - 7, ex + 7, eye_y + 7), fill=(18, 20, 28))
        draw.line((cx - int(r * 0.32), cy + int(r * 0.34), cx + int(r * 0.32), cy + int(r * 0.34)), fill=(18, 20, 28), width=6)


def _draw_outlined_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    *,
    fill: tuple[int, int, int] = (255, 255, 255),
    stroke_fill: tuple[int, int, int] = (12, 14, 20),
    stroke_width: int = 4,
    anchor: str = "mm",
) -> None:
    draw.text(
        xy,
        text,
        font=font,
        fill=fill,
        stroke_fill=stroke_fill,
        stroke_width=stroke_width,
        anchor=anchor,
    )


def render_meme_png(
    message_text: str = "",
    author_name: str = "Someone",
    *,
    template: str | None = None,
) -> tuple[bytes, str]:
    """Render a funny meme card PNG in memory and return ``(png_bytes, template_name)``."""
    chosen = template if template in MEME_TEMPLATES else random.choice(MEME_TEMPLATES)
    quote = clean_meme_text(message_text, max_chars=105)
    author = clean_meme_text(author_name, max_chars=26) or "Chat Member"

    width, height = 720, 420
    img = Image.new("RGB", (width, height), (19, 21, 29))
    draw = ImageDraw.Draw(img)

    if chosen == "nobody_quote":
        draw.rounded_rectangle((16, 16, width - 16, height - 16), radius=22, fill=(28, 31, 43), outline=(66, 72, 98), width=2)
        f_muted = _load_font("regular", 24)
        f_author = _load_font("bold", 26)
        f_quote = _load_font("display", 32)
        f_badge = _load_font("bold", 16)

        draw.text((44, 42), "Nobody:", font=f_muted, fill=(156, 163, 184))
        draw.text((44, 78), "Not a single soul on Earth:", font=f_muted, fill=(156, 163, 184))
        draw.text((44, 128), f"{author}:", font=f_author, fill=(114, 137, 255))

        lines = _wrap_lines(draw, f'"{quote}"', f_quote, width - 96, max_lines=3)
        y = 172
        for line in lines:
            draw.text((44, y), line, font=f_quote, fill=(245, 247, 252))
            y += 42

        badge = random.choice(_VERDICT_BADGES)
        draw.rounded_rectangle((44, height - 74, width - 44, height - 34), radius=12, fill=(43, 48, 66))
        draw.text((width // 2, height - 54), badge, font=f_badge, fill=(242, 201, 76), anchor="mm")

    elif chosen == "breaking_news":
        # Studio dark-blue backdrop with grid lines
        img.paste((14, 22, 42), (0, 0, width, height))
        for gx in range(0, width, 48):
            draw.line((gx, 0, gx, height), fill=(22, 34, 62), width=1)
        for gy in range(0, height, 48):
            draw.line((0, gy, width, gy), fill=(22, 34, 62), width=1)

        # LIVE pill
        f_small = _load_font("bold", 17)
        draw.rounded_rectangle((36, 28, 132, 62), radius=8, fill=(214, 40, 40))
        draw.ellipse((48, 39, 60, 51), fill=(255, 255, 255))
        draw.text((92, 45), "LIVE", font=f_small, fill=(255, 255, 255), anchor="mm")
        draw.text((width - 36, 45), "PARADISE NEWS NETWORK", font=f_small, fill=(164, 182, 218), anchor="rm")

        # Mascot anchor in center-right
        _draw_mascot_face(draw, (492, 74, 684, 266), random.choice(("monocle", "shock", "deadpan")), accent=(59, 130, 246))

        # Headline quote on left
        f_label = _load_font("bold", 19)
        f_head = _load_font("display", 28)
        draw.text((38, 86), f"LOCAL MEMBER {author.upper()} SAYS:", font=f_label, fill=(248, 196, 68))
        lines = _wrap_lines(draw, f'"{quote}"', f_head, 430, max_lines=4)
        y = 122
        for line in lines:
            draw.text((38, y), line, font=f_head, fill=(255, 255, 255))
            y += 36

        # Lower-third chyron
        draw.rectangle((0, height - 110, width, height - 52), fill=(204, 29, 36))
        f_banner = _load_font("display", 28)
        draw.text((36, height - 81), "BREAKING NEWS", font=f_banner, fill=(255, 255, 255), anchor="lm")

        draw.rectangle((0, height - 52, width, height), fill=(244, 246, 250))
        ticker = random.choice(_NEWS_TICKERS)
        draw.text((36, height - 26), ticker, font=_load_font("bold", 16), fill=(18, 22, 33), anchor="lm")

    elif chosen == "live_reaction":
        draw.rounded_rectangle((14, 14, width - 14, height - 14), radius=20, fill=(26, 28, 36), outline=(224, 52, 58), width=4)
        draw.rounded_rectangle((28, 26, width - 28, 84), radius=12, fill=(214, 42, 49))
        draw.text((width // 2, 55), "LIVE PARADISE REACTION", font=_load_font("display", 30), fill=(255, 255, 255), anchor="mm")

        # Left panel: quoted message; Right panel: mascot reaction
        draw.rounded_rectangle((32, 102, 428, 332), radius=16, fill=(36, 40, 54))
        draw.text((52, 124), f"{author} in chat:", font=_load_font("bold", 19), fill=(142, 156, 255))
        q_font = _load_font("medium", 24)
        lines = _wrap_lines(draw, f'"{quote}"', q_font, 356, max_lines=4)
        y = 160
        for line in lines:
            draw.text((52, y), line, font=q_font, fill=(238, 241, 248))
            y += 34

        draw.rounded_rectangle((444, 102, width - 32, 332), radius=16, fill=(36, 40, 54))
        palette = random.choice(((88, 101, 242), (235, 87, 87), (39, 174, 96), (242, 153, 74)))
        _draw_mascot_face(draw, (458, 110, width - 46, 324), random.choice(_FACES), accent=palette)

        caption = random.choice(_LIVE_CAPTIONS)
        draw.text((width // 2, 370), caption, font=_load_font("display", 23), fill=(248, 212, 88), anchor="mm")

    elif chosen == "brain_scan":
        draw.rounded_rectangle((16, 16, width - 16, height - 16), radius=20, fill=(22, 27, 38), outline=(56, 189, 248), width=2)
        draw.text((width // 2, 48), "NEUROLOGICAL CHAT SCANNER v4.2", font=_load_font("display", 24), fill=(56, 189, 248), anchor="mm")

        # Left card
        draw.rounded_rectangle((34, 82, 346, 376), radius=16, fill=(31, 39, 55))
        draw.text((190, 112), "EXPECTED BRAIN", font=_load_font("bold", 19), fill=(148, 163, 184), anchor="mm")
        _draw_mascot_face(draw, (110, 134, 270, 294), "monocle", accent=(52, 211, 153))
        draw.text((190, 326), "Hydrated, calm, rational", font=_load_font("medium", 18), fill=(203, 213, 225), anchor="mm")
        draw.text((190, 352), "100% Normal Activity", font=_load_font("bold", 16), fill=(52, 211, 153), anchor="mm")

        # Right card
        draw.rounded_rectangle((374, 82, width - 34, 376), radius=16, fill=(39, 31, 52))
        draw.text((530, 112), f"{author.upper()}'S BRAIN", font=_load_font("bold", 19), fill=(244, 114, 182), anchor="mm")
        _draw_mascot_face(draw, (450, 130, 610, 266), "nerd", accent=(168, 85, 247))
        q_font = _load_font("italic", 17)
        lines = _wrap_lines(draw, f'"{quote}"', q_font, 280, max_lines=2)
        y = 278
        for line in lines:
            draw.text((530, y), line, font=q_font, fill=(241, 245, 249), anchor="mm")
            y += 23
        draw.text((530, 352), "97% Warm Soup  •  3% Wi-Fi Static", font=_load_font("bold", 15), fill=(251, 191, 36), anchor="mm")

    elif chosen == "museum_frame":
        # Rich gallery burgundy backdrop + gold double frame
        img.paste((36, 22, 28), (0, 0, width, height))
        draw.rectangle((36, 28, width - 36, 304), fill=(24, 22, 26), outline=(212, 175, 55), width=6)
        draw.rectangle((48, 40, width - 48, 292), outline=(168, 132, 38), width=2)

        q_font = _load_font("italic", 29)
        lines = _wrap_lines(draw, f'"{quote}"', q_font, width - 148, max_lines=4)
        total_h = len(lines) * 38
        y = 166 - total_h // 2
        for line in lines:
            draw.text((width // 2, y), line, font=q_font, fill=(248, 244, 232), anchor="mm")
            y += 38

        # Brass museum plaque underneath
        draw.rounded_rectangle((130, 322, width - 130, 396), radius=10, fill=(216, 184, 88), outline=(120, 92, 24), width=2)
        draw.text(
            (width // 2, 347),
            f"{author}, Digital Era",
            font=_load_font("bold", 20),
            fill=(28, 22, 10),
            anchor="mm",
        )
        draw.text(
            (width // 2, 374),
            "Oil on Discord  •  Historians remain deeply confused",
            font=_load_font("regular", 15),
            fill=(48, 38, 16),
            anchor="mm",
        )

    else:  # impact_reaction
        top_text, bottom_text = random.choice(_IMPACT_PAIRS)
        # Retro starburst rays around center
        cx, cy = width // 2, height // 2
        c1, c2 = (31, 41, 77), (46, 61, 112)
        img.paste(c1, (0, 0, width, height))
        for idx in range(0, 360, 30):
            rad1 = math.radians(idx)
            rad2 = math.radians(idx + 15)
            r_far = 600
            poly = [
                (cx, cy),
                (cx + int(r_far * math.cos(rad1)), cy + int(r_far * math.sin(rad1))),
                (cx + int(r_far * math.cos(rad2)), cy + int(r_far * math.sin(rad2))),
            ]
            draw.polygon(poly, fill=c2)

        _draw_mascot_face(draw, (cx - 108, cy - 104, cx + 108, cy + 112), random.choice(_FACES), accent=(242, 153, 74))

        f_impact = _load_font("display", 34)
        _draw_outlined_text(draw, (cx, 48), top_text, f_impact)
        _draw_outlined_text(draw, (cx, height - 46), bottom_text, _load_font("display", 30))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), chosen


def render_sticker_png() -> bytes:
    """Render a transparent die-cut vinyl sticker PNG (fallback when guild has no stickers)."""
    size = 320
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    top_word, bottom_word, color = random.choice(_STICKER_BADGES)
    # Die-cut white vinyl outline + subtle shadow
    draw.ellipse((24, 22, size - 24, size - 34), fill=(0, 0, 0, 75))
    draw.ellipse((20, 14, size - 20, size - 42), fill=(255, 255, 255, 255))
    _draw_mascot_face(draw, (34, 28, size - 34, size - 56), random.choice(_FACES), accent=color)

    # Die-cut banner ribbon across the bottom
    draw.rounded_rectangle((22, size - 96, size - 22, size - 16), radius=22, fill=(255, 255, 255, 255))
    draw.rounded_rectangle((28, size - 90, size - 28, size - 22), radius=18, fill=(24, 27, 38, 255))
    draw.text((size // 2, size - 69), top_word, font=_load_font("bold", 18), fill=(186, 196, 220, 255), anchor="mm")
    draw.text((size // 2, size - 42), bottom_word, font=_load_font("display", 26), fill=(255, 214, 79, 255), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class StickerPicker:
    """Selects random native Discord stickers (guild custom + standard packs)."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._standard_stickers: list | None = None
        self._fetch_lock = asyncio.Lock()
        self._recent_ids: deque[int] = deque(maxlen=12)

    async def _get_standard_stickers(self) -> list:
        if self._standard_stickers is not None:
            return self._standard_stickers
        async with self._fetch_lock:
            if self._standard_stickers is not None:
                return self._standard_stickers
            fetcher = getattr(self.bot, "fetch_premium_sticker_packs", None)
            if not callable(fetcher):
                self._standard_stickers = []
                return self._standard_stickers
            try:
                packs = await asyncio.wait_for(fetcher(), timeout=4.0)
                stickers = [
                    sticker
                    for pack in (packs or ())
                    for sticker in getattr(pack, "stickers", ())
                ]
                self._standard_stickers = stickers
            except Exception:  # noqa: BLE001
                log.debug("Could not fetch standard Discord sticker packs", exc_info=True)
                self._standard_stickers = []
            return self._standard_stickers

    async def pick(self, guild) -> object | None:
        """Pick a random available sticker from the guild or Discord standard packs."""
        guild_stickers = [
            s
            for s in (getattr(guild, "stickers", None) or ())
            if getattr(s, "available", True)
        ]
        standard_stickers = await self._get_standard_stickers()

        # Prefer the server's own custom stickers when present, while still
        # mixing in Discord's standard sticker characters for variety.
        if guild_stickers and standard_stickers:
            pool = guild_stickers if random.random() < 0.65 else standard_stickers
        else:
            pool = guild_stickers or standard_stickers

        if not pool:
            return None

        fresh = [s for s in pool if getattr(s, "id", None) not in self._recent_ids]
        choice = random.choice(fresh or pool)
        sticker_id = getattr(choice, "id", None)
        if sticker_id is not None:
            self._recent_ids.append(sticker_id)
        return choice
