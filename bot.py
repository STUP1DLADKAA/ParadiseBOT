"""Paradise: a small Discord bot that knows when to be quiet."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, load_config
from functions.ai import FALLBACK_UNAVAILABLE, AIService
from functions.personality import Personality
from functions.presence import PresenceManager

log = logging.getLogger("paradise")

EXTENSIONS = (
    "commands.fun",
    "commands.utility",
    "commands.vc",
    "commands.dictionary",
    "commands.recipe",
    "commands.entertainment",
    "commands.ai",
    "commands.system",
)

_FALLBACK_NOTE_COOLDOWN = 15 * 60


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
        intents.voice_states = True
        # Server Members intent: without it the member cache stays empty and
        # voice leaderboards cannot resolve names/avatars (shows "User <id>").
        # Must also be enabled in the Discord Developer Portal for this bot.
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=ParadiseTree,
        )
        self.config = config
        self.started_at = discord.utils.utcnow()
        self.personality = Personality(self, config.personality)
        self.presence_manager = PresenceManager(self)
        self.ai = AIService(self, config)
        self.afk_states: dict = {}
        self._afk_notice_last: dict[tuple[int, int], float] = {}
        self._ai_fallback_last: dict[int, float] = {}
        self._ready_logged = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        try:
            synced = await self.tree.sync()
            names = sorted(command.name for command in synced)
            log.info("Synced %d slash commands: %s", len(synced), ", ".join(names))
        except discord.DiscordException:
            # Existing registered commands still work; a sync failure is not fatal.
            log.warning("Could not sync slash commands", exc_info=True)

        self.presence_manager.start()

    async def close(self) -> None:
        try:
            await self.presence_manager.stop()
        except Exception:
            log.debug("Presence stop failed", exc_info=True)
        try:
            await self.ai.close()
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
            try:
                await self.presence_manager.apply_current()
            except Exception:
                log.debug("Presence re-apply failed", exc_info=True)

        # Reconcile voice sessions after connect / reconnect
        voice_cog = self.get_cog("Voice") or self.get_cog("vc")
        if voice_cog and hasattr(voice_cog, "tracker"):
            try:
                await voice_cog.tracker.reconcile_guilds()
            except Exception:
                log.exception("Voice startup reconciliation failed")

    # ------------------------------------------------------------------
    # Messages: AFK, AI mentions, personality
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
            if await self._maybe_dictionary(message):
                return
        except Exception:
            log.exception("Dictionary handling failed")

        try:
            if await self._maybe_entertainment(message):
                return
        except Exception:
            log.exception("Entertainment handling failed")

        try:
            await self._maybe_ai(message)
        except Exception:
            log.exception("AI handling failed")

        try:
            await self.personality.handle(message)
        except Exception:
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

    # -- Dictionary -----------------------------------------------------------

    async def _maybe_dictionary(self, message: discord.Message) -> bool:
        cog = self.get_cog("Dictionary")
        handler = getattr(cog, "handle_message", None)
        if handler is None:
            return False
        return bool(await handler(message))

    # -- Entertainment ----------------------------------------------------------

    async def _maybe_entertainment(self, message: discord.Message) -> bool:
        cog = self.get_cog("Entertainment")
        handler = getattr(cog, "handle_message", None)
        if handler is None:
            return False
        return bool(await handler(message))

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
            return

        try:
            answer = await asyncio.wait_for(self.ai.respond(message), timeout=60)
        except asyncio.TimeoutError:
            log.warning("AI answer timed out")
            return
        except Exception as error:
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
