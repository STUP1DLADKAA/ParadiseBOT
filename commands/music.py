"""Music commands. Every command delegates to MusicService and then
refreshes the interactive music panel — buttons use the same methods."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions import lyrics
from functions.helpers import MusicError, shorten
from functions.music import MusicService
from functions.music.panel import (
    QueuePaginator,
    render_now_playing,
    render_queue_page,
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

    # -- connection -------------------------------------------------------

    @app_commands.command(description="Have Paradise join your voice channel")
    @app_commands.guild_only()
    @cooldown()
    async def join(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._music().join(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/join failed")
            await interaction.followup.send(
                "that broke on my end. it's been logged.", ephemeral=True
            )
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._music().play(
                interaction.guild, interaction.user, query
            )
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/play failed")
            await interaction.followup.send(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return

        body = (
            f"**{shorten(result.track.title, 90)}**\n"
            f"{shorten(result.track.author, 80)}"
        )
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
        try:
            await self._music().pause(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/pause failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message("⏸️ paused.", ephemeral=True)
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Resume playback")
    @app_commands.guild_only()
    @cooldown()
    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            await self._music().resume(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/resume failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message("▶️ playing again.", ephemeral=True)
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Skip to the next track")
    @app_commands.guild_only()
    @cooldown()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            result = await self._music().skip(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/skip failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        if result.up_next is None:
            text = "⏭️ skipped. nothing else in the queue."
        else:
            text = f"⏭️ skipped. next: **{shorten(result.up_next.title, 60)}**"
        await interaction.response.send_message(text, ephemeral=True)
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Stop playback, clear the queue and disconnect")
    @app_commands.guild_only()
    @cooldown()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            await self._music().stop(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/stop failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⏹️ stopped, queue cleared, disconnected.", ephemeral=True
        )
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Disconnect Paradise and clear everything")
    @app_commands.guild_only()
    @cooldown()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            await self._music().stop(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/leave failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🔌 left the channel. queue cleared, speech too.", ephemeral=True
        )
        await self._music().refresh_panel(interaction.guild_id)

    # -- queue ------------------------------------------------------------

    @app_commands.command(description="Show the current queue")
    @app_commands.guild_only()
    @cooldown()
    async def queue(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        state = self._music().panel_state(interaction.guild_id)
        view = QueuePaginator(self._music(), interaction.guild_id)
        try:
            await interaction.response.send_message(
                embed=render_queue_page(state, 0), view=view
            )
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
        state = self._music().panel_state(interaction.guild_id)
        try:
            await interaction.response.send_message(embed=render_now_playing(state))
        except discord.DiscordException:
            log.debug("Now playing response failed", exc_info=True)

    @app_commands.command(description="Clear upcoming tracks, keep the current one")
    @app_commands.guild_only()
    @cooldown()
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            removed = self._music().clear_queue(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/clearqueue failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        if removed == 0:
            text = "the queue is already empty."
        else:
            text = f"cleared {removed} upcoming track{'s' if removed != 1 else ''}. the current one keeps playing."
        await interaction.response.send_message(text, ephemeral=True)
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Shuffle the upcoming queue")
    @app_commands.guild_only()
    @cooldown()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            count = self._music().shuffle_queue(interaction.guild, interaction.user)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/shuffle failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔀 shuffled {count} upcoming track{'s' if count != 1 else ''}.",
            ephemeral=True,
        )
        await self._music().refresh_panel(interaction.guild_id)

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
        try:
            value = await self._music().set_volume(
                interaction.guild, interaction.user, level
            )
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("/volume failed")
            await interaction.response.send_message(
                "that broke on my end. it's been logged.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🔊 volume set to {value}%.", ephemeral=True)
        await self._music().refresh_panel(interaction.guild_id)

    @app_commands.command(description="Lyrics for the current track")
    @app_commands.guild_only()
    @cooldown()
    async def lyrics(self, interaction: discord.Interaction) -> None:
        if not await self._ready(interaction):
            return
        await interaction.response.defer()
        state = self._music().panel_state(interaction.guild_id)
        if state.track is None:
            await interaction.followup.send(
                "nothing is playing right now, so no lyrics either.", ephemeral=True
            )
            return
        title, artist = lyrics.clean_query(state.track.title, state.track.author)
        text = await lyrics.fetch_lyrics(artist, title)
        if text is None:
            await interaction.followup.send(
                f"couldn't find lyrics for **{shorten(state.track.title, 70)}**. "
                "they might just not be out there."
            )
            return
        pages = lyrics.paginate(text)
        view = lyrics.LyricsPaginator(pages).bind(state.track.title, artist)
        embed = lyrics.render_lyrics_page(pages, 0, state.track.title, artist)
        try:
            await interaction.followup.send(embed=embed, view=view)
        except discord.DiscordException:
            log.debug("Lyrics response failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
