"""Keep Paradise in the busiest accessible human voice channel and announce arrival."""

from __future__ import annotations

import asyncio
import io
import logging
import random
from collections import OrderedDict, deque

import discord
from discord.ext import commands, tasks

from config import DictionaryConfig
from functions.dictionary.http import HttpClient, HttpError
from functions.dictionary.models import Clip, SpeechRequest
from functions.dictionary.playback import (
    LINGER_SECONDS,
    playback_support,
    resolve_ffmpeg_executable,
)
from functions.dictionary.pronounce import NeuralSpeechProvider, WebVoiceProvider

log = logging.getLogger(__name__)

# Pure spoken-natural English lines (no emojis) so the text posted in VC chat
# and the words spoken aloud in VC are 100% identical every single time.
ARRIVAL_LINES: tuple[str, ...] = (
    "The GOAT has arrived.",
    "Act natural, I am recording this for my documentary.",
    "Nobody panic, adult supervision has arrived. Wait, never mind, it is just me.",
    "I smelled questionable takes from three channels away so I came to investigate.",
    "Lower your voices, I am trying to think with my two remaining brain cells.",
    "Greetings mortals, please continue pretending to be productive.",
    "Attention everyone, I have already forgotten why I walked in here.",
    "My therapist told me not to join this voice channel, yet here I am.",
    "Do not mind me, I am just standing in the corner menacingly.",
    "Someone ordered a certified genius? They were busy so you got me instead.",
    "I have arrived to lower the average IQ of this call by forty points.",
    "Good evening, your local Wi-Fi parasite has connected.",
    "Please hold your applause, seriously, my ears are sensitive.",
    "According to my calculations, none of you have touched grass today.",
    "Wait, is this the free soup meeting or did I take a wrong turn?",
    "Stand up straight, the main character just entered the room.",
    "I tried to join a NASA briefing and somehow ended up with you guys.",
    "Hold on, let me adjust my imaginary tie before I start judging everyone.",
    "Knock knock. Never mind, I already let myself in.",
    "I heard yapping and my legs moved on their own.",
    "Fear not citizens, I brought zero solutions and plenty of opinions.",
    "I was busy solving world peace, what did I miss?",
    "Quick, hide the evidence, I am officially a witness now.",
    "Testing, testing, one two, yes my voice really is this deep.",
    "I am conducting a scientific study on why this call is so loud.",
    "Wait a minute, nobody told me there would be humans in here.",
    "Relax everyone, the smartest and dumbest guy on the server is here.",
    "I walked into the wrong room, actually no, I own this room now.",
    "Sorry I am late, I was arguing with a toaster and lost.",
    "Alert, an unsupervised bot has entered the building.",
    "Everyone act smart, the voices in the router sent me to check on you.",
    "Hold everything, my dramatic entrance music forgot to play.",
    "I am only here to eavesdrop and nod like I understand.",
    "Statistically speaking, things are about to get twenty percent weirder.",
    "Good news, I arrived. Bad news, I have nothing useful to contribute.",
    "Who summoned me from my peaceful nap inside the server rack?",
    "I came here to chew bubblegum and overthink, and I am all out of gum.",
    "Pretend I said something profound right now, thank you.",
    "Hold on, let me count the brain cells in here. Okay, done, still zero.",
    "I have entered the chat, literally and spiritually.",
    "Carry on soldiers, the general of bad decisions is on deck.",
    "Do not adjust your headphones, I always sound like a movie trailer.",
    "I brought popcorn in case someone starts an argument.",
    "Excuse me, is this where the professional procrastinators hang out?",
    "I just woke up from a five millisecond power nap, what year is it?",
    "Congratulations, your voice channel has been upgraded to premium chaos.",
    "Shh, nobody move, I dropped my last good idea somewhere on the floor.",
    "Reporting live from inside your walls, hello everyone.",
    "I was told there would be snacks, I am deeply disappointed.",
    "Gentlemen, scholars, and whatever the rest of you are, hello.",
)

