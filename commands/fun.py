from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

BALL_ANSWERS = (
    "yeah, probably.",
    "absolutely not.",
    "ask again when the vibes improve.",
    "signs point to yes.",
    "I wouldn't bet on it.",
    "weirdly, yes.",
    "that's between you and God.",
    "give it a minute.",
    "nope.",
    "the council says maybe.",
    "outlook: suspicious.",
    "I have no idea but now I'm curious.",
)


def cooldown():
    return app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Check if Paradise is still alive")
    @cooldown()
    async def ping(self, interaction: discord.Interaction) -> None:
        ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            random.choice(
                (
                    f"yeah yeah, I'm here. `{ms}ms`",
                    f"alive. mostly. `{ms}ms`",
                    f"pong, apparently. `{ms}ms`",
                )
            )
        )

    @app_commands.command(name="8ball", description="Ask the questionably mystical orb")
    @app_commands.describe(question="The thing keeping you awake")
    @cooldown()
    async def eight_ball(self, interaction: discord.Interaction, question: str) -> None:
        if not question.strip():
            await interaction.response.send_message(
                "you do have to ask something.", ephemeral=True
            )
            return
        await interaction.response.send_message(random.choice(BALL_ANSWERS))

    @app_commands.command(description="Make a deeply important binary decision")
    @cooldown()
    async def coinflip(self, interaction: discord.Interaction) -> None:
        side = random.choice(("heads", "tails"))
        suffix = random.choice(
            (".", ".\ndon't question it.", ".\nfate has spoken.", ".\nthere you go.")
        )
        await interaction.response.send_message(side + suffix)

    @app_commands.command(description="Roll a die")
    @app_commands.describe(sides="Number of sides (2–1,000,000)")
    @cooldown()
    async def roll(
        self,
        interaction: discord.Interaction,
        sides: app_commands.Range[int, 2, 1_000_000] = 6,
    ) -> None:
        result = random.randint(1, sides)
        await interaction.response.send_message(
            random.choice(
                (
                    f"**{result}**.",
                    f"you rolled **{result}**.",
                    f"**{result}**. make of that what you will.",
                )
            )
        )

    @app_commands.command(description="Let Paradise choose so you don't have to")
    @app_commands.describe(options="Separate choices with | (2–20 choices)")
    @cooldown()
    async def choose(self, interaction: discord.Interaction, options: str) -> None:
        choices = [item.strip() for item in options.split("|") if item.strip()]
        if not 2 <= len(choices) <= 20:
            await interaction.response.send_message(
                "give me 2–20 choices separated by `|`.", ephemeral=True
            )
            return
        if any(len(item) > 100 for item in choices):
            await interaction.response.send_message(
                "one of those choices is an entire novel. keep each under 100 characters.",
                ephemeral=True,
            )
            return
        pick = random.choice(choices)
        await interaction.response.send_message(
            f"**{discord.utils.escape_markdown(pick)}**.\n\nthe council has spoken.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
