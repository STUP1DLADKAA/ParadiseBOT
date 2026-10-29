"""The song queue: bounded, ordered, and deliberately boring.

Boring is the point. A queue is the one part of a music bot that users notice
immediately when it is wrong — a track that vanishes, a "skip" that skips two,
an off-by-one when the last song ends — so it is a plain list with explicit
operations and no clever side effects.
"""

from __future__ import annotations

import random
from typing import Iterable, Iterator, Sequence

from functions.helpers import ParadiseError

from .models import Track

DEFAULT_LIMIT = 50


class QueueFull(ParadiseError):
    """The queue is at its configured limit."""


class MusicQueue:
    """An ordered list of :class:`Track` with a hard limit."""

    def __init__(self, limit: int = DEFAULT_LIMIT, tracks: Iterable[Track] = ()) -> None:
        self._limit = max(1, int(limit))
        self._tracks: list[Track] = list(tracks)[: self._limit]

    # -- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tracks)

    def __bool__(self) -> bool:
        return bool(self._tracks)

    def __iter__(self) -> Iterator[Track]:
        return iter(list(self._tracks))

    @property
    def limit(self) -> int:
        return self._limit

    def snapshot(self) -> tuple[Track, ...]:
        return tuple(self._tracks)

    def peek(self) -> Track | None:
        return self._tracks[0] if self._tracks else None

    # -- mutation -----------------------------------------------------------

    def add(self, track: Track) -> None:
        if len(self._tracks) >= self._limit:
            raise QueueFull(
                f"the queue is full ({self._limit} songs). skip something first."
            )
        self._tracks.append(track)

    def add_many(self, tracks: Iterable[Track]) -> int:
        """Add as many as fit and return how many were added."""
        added = 0
        for track in tracks:
            if len(self._tracks) >= self._limit:
                break
            self._tracks.append(track)
            added += 1
        return added

    def pop(self) -> Track | None:
        return self._tracks.pop(0) if self._tracks else None

    def push_front(self, track: Track) -> None:
        """Used by loop-one and by "play this next"."""
        self._tracks.insert(0, track)

    def remove(self, index: int) -> Track | None:
        """Remove a 1-based entry, as shown in the queue embed."""
        position = int(index) - 1
        if position < 0 or position >= len(self._tracks):
            return None
        return self._tracks.pop(position)

    def clear(self) -> None:
        self._tracks.clear()

    def shuffle(self, rng: random.Random | None = None) -> int:
        (rng or random).shuffle(self._tracks)
        return len(self._tracks)

    def move(self, source: int, destination: int) -> bool:
        """Move a 1-based entry to another 1-based position."""
        start, end = int(source) - 1, int(destination) - 1
        if not (0 <= start < len(self._tracks)) or not (0 <= end < len(self._tracks)):
            return False
        self._tracks.insert(end, self._tracks.pop(start))
        return True

    def total_duration(self) -> int:
        return sum(max(0, track.duration) for track in self._tracks)

    def as_list(self) -> Sequence[Track]:
        return tuple(self._tracks)