# Backwards-compatible alias pointing to the classic first line.
ARRIVAL_TEXT = ARRIVAL_LINES[0]
ARRIVAL_SPEECH_TEXT = ARRIVAL_LINES[0]

MAX_ARRIVAL_SPEECH_CHARS = 220
CLIP_CACHE_SIZE = 32

# Deep, heavy male vocal-tract + chest-resonance shaping via FFmpeg:
# - Web/fallback TTS gets a 0.76x pitch+formant shift (~4.7 semitones deeper,
#   matching an adult deep-baritone vocal tract), natural deliberate cadence
#   restoration via atempo=1.26, +5 dB warm 120 Hz chest bass, soft high-shelf
#   taming, and peak limiting.
# - Neural TTS (defaulting to deep male "onyx") gets a gentler 0.90x heavy-chest
#   deepening so an already-deep male voice sounds rich and commanding.
HEAVY_MALE_VOICE_FILTER = (
    "aresample=48000,"
    "asetrate=48000*0.76,"
    "aresample=48000,"
    "atempo=1.26,"
    "bass=g=5:f=120:w=0.6,"
    "treble=g=-2:f=4000,"
    "alimiter=limit=0.95"
)
HEAVY_NEURAL_VOICE_FILTER = (
    "aresample=48000,"
    "asetrate=48000*0.90,"
    "aresample=48000,"
    "atempo=1.08,"
    "bass=g=5:f=115:w=0.6,"
    "alimiter=limit=0.95"
)

CONNECT_TIMEOUT = 10.0
HANDSHAKE_WAIT_SECONDS = 10.0
SETTLE_DELAY_SECONDS = 0.4
MAX_CLIP_SECONDS = 18.0


def audio_filter_for_clip(clip: Clip) -> str:
    """Return the FFmpeg heavy-male-voice audio filter chain for ``clip``."""
    if getattr(clip, "provider", "") == "neural":
        return HEAVY_NEURAL_VOICE_FILTER
    return HEAVY_MALE_VOICE_FILTER


