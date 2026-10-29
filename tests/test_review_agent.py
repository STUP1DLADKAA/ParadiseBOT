"""Tests for the static review agent (`tools/review_agent.py`).

The agent is CI's gate against committed secrets and other high-risk changes,
so its two failure modes matter equally:

* a **false negative** — a real credential slips through because a rule was
  narrowed too far;
* a **false positive** — documentation that merely shows the *shape* of a
  setting fails the build, which teaches people to ignore the tool.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import review_agent as agent  # noqa: E402


def review_line(path: str, line: str):
    """Run the reviewer over a one-line synthetic addition."""
    diff = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,1 @@\n"
        f"+{line}\n"
    )
    files = agent.parse_diff(diff)
    return agent.review(agent.ReviewReport(base="a", head="b"), files)


class TestPlaceholderDetection(unittest.TestCase):
    DOC_SHAPES = [
        "SUPABASE_SECRET_KEY=<service_role key>",
        "SUPABASE_SECRET_KEY=${SUPABASE_SECRET_KEY}",
        "DISCORD_TOKEN=your_bot_token_here",
        "DISCORD_TOKEN=<your token>",
        "GEMINI_API_KEY=xxxx",
        "AI_API_KEY=changeme",
        "IMAGE_SEARCH_API_KEY=...",
    ]
    # Credential-shaped strings are assembled at runtime: a literal that looks
    # like a real Discord token trips GitHub's own push protection, which is a
    # good reason not to have one in a public repository.
    FAKE_DISCORD_TOKEN = ".".join(["M" + "T" * 23, "GaBcDe", "a" * 27])
    REAL_VALUES = [
        'SUPABASE_SECRET_KEY = "sb_secret_abcdefghijklmnop"',
        f"DISCORD_TOKEN={FAKE_DISCORD_TOKEN}",
        "GEMINI_API_KEY=" + "AIza" + "SyD-" + "abcdefghijklmnopqrstuvwxyz12345",
    ]

    def test_documentation_shapes_are_recognised(self):
        for line in self.DOC_SHAPES:
            with self.subTest(line=line):
                self.assertTrue(agent._is_placeholder(line))

    def test_real_values_are_never_excused(self):
        for line in self.REAL_VALUES:
            with self.subTest(line=line):
                self.assertFalse(agent._is_placeholder(line))


class TestSecretRule(unittest.TestCase):
    def test_a_documented_placeholder_does_not_block(self):
        for line in TestPlaceholderDetection.DOC_SHAPES:
            with self.subTest(line=line):
                report = review_line("docs/setup.md", line)
                self.assertEqual(
                    [f for f in report.findings if f.rule == "SEC001"], [],
                    f"documentation line flagged as a secret: {line}",
                )

    def test_a_quoted_credential_in_code_still_blocks(self):
        report = review_line(
            "functions/example.py", 'SUPABASE_SECRET_KEY = "sb_secret_abcdefghijklmnop"'
        )
        findings = [f for f in report.findings if f.rule == "SEC001"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, agent.BLOCKING)

    def test_a_discord_token_still_blocks(self):
        # A realistic bot-token shape: three base64url segments.
        token = TestPlaceholderDetection.FAKE_DISCORD_TOKEN
        report = review_line("bot.py", f'TOKEN = "{token}"')
        self.assertTrue([f for f in report.findings if f.rule == "SEC001"])

    def test_env_example_is_always_exempt(self):
        report = review_line(".env.example", "DISCORD_TOKEN=")
        self.assertFalse([f for f in report.findings if f.rule == "SEC001"])


class TestSelfTest(unittest.TestCase):
    def test_the_agent_self_test_passes(self):
        self.assertEqual(agent.run_self_test(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
