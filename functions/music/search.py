"""Turn a search phrase into a playable track, and a track into a stream URL.

yt-dlp does the actual work: it understands YouTube, YouTube Music,
SoundCloud, Bandcamp, Vimeo and a thousand other sites, and it keeps working
when those sites change because it is updated far more often than this bot.

Two rules keep it honest:

* **Search is separate from streaming.** Searching returns metadata
  (title/duration/thumbnail); the direct media URL is fetched at the moment
  playback starts, because those URLs expire.
* **It all runs in the thread pool.** yt-dlp is synchronous, network-bound and
  occasionally slow. Calling it on the event loop would freeze every command
  in the bot — the exact thing `functions/perf.py` exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol, runtime_checkable

from functions.helpers import ParadiseError

from .models import Track

log = logging.getLogger(__name__)

_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})")
_URL = re.compile(r"^https?://", re.IGNORECASE)


class SearchUnavailable(ParadiseError):
    """Search cannot run right now (no yt-dlp, or the site refused)."""


class NoResults(SearchUnavailable):
    """Search ran and found nothing playable."""


@runtime_checkable
class TrackResolver(Protocol):
    """What the player needs from a search backend."""

    async def search(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        limit: int = 1,
    ) -> list[Track]:
        ...

    async def stream_url(self, track: Track) -> str:
        ...


class YtDlpResolver:
    """yt-dlp as a resolver: ``ytsearch1:<query>`` for text, direct for URLs."""

    def __init__(
        self,
        *,
        search_prefix: str = "ytsearch",
        timeout: float = 25.0,
        cookies_file: str = "",
        proxy: str = "",
        prefer_m4a: bool = True,
    ) -> None:
        self.search_prefix = (search_prefix or "ytsearch").strip().rstrip(":")
        self.timeout = max(5.0, float(timeout))
        self.cookies_file = cookies_file.strip()
        self.proxy = proxy.strip()
        self.prefer_m4a = prefer_m4a

    # -- capability ---------------------------------------------------------

    @staticmethod
    def installed() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("yt_dlp") is not None
        except (ImportError, ValueError):  # pragma: no cover - broken install
            return False

    @property
    def available(self) -> bool:
        return self.installed()

    def describe(self) -> str:
        if not self.available:
            return "search: unavailable (pip install yt-dlp)"
        return f"search: yt-dlp ({self.search_prefix})"

    # -- public API ---------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        limit: int = 1,
    ) -> list[Track]:
        text = " ".join(str(query or "").split()).strip()
        if not text:
            raise NoResults("there's nothing to search for.")
        target = text if _URL.match(text) else f"{self.search_prefix}{max(1, limit)}:{text}"
        info = await self._extract(target)
        entries = self._entries(info)
        if not entries:
            raise NoResults(f"i couldn't find anything for “{text[:80]}”.")
        tracks: list[Track] = []
        for entry in entries[: max(1, limit)]:
            track = self._to_track(entry)
            if track is not None:
                tracks.append(track.with_requester(requester_id, requester_name))
        if not tracks:
            raise NoResults(f"i couldn't find anything playable for “{text[:80]}”.")
        return tracks

    async def stream_url(self, track: Track) -> str:
        """Resolve a fresh, direct media URL for playback."""
        info = await self._extract(track.url or track.identifier)
        url = self._pick_stream(info)
        if not url:
            raise SearchUnavailable(
                f"i can't stream “{track.title}” right now (the site gave no audio)."
            )
        return url

    # -- internals ----------------------------------------------------------

    async def _extract(self, target: str) -> dict:
        if not self.available:
            raise SearchUnavailable(
                "music search needs yt-dlp. the owner has to run "
                "`pip install yt-dlp` (it is in requirements.txt)."
            )
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._extract_sync, target),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise SearchUnavailable("that search took too long. try again?") from None
        except SearchUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - yt-dlp raises everything
            log.info("yt-dlp failed for %r: %s", target[:80], error)
            raise SearchUnavailable(
                "the music site refused that one. try a different search."
            ) from error

    def _extract_sync(self, target: str) -> dict:
        import yt_dlp  # imported here so the package is optional

        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "nocheckcertificate": True,
            "socket_timeout": int(self.timeout),
            "extract_flat": False,
            "default_search": self.search_prefix,
            "cachedir": False,
            "logger": _QuietLogger(),
        }
        if self.cookies_file:
            options["cookiefile"] = self.cookies_file
        if self.proxy:
            options["proxy"] = self.proxy
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(target, download=False) or {}

    @staticmethod
    def _entries(info: dict) -> list[dict]:
        if not info:
            return []
        entries = info.get("entries")
        if entries is None:
            playable = info.get("title") or info.get("url") or info.get("formats")
            return [info] if playable else []
        return [entry for entry in entries if entry]

    def _to_track(self, entry: dict) -> Track | None:
        webpage = str(entry.get("webpage_url") or entry.get("original_url") or "").strip()
        identifier = str(entry.get("id") or "").strip()
        if not webpage and identifier:
            webpage = f"https://www.youtube.com/watch?v={identifier}"
        if not webpage:
            return None
        title = str(entry.get("title") or "untitled").strip()
        duration = entry.get("duration") or 0
        try:
            duration = int(float(duration))
        except (TypeError, ValueError):
            duration = 0
        thumbnail = str(entry.get("thumbnail") or "").strip()
        if not thumbnail:
            thumbnails = entry.get("thumbnails") or []
            if thumbnails and isinstance(thumbnails[-1], dict):
                thumbnail = str(thumbnails[-1].get("url") or "").strip()
        return Track(
            title=title,
            url=webpage,
            duration=max(0, duration),
            thumbnail=thumbnail,
            source=str(entry.get("extractor_key") or entry.get("ie_key") or "youtube"),
            identifier=identifier,
        )

    def _pick_stream(self, info: dict) -> str:
        """Prefer a pure-audio format; fall back to whatever has audio."""
        candidates: list[dict] = []
        for entry in self._entries(info):
            formats = entry.get("formats") or []
            for fmt in formats:
                if not fmt.get("url"):
                    continue
                if fmt.get("acodec") in (None, "none"):
                    continue
                candidates.append(fmt)
            if entry.get("url") and not formats:
                return str(entry["url"])
        if not candidates:
            return str(info.get("url") or "")

        def score(fmt: dict) -> tuple:
            audio_only = fmt.get("vcodec") in (None, "none")
            container = str(fmt.get("ext") or "")
            return (
                1 if audio_only else 0,
                1 if (self.prefer_m4a and container in {"m4a", "mp4"}) else 0,
                float(fmt.get("abr") or fmt.get("tbr") or 0),
            )

        best = max(candidates, key=score)
        return str(best.get("url") or "")


class _QuietLogger:
    """Route yt-dlp's chatter into our debug log instead of stdout."""

    def debug(self, message: str) -> None:  # pragma: no cover - logging shim
        log.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:  # pragma: no cover - logging shim
        log.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:  # pragma: no cover - logging shim
        log.debug("yt-dlp: %s", message)

    def error(self, message: str) -> None:  # pragma: no cover - logging shim
        log.info("yt-dlp: %s", message)
