"""Paradise: a small Discord bot that knows when to be quiet."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, load_config
from functions.ai import FALLBACK_UNAVAILABLE, AIService
from functions.helpers import TTSError
from functions.music import MusicService
from functions.personality import Personality
from functions.presence import PresenceManager
from functions.settings import SettingsStore
from functions.tts import TTSService

log = logging.getLogger("paradise")

EXTENSIONS = (
    "commands.fun",
    "commands.utility",
    "commands.music",
    "commands.voice",
    "commands.ai",
    "commands.system",
)

# "Paradise say hello everyone" without a mention (only while in voice together)
_PLAIN_TTS_RE = re.compile(
    r"^\s*paradise[\s,:!\-–—]*(?:say|speak|bolo|बोलो)\s+(.+)$", re.IGNORECASE
)
# "@Paradise say hello everyone"
_MENTION_TTS_RE = re.compile(
    r"^\s*[,.!:\s]*(?:say|speak|bolo|बोलो)\s+(.+)$", re.IGNORECASE
)

_FALLBACK_NOTE_COOLDOWN = 15 * 60
_TTS_NOTE_COOLDOWN = 60


class ParadiseTree(app_commands.CommandTree):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = f"give it {error.retry_after:.1f}s. I'm only one little guy."
        elif isinstance(
            error, (app_commands.TransformerError, app_commands.RangeError)
        ):
            text = "that input doesn't quite work. check the command hints."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "that one only works in a server."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "you need more permissions for that one."
        else:
            text = "well that broke. give it a minute."
            log.error("Unhandled command error: %s", error, exc_info=error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.DiscordException:
            log.warning("Could not send command error response", exc_info=True)


class ParadiseBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=ParadiseTree,
        )
        self.config = config
        self.started_at = discord.utils.utcnow()
        self.settings_store = SettingsStore(
            os.path.join(config.data_dir, "settings.json")
        )
        self.personality = Personality(self)
        self.presence_manager = PresenceManager(self)
        self.ai = AIService(self, config)
        self.music = MusicService(self, config, self.settings_store)
        self.tts = TTSService(self, config, self.music, self.settings_store)
        self.music.tts = self.tts
        self.afk_states: dict = {}
        self._afk_notice_last: dict[tuple[int, int], float] = {}
        self._tts_note_last: dict[int, float] = {}
        self._ai_fallback_last: dict[int, float] = {}
        self._ready_logged = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        self.prune_disabled_commands()
        await self.music.start()

        try:
            synced = await self.tree.sync()
            names = sorted(command.name for command in synced)
            log.info("Synced %d slash commands: %s", len(synced), ", ".join(names))
        except discord.DiscordException:
            # Existing registered commands still work; a sync failure is not fatal.
            log.warning("Could not sync slash commands", exc_info=True)

        self.presence_manager.start()

    def prune_disabled_commands(self) -> None:
        """Optional integrations are removed from the tree entirely when off,
        so /help never advertises something that isn't registered."""
        if not self.config.lyrics_enabled and self.tree.get_command("lyrics"):
            self.tree.remove_command("lyrics")

    async def close(self) -> None:
        try:
            await self.presence_manager.stop()
        except Exception:
            log.debug("Presence stop failed", exc_info=True)
        for closer in (self.tts.shutdown, self.music.shutdown, self.ai.close):
            try:
                await closer()
            except Exception:
                log.debug("Shutdown step failed", exc_info=True)
        await super().close()

    async def on_ready(self) -> None:
        if not self._ready_logged:
            log.info(
                "Paradise is online as %s (%s)",
                self.user,
                self.user.id if self.user else "unknown",
            )
            self._ready_logged = True
        else:
            log.info("Paradise reconnected")

    # ------------------------------------------------------------------
    # Voice
    # ------------------------------------------------------------------

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user is None or member.id != self.user.id:
            return
        if before.channel is not None and after.channel is None:
            # Kicked, disconnected, or the channel vanished.
            try:
                await self.music.handle_forced_disconnect(member.guild.id)
            except Exception:
                log.exception("Forced-disconnect cleanup failed")

    # ------------------------------------------------------------------
    # Messages: AFK, TTS triggers, AI mentions, personality
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        self.ai.record(message)

        try:
            await self._handle_afk(message)
        except Exception:
            log.exception("AFK handling failed")

        try:
            spoken = await self._maybe_tts(message)
        except Exception:
            log.exception("TTS trigger failed")
            spoken = False

        if not spoken:
            try:
                await self._maybe_ai(message)
            except Exception:
                log.exception("AI handling failed")

        try:
            await self.personality.handle(message)
        except Exception:
            # Personality is optional; a bad message should never take down the client.
            log.exception("Personality handler failed")

    # -- AFK ----------------------------------------------------------------

    async def _handle_afk(self, message: discord.Message) -> None:
        afk_key = (message.guild.id, message.author.id)
        if self.afk_states.pop(afk_key, None) is not None:
            try:
                await asyncio.wait_for(
                    message.channel.send(
                        f"welcome back, {message.author.mention}. AFK cleared.",
                        delete_after=12,
                    ),
                    timeout=10,
                )
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("Could not announce AFK return", exc_info=True)
            return

        if not message.mentions:
            return
        for member in message.mentions:
            entry = self.afk_states.get((message.guild.id, member.id))
            if entry is None:
                continue
            notice_key = (message.channel.id, member.id)
            if time.monotonic() - self._afk_notice_last.get(notice_key, 0.0) < 300:
                return
            self._afk_notice_last[notice_key] = time.monotonic()
            try:
                await asyncio.wait_for(
                    message.channel.send(
                        f"{member.display_name} is AFK — {entry.reason} "
                        f"({discord.utils.format_dt(entry.since, 'R')})",
                        delete_after=30,
                        allowed_mentions=discord.AllowedMentions.none(),
                    ),
                    timeout=10,
                )
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("Could not post AFK notice", exc_info=True)
            return

    # -- TTS ------------------------------------------------------------------

    def _tts_trigger_text(self, message: discord.Message) -> str | None:
        mentioned = self.user is not None and self.user.mentioned_in(message)
        if mentioned:
            content = message.content
            if self.user:
                for form in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
                    content = content.replace(form, " ")
            match = _MENTION_TTS_RE.match(content)
            if match:
                return match.group(1).strip()
            return None
        match = _PLAIN_TTS_RE.match(message.content)
        if match:
            return match.group(1).strip()
        return None

    async def _maybe_tts(self, message: discord.Message) -> bool:
        text = self._tts_trigger_text(message)
        if not text:
            return False
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        try:
            await self.tts.say(
                message.guild, member, text, channel=message.channel
            )
        except TTSError as error:
            await self._tts_error_note(message, str(error))
            return True
        except Exception:
            log.exception("TTS say failed")
            return True
        try:
            await asyncio.wait_for(message.add_reaction("🔊"), timeout=8)
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("TTS ack reaction failed", exc_info=True)
        return True

    async def _tts_error_note(self, message: discord.Message, error: str) -> None:
        """Cooldowns get a quiet ⏳; other errors get a transient, deduped note."""
        if "easy —" in error or "already talking" in error:
            try:
                await asyncio.wait_for(message.add_reaction("⏳"), timeout=8)
            except (discord.DiscordException, asyncio.TimeoutError):
                pass
            return
        if time.monotonic() - self._tts_note_last.get(message.channel.id, 0.0) < _TTS_NOTE_COOLDOWN:
            return
        self._tts_note_last[message.channel.id] = time.monotonic()
        try:
            await asyncio.wait_for(
                message.reply(error, mention_author=False, delete_after=15),
                timeout=10,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("TTS error note failed", exc_info=True)

    # -- AI -------------------------------------------------------------------

    async def _maybe_ai(self, message: discord.Message) -> None:
        if not self.ai.should_reply_to(message):
            return
        if not self.ai.available:
            if time.monotonic() - self._ai_fallback_last.get(message.channel.id, 0.0) < _FALLBACK_NOTE_COOLDOWN:
                return
            self._ai_fallback_last[message.channel.id] = time.monotonic()
            try:
                await asyncio.wait_for(
                    message.reply(FALLBACK_UNAVAILABLE, mention_author=False),
                    timeout=10,
                )
            except (discord.DiscordException, asyncio.TimeoutError):
                log.debug("AI fallback note failed", exc_info=True)
            return

        if self.ai.cooldown_remaining(message) > 0:
            return  # stay quiet rather than nag

        try:
            answer = await asyncio.wait_for(self.ai.respond(message), timeout=60)
        except asyncio.TimeoutError:
            log.warning("AI answer timed out")
            return
        except Exception as error:
            # ParadiseError messages are safe to show; anything else stays quiet.
            from functions.helpers import ParadiseError

            if isinstance(error, ParadiseError):
                try:
                    await asyncio.wait_for(
                        message.reply(str(error), mention_author=False),
                        timeout=10,
                    )
                except (discord.DiscordException, asyncio.TimeoutError):
                    pass
                return
            log.error("AI answer failed: %s", error, exc_info=error)
            return
        if not answer:
            return
        try:
            await asyncio.wait_for(
                message.reply(
                    answer, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
                ),
                timeout=10,
            )
        except (discord.DiscordException, asyncio.TimeoutError):
            log.debug("AI reply send failed", exc_info=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # discord.py's HTTP logger can include request details; warnings are enough here.
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    config = load_config()
    if not config.token:
        print(
            "Paradise couldn't start: DISCORD_TOKEN is missing. "
            "Copy .env.example to .env and add your bot token.",
            file=sys.stderr,
        )
        return 1

    bot = ParadiseBot(config)
    try:
        bot.run(config.token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN in .env.")
        return 1
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Paradise stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
