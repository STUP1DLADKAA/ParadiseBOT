"""Small per-guild settings store, persisted to a JSON file.

Only genuinely supported options live here. Nothing speculative.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from typing import Any

log = logging.getLogger(__name__)

TTS_COOLDOWN_CHOICES = (10, 20, 30, 45, 60)


@dataclass(slots=True)
class GuildSettings:
    tts_enabled: bool = True
    tts_cooldown: int = 30  # per-user cooldown, seconds
    auto_disconnect: bool = True  # leave empty channels during music sessions
    panel_channel_id: int | None = None
    panel_message_id: int | None = None

    def validate(self) -> None:
        self.tts_cooldown = int(self.tts_cooldown)
        if self.tts_cooldown not in TTS_COOLDOWN_CHOICES:
            # Snap to the nearest allowed value rather than rejecting state.
            self.tts_cooldown = min(
                TTS_COOLDOWN_CHOICES, key=lambda c: abs(c - self.tts_cooldown)
            )


_GUILD_FIELDS = {f.name for f in fields(GuildSettings)}


class SettingsStore:
    """Load-once, write-through settings persistence."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._guilds: dict[int, GuildSettings] = {}
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Could not read settings file; starting fresh", exc_info=True)
            return
        for guild_id, data in raw.get("guilds", {}).items():
            try:
                settings = GuildSettings(
                    **{k: v for k, v in data.items() if k in _GUILD_FIELDS}
                )
            except TypeError:
                continue
            settings.validate()
            self._guilds[int(guild_id)] = settings

    async def _save(self) -> None:
        payload = {
            "guilds": {str(k): asdict(v) for k, v in self._guilds.items()},
        }
        async with self._lock:
            await asyncio.to_thread(self._write, payload)

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            log.warning("Could not persist settings", exc_info=True)

    # -- access ------------------------------------------------------------

    def get(self, guild_id: int) -> GuildSettings:
        settings = self._guilds.get(guild_id)
        if settings is None:
            settings = GuildSettings()
            self._guilds[guild_id] = settings
        return settings

    def all_items(self) -> dict[int, GuildSettings]:
        return dict(self._guilds)

    async def update(self, guild_id: int, **changes: Any) -> GuildSettings:
        settings = self.get(guild_id)
        for key, value in changes.items():
            if key not in _GUILD_FIELDS:
                raise KeyError(f"unknown setting: {key}")
            setattr(settings, key, value)
        settings.validate()
        await self._save()
        return settings
