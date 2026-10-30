"""Paradise: a small Discord bot that knows when to be quiet."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, load_config
from functions import autoupdate, perf
from functions.ai import AIService
from functions.personality import Personality
from functions.presence import PresenceManager
from functions.startup import PRIMARY_GUILD_ID, CommandSynchronizer

log = logging.getLogger("paradise")

EXTENSIONS = (
    "commands.fun",
    "commands.banter",
    "commands.utility",
    "commands.vc",
    "commands.clip",
    "commands.voice_presence",
    "commands.minigames",
    "commands.dictionary",
    "commands.recipe",
    "commands.ai",
    "commands.system",
    "commands.assistant",
    "commands.edit",
)


class ParadiseTree(app_commands.CommandTree):
    """Every command is a **guild** command — never a DM, never global.

    The context flags are set here, once, instead of on 40 decorators: a
    command registered for DMs cannot run anyway (Paradise needs a server for
    voice, stats and permissions), and offering it there is clutter.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.allowed_contexts = app_commands.AppCommandContext(
            guild=True, dm_channel=False, private_channel=False
        )
        self.allowed_installs = app_commands.AppInstallationType(
            guild=True, user=False
        )

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
        self._ready_logged = False
        # The single owner of every command-tree write (see functions/startup).
        # Commands are registered ONLY in the home guild: Discord publishes
        # guild commands instantly, and a global copy would show up as a
        # second, identical entry for up to an hour. Whatever an older release
        # published globally is cleared on the next start.
        self.synchronizer = CommandSynchronizer(
            self,
            guild_id=config.guild_id or PRIMARY_GUILD_ID,
            sync_global=False,
            purge_global=True,
        )
        self.commands_ready = False
        self.autoupdate_task: asyncio.Task | None = None
        # Exactly-once guard for message dispatch (bounded, in-memory).
        self._handled: deque[int] = deque()
        self._handled_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """The explicit startup flow: extensions → prune → sync → ready."""
        workers = tune_default_executor()
        if workers:
            log.info("Worker threads: %d (audio, rendering, file I/O)", workers)
        await self._load_extensions()

        # Command synchronisation is awaited here, before the bot is
        # considered ready, so a restart always publishes the current tree
        # (including removals) to the home guild immediately.
        report = await self.synchronizer.run()
        self.commands_ready = report.ok
        if report.guild_commands:
            log.info(
                "Guild %s commands: %s",
                report.guild_id,
                ", ".join(report.guild_commands),
            )
        for problem in report.errors:
            log.warning("Command sync issue — %s", problem)

        log.info("AI provider: %s", self.ai.describe())

        # Warm fonts, card backdrops and the render pool while Discord is
        # still connecting, so the first `/vc leaderboard` is not the slow one.
        self.loop.create_task(
            perf.prewarm(enabled=bool(getattr(self.config, "prewarm", True))),
            name="paradise-prewarm",
        )
        # Validate the Gemini key (if any) in the background — validation is
        # never fatal: an unreachable network simply skips it.
        self.loop.create_task(self._validate_ai())

        # Warm the meme cache so the first meme reply is instant instead of
        # waiting for Reddit. Best effort: no internet just means an empty cache.
        self.loop.create_task(self._warm_memes(), name="paradise-memes")

        self.presence_manager.start()

        # Keep the checkout fresh while online (the start-up check already
        # ran before login; this one handles long-lived processes).
        self.autoupdate_task = asyncio.create_task(
            autoupdate.watch(self, self.config), name="paradise-autoupdate"
        )

    async def _load_extensions(self) -> None:
        """Load every extension; one broken cog never blocks the others."""
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
            except commands.ExtensionAlreadyLoaded:
                continue
            except Exception:  # noqa: BLE001 - startup must stay resilient
                log.exception("Could not load extension %s", extension)

    async def _validate_ai(self) -> None:
        try:
            await asyncio.wait_for(self.ai.validate_startup(), timeout=20)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.debug("AI startup validation skipped", exc_info=True)

    async def close(self) -> None:
        watcher = getattr(self, "autoupdate_task", None)
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        clip_service = getattr(self, "voice_clip_service", None)
        if clip_service is not None:
            try:
                await clip_service.shutdown()
            except Exception:
                log.debug("Voice clip shutdown failed", exc_info=True)
        try:
            await self.presence_manager.stop()
        except Exception:
            log.debug("Presence stop failed", exc_info=True)
        try:
            await self.ai.close()
        except Exception:
            log.debug("Shutdown step failed", exc_info=True)
        try:
            await perf.close_shared_sessions()
            await perf.shutdown_render_pool()
        except Exception:
            log.debug("Shared-session shutdown failed", exc_info=True)
        await super().close()

    async def on_ready(self) -> None:
        # Reaching Discord proves the code that is running works, so the
        # auto-update crash-loop counter can be cleared.
        autoupdate.note_ready(self.config)
        if not self._ready_logged:
            log.info(
                "Paradise is online as %s (%s)",
                self.user,
                self.user.id if self.user else "unknown",
            )
            self._ready_logged = True
        else:
            log.info("Paradise reconnected")

        # Recover scheduled community systems before restoring their shared
        # presence. On a fresh start this also reconciles an interrupted game.
        mini_games = self.get_cog("MiniGames")
        if mini_games is not None:
            try:
                await mini_games.manager.recover_after_restart()
            except Exception:
                log.exception("Mini-game restart recovery failed")
            mini_games.manager.start()
        try:
            await self.presence_manager.apply_current(force=True)
        except Exception:
            log.debug("Presence re-apply failed", exc_info=True)

        # Reconcile voice sessions after connect / reconnect
        voice_cog = self.get_cog("Voice") or self.get_cog("vc")
        if voice_cog and getattr(voice_cog, "tracker", None):
            try:
                await voice_cog.tracker.reconcile_guilds()
            except Exception:
                log.exception("Voice startup reconciliation failed")

    async def _warm_memes(self) -> None:
        """Prefetch a couple of internet memes in the background, once."""
        feed = getattr(getattr(self, "personality", None), "meme_feed", None)
        prefetch = getattr(feed, "prefetch", None)
        if prefetch is None:
            return
        try:
            count = await prefetch(3)
            log.info("Meme feed: %d meme(s) ready", count)
        except Exception:  # noqa: BLE001 - an empty cache is not an error
            log.debug("Meme prefetch failed", exc_info=True)

    # ------------------------------------------------------------------
    # Messages: AFK, AI mentions, personality
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        """One message, one path.

        basic filtering → AFK → explicitly addressed? → assistant → STOP.
        Only messages that were *not* addressed to Paradise fall through to
        the dictionary text triggers and the passive personality, so a ping
        can never produce two replies.
        """
        if message.author.bot or not message.guild:
            return
        if not self._claim(message):
            return  # a duplicate dispatch of the same message

        self.ai.record(message)

        try:
            await self._handle_afk(message)
        except Exception:
            log.exception("AFK handling failed")

        # Explicit address (mention or reply): the assistant owns it.
        addressed = False
        try:
            addressed = self.ai.should_reply_to(message)
        except Exception:
            log.exception("Mention detection failed")
        if addressed:
            try:
                if await self._maybe_assistant(message):
                    return
            except Exception:
                log.exception("Assistant handling failed")
                await self._assistant_failure(message)
            return

        try:
            if await self._maybe_dictionary(message):
                return
        except Exception:
            log.exception("Dictionary handling failed")

        try:
            await self.personality.handle(message)
        except Exception:
            log.exception("Personality handler failed")

    def _claim(self, message: discord.Message) -> bool:
        """True the first time a message id is seen (bounded memory)."""
        message_id = getattr(message, "id", None)
        if message_id is None:
            return True
        if message_id in self._handled_ids:
            return False
        self._handled.append(message_id)
        self._handled_ids.add(message_id)
        while len(self._handled) > 512:
            self._handled_ids.discard(self._handled.popleft())
        return True

    async def _assistant_failure(self, message: discord.Message) -> None:
        """A ping always gets an answer, even when the pipeline broke."""
        try:
            await message.reply(
                "that broke on my end — it's been logged. try again in a moment.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            log.debug("Could not send assistant failure notice", exc_info=True)

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

    # -- Assistant (mentions → tools first, AI last) ----------------------------

    async def _maybe_assistant(self, message: discord.Message) -> bool:
        cog = self.get_cog("Assistant")
        handler = getattr(cog, "handle_message", None)
        if handler is None:
            return False
        return bool(await handler(message))


def tune_default_executor(loop: asyncio.AbstractEventLoop | None = None) -> int:
    """Use the whole box: a fat thread pool for audio/rendering work.

    Opus decoding, timeline mixing, FFmpeg pipes, Supabase calls and file I/O
    all run in the default executor. Sizing is centralised in
    :mod:`functions.perf` so the bot, the benchmarks and the tests agree.

    Must run inside the *running* loop (``setup_hook``): ``Client.run`` creates
    its own loop, so configuring one beforehand would be thrown away.
    """
    try:
        target = loop or asyncio.get_running_loop()
        workers = perf.auto_workers()
        target.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="paradise-worker"
            )
        )
        return workers
    except Exception:  # noqa: BLE001 - tuning is optional
        log.debug("Could not tune the default executor", exc_info=True)
        return 0


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

    # Pull the latest files from GitHub *before* anything imports the cogs.
    try:
        update = asyncio.run(autoupdate.run_startup_check(config))
    except Exception:  # noqa: BLE001 - updating must never block start-up
        log.debug("Auto-update start-up check failed", exc_info=True)
        update = None
    if (
        update is not None
        and update.applied
        and bool(getattr(config.autoupdate, "restart_after_update", True))
    ):
        print(
            "Paradise updated itself and will exit now so the new files are "
            "loaded cleanly. Start it again (or let your supervisor restart it).",
            file=sys.stderr,
        )
        return 0

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
