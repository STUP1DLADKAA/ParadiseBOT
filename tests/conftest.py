"""Shared pytest configuration.

The suite is fully offline by contract: no test may open a socket. Most of the
code already injects fakes at the seam it cares about, but the leaderboard
renderer fetches Discord avatars by URL deep inside `render_leaderboard`. This
fixture replaces that single seam for every test, returning the same
deterministic fallback avatar the renderer uses when a fetch fails — so a test
that happens to include an avatar URL cannot reach `cdn.discordapp.com`.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.voice import render as render_module  # noqa: E402


@pytest.fixture(autouse=True)
def offline_avatars(monkeypatch):
    """Never fetch a real avatar image; draw the fallback immediately."""

    async def fake_fetch_avatar(url, name, size=128, *, session=None):
        return render_module._render_fallback_avatar(name, size, hash(name))

    monkeypatch.setattr(render_module, "fetch_avatar", fake_fetch_avatar)
    return fake_fetch_avatar
