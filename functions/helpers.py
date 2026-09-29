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


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"
