"""Small shared helpers: user-facing errors, parsing, formatting, colours.

Everything here is dependency-light and safe to import from anywhere.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import discord


# ---------------------------------------------------------------------------
# User-facing errors
# ---------------------------------------------------------------------------


class ParadiseError(Exception):
    """An error whose message is safe and friendly to show to users."""


class MusicError(ParadiseError):
    """Raised by the music service; the message is shown to the user."""


class NodePlaybackUnsupported(ParadiseError):
    """The Lavalink node cannot play direct audio links (no http source)."""


class TTSError(ParadiseError):
    """Raised by the TTS service; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Palette — muted, dark-Discord-native. No neon.
# ---------------------------------------------------------------------------


class Colour:
    PLAYING = 0x2C8C64  # muted green
    PAUSED = 0xB58B2A  # muted amber
    IDLE = 0x6B7280  # neutral grey
    DISCONNECTED = 0x4F545C  # discord grey
    SPEAKING = 0x5B6EAE  # soft indigo
    ACCENT = 0x2E8B74  # general Paradise accent


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # symbols, pictographs, emoji
    "\U00002600-\U000027BF"  # misc symbols / dingbats
    "\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "\U00002190-\U000021FF"  # arrows
    "\U0000FE0F\U0000200D"  # variation selector / ZWJ
    "]",
)
_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>|<a?:\w+:\d+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_for_speech(text: str, max_length: int) -> str:
    """Prepare raw chat text for text-to-speech."""
    text = _MENTION_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _EMOJI_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_length]


def is_hindi(text: str) -> bool:
    """True when a meaningful share of the text is Devanagari."""
    if not text:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    devanagari = sum("\u0900" <= ch <= "\u097F" for ch in letters)
    return devanagari / len(letters) > 0.2


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


def format_duration(ms: int) -> str:
    """Format milliseconds as m:ss / h:mm:ss."""
    seconds = max(0, int(ms // 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(position_ms: int, duration_ms: int, width: int = 10) -> str:
    """A compact textual progress bar like ``▬▬▬●▬▬▬▬▬▬``."""
    if duration_ms <= 0:
        return "▬" * width
    ratio = max(0.0, min(1.0, position_ms / duration_ms))
    filled = max(0, min(round(width * ratio), width - 1))
    return "▬" * filled + "●" + "▬" * (width - filled - 1)


# ---------------------------------------------------------------------------
# Voice / permission helpers
# ---------------------------------------------------------------------------


def human_voice_members(guild: discord.Guild, channel_id: int) -> list[discord.Member]:
    """Humans currently in a voice channel (uses live voice states, not cache)."""
    members = []
    for member, state in guild.voice_states.items():
        if state.channel and state.channel.id == channel_id and not member.bot:
            members.append(member)
    return members


def member_in_voice(member: discord.Member) -> bool:
    return member.voice is not None and member.voice.channel is not None


def same_voice_channel(member: discord.Member, channel_id: int | None) -> bool:
    return (
        channel_id is not None
        and member.voice is not None
        and member.voice.channel is not None
        and member.voice.channel.id == channel_id
    )


def can_bypass_voice_check(member: discord.Member) -> bool:
    """Server permissions that reasonably allow remote voice control."""
    return member.guild_permissions.move_members or member.guild_permissions.administrator


def voice_channel_name(guild: discord.Guild, channel_id: int | None) -> str | None:
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    return channel.name if channel else None


async def send_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Best-effort ephemeral reply that never raises."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.DiscordException:
        pass


def chunks(items: Sequence, size: int) -> Iterable[list]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])
