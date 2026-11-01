"""Tests for the free offline toolkit (functions/toolkit.py).

Every tool here must be pure local computation: no network, no key, no AI.
These tests pin the happy paths, the round trips and the friendly failures —
a tool never raises past :func:`run_tool`, because refusing is not in
character.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.toolkit import (  # noqa: E402
    ToolResult, base64_decode, base64_encode, caesar, color_card, count_text,
    countdown, dice_roll, eightball, emojify, format_duration, hash_text,
    lorem_ipsum, make_password, make_uuids, mock_text, morse_decode,
    morse_encode, parse_duration, qr_png, random_number, reverse_text,
    rock_paper_scissors, run_tool, spoiler_text,
)


class TestTextTools(unittest.TestCase):
    def test_reverse(self):
        self.assertEqual(reverse_text("hello world"), "dlrow olleh")

    def test_mock_alternates_and_keeps_punctuation(self):
        self.assertEqual(mock_text("ab!c"), "aB!c")
        self.assertIn(mock_text("paradise"), ("pArAdIsE",))

    def test_count_reports_words_chars_and_sentences(self):
        line = count_text("one two three. four five!")
        self.assertIn("**5** words", line)
        self.assertIn("**2** sentences", line)
        for needle in ("words", "characters", "sentences"):
            self.assertIn(needle, line)

    def test_spoiler_wraps_in_discord_tags(self):
        self.assertEqual(spoiler_text("the butler did it"), "|| the butler did it ||")

    def test_emojify_maps_words_and_letters(self):
        out = emojify("paradise is lit")
        self.assertIn("🌴", out)  # whole-word mapping
        self.assertIn("🔥", out)
        self.assertIn(":regional_indicator_i:", out)  # letter fallback

    def test_hash_is_stable_and_double(self):
        out = hash_text("hello")
        self.assertIn("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", out)
        self.assertIn("5d41402abc4b2a76b9719d911017c592", out)  # md5


class TestCiphersAndEncoding(unittest.TestCase):
    def test_caesar_shifts_and_round_trips(self):
        self.assertEqual(caesar("abc", 3), "def")
        self.assertEqual(caesar("Attack at Dawn", 5), "Fyyfhp fy Ifbs")
        self.assertEqual(caesar(caesar("Secret Message", 7), -7), "Secret Message")

    def test_rot13_is_its_own_inverse(self):
        self.assertEqual(caesar(caesar("urgent banana", 13), 13), "urgent banana")

    def test_base64_round_trip_with_unicode(self):
        self.assertEqual(base64_encode("hello"), "aGVsbG8=")
        self.assertEqual(base64_decode("aGVsbG8="), "hello")
        self.assertEqual(base64_decode(base64_encode("héllo wörld")), "héllo wörld")

    def test_base64_decode_rejects_garbage(self):
        with self.assertRaises(Exception):
            base64_decode("this is not base64 at all !!")

    def test_morse_round_trip(self):
        self.assertEqual(morse_encode("sos"), "... --- ...")
        self.assertEqual(morse_decode("... --- ..."), "sos")
        self.assertEqual(morse_decode(morse_encode("paradise 42")), "paradise 42")

    def test_morse_rejects_emoji(self):
        with self.assertRaises(Exception):
            morse_encode("héllo 🎉")


class TestGenerators(unittest.TestCase):
    def test_uuids_are_unique_and_counted(self):
        out = make_uuids(3)
        ids = re.findall(r"`[0-9a-f-]{36}`", out)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)

    def test_uuid_count_is_capped(self):
        self.assertEqual(len(re.findall(r"`[0-9a-f-]{36}`", make_uuids(50))), 5)

    def test_password_length_and_guaranteed_variety(self):
        out = make_password(20, 1)
        password = re.search(r"`(\S+)`", out).group(1)
        self.assertEqual(len(password), 20)
        self.assertTrue(re.search(r"[a-z]", password))
        self.assertTrue(re.search(r"[A-Z]", password))
        self.assertTrue(re.search(r"\d", password))

    def test_password_length_is_bounded(self):
        too_short = re.search(r"`(\S+)`", make_password(2, 1)).group(1)
        self.assertEqual(len(too_short), 8)
        too_long = re.search(r"`(\S+)`", make_password(500, 1)).group(1)
        self.assertEqual(len(too_long), 64)

    def test_random_number_respects_bounds(self):
        for _ in range(20):
            self.assertIn(random_number(1, 6), [f"🎲 **{n}** (from 1 to 6)" for n in range(1, 7)])

    def test_random_number_swaps_reversed_bounds(self):
        self.assertIn("(from 1 to 10)", random_number(10, 1))

    def test_dice_notation(self):
        for notation, count in (("2d6", 2), ("d20", 1), ("4d4+2", 4)):
            out = dice_roll(notation)
            self.assertIn("**", out)
        self.assertIn("= **", dice_roll("3d6+1"))

    def test_dice_tolerates_wrapper_words(self):
        self.assertIn("**", dice_roll("roll a d20"))
        self.assertIn("**", dice_roll("throw 2 d6 for me"))

    def test_dice_rejects_silliness(self):
        for bad in ("roll 999d99999", "2d1", "0d6"):
            with self.assertRaises(Exception):
                dice_roll(bad)

    def test_eightball_always_answers(self):
        answered = 0
        for _ in range(30):
            out = eightball("will it rain")
            self.assertTrue(out.startswith("🔮"))
            self.assertIn("**", out)
            answered += 1
        self.assertEqual(answered, 30)

    def test_rps_decides(self):
        # The bot's pick is random, so sample enough rounds to see every
        # outcome shape for a fixed player choice.
        outcomes = [rock_paper_scissors("rock") for _ in range(60)]
        self.assertTrue(all(("vs" in line or "forfeit" in line) for line in outcomes))
        self.assertTrue(any("tie" in line for line in outcomes))
        self.assertTrue(any("win" in line for line in outcomes))

    def test_lorem_is_bounded_and_paragraphed(self):
        two = lorem_ipsum(2)
        self.assertEqual(len(two.split("\n\n")), 2)
        self.assertTrue(two.startswith("Lorem ipsum"))

    def test_lorem_caps_paragraph_count(self):
        self.assertEqual(len(lorem_ipsum(9).split("\n\n")), 5)


class TestCountdown(unittest.TestCase):
    def test_christmas_is_coming(self):
        out = countdown("christmas")
        self.assertIn("Christmas", out)
        self.assertRegex(out, r"days to go|TODAY|tomorrow")

    def test_new_year_rolls_to_next_year(self):
        out = countdown("new year")
        self.assertIn("New Year", out)

    def test_explicit_date(self):
        out = countdown("25 dec 2026")
        self.assertIn("25 December 2026", out)

    def test_past_date_means_next_year(self):
        out = countdown("1 jan")  # from November, that's next January
        self.assertRegex(out, r"days to go|TODAY|tomorrow")

    def test_gibberish_asks_for_a_real_date(self):
        with self.assertRaises(Exception):
            countdown("the vibe")


class TestDurations(unittest.TestCase):
    def test_plain_english(self):
        self.assertEqual(parse_duration("in 20 minutes"), 1200.0)
        self.assertEqual(parse_duration("3 days"), 259200.0)
        self.assertEqual(parse_duration("in 2 hours from now"), 7200.0)

    def test_compound_shorthand(self):
        self.assertEqual(parse_duration("2h30m"), 9000.0)
        self.assertEqual(parse_duration("1d12h"), 129600.0)
        self.assertEqual(parse_duration("90s"), 90.0)

    def test_word_durations(self):
        self.assertEqual(parse_duration("in a day"), 86400.0)

    def test_unparseable_is_none(self):
        self.assertIsNone(parse_duration("tomorrow maybe"))
        self.assertIsNone(parse_duration(""))

    def test_format_duration(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(5400), "1h 30m")
        self.assertEqual(format_duration(90000), "1d 1h")


class TestImageTools(unittest.TestCase):
    def test_qr_png_is_a_real_png(self):
        png = qr_png("https://example.com")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(png), 200)

    def test_qr_strips_chatty_prefixes(self):
        self.assertEqual(qr_png("qr code for hello")[:8], b"\x89PNG\r\n\x1a\n")

    def test_qr_rejects_huge_payloads(self):
        with self.assertRaises(Exception):
            qr_png("x" * 2000)

    def test_color_card_hex_rgb_and_named(self):
        for value in ("#5865F2", "88 101 242", "88, 101, 242", "teal"):
            line, png = color_card(value)
            self.assertIn("🎨", line)
            self.assertGreaterEqual(len(png), 100)

    def test_color_card_rejects_gibberish(self):
        with self.assertRaises(Exception):
            color_card("the sky")


class TestRunToolNeverRaises(unittest.TestCase):
    def test_every_tool_with_bad_args_still_answers(self):
        from functions.toolkit import TOOL_KEYS

        for key in sorted(TOOL_KEYS):
            result = run_tool(key, "")
            self.assertIsInstance(result, ToolResult)
            self.assertTrue(result.text, f"{key} went silent on empty args")

    def test_every_tool_with_garbage_args_still_answers(self):
        from functions.toolkit import TOOL_KEYS

        for key in sorted(TOOL_KEYS):
            result = run_tool(key, "🎉 \x00 nonsense")
            self.assertIsInstance(result, ToolResult)
            self.assertTrue(result.text, f"{key} went silent on garbage")

    def test_unknown_tool_key_is_handled(self):
        self.assertIn("on paper", run_tool("teleport", "home").text)

    def test_result_image_flag(self):
        self.assertTrue(run_tool("qr", "hello").has_image)
        self.assertFalse(run_tool("reverse", "hello").has_image)


if __name__ == "__main__":
    unittest.main()
