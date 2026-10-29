"""Value objects for the music engine.

Nothing here touches Discord, FFmpeg or the network: a :class:`Track` is a
description of a song, not a stream. The stream is resolved at the moment
playback starts (see :meth:`functions.music.search.YtDlpResolver.stream_url`)
because YouTube's media URLs expire within hours — caching one in the queue
would produce a track that starts fine and dies halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def format_duration(seconds: float | int | None) -> str:
    """``3725`` → ``1:02:05``; unknown durations render as ``live``."""
    total = int(seconds or 0)
    if total <= 0:
        return "live"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class LoopMode(str, Enum):
    """What happens when a track ends."""

    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

    @classmethod
    def parse(cls, raw: object) -> "LoopMode":
        text = str(raw or "").strip().lower()
        for mode in cls:
            if mode.value == text:
                return mode
        if text in {"one", "single", "song", "again", "repeat"}:
            return cls.TRACK
        if text in {"all", "playlist", "list"}:
            return cls.QUEUE
        return cls.OFF

    @property
    def label(self) -> str:
        return {"off": "off", "track": "this song", "queue": "the queue"}[self.value]


@dataclass(frozen=True, slots=True)
class Track:
    """One request: metadata now, audio later."""

    title: str
    url: str
    requester_id: int = 0
    requester_name: str = ""
    duration: int = 0
    thumbnail: str = ""
    source: str = "youtube"
    identifier: str = ""

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)

    @property
    def requester_label(self) -> str:
        return self.requester_name or "someone"

    def with_requester(self, user_id: int, name: str) -> "Track":
        return Track(
            title=self.title,
            url=self.url,
            requester_id=user_id,
            requester_name=name,
            duration=self.duration,
            thumbnail=self.thumbnail,
            source=self.source,
            identifier=self.identifier,
        )


@dataclass(slots=True)
class PlayerState:
    """A point-in-time view used by the panel and the queue embed."""

    guild_id: int
    channel_id: int = 0
    current: Track | None = None
    position: float = 0.0
    volume: float = 0.5
    paused: bool = False
    playing: bool = False
    speaking: bool = False
    loop: LoopMode = LoopMode.OFF
    upcoming: tuple[Track, ...] = ()

    @property
    def connected(self) -> bool:
        return bool(self.channel_id)

    @property
    def progress(self) -> float:
        """0.0 → 1.0 through the current track (0.0 for live streams)."""
        if self.current is None or not self.current.duration:
            return 0.0
        return max(0.0, min(1.0, self.position / self.current.duration))
