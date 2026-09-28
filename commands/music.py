"""Music commands. Every command delegates to MusicService and then
refreshes the interactive music panel — buttons use the same methods."""

from __future__ import annotations

import logging
from typing import Callable, Coroutine, Any

import discord
from discord import app_commands
from discord.ext import commands

from functions import lyrics
from functions.helpers import MusicError, parse_duration, shorten
from functions.music import MusicService
from functions.music.panel import (
    MusicPanelView,
    QueuePaginator,
    TrackInfo,
)

log = logging.getLogger(__name__)


def cooldown():
    return app_commands.checks.cooldown(1, 3, key=lambda i: (i.guild_id, i.user.id))


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music: MusicService = bot.music

    # -- helpers ----------------------------------------------------------

    async def _ready(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "music only works in a server.", ephemeral=True
            )
            return False
        return True

    def _music(self) -> MusicService:
        return self.bot.music

    async def _run(
        self,
        interaction: discord.Interaction,
        action: Callable[[], Coroutine[Any, Any, Any]],
        *,
        refresh: bool = True,
    ) -> Any:
        """Defer, run one MusicService action, show friendly errors.

        The single error path for every music command: MusicError messages are
        user-facing by design; anything else is logged and reduced to a short
        note. The panel is refreshed only on success.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await action()
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return None
        except Exception:
            log.exception(
                "/%s failed",
                interaction.command.qualified_name if interaction.command else "music",
            )
            await interaction.followup.send(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return None
        if refresh:
            await self._music().refresh_panel(interaction.guild_id)
        return result

    @staticmethod
    def _track_line(track: TrackInfo) -> str:
        return f"**{shorten(track.title, 90)}**\n{shorten(track.author, 80)}"

    # -- connection -------------------------------------------------------

    @app_commands.command(description="Have Paradise join your voice channel")
    @app_commands.guild_only()
    @cooldown()
    async def join(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().join(interaction.guild, interaction.user),
            refresh=False,
        )
        if result is None:
            return
        if result.action == "moved":
            note = f"moved to **{result.channel_name}**."
        elif result.action == "already":
            note = f"already in **{result.channel_name}**."
        else:
            note = (
                f"joined **{result.channel_name}**. I'll hang out here until "
                "someone uses /stop or /leave."
            )
        await interaction.followup.send(note, ephemeral=True)
        await self._music().refresh_panel(
            interaction.guild_id, fallback=interaction.channel
        )

    @app_commands.command(description="Play a song or URL (opens the music panel)")
    @app_commands.guild_only()
    @app_commands.describe(query="Song name or URL")
    @cooldown()
    async def play(
        self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().play(interaction.guild, interaction.user, query),
            refresh=False,
        )
        if result is None:
            return
        body = self._track_line(result.track)
        if result.started:
            text = f"🎵 Now playing\n\n{body}"
        else:
            text = f"🎵 Added to queue\n\n{body}\n\nPosition #{result.position}"
        await interaction.followup.send(text, ephemeral=True)
        await self._music().refresh_panel(
            interaction.guild_id, fallback=interaction.channel
        )

    # -- playback controls ------------------------------------------------

    @app_commands.command(description="Pause the current track")
    @app_commands.guild_only()
    @cooldown()
    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction, lambda: self._music().pause(interaction.guild, interaction.user)
        )
        if result is not None:
            await interaction.followup.send("⏸️ paused.", ephemeral=True)

    @app_commands.command(description="Resume playback")
    @app_commands.guild_only()
    @cooldown()
    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction, lambda: self._music().resume(interaction.guild, interaction.user)
        )
        if result is not None:
            await interaction.followup.send("▶️ playing again.", ephemeral=True)

    @app_commands.command(description="Skip to the next track")
    @app_commands.guild_only()
    @cooldown()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction, lambda: self._music().skip(interaction.guild, interaction.user)
        )
        if result is None:
            return
        if result.up_next is None:
            text = "⏭️ skipped. nothing else in the queue."
        else:
            text = f"⏭️ skipped. next: **{shorten(result.up_next.title, 60)}**"
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(description="Go back to the previous track")
    @app_commands.guild_only()
    @cooldown()
    async def previous(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().previous(interaction.guild, interaction.user),
        )
        if result is not None:
            await interaction.followup.send(
                f"⏮️ back to **{shorten(result.track.title, 70)}**.", ephemeral=True
            )

    @app_commands.command(description="Stop playback, clear the queue and disconnect")
    @app_commands.guild_only()
    @cooldown()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction, lambda: self._music().stop(interaction.guild, interaction.user)
        )
        if result is not None:
            await interaction.followup.send(
                "⏹️ stopped, queue cleared, disconnected.", ephemeral=True
            )

    @app_commands.command(description="Disconnect Paradise and clear everything")
    @app_commands.guild_only()
    @cooldown()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction, lambda: self._music().stop(interaction.guild, interaction.user)
        )
        if result is not None:
            await interaction.followup.send(
                "🔌 left the channel. queue cleared, speech too.", ephemeral=True
            )

    # -- playback position --------------------------------------------------

    @app_commands.command(description="Jump to a position in the current track")
    @app_commands.guild_only()
    @app_commands.describe(position="Position like 1:32, 1:02:03 or seconds like 92")
    @cooldown()
    async def seek(
        self, interaction: discord.Interaction, position: app_commands.Range[str, 1, 12]
    ) -> None:
        if not await self._ready(interaction):
            return
        position_ms = parse_duration(position)
        if position_ms is None:
            await interaction.response.send_message(
                "I couldn't read that position — try `1:32`, `1:02:03` or plain seconds.",
                ephemeral=True,
            )
            return
        result = await self._run(
            interaction,
            lambda: self._music().seek(interaction.guild, interaction.user, position_ms),
        )
        if result is not None:
            minutes, seconds = divmod(result // 1000, 60)
            await interaction.followup.send(
                f"⏱️ jumped to **{minutes}:{seconds:02d}**.", ephemeral=True
            )

    @app_commands.command(description="Jump forward in the current track")
    @app_commands.guild_only()
    @app_commands.describe(seconds="How many seconds to jump forward")
    @cooldown()
    async def forward(
        self, interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 3600]
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().nudge(interaction.guild, interaction.user, seconds * 1000),
        )
        if result is not None:
            minutes, secs = divmod(result // 1000, 60)
            await interaction.followup.send(
                f"⏩ now at **{minutes}:{secs:02d}**.", ephemeral=True
            )

    @app_commands.command(description="Jump backward in the current track")
    @app_commands.guild_only()
    @app_commands.describe(seconds="How many seconds to jump back")
    @cooldown()
    async def backward(
        self, interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 3600]
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().nudge(interaction.guild, interaction.user, -seconds * 1000),
        )
        if result is not None:
            minutes, secs = divmod(result // 1000, 60)
            await interaction.followup.send(
                f"⏪ now at **{minutes}:{secs:02d}**.", ephemeral=True
            )

    # -- loop / autoplay ------------------------------------------------------

    @app_commands.command(description="Loop off, this track, or the whole queue")
    @app_commands.guild_only()
    @app_commands.describe(mode="Leave empty to cycle: off → track → queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="track", value="track"),
            app_commands.Choice(name="queue", value="queue"),
        ],
    )
    @cooldown()
    async def loop(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._ready(interaction):
            return
        chosen = mode.value if mode is not None else None
        result = await self._run(
            interaction,
            lambda: self._music().set_loop(interaction.guild, interaction.user, chosen),
        )
        if result is None:
            return
        labels = {
            "off": "🔁 loop off.",
            "track": "🔁 looping this track.",
            "queue": "🔁 looping the whole queue.",
        }
        await interaction.followup.send(labels.get(result, f"🔁 loop: {result}."), ephemeral=True)

    @app_commands.command(description="Keep related music playing when the queue ends")
    @app_commands.guild_only()
    @app_commands.describe(state="Leave empty to toggle")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ],
    )
    @cooldown()
    async def autoplay(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._ready(interaction):
            return
        enabled = state is None or state.value == "on"
        result = await self._run(
            interaction,
            lambda: self._music().set_autoplay(interaction.guild, interaction.user, enabled),
        )
        if result is None:
            return
        if result:
            await interaction.followup.send(
                "♾️ autoplay on — I'll keep the music going when the queue ends.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("♾️ autoplay off.", ephemeral=True)

    # -- queue ------------------------------------------------------------

    @app_commands.command(description="Show the current queue")
    @app_commands.guild_only()
    @cooldown()
    async def queue(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        view = QueuePaginator(self._music(), interaction.guild_id)
        try:
            await interaction.response.send_message(view=view)
        except discord.DiscordException:
            log.debug("Queue response failed", exc_info=True)
            await interaction.response.send_message(
                "I couldn't post the queue here — permissions, probably.", ephemeral=True
            )

    @app_commands.command(description="Show what's currently playing")
    @app_commands.guild_only()
    @cooldown()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        # Same Components V2 card the persistent panel uses — an ephemeral
        # snapshot with working controls, not a second persistent panel.
        view = MusicPanelView(self._music(), interaction.guild_id)
        view.timeout = 900  # snapshots expire; only the real panel persists
        try:
            await interaction.response.send_message(view=view, ephemeral=True)
        except discord.DiscordException:
            log.debug("Now playing response failed", exc_info=True)

    @app_commands.command(description="Remove one track from the queue")
    @app_commands.guild_only()
    @app_commands.describe(position="Queue position to remove (1 = next up)")
    @cooldown()
    async def remove(
        self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 999]
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().remove_track(
                interaction.guild, interaction.user, position
            ),
        )
        if result is not None:
            await interaction.followup.send(
                f"❌ removed **{shorten(result.title, 70)}** (was #{position}).",
                ephemeral=True,
            )

    @app_commands.command(description="Move a track to a different queue position")
    @app_commands.guild_only()
    @app_commands.describe(position="Queue position to move (1 = next up)", to="New position")
    @cooldown()
    async def move(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1, 999],
        to: app_commands.Range[int, 1, 999],
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().move_track(
                interaction.guild, interaction.user, position, to
            ),
        )
        if result is not None:
            await interaction.followup.send(
                f"↕️ **{shorten(result.title, 60)}** moved to position {to}.",
                ephemeral=True,
            )

    @app_commands.command(description="Clear upcoming tracks, keep the current one")
    @app_commands.guild_only()
    @cooldown()
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().clear_queue(interaction.guild, interaction.user),
        )
        if result is None:
            return
        if result == 0:
            text = "the queue is already empty."
        else:
            text = f"cleared {result} upcoming track{'s' if result != 1 else ''}. the current one keeps playing."
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(description="Shuffle the upcoming queue")
    @app_commands.guild_only()
    @cooldown()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().shuffle_queue(interaction.guild, interaction.user),
        )
        if result is not None:
            await interaction.followup.send(
                f"🔀 shuffled {result} upcoming track{'s' if result != 1 else ''}.",
                ephemeral=True,
            )

    # -- effects ------------------------------------------------------------

    @app_commands.command(description="Apply an audio effect (off clears them)")
    @app_commands.guild_only()
    @app_commands.describe(effect="Effect to toggle — 'off' clears everything")
    @app_commands.choices(
        effect=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="bass boost", value="bassboost"),
            app_commands.Choice(name="nightcore", value="nightcore"),
            app_commands.Choice(name="daycore", value="daycore"),
            app_commands.Choice(name="karaoke", value="karaoke"),
            app_commands.Choice(name="tremolo", value="tremolo"),
            app_commands.Choice(name="vibrato", value="vibrato"),
            app_commands.Choice(name="rotation", value="rotation"),
        ],
    )
    @cooldown()
    async def filter(
        self, interaction: discord.Interaction, effect: app_commands.Choice[str]
    ) -> None:
        if not await self._ready(interaction):
            return
        if effect.value == "off":
            result = await self._run(
                interaction,
                lambda: self._music().clear_filters(interaction.guild, interaction.user),
            )
            if result is not None:
                if result:
                    await interaction.followup.send(
                        f"🎚️ cleared {result} effect{'s' if result != 1 else ''}.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send("no effects were on.", ephemeral=True)
            return
        result = await self._run(
            interaction,
            lambda: self._music().set_filter(
                interaction.guild, interaction.user, effect.value
            ),
        )
        if result is None:
            return
        label = effect.name
        if result == "removed":
            await interaction.followup.send(f"🎚️ {label} off.", ephemeral=True)
        else:
            await interaction.followup.send(f"🎚️ {label} on.", ephemeral=True)

    # -- settings-adjacent ------------------------------------------------

    @app_commands.command(description="Set the playback volume (0–150)")
    @app_commands.guild_only()
    @app_commands.describe(level="New volume level")
    @cooldown()
    async def volume(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 150],
    ) -> None:
        if not await self._ready(interaction):
            return
        result = await self._run(
            interaction,
            lambda: self._music().set_volume(interaction.guild, interaction.user, level),
        )
        if result is not None:
            await interaction.followup.send(f"🔊 volume set to {result}%.", ephemeral=True)

    @app_commands.command(description="Lyrics for the current track")
    @app_commands.guild_only()
    @cooldown()
    async def lyrics(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        await interaction.response.defer()
        try:
            track, result = await self._music().lyrics_for_guild(interaction.guild_id)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/lyrics failed")
            await interaction.followup.send("lyrics are unavailable right now.", ephemeral=True)
            return
        if result is None or not result.text:
            await interaction.followup.send(
                f"couldn't find lyrics for **{shorten(track.title, 70)}**. "
                "they may not be available from any provider."
            )
            return
        pages = lyrics.paginate(result.text)
        view = lyrics.LyricsPaginator(pages).bind(track.title, track.author, result.source)
        try:
            await interaction.followup.send(view=view)
            await self._music().refresh_panel(interaction.guild_id)
        except discord.DiscordException:
            log.debug("Lyrics response failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
