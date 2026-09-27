"""Paradise: a small Discord bot that knows when to be quiet."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from functions.personality import Personality
from functions.presence import PresenceManager

log = logging.getLogger("paradise")
EXTENSIONS = ("commands.fun", "commands.games", "commands.utility", "commands.server")


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
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=ParadiseTree,
        )
        self.afk_states: dict = {}
        self.personality = Personality(self)
        self.presence_manager = PresenceManager(self)
        self._ready_logged = False

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except discord.DiscordException:
            # Existing registered commands still work; a temporary sync failure is not fatal.
            log.warning("Could not sync slash commands", exc_info=True)
        self.presence_manager.start()

    async def close(self) -> None:
        await self.presence_manager.stop()
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

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

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

        try:
            await self.personality.handle(message)
        except Exception:
            # Personality is optional; a bad message should never take down the client.
            log.exception("Personality handler failed")


def token_from_environment() -> str:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and add your bot token."
        )
    return token


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # discord.py's HTTP logger can include request details; warnings are enough for this tiny bot.
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    try:
        token = token_from_environment()
    except RuntimeError as error:
        print(f"Paradise couldn't start: {error}", file=sys.stderr)
        return 1

    bot = ParadiseBot()
    try:
        bot.run(token, log_handler=None)
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
