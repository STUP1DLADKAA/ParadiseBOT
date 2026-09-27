"""The discord.py <-> Lavalink voice bridge.

With Lavalink v4 the bot itself never opens a voice websocket; it forwards
``VOICE_STATE_UPDATE`` / ``VOICE_SERVER_UPDATE`` payloads to the node, which
joins voice on the bot's behalf. This protocol class is the standard bridge
(see the Lavalink.py discord.py example).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from lavalink import Client

log = logging.getLogger(__name__)


class LavalinkVoiceClient(discord.VoiceProtocol):
    """Forwards Discord voice events to the Lavalink client."""

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
        self.client = client
        self.channel = channel
        self.guild_id = channel.guild.id
        self._destroyed = False

        self.lavalink: Client = getattr(client, "lavalink", None)  # type: ignore[assignment]
        if self.lavalink is None:
            raise RuntimeError(
                "Lavalink client is not initialised; MusicService.start() must run first."
            )

    async def on_voice_server_update(self, data: dict) -> None:
        await self.lavalink.voice_update_handler(
            {"t": "VOICE_SERVER_UPDATE", "d": data}
        )

    async def on_voice_state_update(self, data: dict) -> None:
        channel_id = data.get("channel_id")
        if not channel_id:
            await self._destroy()
            return

        channel = self.client.get_channel(int(channel_id))
        if channel is not None:
            self.channel = channel

        await self.lavalink.voice_update_handler(
            {"t": "VOICE_STATE_UPDATE", "d": data}
        )

    async def connect(
        self,
        *,
        timeout: float,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        """Connect the bot to voice and make sure a player exists."""
        self.lavalink.player_manager.create(guild_id=self.channel.guild.id)
        await self.channel.guild.change_voice_state(
            channel=self.channel, self_mute=self_mute, self_deaf=self_deaf
        )

    async def move_to(self, channel: discord.abc.Connectable) -> None:
        """Move to another channel in the same guild."""
        self.channel = channel
        await channel.guild.change_voice_state(channel=channel, self_deaf=True)

    async def disconnect(self, *, force: bool = False) -> None:
        """Leave voice and clean up the player."""
        player = self.lavalink.player_manager.get(self.guild_id)

        if not force and (player is None or not player.is_connected):
            return

        # None disconnects the bot from voice.
        try:
            await self.channel.guild.change_voice_state(channel=None)
        except discord.DiscordException:
            log.debug("Voice state change on disconnect failed", exc_info=True)

        if player is not None:
            player.channel_id = None
        await self._destroy()

    async def _destroy(self) -> None:
        self.cleanup()

        if self._destroyed:
            return
        self._destroyed = True

        try:
            await self.lavalink.player_manager.destroy(self.guild_id)
        except Exception:  # player may already be gone
            log.debug("Player destroy on voice cleanup failed", exc_info=True)
