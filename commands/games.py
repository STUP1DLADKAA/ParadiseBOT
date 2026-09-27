from __future__ import annotations

import hashlib
import random

import discord
from discord import app_commands
from discord.ext import commands

TRUTHS = (
    "What's a tiny thing that instantly improves your day?",
    "What's the weirdest food combination you genuinely like?",
    "What harmless lie did you believe for way too long?",
    "What's a song you know every word to?",
    "What skill would you download into your brain right now?",
    "What's your most unnecessary purchase that you still love?",
    "What game do you wish you could experience for the first time again?",
    "What opinion will you defend even though it barely matters?",
    "What's the funniest thing that's happened to you this month?",
    "What fictional place would you visit for one day?",
    "What's something you're surprisingly good at?",
    "Which phase of your life had the worst fashion choices?",
)
DARES = (
    "Send the last harmless photo in your camera roll.",
    "Describe your day using only three words.",
    "Change your nickname to a type of bread for ten minutes.",
    "Draw a cat in 30 seconds and send it.",
    "Speak only in questions for your next three messages.",
    "Recommend a song without explaining why.",
    "Write a dramatic review of the nearest object.",
    "Send a voice message saying a tongue twister twice.",
    "Use your least-used emoji in a completely serious sentence.",
    "Make up a terrible superhero name for yourself.",
    "Tell the group one genuinely nice thing about someone here.",
    "Set a five-minute timer and go drink some water.",
)
VIBES = (
    "rain against the window at 2am",
    "a kitchen with one light on",
    "finding money in an old coat",
    "loading screen music",
    "quietly unstoppable",
    "three raccoons with a plan",
    "Sunday with no obligations",
    "slightly haunted bookstore",
    "fresh sheets and nowhere to be",
    "walking home after a good night",
    "low battery, good intentions",
    "the calm before someone says something unhinged",
    "warm lamp energy",
    "mysterious but approachable",
)


def cooldown():
    return app_commands.checks.cooldown(1, 6, key=lambda i: (i.guild_id, i.user.id))


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(description="Get a harmless truth question")
    @cooldown()
    async def truth(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(random.choice(TRUTHS))

    @app_commands.command(description="Get a harmless dare")
    @cooldown()
    async def dare(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(random.choice(DARES))

    @app_commands.command(description="Receive an extremely scientific rating")
    @app_commands.describe(thing="The thing being rated")
    @cooldown()
    async def rate(self, interaction: discord.Interaction, thing: str) -> None:
        thing = thing.strip()[:150]
        if not thing:
            await interaction.response.send_message(
                "rate what, exactly?", ephemeral=True
            )
            return
        score = random.randint(0, 10)
        comments = {
            range(3): "rough.",
            range(3, 6): "it has potential.",
            range(6, 9): "honestly pretty solid.",
            range(9, 11): "massive.",
        }
        comment = next(text for band, text in comments.items() if score in band)
        await interaction.response.send_message(
            f"**{discord.utils.escape_markdown(thing)}:** {score}/10. {comment}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        description="Calculate a completely unserious compatibility score"
    )
    @app_commands.describe(first="Person one", second="Person two")
    @cooldown()
    async def ship(
        self,
        interaction: discord.Interaction,
        first: discord.Member,
        second: discord.Member,
    ) -> None:
        # Stable for a pair for one UTC day, but never stored.
        ids = sorted((first.id, second.id))
        seed = f"{ids[0]}:{ids[1]}:{discord.utils.utcnow().date().isoformat()}".encode()
        score = int.from_bytes(hashlib.sha256(seed).digest()[:2], "big") % 101
        if first.id == second.id:
            line = "self-love wins again."
        elif score >= 80:
            line = "suspiciously powerful."
        elif score >= 50:
            line = "there's a plot here."
        elif score >= 25:
            line = "could be worse."
        else:
            line = "maybe keep the group chat involved."
        await interaction.response.send_message(
            f"**{first.display_name} × {second.display_name}: {score}%**\n{line}\n-# entirely fake science",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="Get the current atmosphere")
    @cooldown()
    async def vibe(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"today's vibe: **{random.choice(VIBES)}**"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))
