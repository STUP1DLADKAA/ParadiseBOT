"""Small shared helpers: user-facing errors, parsing, formatting, colours.

Everything here is dependency-light and safe to import from anywhere.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# User-facing errors
# ---------------------------------------------------------------------------


class ParadiseError(Exception):
    """An error whose message is safe and friendly to show to users."""


# ---------------------------------------------------------------------------
# Palette — muted, dark-Discord-native. No neon.
# ---------------------------------------------------------------------------


class Colour:
    ACCENT = 0x2E8B74  # general Paradise accent


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def parse_options(raw: str, limit: int = 20) -> list[str]:
    """Split a multi-option argument on ``|`` or ``,`` and drop empties."""
    parts = re.split(r"[|,]", raw)
    options: list[str] = []
    for part in parts:
        part = part.strip()
        if part and part not in options:
            options.append(part)
    return options[:limit]


DISCORD_MESSAGE_LIMIT = 2000


def split_discord_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into complete Discord-sized messages.

    Normal bot replies should fit in one message.  This is intentionally a
    boundary-aware fallback for an unexpectedly long model response or a
    long native result: prefer paragraphs, then lines, sentences, and words
    before ever falling back to a hard split.  It never adds an ellipsis or
    silently drops content.
    """
    value = str(text or "")
    if not value:
        return [""]
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(value) <= limit:
        return [value]

    chunks: list[str] = []
    remaining = value.strip()
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cut = max(
            window.rfind("\n\n"),
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("? "),
            window.rfind("! "),
            window.rfind(" "),
        )
        if cut <= 0:
            cut = limit
        elif window[cut:cut + 1] in ".?!":
            cut += 1
        chunk = remaining[:cut].rstrip()
        if not chunk:  # defensive: always make progress
            chunk = remaining[:limit]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"
