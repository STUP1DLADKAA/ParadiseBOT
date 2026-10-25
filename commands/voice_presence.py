"""Keep Paradise in the busiest accessible human voice channel."""
import logging

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)


class VoicePresence(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.follow_crowd.start()

    async def cog_unload(self):
        self.follow_crowd.cancel()

    @tasks.loop(seconds=30)
    async def follow_crowd(self):
        for guild in self.bot.guilds:
            try:
                await self.join_busiest(guild)
            except (discord.DiscordException, RuntimeError, TimeoutError):
                log.warning("Could not join busiest VC in %s", guild.id, exc_info=True)

    @follow_crowd.before_loop
    async def before_follow(self):
        await self.bot.wait_until_ready()

    async def join_busiest(self, guild):
        candidates = []
        for channel in guild.voice_channels:
            permissions = channel.permissions_for(guild.me)
            humans = sum(not member.bot for member in channel.members)
            if (humans and channel != guild.afk_channel and permissions.view_channel
                    and permissions.connect and (not channel.user_limit
                    or len(channel.members) < channel.user_limit
                    or permissions.move_members
                    or guild.voice_client and guild.voice_client.channel == channel)):
                candidates.append((humans, channel))
        if not candidates:
            return
        client = guild.voice_client
        # Keep the current channel when tied, and never interrupt playback.
        if client and client.is_playing():
            return
        _, target = max(candidates, key=lambda item: (
            item[0], bool(client and client.channel == item[1]), -item[1].id))
        if client and client.is_connected():
            if client.channel == target:
                return
            await client.move_to(target)
        else:
            await target.connect(timeout=10, self_deaf=True)
        try:
            await target.send("the goat has arrived 🐐", delete_after=120,
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            log.warning("Could not announce VC arrival", exc_info=True)


async def setup(bot):
    await bot.add_cog(VoicePresence(bot))
