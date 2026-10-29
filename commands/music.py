"""Music: `/music …`, `/cmd`, `!p …`, and "Hey Paradise play …".

One execution path, four front doors
------------------------------------
Every way of asking lands in :meth:`Music.execute`, which takes a parsed
:class:`MusicIntent` and returns one short sentence that can be *shown* and
*spoken*:

======================  =====================================================
``/music play x``       slash command (needs no voice state to see)
``!p x``                text command, any channel
"Hey Paradise play x"   wake word in voice chat → answered out loud
panel buttons           ``/cmd``
======================  =====================================================

That is deliberate: a feature added to the engine shows up everywhere at
once, and the confirmation text is written to be read aloud ("playing billie
jean, three minutes thirty-seven"), so the wake-word path can speak it while
the song ducks underneath.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import MusicConfig
from functions.helpers import ParadiseError
from functions.music import (
    GuildPlayer,
    LoopMode,
    MusicError,
    MusicIntent,
    MusicService,
    PlayerState,
    Track,
    format_duration,
    is_control_only,
    parse_music_intent,
)
from functions.music.intents import (
    LEAVE,
    LOOP,
    NOWPLAYING,
    PAUSE,
    PLAY,
    QUEUE,
    RESUME,
    SHUFFLE,
    SKIP,
    STOP,
    VOLUME,
)
from functions.music.ui import help_embed, now_playing_embed, panel_embed, queue_embed

log = logging.getLogger(__name__)

#: `!p`, `!s`, `!np` … — the abbreviations people actually type.
ALIASES: dict[str, str] = {
    "p": PLAY, "play": PLAY, "pl": PLAY, "baja": PLAY, "bajao": PLAY,
    "pause": PAUSE, "ps": PAUSE, "tham": PAUSE,
    "r": RESUME, "res": RESUME, "resume": RESUME, "unpause": RESUME,
    "s": SKIP, "skip": SKIP, "next": SKIP, "n": SKIP, "agla": SKIP,
    "stop": STOP, "st": STOP, "band": STOP,
    "q": QUEUE, "queue": QUEUE, "list": QUEUE,
    "np": NOWPLAYING, "nowplaying": NOWPLAYING, "current": NOWPLAYING,
    "vol": VOLUME, "volume": VOLUME, "v": VOLUME,
    "loop": LOOP, "repeat": LOOP,
    "shuffle": SHUFFLE, "shuf": SHUFFLE, "random": SHUFFLE,
    "leave": LEAVE, "dc": LEAVE, "disconnect": LEAVE, "quit": LEAVE,
    "panel": "panel", "cmd": "panel", "controls": "panel", "m": "panel",
}


def _short(track: Track) -> str:
    return f"**{track.title}** (`{track.duration_text}`)"


class Music(commands.Cog):
    """Play music, and talk over it without stopping it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        raw = getattr(getattr(bot, "config", None), "music", None)
        self.config: MusicConfig = raw if isinstance(raw, MusicConfig) else MusicConfig()
        self.service = MusicService(bot, config=self.config)
        self.prefix = self.config.prefix or "!"
        self.panel_view = MusicPanelView(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        # Persistent view: the panel keeps working after a restart.
        try:
            self.bot.add_view(self.panel_view)
        except Exception:  # noqa: BLE001 - a non-persistent panel is not fatal
            log.debug("could not register the music panel view", exc_info=True)

    async def cog_unload(self) -> None:
        await self.service.close()

    # ------------------------------------------------------------------
    # Text commands: !p, !skip, !stop …
    # ------------------------------------------------------------------

    async def handle_message(self, message: discord.Message) -> bool:
        """True when this message was a music command."""
        if not self.config.enabled or not self.config.text_commands:
            return False
        if message.author.bot or message.guild is None:
            return False
        content = (getattr(message, "content", "") or "").strip()
        if not content:
            return False

        parsed = self._split_command(content)
        if parsed is not None:
            name, args = parsed
            await self._run_text_command(message, name, args)
            return True

        # Bare "skip" / "pause" in the voice channel's own text chat, while
        # music is playing. No wake word needed — nobody types a full sentence
        # to skip a song.
        if (
            self.service.is_playing(message.guild.id)
            and isinstance(
                getattr(message, "channel", None),
                (discord.VoiceChannel, discord.StageChannel),
            )
            and is_control_only(content)
        ):
            intent = parse_music_intent(content)
            if intent is not None:
                await self._run_text_command(message, intent.action, "")
                return True
        return False

    def _split_command(self, content: str) -> tuple[str, str] | None:
        prefix = self.prefix
        if not prefix or not content.startswith(prefix):
            return None
        body = content[len(prefix) :].strip()
        if not body:
            return None
        head, _, rest = body.partition(" ")
        name = head.lower().strip(".,!?")
        action = ALIASES.get(name)
        if action is None:
            return None
        return action, rest.strip()

    async def _run_text_command(
        self, message: discord.Message, action: str, args: str
    ) -> None:
        guild = message.guild
        member = message.author
        if action == "panel":
            await self._reply(message, embed=self._panel_embed(guild), view=self.panel_view)
            return

        intent = self._intent_for(action, args)
        if intent is None:
            return
        try:
            text = await self.execute(
                intent,
                guild=guild,
                member=member,
                channel=self._channel_for(message),
                surface="text",
            )
        except ParadiseError as error:
            await self._reply(message, content=str(error))
            return
        except Exception:  # noqa: BLE001 - never leave a command unanswered
            log.exception("music text command failed")
            await self._reply(message, content="that didn't work. try again in a moment.")
            return

        if action == QUEUE:
            player = self.service.player(guild.id)
            await self._reply(message, embed=queue_embed(player.state))
        elif action == NOWPLAYING:
            player = self.service.player(guild.id)
            await self._reply(message, embed=now_playing_embed(player.state))
        else:
            await self._reply(message, content=text)

    def _intent_for(self, action: str, args: str):
        text = (args or "").strip()
        if action == PLAY:
            return MusicIntent(action=PLAY, query=text)
        if action == VOLUME:
            value = None
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                value = max(0.0, min(2.0, int(digits) / 100.0))
            elif text.lower() in {"up", "louder", "badha"}:
                value = 0.1
            elif text.lower() in {"down", "quieter", "kam"}:
                value = -0.1
            return MusicIntent(action=VOLUME, value=value, absolute=bool(digits))
        if action == LOOP:
            return MusicIntent(action=LOOP, query=text or "cycle")
        if action == SKIP:
            digits = "".join(ch for ch in text if ch.isdigit())
            return MusicIntent(action=SKIP, value=float(digits or 1))
        return MusicIntent(action=action)

    # ------------------------------------------------------------------
    # The one execution path
    # ------------------------------------------------------------------

    async def execute(
        self,
        intent,
        *,
        guild: discord.Guild,
        member,
        channel=None,
        surface: str = "slash",
    ) -> str:
        """Run a parsed intent. Returns a sentence fit to be spoken aloud."""
        player = self.service.player(guild.id)
        action = intent.action

        if action == PLAY:
            query = (intent.query or "").strip()
            if not query:
                return (
                    f"what should i play? try `{self.prefix}p <song>` "
                    "or `/music play <song>`."
                )
            target = channel or self._member_channel(member) or getattr(
                player.voice, "channel", None
            )
            if target is None:
                raise MusicError("join a voice channel first, then ask me again.")
            was_playing = player.playing_music
            tracks = await self.service.play(
                guild,
                target,
                query,
                requester_id=getattr(member, "id", 0),
                requester_name=getattr(member, "display_name", ""),
            )
            track = tracks[0]
            if was_playing and player.current is not track:
                return f"queued {_short(track)}."
            return f"playing {_short(track)}."

        if action == PAUSE:
            if player.pause():
                return "paused."
            raise MusicError("nothing is playing right now.")
        if action == RESUME:
            if player.resume():
                return "resumed."
            raise MusicError("it isn't paused.")
        if action == SKIP:
            count = int(intent.value or 1)
            if not player.playing_music and not player.queue:
                raise MusicError("the queue is empty.")
            upcoming = await player.skip(count)
            if upcoming is None:
                return "skipped. the queue is empty now."
            return f"skipped to {_short(upcoming)}."
        if action == STOP:
            if not player.playing_music and not player.queue:
                raise MusicError("nothing is playing.")
            await player.stop()
            return "stopped, and cleared the queue."
        if action == LEAVE:
            if await player.leave():
                return "left the voice channel."
            raise MusicError("i'm not in a voice channel.")
        if action == VOLUME:
            current = player.volume
            value = intent.value
            if value is None:
                return f"volume is at {round(current * 100)}%."
            target = value if intent.absolute else current + value
            return f"volume {round(player.set_volume(target) * 100)}%."
        if action == LOOP:
            mode = self._next_loop(player.loop_mode, intent.query)
            player.set_loop(mode)
            return f"looping {mode.label}."
        if action == SHUFFLE:
            if not player.queue:
                raise MusicError("there's nothing queued to shuffle.")
            count = player.shuffle()
            return f"shuffled {count} song{'s' if count != 1 else ''}."
        if action == NOWPLAYING:
            state = player.state
            if state.current is None:
                raise MusicError("nothing is playing right now.")
            return (
                f"{state.current.title}, {format_duration(state.position)} in, "
                f"requested by {state.current.requester_label}."
            )
        if action == QUEUE:
            state = player.state
            if state.current is None and not state.upcoming:
                raise MusicError("the queue is empty.")
            return f"{len(state.upcoming)} song(s) queued."
        raise MusicError("i didn't understand that one.")

    @staticmethod
    def _next_loop(current: LoopMode, requested: str | None) -> LoopMode:
        text = (requested or "cycle").strip().lower()
        if text in {"cycle", ""}:
            order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
            index = order.index(current) if current in order else 0
            return order[(index + 1) % len(order)]
        return LoopMode.parse(text)

    # ------------------------------------------------------------------
    # Wake-word bridge (called from commands/wake.py)
    # ------------------------------------------------------------------

    async def handle_voice_request(self, message: discord.Message, text: str) -> str | None:
        """Handle a *typed* music request after the wake word.

        Returns the sentence to speak, or ``None`` when this was not a music
        request at all (so the assistant answers it instead).
        """
        return await self.handle_spoken_request(
            guild=message.guild,
            member=message.author,
            channel=self._channel_for(message),
            text=text,
        )

    async def handle_spoken_request(
        self, *, guild, member, channel, text: str
    ) -> str | None:
        """Same thing for a request that was *heard*, then transcribed.

        The caller supplies the pieces the spoken path already knows (guild,
        member, the channel the bot is in), so this works identically for
        "hey paradise play x" typed, spoken, or coming from a panel button.
        """
        if not self.config.enabled:
            return None
        intent = parse_music_intent(text)
        if intent is None:
            return None
        try:
            return await self.execute(
                intent, guild=guild, member=member, channel=channel, surface="voice"
            )
        except ParadiseError as error:
            return str(error)
        except Exception:  # noqa: BLE001 - the voice path must always answer
            log.exception("music voice request failed")
            return "that didn't work. try again in a moment."

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _member_channel(self, member):
        return getattr(getattr(member, "voice", None), "channel", None)

    def _channel_for(self, message: discord.Message):
        channel = getattr(message, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return self._member_channel(message.author)

    def _state(self, guild: discord.Guild) -> PlayerState:
        return self.service.player(guild.id).state

    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        return panel_embed(self._state(guild), prefix=self.prefix)

    async def _reply(
        self,
        message: discord.Message,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        try:
            await message.reply(
                content=content,
                embed=embed,
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            log.debug("could not reply to a music command", exc_info=True)

    # ------------------------------------------------------------------
    # Panel buttons
    # ------------------------------------------------------------------

    async def button_action(self, interaction: discord.Interaction, action: str) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("servers only.", ephemeral=True)
            return
        player = self.service.player(guild.id)

        if action == "queue":
            await interaction.response.send_message(
                embed=queue_embed(player.state), ephemeral=True
            )
            return

        member = interaction.user
        member_channel = self._member_channel(member)
        bot_channel = getattr(player.voice, "channel", None)
        if bot_channel is not None and member_channel is None:
            await interaction.response.send_message(
                "join the voice channel first, then use the panel.", ephemeral=True
            )
            return

        try:
            note = await self._panel_action(player, action)
        except ParadiseError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        try:
            await interaction.response.edit_message(
                embed=self._panel_embed(guild), view=self.panel_view
            )
        except discord.DiscordException:
            log.debug("could not refresh the music panel", exc_info=True)
        if note:
            try:
                await interaction.followup.send(note, ephemeral=True)
            except discord.DiscordException:
                pass

    async def _panel_action(self, player: GuildPlayer, action: str) -> str:
        if action == "toggle":
            if player.paused:
                return "resumed." if player.resume() else "nothing is playing."
            return "paused." if player.pause() else "nothing is playing."
        if action == "skip":
            upcoming = await player.skip(1)
            return f"skipped to {upcoming.title}." if upcoming else "queue is empty."
        if action == "stop":
            await player.stop()
            return "stopped."
        if action == "vol_down":
            return f"volume {round(player.set_volume(player.volume - 0.1) * 100)}%."
        if action == "vol_up":
            return f"volume {round(player.set_volume(player.volume + 0.1) * 100)}%."
        if action == "loop":
            mode = self._next_loop(player.loop_mode, "cycle")
            player.set_loop(mode)
            return f"looping {mode.label}."
        return ""

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    group = app_commands.Group(
        name="music", description="Play music in your voice channel"
    )

    @group.command(name="play", description="Search for a song and play it (or queue it)")
    @app_commands.describe(query="Song name, artist, or a YouTube/SoundCloud link")
    async def music_play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        await self._slash(interaction, PLAY, text=query)

    @group.command(name="pause", description="Pause the music")
    async def music_pause(self, interaction: discord.Interaction) -> None:
        await self._slash(interaction, PAUSE)

    @group.command(name="resume", description="Resume the music")
    async def music_resume(self, interaction: discord.Interaction) -> None:
        await self._slash(interaction, RESUME)

    @group.command(name="skip", description="Skip to the next song")
    @app_commands.describe(count="How many songs to skip")
    async def music_skip(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 20] = 1
    ) -> None:
        await self._slash(interaction, SKIP, count=count)

    @group.command(name="stop", description="Stop the music and clear the queue")
    async def music_stop(self, interaction: discord.Interaction) -> None:
        await self._slash(interaction, STOP)

    @group.command(name="queue", description="Show what is queued")
    async def music_queue(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("servers only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=queue_embed(self._state(interaction.guild)))

    @group.command(name="nowplaying", description="Show the song that is playing")
    async def music_np(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("servers only.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=now_playing_embed(self._state(interaction.guild))
        )

    @group.command(name="volume", description="Set the music volume (0–200 %)")
    @app_commands.describe(level="0–200; leave it out to just read the current level")
    async def music_volume(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200] | None = None
    ) -> None:
        await self._slash(
            interaction,
            VOLUME,
            value=None if level is None else level / 100.0,
            absolute=True,
        )

    @group.command(name="loop", description="Loop off / this song / the whole queue")
    @app_commands.describe(mode="off, track or queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="this song", value="track"),
            app_commands.Choice(name="the queue", value="queue"),
        ]
    )
    async def music_loop(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None
    ) -> None:
        await self._slash(interaction, LOOP, text=mode.value if mode else "cycle")

    @group.command(name="shuffle", description="Shuffle the queue")
    async def music_shuffle(self, interaction: discord.Interaction) -> None:
        await self._slash(interaction, SHUFFLE)

    @group.command(name="leave", description="Leave the voice channel")
    async def music_leave(self, interaction: discord.Interaction) -> None:
        await self._slash(interaction, LEAVE)

    @group.command(name="panel", description="Show the button panel")
    async def music_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("servers only.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=self._panel_embed(interaction.guild), view=self.panel_view
        )

    @app_commands.command(name="cmd", description="Music control panel and command list")
    async def cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=help_embed(self.prefix), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=self._panel_embed(interaction.guild),
            view=self.panel_view,
        )

    async def _slash(self, interaction: discord.Interaction, action: str, **kwargs) -> None:
        if interaction.guild is None:
            await interaction.followup.send("servers only.", ephemeral=True)
            return

        intent = MusicIntent(
            action=action,
            query=kwargs.get("text", "") or "",
            value=kwargs.get("value", kwargs.get("count")),
            absolute=kwargs.get("absolute", False),
        )
        try:
            text = await self.execute(
                intent,
                guild=interaction.guild,
                member=interaction.user,
                channel=self._member_channel(interaction.user),
                surface="slash",
            )
        except ParadiseError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("music slash command failed")
            await interaction.followup.send(
                "that didn't work. try again in a moment.", ephemeral=True
            )
            return

        await interaction.followup.send(text)
        if self.config.auto_panel and action == PLAY:
            try:
                await interaction.channel.send(
                    embed=self._panel_embed(interaction.guild), view=self.panel_view
                )
            except discord.DiscordException:
                log.debug("could not post the music panel", exc_info=True)


class _PanelButton(discord.ui.Button):
    """One panel button; the action name is what the cog understands."""

    def __init__(self, *, label: str, action: str, style=discord.ButtonStyle.secondary,
                 emoji: str | None = None, row: int = 0) -> None:
        super().__init__(
            label=label,
            style=style,
            emoji=emoji,
            row=row,
            custom_id=f"paradise:music:{action}",
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog.button_action(interaction, self.action)  # type: ignore[attr-defined]


class MusicPanelView(discord.ui.View):
    """Persistent transport controls: play/pause, skip, stop, volume, loop."""

    def __init__(self, cog: Music) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(_PanelButton(label="Pause/Resume", action="toggle", emoji="⏯", row=0))
        self.add_item(_PanelButton(label="Skip", action="skip", emoji="⏭", row=0))
        self.add_item(_PanelButton(label="Stop", action="stop", emoji="⏹", style=discord.ButtonStyle.danger, row=0))
        self.add_item(_PanelButton(label="Vol -", action="vol_down", emoji="🔉", row=0))
        self.add_item(_PanelButton(label="Vol +", action="vol_up", emoji="🔊", row=0))
        self.add_item(_PanelButton(label="Queue", action="queue", emoji="📜", row=1))
        self.add_item(_PanelButton(label="Loop", action="loop", emoji="🔁", row=1))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
