"""Unit tests for the safe, deterministic calculator."""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.calculator import calculate, looks_like_math, strip_question_words
from functions.helpers import ParadiseError


class TestCalculate(unittest.TestCase):
    def result(self, query: str) -> float:
        return calculate(query).result

    def test_percent_of(self):
        self.assertEqual(self.result("25% of 800"), 200)
        self.assertEqual(self.result("15% of 500"), 75)
        self.assertEqual(self.result("100% of 42"), 42)
        self.assertAlmostEqual(self.result("12.5% of 80"), 10)

    def test_percent_nested(self):
        self.assertEqual(self.result("15 % of 500 + 10"), 85)
        self.assertEqual(self.result("50% of (200 + 100)"), 150)

    def test_percent_standalone(self):
        self.assertEqual(self.result("50%"), 0.5)
        self.assertEqual(self.result("200%"), 2.0)

    def test_google_style_add_subtract_percent(self):
        self.assertEqual(self.result("100 + 15%"), 115)
        self.assertEqual(self.result("800 - 25%"), 600)
        self.assertEqual(self.result("2,500 + 10%"), 2750)

    def test_percent_times(self):
        self.assertEqual(self.result("50% * 300"), 150)

    def test_basic_arithmetic(self):
        self.assertEqual(self.result("234 × 56"), 13104)
        self.assertEqual(self.result("2 + 2 * 10"), 22)
        self.assertEqual(self.result("(3 + 4) * 2^10"), 7168)
        self.assertEqual(self.result("-40 / 2.5"), -16)
        self.assertEqual(self.result("2**8"), 256)

    def test_thousand_separators(self):
        self.assertAlmostEqual(self.result("1,000,000 / 3"), 1_000_000 / 3)

    def test_functions_and_constants(self):
        self.assertEqual(self.result("sqrt(144)"), 12)
        self.assertEqual(self.result("sqrt 144"), 12)
        self.assertAlmostEqual(self.result("ln(e^2)"), 2)
        self.assertAlmostEqual(self.result("pi * 2"), math.pi * 2)
        self.assertEqual(self.result("abs(-7)"), 7)
        self.assertEqual(self.result("log(1000)"), 3)

    def test_modulo(self):
        self.assertEqual(self.result("10 mod 3"), 1)
        self.assertEqual(self.result("10 % 3"), 1)
        self.assertEqual(self.result("7 modulo 4"), 3)

    def test_word_operators(self):
        self.assertEqual(self.result("6 times 7"), 42)
        self.assertEqual(self.result("20 divided by 4"), 5)
        self.assertEqual(self.result("3 squared"), 9)
        self.assertEqual(self.result("2 raised to 10"), 1024)
        self.assertEqual(self.result("square root of 81"), 9)

    def test_question_words(self):
        self.assertEqual(strip_question_words("what's 15% of 500"), "15% of 500")
        self.assertEqual(strip_question_words("how much is 800 + 15%"), "800 + 15%")
        self.assertEqual(self.result(strip_question_words("what is 2+2")), 4)

    def test_display_formatting(self):
        self.assertEqual(calculate("234 × 56").result_text, "13,104")
        self.assertIn("=", calculate("2+2").display)

    def test_errors_are_friendly(self):
        for bad in ("10/0", "1 % 0"):
            with self.assertRaises(ParadiseError) as ctx:
                calculate(bad)
            self.assertIn("zero", str(ctx.exception))

        with self.assertRaises(ParadiseError):
            calculate("")
        with self.assertRaises(ParadiseError):
            calculate("hello world")
        with self.assertRaises(ParadiseError):
            calculate("2**10**10")  # absurd magnitude refused
        with self.assertRaises(ParadiseError):
            calculate("sqrt(-4)")
        with self.assertRaises(ParadiseError):
            calculate("__import__('os').system('id')")  # sandbox holds

    def test_no_code_execution(self):
        with self.assertRaises(ParadiseError):
            calculate("open('/etc/passwd')")
        with self.assertRaises(ParadiseError):
            calculate("().__class__")


class TestLooksLikeMath(unittest.TestCase):
    def test_math_shapes(self):
        for text in (
            "25% of 800", "234 × 56", "what is 2+2", "calculate 3*17",
            "100 + 15%", "(3+4)*7", "sqrt(144)", "15 % of 500",
        ):
            self.assertTrue(looks_like_math(text), f"should be math: {text}")

    def test_not_math(self):
        for text in (
            "convert 100 USD to INR", "10 km to miles", "define photosynthesis",
            "weather in Delhi", "search latest minecraft", "one piece",
            "recommend anime like naruto", "what time is it in tokyo",
            "a5 notebook price", "ps5 restock news",
        ):
            self.assertFalse(looks_like_math(text), f"should not be math: {text}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Statistics over number lists and single-variable linear equations
# ---------------------------------------------------------------------------


class TestStatsRequests(unittest.TestCase):
    def test_average(self):
        self.assertEqual(calculate("average of 4 8 15 16 23 42").result, 18.0)
        self.assertEqual(calculate("what is the average of 10 20 and 30").result, 20.0)

    def test_sum_median_min_max_product(self):
        self.assertEqual(calculate("sum of 10 20 30").result, 60.0)
        self.assertEqual(calculate("sum of 3, 5, and 7").result, 15.0)
        self.assertEqual(calculate("median of 1 9 2").result, 2.0)
        self.assertEqual(calculate("median of 4 8 15 16").result, 11.5)
        self.assertEqual(calculate("min of 5 3 8").result, 3.0)
        self.assertEqual(calculate("max of 5 3 8").result, 8.0)
        self.assertEqual(calculate("product of 2 3 4").result, 24.0)

    def test_non_numeric_lists_are_rejected(self):
        with self.assertRaises(ParadiseError):
            calculate("average of pizza burgers")

    def test_stats_shape_routes_as_math(self):
        for text in ("average of 4 8 15", "the average of 3, 5 and 7",
                     "sum of 10 20 30"):
            self.assertTrue(looks_like_math(text), text)


class TestEquationSolving(unittest.TestCase):
    def test_simple_linear_equations(self):
        self.assertEqual(calculate("x + 2 = 5").result, 3.0)
        self.assertEqual(calculate("2x + 3 = 11").result, 4.0)
        self.assertEqual(calculate("solve 5x - 10 = 0").result, 2.0)
        self.assertEqual(calculate("10 = 2x").result, 5.0)
        self.assertEqual(calculate("3(x + 1) = 12").result, 3.0)

    def test_infinite_and_no_solutions_are_reported_honestly(self):
        with self.assertRaises(ParadiseError):
            calculate("x + 1 = x + 1")  # always true
        try:
            calculate("x + 1 = x + 2")
        except ParadiseError as error:
            self.assertIn("no solution", str(error).casefold())
        else:
            self.fail("expected a no-solution error")

    def test_non_linear_equations_are_refused_not_guessed(self):
        # x^2 = 4 has two solutions; pretending a linear solve answers it
        # would be fabrication, so the calculator declines.
        for text in ("x**2 = 4", "x^2 = 4"):
            with self.assertRaises(ParadiseError):
                calculate(text)

    def test_equation_shape_routes_as_math(self):
        for text in ("x + 2 = 5", "whats 2x + 3 = 11", "10 = 2x"):
            self.assertTrue(looks_like_math(text), text)

    def test_equations_with_two_variables_are_not_ours(self):
        with self.assertRaises(ParadiseError):
            calculate("x + y = 5")