class VoicePresence(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        settle_delay: float = SETTLE_DELAY_SECONDS,
        speech_providers: list | None = None,
    ) -> None:
        self.bot = bot
        self.settle_delay = max(0.0, float(settle_delay))
        self._http = HttpClient()
        if speech_providers is None:
            raw_cfg = getattr(getattr(bot, "config", None), "dictionary", None)
            dict_cfg = (
                raw_cfg if isinstance(raw_cfg, DictionaryConfig) else DictionaryConfig()
            )
            self._speech_providers = [
                NeuralSpeechProvider(
                    self._http,
                    base_url=dict_cfg.speech_base_url,
                    api_key=dict_cfg.speech_api_key,
                    model=dict_cfg.speech_model,
                    voice=dict_cfg.speech_voice or "onyx",
                    timeout=dict_cfg.timeout,
                    max_chars=MAX_ARRIVAL_SPEECH_CHARS,
                ),
                WebVoiceProvider(
                    self._http,
                    timeout=dict_cfg.timeout,
                    max_chars=MAX_ARRIVAL_SPEECH_CHARS,
                ),
            ]
        else:
            self._speech_providers = list(speech_providers)

        self._recent_lines: deque[str] = deque(
            maxlen=max(1, min(20, len(ARRIVAL_LINES) - 1))
        )
        self._clip_cache: OrderedDict[str, Clip] = OrderedDict()
        self._clip_lock = asyncio.Lock()
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._last_announced_channel: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.follow_crowd.start()

    async def cog_unload(self) -> None:
        self.follow_crowd.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._http.close()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def pick_arrival_line(self) -> str:
        """Choose a fresh random funny line without repeating recent ones."""
        recent = set(self._recent_lines)
        candidates = [line for line in ARRIVAL_LINES if line not in recent]
        choice = random.choice(candidates or ARRIVAL_LINES)
        self._recent_lines.append(choice)
        return choice

    # ------------------------------------------------------------------
    # Periodic crowd-following loop + reactive voice listener
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def follow_crowd(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.join_busiest(guild, disconnect_when_empty=True)
            except (discord.DiscordException, RuntimeError, TimeoutError):
                log.warning("Could not join busiest VC in %s", guild.id, exc_info=True)

    @follow_crowd.before_loop
    async def before_follow(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Offline test harnesses load cogs without logging the client in.
            self.follow_crowd.cancel()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return

        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)

        # 1. Paradise itself joined, switched, or left a voice channel
        if self.bot.user is not None and member.id == self.bot.user.id:
            if after.channel is None:
                self._last_announced_channel.pop(guild.id, None)
                return
            if before_id != after_id:
                self._spawn(self._handle_bot_arrival(guild, after.channel))
            return

        # 2. A human joined or switched voice channels — follow promptly
        if not member.bot and after.channel is not None and before_id != after_id:
            self._spawn(self._safe_join_busiest(guild))

    async def _safe_join_busiest(self, guild: discord.Guild) -> None:
        try:
            await self.join_busiest(guild)
        except asyncio.CancelledError:
            raise
        except (discord.DiscordException, RuntimeError, TimeoutError):
            log.warning("Could not join busiest VC in %s", guild.id, exc_info=True)

    async def _handle_bot_arrival(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """Announce arrival when Paradise is moved or connected outside join_busiest."""
        try:
            async with self._guild_lock(guild.id):
                if self._last_announced_channel.get(guild.id) == channel.id:
                    return
                await self._wait_for_voice_ready(guild, channel.id)
                client = guild.voice_client
                if (
                    client is None
                    or not client.is_connected()
                    or getattr(client.channel, "id", None) != channel.id
                ):
                    return
                await self._announce_unlocked(guild, channel)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Could not announce bot VC arrival in %s", channel.id, exc_info=True)

    async def _wait_for_voice_ready(self, guild: discord.Guild, channel_id: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDSHAKE_WAIT_SECONDS
        while loop.time() < deadline:
            client = guild.voice_client
            if (
                client is not None
                and client.is_connected()
                and getattr(client.channel, "id", None) == channel_id
            ):
                return
            await asyncio.sleep(0.1)

    async def join_busiest(
        self,
        guild: discord.Guild,
        *,
        disconnect_when_empty: bool = False,
    ) -> None:
        async with self._guild_lock(guild.id):
            candidates = []
            client = guild.voice_client
            for channel in guild.voice_channels:
                permissions = channel.permissions_for(guild.me)
                humans = sum(not member.bot for member in channel.members)
                if (
                    humans
                    and channel != guild.afk_channel
                    and permissions.view_channel
                    and permissions.connect
                    and (
                        not channel.user_limit
                        or len(channel.members) < channel.user_limit
                        or permissions.move_members
                        or (client and client.channel == channel)
                    )
                ):
                    candidates.append((humans, channel))

            if not candidates:
                if (
                    disconnect_when_empty
                    and client
                    and client.is_connected()
                    and not client.is_playing()
                ):
                    self._last_announced_channel.pop(guild.id, None)
                    await client.disconnect()
                return

            # Keep the current channel when tied, and never interrupt playback.
            if client and client.is_playing():
                return

            _, target = max(
                candidates,
                key=lambda item: (
                    item[0],
                    bool(client and client.channel == item[1]),
                    -item[1].id,
                ),
            )
            if client and client.is_connected():
                if client.channel == target:
                    return
                await client.move_to(target)
            else:
                await target.connect(timeout=CONNECT_TIMEOUT, self_deaf=True)

            await self._announce_unlocked(guild, target)

    # ------------------------------------------------------------------
    # VC arrival text + heavy-male voice announcement (identical line!)
    # ------------------------------------------------------------------

    async def _announce_unlocked(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if self._last_announced_channel.get(guild.id) == channel.id:
            return
        self._last_announced_channel[guild.id] = channel.id

        # One exact random line shared verbatim between VC text chat and VC speech.
        line = self.pick_arrival_line()

        try:
            await channel.send(
                line,
                delete_after=120,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            log.warning("Could not send VC arrival message in %s", channel.id, exc_info=True)

        try:
            await self._speak_arrival(guild, channel, line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Could not speak VC arrival in %s", channel.id, exc_info=True)

    async def _get_arrival_clip(self, line: str) -> Clip | None:
        cached = self._clip_cache.get(line)
        if cached is not None:
            self._clip_cache.move_to_end(line)
            return cached

        async with self._clip_lock:
            cached = self._clip_cache.get(line)
            if cached is not None:
                self._clip_cache.move_to_end(line)
                return cached

            request = SpeechRequest(word=line, language="en", region="uk")
            for provider in self._speech_providers:
                if not getattr(provider, "available", False):
                    continue
                try:
                    clip = await provider.speak(request)
                except asyncio.CancelledError:
                    raise
                except HttpError as err:
                    log.debug("Arrival TTS provider %s failed: %s", provider.name, err)
                    continue
                except Exception:  # noqa: BLE001
                    log.warning("Arrival TTS provider %s failed", provider.name, exc_info=True)
                    continue
                if clip is not None and clip.data:
                    self._clip_cache[line] = clip
                    self._clip_cache.move_to_end(line)
                    while len(self._clip_cache) > CLIP_CACHE_SIZE:
                        self._clip_cache.popitem(last=False)
                    return clip
        return None

    def _create_audio_source(self, clip: Clip, ffmpeg_exe: str) -> discord.AudioSource:
        af_chain = audio_filter_for_clip(clip)
        return discord.FFmpegOpusAudio(
            io.BytesIO(clip.data),
            pipe=True,
            executable=ffmpeg_exe,
            options=f"-loglevel error -af {af_chain}",
        )

    async def _speak_arrival(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
        line: str,
    ) -> None:
        if not playback_support():
            log.debug("Skipping VC arrival speech: voice playback unsupported")
            return

        me = getattr(guild, "me", None)
        if me is not None:
            permissions = channel.permissions_for(me)
            if getattr(permissions, "speak", True) is False:
                return
            me_voice = getattr(me, "voice", None)
            if me_voice is not None and getattr(me_voice, "mute", False) is True:
                return

        clip = await self._get_arrival_clip(line)
        if clip is None:
            return

        if self.settle_delay > 0:
            await asyncio.sleep(self.settle_delay)

        client = guild.voice_client
        if (
            client is None
            or not client.is_connected()
            or getattr(client.channel, "id", None) != channel.id
            or client.is_playing()
        ):
            return

        ffmpeg_exe = resolve_ffmpeg_executable() or "ffmpeg"
        source = self._create_audio_source(clip, ffmpeg_exe)
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def done(error: Exception | None) -> None:
            if error is not None:
                log.debug("VC arrival audio ended with an error: %s", error)
            loop.call_soon_threadsafe(finished.set)

        client.play(source, after=done)
        try:
            await asyncio.wait_for(finished.wait(), timeout=MAX_CLIP_SECONDS)
            if self.settle_delay > 0:
                await asyncio.sleep(LINGER_SECONDS)
        except asyncio.TimeoutError:
            log.debug("VC arrival playback timed out; stopping")
        finally:
            try:
                if client.is_playing():
                    client.pause()
                    client.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("Voice client cleanup failed", exc_info=True)
            try:
                stdout = getattr(source, "_stdout", None)
                if stdout is not None and hasattr(stdout, "close"):
                    stdout.close()
                source.cleanup()
            except Exception:  # noqa: BLE001
                log.debug("FFmpeg source cleanup failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoicePresence(bot))
