"""Tests for the banter update: roasts, internet memes, GIFs, and the louder
personality engine.

Nothing here touches the network: the meme/GIF APIs are exercised through an
injected ``fetch_json`` (and, for the engine, an injected feed object), which
is exactly how they are wired in production.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands.banter import (  # noqa: E402
    _NO_GIF_KEY,
    build_roast_line,
    meme_embed,
    render_topic_meme,
)
from config import Config, PersonalityConfig  # noqa: E402
from functions.meme_feed import (  # noqa: E402
    ALLOWED_HOSTS,
    GifSearch,
    MemeFeed,
    OnlineMeme,
    clean_title,
    is_postable_image,
    parse_giphy,
    parse_meme,
    parse_tenor,
)
from functions.personality import AMBIENT_REACTIONS, Personality  # noqa: E402
from functions.roasts import (  # noqa: E402
    INTENSITIES,
    ROASTS,
    build_comeback,
    build_roast,
    build_self_roast,
    clean_name,
    invites_roast,
    looks_sensitive,
    sanitize_intensity,
)


def _make_msg(
    content: str,
    *,
    author_id: int = 42,
    display_name: str = "Ash",
    channel_id: int = 10,
    guild_stickers: list | None = None,
):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=False, display_name=display_name),
        webhook_id=None,
        mentions=[],
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=1, stickers=guild_stickers or []),
        type=discord.MessageType.default,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )


def _personality(**overrides) -> Personality:
    defaults = dict(
        enabled=True,
        reaction_chance=0.0,
        ambient_reaction_chance=0.0,
        reply_chance=0.0,
        sticker_chance=0.0,
        meme_chance=0.0,
        online_meme_chance=0.0,
        gif_chance=0.0,
        random_quip_chance=0.0,
        roast_chance=0.0,
        grammar_chance=0.0,
        global_cooldown=0,
        channel_cooldown=0,
        reply_channel_cooldown=0,
        humour_cooldown=0,
        grammar_cooldown=0,
        roast_user_cooldown=0,
    )
    defaults.update(overrides)
    config = PersonalityConfig(**defaults)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    return Personality(bot, config)


# ---------------------------------------------------------------------------
# Roasts
# ---------------------------------------------------------------------------


class TestRoastBook(unittest.TestCase):
    def test_every_intensity_has_a_real_pool(self):
        for intensity in INTENSITIES:
            self.assertGreaterEqual(len(ROASTS[intensity]), 10, intensity)

    def test_lines_are_short_enough_for_chat(self):
        for intensity, lines in ROASTS.items():
            for line in lines:
                self.assertLess(len(line), 220, f"{intensity}: {line}")

    def test_a_roast_uses_the_name_and_nothing_rude(self):
        line = build_roast("Ash", intensity="nuclear", with_twist=False)
        self.assertIn("Ash", line)
        self.assertNotIn("{name}", line)

    def test_antirepetition_walks_the_pool_before_repeating(self):
        recent: deque[str] = deque(maxlen=40)
        seen = [build_roast("Ash", intensity="spicy", recent=recent) for _ in range(8)]
        self.assertEqual(len(set(seen)), len(seen), seen)

    def test_intensity_is_clamped(self):
        self.assertEqual(sanitize_intensity("NUCLEAR"), "nuclear")
        self.assertEqual(sanitize_intensity("banana"), "spicy")
        self.assertEqual(sanitize_intensity(None), "spicy")

    def test_mild_and_nuclear_are_different_texts(self):
        mild = set(ROASTS["mild"])
        nuclear = set(ROASTS["nuclear"])
        self.assertFalse(mild & nuclear)

    def test_mentions_and_markdown_never_leak_into_a_roast(self):
        self.assertEqual(clean_name("<@123> **Ash**"), "Ash")
        self.assertEqual(clean_name("@everyone"), "you")
        line = build_roast("<@!456>", intensity="mild")
        self.assertNotIn("<@", line)

    def test_no_slurs_anywhere_in_the_book(self):
        for pool in list(ROASTS.values()) + [
            [build_comeback() for _ in range(5)],
            [build_self_roast() for _ in range(5)],
        ]:
            for line in pool:
                lowered = line.lower()
                for bad in ("nigg", "fagg", "retard", "tranny", "chink", "kike", "dyke"):
                    self.assertNotIn(bad, lowered, line)

    def test_serious_topics_are_recognised(self):
        for text in (
            "my grandfather passed away yesterday",
            "i'm in hospital again",
            "honestly i've been depressed lately",
            "we broke up last night",
            "i got fired from work",
        ):
            self.assertTrue(looks_sensitive(text), text)

    def test_normal_banter_is_not_flagged_as_serious(self):
        for text in (
            "i lost the game again",
            "my code is broken",
            "i'm so tired",
            "bro really said that",
        ):
            self.assertFalse(looks_sensitive(text), text)

    def test_invitations_are_recognised(self):
        self.assertEqual(invites_roast("roast me"), "self")
        self.assertEqual(invites_roast("do your worst"), "self")
        self.assertEqual(invites_roast("i'm him"), "cocky")
        self.assertEqual(invites_roast("1v1 me bro"), "cocky")
        self.assertIsNone(invites_roast("i'm going to the shop"))
        self.assertIsNone(invites_roast("roast me, my mum died"))  # never

    def test_roast_command_line_is_pure(self):
        line = build_roast_line("Ash", intensity="mild")
        self.assertIsInstance(line, str)
        self.assertTrue(line)


# ---------------------------------------------------------------------------
# Meme feed
# ---------------------------------------------------------------------------


def _meme_payload(**overrides):
    payload = {
        "postLink": "https://reddit.com/r/dankmemes/comments/abc/x",
        "subreddit": "dankmemes",
        "title": "when the code compiles first try",
        "url": "https://i.redd.it/abcdef.jpg",
        "nsfw": False,
        "spoiler": False,
    }
    payload.update(overrides)
    return payload


class TestMemeValidation(unittest.TestCase):
    def test_accepts_a_real_reddit_meme(self):
        meme = parse_meme(_meme_payload())
        self.assertIsNotNone(meme)
        self.assertEqual(meme.source, "dankmemes")
        self.assertEqual(meme.url, "https://i.redd.it/abcdef.jpg")
        self.assertFalse(meme.is_gif)

    def test_rejects_nsfw_and_spoilers_by_default(self):
        self.assertIsNone(parse_meme(_meme_payload(nsfw=True)))
        self.assertIsNone(parse_meme(_meme_payload(spoiler=True)))
        self.assertIsNotNone(parse_meme(_meme_payload(nsfw=True), allow_nsfw=True))

    def test_rejects_non_image_and_unknown_hosts(self):
        for url in (
            "https://i.redd.it/abcdef.mp4",
            "https://evil.example.com/abcdef.jpg",
            "http://i.redd.it/abcdef.jpg",  # not https
            "https://reddit.com/r/dankmemes/comments/abc/x",
            "",
            None,
        ):
            self.assertFalse(is_postable_image(url), url)

    def test_accepts_every_allowed_cdn(self):
        for host in ALLOWED_HOSTS:
            self.assertTrue(
                is_postable_image(f"https://{host}/x/y.gif", gifs_only=True), host
            )

    def test_gifs_only_filter(self):
        self.assertTrue(is_postable_image("https://i.redd.it/a.gif", gifs_only=True))
        self.assertFalse(is_postable_image("https://i.redd.it/a.jpg", gifs_only=True))

    def test_titles_are_flattened_and_capped(self):
        self.assertEqual(clean_title("  hi <@123> @everyone **there**  "), "hi there")
        self.assertLessEqual(len(clean_title("x" * 500)), 140)

    def test_garbage_payloads_are_safe(self):
        for payload in (None, [], "text", 42, {}):
            self.assertIsNone(parse_meme(payload))


class TestMemeFeed(unittest.TestCase):
    def _feed(self, responses, **kwargs):
        calls: list[str] = []

        async def fetch(url, params, timeout):
            calls.append(url)
            result = responses.pop(0) if responses else None
            if isinstance(result, Exception):
                raise result
            return result

        feed = MemeFeed(fetch_json=fetch, sources=("dankmemes",), retry_after=60, **kwargs)
        return feed, calls

    def test_pick_returns_a_meme_and_caches_the_next(self):
        feed, calls = self._feed([_meme_payload(), _meme_payload(url="https://i.redd.it/2.png")])

        async def run():
            first = await feed.pick()
            await asyncio.sleep(0)  # let the quiet prefetch run
            second = await feed.pick()
            return first, second

        first, second = asyncio.run(run())
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(calls)

    def test_a_dead_api_backs_off_instead_of_hammering(self):
        clock_value = [100.0]
        feed, calls = self._feed([None], clock=lambda: clock_value[0])

        async def run():
            first = await feed.pick()
            after_failure = len(calls)
            second = await feed.pick()  # still inside the cooldown
            return first, after_failure, second

        first, after_failure, second = asyncio.run(run())
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(calls), after_failure, "the API was retried during backoff")

    def test_an_exception_is_just_none(self):
        feed, _ = self._feed([RuntimeError("boom")])
        self.assertIsNone(asyncio.run(feed.pick()))

    def test_prefetch_fills_the_cache(self):
        feed, _ = self._feed([_meme_payload(), _meme_payload(url="https://i.redd.it/2.png")])
        self.assertEqual(asyncio.run(feed.prefetch(2)), 2)
        self.assertEqual(len(feed), 2)


class TestGifProviders(unittest.TestCase):
    TENOR = {
        "results": [
            {"media_formats": {"gif": {"url": "https://media.tenor.com/abc/giphy.gif"}},
             "content_description": "bruh", "itemurl": "https://tenor.com/view/x"},
        ]
    }
    GIPHY = {
        "data": [
            {"images": {"original": {"url": "https://media.giphy.com/media/x/giphy.gif"}},
             "title": "this is fine", "url": "https://giphy.com/gifs/x"},
        ]
    }

    def test_without_a_key_nothing_happens(self):
        search = GifSearch(tenor_key="", giphy_key="")
        self.assertFalse(search.available)
        self.assertIsNone(asyncio.run(search.search("bruh")))

    def test_tenor_parses(self):
        meme = parse_tenor(self.TENOR)
        self.assertIsNotNone(meme)
        self.assertTrue(meme.is_gif)
        self.assertEqual(meme.source, "tenor")

    def test_giphy_parses(self):
        meme = parse_giphy(self.GIPHY)
        self.assertIsNotNone(meme)
        self.assertTrue(meme.is_gif)
        self.assertEqual(meme.source, "giphy")

    def test_tenor_is_preferred_when_both_keys_exist(self):
        async def fetch(url, params, timeout):
            return self.TENOR

        search = GifSearch(tenor_key="t", giphy_key="g", fetch_json=fetch)
        self.assertEqual(search.provider, "tenor")
        meme = asyncio.run(search.search("bruh"))
        self.assertEqual(meme.source, "tenor")

    def test_an_empty_search_is_none(self):
        async def fetch(url, params, timeout):
            return {"results": []}

        search = GifSearch(tenor_key="t", fetch_json=fetch)
        self.assertIsNone(asyncio.run(search.search("nothing")))

    def test_garbage_provider_responses_are_safe(self):
        for payload in (None, [], "x", {}, {"results": "no"}):
            self.assertIsNone(parse_tenor(payload))
            self.assertIsNone(parse_giphy(payload))


# ---------------------------------------------------------------------------
# Personality: louder, funnier, roastier
# ---------------------------------------------------------------------------


class TestLouderPersonality(unittest.IsolatedAsyncioTestCase):
    async def test_a_cocky_message_gets_a_comeback(self):
        personality = _personality(reply_chance=0.0)
        msg = _make_msg("i'm him, undefeated, 1v1 me")
        await personality.handle(msg)
        msg.reply.assert_awaited_once()
        sent = msg.reply.call_args.args[0]
        self.assertIsInstance(sent, str)
        self.assertGreater(len(sent), 5)

    async def test_roast_me_always_gets_an_answer(self):
        personality = _personality(
            global_cooldown=3600, channel_cooldown=3600, roast_user_cooldown=0
        )
        msg = _make_msg("roast me")
        await personality.handle(msg)
        msg.reply.assert_awaited_once()
        self.assertIn("Ash", msg.reply.call_args.args[0])

    async def test_the_same_person_is_not_roasted_twice_in_a_row(self):
        personality = _personality(roast_chance=1.0, roast_user_cooldown=3600)
        first = _make_msg("honestly i think cereal is cold soup")
        await personality.handle(first)
        first.reply.assert_awaited_once()
        second = _make_msg("another absolutely normal sentence about lunch")
        await personality.handle(second)
        second.reply.assert_not_called()

    async def test_roasting_never_happens_on_a_heavy_message(self):
        personality = _personality(roast_chance=1.0)
        for text in (
            "my grandfather passed away this morning",
            "i've been really depressed lately",
            "we broke up and i'm not okay",
        ):
            msg = _make_msg(text)
            await personality.handle(msg)
            msg.reply.assert_not_called()
            msg.add_reaction.assert_not_called()

    async def test_stop_roasting_me_is_honoured(self):
        personality = _personality(roast_chance=1.0, roast_user_cooldown=0)
        ask = _make_msg("stop roasting me please")
        await personality.handle(ask)
        ask.reply.assert_not_called()
        msg = _make_msg("i am, objectively, the greatest player here")
        await personality.handle(msg)
        msg.reply.assert_not_called()

    async def test_the_same_action_is_not_repeated_twice_in_a_row(self):
        """Two memes back to back reads as a broken bot, not a funny one."""
        personality = _personality(meme_chance=1.0, online_meme_chance=0.0)
        first = _make_msg("honestly i think cereal is technically cold soup")
        await personality.handle(first)
        first.reply.assert_awaited_once()

        # Same channel, cooldowns open, but the last action was a meme, so the
        # meme weight is penalised and the roll lands nowhere.
        second = _make_msg("another extremely normal sentence about lunch")
        await personality.handle(second)
        second.reply.assert_not_called()

        # A different channel is unaffected.
        third = _make_msg("a third normal sentence about the weather", channel_id=99)
        await personality.handle(third)
        third.reply.assert_awaited_once()

    async def test_the_opt_out_survives_an_invitation(self):
        personality = _personality(roast_chance=1.0, roast_user_cooldown=0)
        await personality.handle(_make_msg("please stop roasting me"))
        msg = _make_msg("1v1 me, i'm him")
        await personality.handle(msg)
        msg.reply.assert_not_called()

    async def test_ambient_reaction_makes_the_bot_feel_present(self):
        personality = _personality(ambient_reaction_chance=1.0)
        msg = _make_msg("just had a really long day at the office honestly")
        await personality.handle(msg)
        msg.add_reaction.assert_awaited_once()
        self.assertIn(msg.add_reaction.call_args.args[0], AMBIENT_REACTIONS)

    async def test_online_memes_are_used_when_the_feed_has_one(self):
        meme = OnlineMeme(url="https://i.redd.it/abc.jpg", title="when it compiles", source="memes")
        feed = SimpleNamespace(pick=AsyncMock(return_value=meme))
        personality = Personality(
            SimpleNamespace(user=SimpleNamespace(id=999)),
            PersonalityConfig(
                meme_chance=1.0,
                online_meme_chance=1.0,
                gif_chance=0.0,
                reaction_chance=0.0,
                ambient_reaction_chance=0.0,
                reply_chance=0.0,
                sticker_chance=0.0,
                random_quip_chance=0.0,
                roast_chance=0.0,
                grammar_chance=0.0,
                global_cooldown=0,
                channel_cooldown=0,
                humour_cooldown=0,
            ),
            meme_feed=feed,
        )
        msg = _make_msg("just finished the whole project in one sitting somehow")
        await personality.handle(msg)
        msg.reply.assert_awaited_once()
        embed = msg.reply.call_args.kwargs["embed"]
        self.assertEqual(embed.image.url, "https://i.redd.it/abc.jpg")

    async def test_a_dead_feed_falls_back_to_a_drawn_meme(self):
        feed = SimpleNamespace(pick=AsyncMock(return_value=None))
        personality = Personality(
            SimpleNamespace(user=SimpleNamespace(id=999)),
            PersonalityConfig(
                meme_chance=1.0,
                online_meme_chance=1.0,
                gif_chance=0.0,
                reaction_chance=0.0,
                ambient_reaction_chance=0.0,
                reply_chance=0.0,
                sticker_chance=0.0,
                random_quip_chance=0.0,
                roast_chance=0.0,
                grammar_chance=0.0,
                global_cooldown=0,
                channel_cooldown=0,
                humour_cooldown=0,
            ),
            meme_feed=feed,
        )
        msg = _make_msg("honestly i think cereal is technically cold soup")
        await personality.handle(msg)
        msg.reply.assert_awaited_once()
        attached = msg.reply.call_args.kwargs["file"]
        self.assertEqual(attached.filename, "paradise_meme.png")

    async def test_no_gif_key_means_no_gif_attempt(self):
        personality = _personality(meme_chance=1.0, gif_chance=1.0, online_meme_chance=0.0)
        self.assertFalse(personality.gif_search.available)
        msg = _make_msg("honestly i think cereal is technically cold soup")
        await personality.handle(msg)
        # The meme path still runs; no embed-only GIF reply is produced.
        self.assertIn("file", msg.reply.call_args.kwargs)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestBanterCommands(unittest.IsolatedAsyncioTestCase):
    def _cog(self, *, feed=None, gifs=None):
        from commands.banter import Banter

        bot = SimpleNamespace(user=SimpleNamespace(id=999), personality=None)
        cog = Banter(bot)
        if feed is not None:
            cog._fallback_feed = feed
        if gifs is not None:
            cog._fallback_gifs = gifs
        return cog

    def _interaction(self, *, user="Ash"):
        return SimpleNamespace(
            user=SimpleNamespace(
                id=42, display_name=user, name=user, bot=False
            ),
            guild_id=1,
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock()
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_roast_targets_the_member(self):
        cog = self._cog()
        interaction = self._interaction()
        member = SimpleNamespace(id=77, display_name="Rohan", name="Rohan")
        await cog.roast.callback(cog, interaction, member)
        text = interaction.response.send_message.call_args.args[0]
        self.assertIn("Rohan", text)

    async def test_roast_without_a_member_is_self_deprecating(self):
        cog = self._cog()
        interaction = self._interaction()
        await cog.roast.callback(cog, interaction, None)
        text = interaction.response.send_message.call_args.args[0]
        self.assertIn("roasting myself", text)

    async def test_meme_uses_the_online_feed(self):
        meme = OnlineMeme(url="https://i.redd.it/x.jpg", title="t", source="memes")
        feed = SimpleNamespace(pick=AsyncMock(return_value=meme))
        cog = self._cog(feed=feed)
        interaction = self._interaction()
        await cog.meme.callback(cog, interaction, None)
        interaction.response.defer.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        self.assertEqual(embed.image.url, "https://i.redd.it/x.jpg")

    async def test_meme_offline_draws_one(self):
        feed = SimpleNamespace(pick=AsyncMock(return_value=None))
        cog = self._cog(feed=feed)
        interaction = self._interaction()
        await cog.meme.callback(cog, interaction, "tuesday")
        attached = interaction.followup.send.call_args.kwargs["file"]
        self.assertEqual(attached.filename, "paradise_meme.png")

    async def test_gif_without_a_key_explains_and_offers_a_meme(self):
        meme = OnlineMeme(url="https://i.redd.it/x.jpg", title="t", source="memes")
        feed = SimpleNamespace(pick=AsyncMock(return_value=meme))
        cog = self._cog(feed=feed, gifs=GifSearch(tenor_key="", giphy_key=""))
        interaction = self._interaction()
        await cog.gif.callback(cog, interaction, "bruh")
        args = interaction.followup.send.call_args
        self.assertEqual(args.args[0], _NO_GIF_KEY)
        self.assertEqual(args.kwargs["embed"].image.url, "https://i.redd.it/x.jpg")

    async def test_gif_with_a_key_posts_the_gif(self):
        gif = OnlineMeme(url="https://media.tenor.com/a/b.gif", source="tenor", is_gif=True)
        gifs = SimpleNamespace(available=True, search=AsyncMock(return_value=gif))
        cog = self._cog(gifs=gifs)
        interaction = self._interaction()
        await cog.gif.callback(cog, interaction, "bruh")
        self.assertEqual(
            interaction.followup.send.call_args.kwargs["embed"].image.url,
            "https://media.tenor.com/a/b.gif",
        )

    async def test_gif_with_no_results_says_so(self):
        gifs = SimpleNamespace(available=True, search=AsyncMock(return_value=None))
        cog = self._cog(gifs=gifs)
        interaction = self._interaction()
        await cog.gif.callback(cog, interaction, "aaa")
        self.assertIn("empty", interaction.followup.send.call_args.args[0])

    def test_topic_meme_renders_a_png(self):
        png = render_topic_meme("my code compiles", author="Ash")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 1000)

    def test_meme_embed_is_safe(self):
        embed = meme_embed(OnlineMeme(url="https://i.redd.it/x.jpg", title="t"))
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.image.url, "https://i.redd.it/x.jpg")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestBanterConfig(unittest.TestCase):
    def test_defaults_are_lively(self):
        cfg = Config.from_env({}).personality
        self.assertGreaterEqual(cfg.reaction_chance, 0.2)
        self.assertGreaterEqual(cfg.roast_chance, 0.1)
        self.assertLessEqual(cfg.global_cooldown, 15)
        self.assertLessEqual(cfg.channel_cooldown, 60)
        self.assertEqual(cfg.roast_intensity, "spicy")

    def test_env_overrides_are_parsed(self):
        cfg = Config.from_env(
            {
                "PERSONALITY_ROAST_CHANCE": "0.4",
                "PERSONALITY_ROAST_INTENSITY": "NUCLEAR",
                "PERSONALITY_ROAST_USER_COOLDOWN": "120",
                "PERSONALITY_AMBIENT_REACTION_CHANCE": "0.3",
                "PERSONALITY_GIF_CHANCE": "0.2",
                "PERSONALITY_ONLINE_MEME_CHANCE": "0.5",
                "MEME_SOURCES": "memes, dankmemes",
                "TENOR_API_KEY": "key123",
                "GIPHY_API_KEY": "",
            }
        ).personality
        self.assertEqual(cfg.roast_chance, 0.4)
        self.assertEqual(cfg.roast_intensity, "nuclear")
        self.assertEqual(cfg.roast_user_cooldown, 120)
        self.assertEqual(cfg.ambient_reaction_chance, 0.3)
        self.assertEqual(cfg.gif_chance, 0.2)
        self.assertEqual(cfg.online_meme_chance, 0.5)
        self.assertEqual(cfg.meme_sources, ("memes", "dankmemes"))
        self.assertEqual(cfg.tenor_api_key, "key123")
        self.assertEqual(cfg.giphy_api_key, "")

    def test_a_silly_intensity_falls_back_to_spicy(self):
        cfg = Config.from_env({"PERSONALITY_ROAST_INTENSITY": "yes please"}).personality
        self.assertEqual(cfg.roast_intensity, "spicy")

    def test_a_silly_chance_falls_back(self):
        cfg = Config.from_env({"PERSONALITY_ROAST_CHANCE": "12"}).personality
        self.assertEqual(cfg.roast_chance, 0.12)


if __name__ == "__main__":
    unittest.main()
