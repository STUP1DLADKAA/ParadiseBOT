from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


def cooldown():
    return app_commands.checks.cooldown(1, 8, key=lambda i: (i.guild_id, i.user.id))


class Server(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Show basic public info about a server member")
    @app_commands.guild_only()
    @app_commands.describe(user="Member to inspect (defaults to you)")
    @cooldown()
    async def userinfo(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "couldn't find that person.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=member.display_name, colour=member.colour or discord.Colour.blurple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Username", value=str(member), inline=True)
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, "R"),
            inline=True,
        )
        embed.add_field(
            name="Joined Paradise"
            if interaction.guild and interaction.guild.name.lower() == "paradise"
            else "Joined server",
            value=discord.utils.format_dt(member.joined_at, "R")
            if member.joined_at
            else "not sure",
            inline=True,
        )
        embed.set_footer(text=f"User ID: {member.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show basic info about this server")
    @app_commands.guild_only()
    @cooldown()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "this isn't a server.", ephemeral=True
            )
            return
        embed = discord.Embed(title=guild.name, colour=discord.Colour.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(
            name="Members", value=str(guild.member_count or "unknown"), inline=True
        )
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, "R"),
            inline=True,
        )
        embed.add_field(
            name="Boosts", value=str(guild.premium_subscription_count), inline=True
        )
        personality = getattr(self.bot, "personality", None)
        if personality:
            embed.add_field(
                name="Messages seen this run",
                value=str(personality.message_counts[guild.id]),
                inline=True,
            )
            embed.add_field(
                name="Active-day streak",
                value=f"{personality.daily_streak(guild.id)} day(s)",
                inline=True,
            )
        embed.set_footer(text=f"Server ID: {guild.id}")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Server(bot))
