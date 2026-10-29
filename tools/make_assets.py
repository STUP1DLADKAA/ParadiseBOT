"""Regenerate the images used in the README.

Everything here is drawn by the bot's own code — the same renderer that
produces `/vc leaderboard` — so the showcase can never drift from reality.

    python tools/make_assets.py

Outputs:
    assets/banner.png             README header
    assets/leaderboard-card.png   a real leaderboard card (fictional members)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw  # noqa: E402

from functions.voice.models import LeaderboardMember, VoicePeriod  # noqa: E402
from functions.voice.render import (  # noqa: E402
    ACCENT_CYAN,
    ACCENT_PINK,
    ACCENT_PURPLE,
    CARD_BORDER,
    SURFACE_COLOR,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    LeaderboardImageRenderer,
    _background,
    _horizontal_ramp,
)

ASSETS = REPO / "assets"

# Fictional members. The numbers are made up; the rendering is real.
MEMBERS: list[tuple[str, int]] = [
    ("Aarav Mehta", 18_640),
    ("Riya Sharma", 14_220),
    ("Kabir Singh", 11_980),
    ("Ananya Iyer", 8_320),
    ("Dev Patel", 6_410),
    ("Meera Nair", 5_200),
    ("Arjun Rao", 4_340),
    ("Sara Khan", 3_210),
    ("Vikram Bose", 2_500),
    ("Ishita Verma", 1_880),
    ("Rohan Gupta", 1_120),
    ("Tara Menon", 640),
]


def sample_board() -> list[LeaderboardMember]:
    total = sum(seconds for _, seconds in MEMBERS)
    return [
        LeaderboardMember(
            rank=rank,
            user_id=1_000 + rank,
            display_name=name,
            username=name.lower().replace(" ", ""),
            avatar_url=None,
            total_seconds=seconds,
            percentage=round(seconds / total * 100, 1),
            is_active_now=rank in (2, 5),
        )
        for rank, (name, seconds) in enumerate(MEMBERS, start=1)
    ]


def write_leaderboard_card() -> Path:
    members = sample_board()
    total = sum(member.total_seconds for member in members)
    png = LeaderboardImageRenderer()._draw_canvas(
        "Paradise Community",
        VoicePeriod.WEEK,
        members,
        [None] * len(members),
        "13 – 19 OCT 2026",
        1,
        len(members),
        total,
        None,
        None,
    )
    path = ASSETS / "leaderboard-card.png"
    path.write_bytes(png)
    return path


def write_banner(width: int = 1280, height: int = 400) -> Path:
    """A quiet, brand-consistent header — gradient wash, wordmark, strapline."""
    from functions.dictionary.fonts import FontLibrary

    canvas = _background(width, height, ACCENT_CYAN).copy()
    draw = ImageDraw.Draw(canvas)
    library = FontLibrary()

    def font(role: str, size: int):
        face = library.get(role, size)
        return face.faces[0].font if face.faces else None

    title_font = font("display", 78) or font("semibold", 78)
    sub_font = font("medium", 24) or font("regular", 24)
    pill_font = font("semibold", 15) or font("regular", 15)

    # Brand pill.
    pill_text = "DISCORD BOT  ·  TOOLS FIRST, AI LAST"
    pill_w = draw.textbbox((0, 0), pill_text, font=pill_font)[2] + 44
    draw.rounded_rectangle(
        (64, 54, 64 + pill_w, 54 + 34), radius=17, fill=(*SURFACE_COLOR, 235),
        outline=CARD_BORDER, width=1,
    )
    draw.ellipse((80, 66, 90, 76), fill=ACCENT_CYAN)
    draw.text((100, 63), pill_text, font=pill_font, fill=TEXT_SECONDARY)

    # Wordmark.
    draw.text((62, 108), "Paradise", font=title_font, fill=TEXT_PRIMARY)
    word_w = draw.textbbox((0, 0), "Paradise", font=title_font)[2]
    draw.text((62 + word_w + 16, 108), "Bot", font=title_font, fill=ACCENT_PURPLE)

    # Strapline.
    draw.text(
        (64, 222),
        "Voice leaderboards · dictionary cards · weather cards · spoken answers",
        font=sub_font,
        fill=TEXT_SECONDARY,
    )
    draw.text(
        (64, 262),
        "Zero mandatory API keys. Every card drawn in code, never generated.",
        font=sub_font,
        fill=TEXT_MUTED,
    )

    # Accent rule + glow dots.
    ramp = _horizontal_ramp(width - 128, 4, ACCENT_PURPLE, ACCENT_PINK).convert("RGB")
    canvas.paste(ramp, (64, height - 46))
    for index, colour in enumerate((ACCENT_PURPLE, ACCENT_CYAN, ACCENT_PINK)):
        x = width - 92 + index * 14
        draw.ellipse((x, height - 54, x + 8, height - 46), fill=colour)

    path = ASSETS / "banner.png"
    canvas.convert("RGB").save(path, "PNG", optimize=True)
    return path


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for path in (write_leaderboard_card(), write_banner()):
        with Image.open(path) as image:
            print(f"wrote {path.relative_to(REPO)} ({image.width}x{image.height}, "
                  f"{path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
