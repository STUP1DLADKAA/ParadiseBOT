"""System commands: help, status, settings."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from functions.helpers import Colour
from functions.settings import GuildSettings, TTS_COOLDOWN_CHOICES

log = logging.getLogger(__name__)

HELP_BLURBS = {
    "ping": "am I alive (with the real latency)",
    "8ball": "questionable predictions",
    "coinflip": "a very serious coin toss",
    "choose": "decisions, outsourced",
    "join": "pull me into your voice channel",
    "play": "play a song or URL",
    "pause": "pause the current track",
    "resume": "keep playing",
    "skip": "next track",
    "stop": "stop, clear the queue, disconnect",
    "queue": "see the queue",
    "nowplaying": "see what's playing",
    "volume": "0–150, keep it kind",
    "clearqueue": "drop upcoming tracks, keep the current one",
    "shuffle": "mix up what's coming",
    "leave": "disconnect me entirely",
    "lyrics": "lyrics for the current track",
    "remind": "nudge you later in this channel",
    "poll": "reaction poll",
    "afk": "temporary AFK status",
    "translate": "translate a short text",
    "define": "what does that word mean",
    "status": "my current state of affairs",
    "settings": "small server settings (Manage Server)",
    "say": "speak a short message in voice",
    "summarize": "recap recent chat in this channel",
}

HELP_GROUPS = {
    "🎲 fun": ("ping", "8ball", "coinflip", "choose"),
    "🎵 music": (
        "join", "play", "pause", "resume", "skip", "stop",
        "queue", "nowplaying", "volume", "clearqueue", "shuffle", "leave", "lyrics",
    ),
    "🧰 useful": (
        "remind", "poll", "afk", "translate", "define", "status", "settings",
    ),
    "🔊 voice": ("say",),
    "🤖 ai": ("summarize",),
}


def cooldown():
    return app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))


def _format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _settings_embed(guild: discord.Guild, settings: GuildSettings, admin: bool) -> discord.Embed:
    embed = discord.Embed(title="⚙️ Paradise settings", colour=Colour.ACCENT)
    embed.add_field(
        name="TTS",
        value="enabled" if settings.tts_enabled else "disabled",
        inline=True,
    )
    embed.add_field(
        name="Auto-disconnect",
        value=(
            "on — I leave empty channels during music sessions"
            if settings.auto_disconnect
            else "off — I stay until stopped"
        ),
        inline=True,
    )
    embed.add_field(
        name="TTS cooldown", value=f"{settings.tts_cooldown}s per person", inline=True
    )
    panel_channel = (
        guild.get_channel(settings.panel_channel_id) if settings.panel_channel_id else None
    )
    embed.add_field(
        name="Music panel channel",
        value=panel_channel.mention if panel_channel else "wherever music is used",
        inline=True,
    )
    note = (
        "`/join` always keeps me in the channel until someone stops me — "
        "auto-disconnect only applies to sessions started by `/play`."
    )
    embed.description = (
        f"-# {note}\n-# only implemented settings are shown. "
        + ("use the menus below." if admin else "ask an admin to change these.")
    )
    return embed


class System(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- help ---------------------------------------------------------------

    @app_commands.command(description="What Paradise can actually do")
    @cooldown()
    async def help(self, interaction: discord.Interaction) -> None:
        registered = {command.name for command in self.bot.tree.get_commands()}
        embed = discord.Embed(
            title="🌴 Paradise",
            description=(
                "a small bot for a small server.\n"
                "mention me any time to talk — I answer when it makes sense."
            ),
            colour=Colour.ACCENT,
        )
        for group, names in HELP_GROUPS.items():
            lines = []
            for name in names:
                if name not in registered:
                    continue  # never advertise unregistered commands
                lines.append(f"`/{name}` — {HELP_BLURBS.get(name, '')}")
            if not lines:
                continue
            value = "\n".join(lines)
            if group == "🎵 music":
                value = (
                    "every music command opens or updates the interactive music panel "
                    "— its buttons do everything the commands do.\n" + value
                )
            if group == "🤖 ai":
                value += "\ntalk to me naturally by mentioning me."
            embed.add_field(name=group, value=value, inline=False)
        embed.set_footer(text="quiet by design · /status for a health check")
        await interaction.response.send_message(embed=embed)

    # -- status -------------------------------------------------------------

    @app_commands.command(description="Paradise's current state of affairs")
    @cooldown()
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        music = bot.music
        state = (
            music.panel_state(interaction.guild_id) if interaction.guild_id else None
        )

        latency_ms = round(bot.latency * 1000) if bot.latency == bot.latency else None
        embed = discord.Embed(title="🌴 Paradise", colour=Colour.ACCENT)
        embed.add_field(
            name="Uptime",
            value=_format_uptime((discord.utils.utcnow() - bot.started_at).total_seconds()),
            inline=True,
        )
        embed.add_field(
            name="Latency",
            value=f"{latency_ms}ms" if latency_ms is not None else "measuring…",
            inline=True,
        )
        embed.add_field(
            name="Voice",
            value=state.voice_channel if state and state.voice_channel else "—",
            inline=True,
        )
        track = "—"
        if state and state.track:
            label = state.track.title
            track = f"{label[:45]}{'…' if len(label) > 45 else ''} ({state.status})"
        embed.add_field(name="Now playing", value=track, inline=True)
        embed.add_field(
            name="Queue",
            value=str(len(state.upcoming)) if state else "0",
            inline=True,
        )

        lavalink = music.lavalink_status()
        lavalink_label = {
            "connected": "connected",
            "down": "down",
            "off": "not configured",
        }
        embed.add_field(
            name="Lavalink", value=lavalink_label.get(lavalink, lavalink), inline=True
        )
        embed.add_field(name="TTS", value=bot.tts.status().split(" (")[0], inline=True)
        embed.add_field(
            name="AI", value="ready" if bot.ai.available else "not configured", inline=True
        )
        embed.set_footer(text="all quiet on the paradise front")
        await interaction.response.send_message(embed=embed)

    # -- settings -------------------------------------------------------------

    @app_commands.command(description="Small server settings (needs Manage Server)")
    @app_commands.guild_only()
    @cooldown()
    async def settings(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "settings only make sense in a server.", ephemeral=True
            )
            return
        settings = self.bot.settings_store.get(guild.id)
        admin = member.guild_permissions.manage_guild
        embed = _settings_embed(guild, settings, admin)
        if not admin:
            await interaction.response.send_message(
                embed=embed,
                content="you need **Manage Server** to change these.",
            )
            return
        await interaction.response.send_message(embed=embed, view=SettingsView(self.bot, guild.id))


class SettingsView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        settings = bot.settings_store.get(guild_id)
        for option in self.tts_select.options:
            option.default = (option.value == "on") == settings.tts_enabled
        for option in self.autodc_select.options:
            option.default = (option.value == "on") == settings.auto_disconnect
        for option in self.cooldown_select.options:
            option.default = int(option.value) == settings.tts_cooldown

    async def _guard(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "you need **Manage Server** for that.", ephemeral=True
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return
        settings = self.bot.settings_store.get(self.guild_id)
        await interaction.response.edit_message(
            embed=_settings_embed(guild, settings, True), view=self
        )

    @discord.ui.select(
        placeholder="TTS",
        options=[
            discord.SelectOption(label="Enabled", value="on", emoji="🔊"),
            discord.SelectOption(label="Disabled", value="off", emoji="🔇"),
        ],
        row=0,
    )
    async def tts_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._guard(interaction):
            return
        await self.bot.settings_store.update(
            self.guild_id, tts_enabled=select.values[0] == "on"
        )
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="Auto-disconnect",
        options=[
            discord.SelectOption(label="On (music sessions)", value="on", emoji="🚪"),
            discord.SelectOption(label="Off (stay until stopped)", value="off", emoji="📌"),
        ],
        row=1,
    )
    async def autodc_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._guard(interaction):
            return
        await self.bot.settings_store.update(
            self.guild_id, auto_disconnect=select.values[0] == "on"
        )
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="TTS cooldown",
        options=[
            discord.SelectOption(label=f"{seconds}s", value=str(seconds))
            for seconds in TTS_COOLDOWN_CHOICES
        ],
        row=2,
    )
    async def cooldown_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._guard(interaction):
            return
        value = int(select.values[0])
        if value not in TTS_COOLDOWN_CHOICES:
            await interaction.response.send_message(
                "that isn't a valid cooldown.", ephemeral=True
            )
            return
        await self.bot.settings_store.update(self.guild_id, tts_cooldown=value)
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Music panel channel",
        channel_types=[discord.ChannelType.text],
        row=3,
    )
    async def panel_channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        if not await self._guard(interaction):
            return
        channel = select.values[0]
        guild = self.bot.get_guild(self.guild_id)
        if guild is None or channel is None:
            return
        moved = await self.bot.music.move_panel(self.guild_id, channel)
        if not moved:
            await interaction.response.send_message(
                "I can't post the panel there — check my permissions.", ephemeral=True
            )
            return
        await self._refresh(interaction)

    @discord.ui.button(
        label="Reset panel channel", emoji="↩️", style=discord.ButtonStyle.secondary, row=4
    )
    async def reset_panel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        music = self.bot.music
        settings = self.bot.settings_store.get(self.guild_id)
        guild = self.bot.get_guild(self.guild_id)
        if settings.panel_channel_id and settings.panel_message_id and guild:
            channel = guild.get_channel(settings.panel_channel_id)
            if channel is not None:
                try:
                    message = await channel.fetch_message(settings.panel_message_id)
                    await message.delete()
                except discord.DiscordException:
                    pass
        music._panel_messages.pop(self.guild_id, None)
        await self.bot.settings_store.update(
            self.guild_id, panel_channel_id=None, panel_message_id=None
        )
        await self._refresh(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
