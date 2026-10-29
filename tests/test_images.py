"""Tests for the web image search capability (functions/images/).

Everything runs offline: the aiohttp session is replaced with a fake
transport that serves realistic provider payloads, so the real parsing,
validation, ranking, caching and failure paths are all exercised.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions.images import (  # noqa: E402
    ImageResponse,
    ImageResult,
    ImageSearchService,
    build_image_embeds,
    is_safe_image_url,
)
from functions.images import provider as provider_module  # noqa: E402


# ---------------------------------------------------------------------------
# Realistic provider payloads
# ---------------------------------------------------------------------------

WIKI_PAYLOAD = {
    "query": {
        "pages": [
            {
                "pageid": 1,
                "title": "Red panda",
                "fullurl": "https://en.wikipedia.org/wiki/Red_panda",
                "original": {
                    "source": "https://upload.wikimedia.org/wikipedia/commons/1/19/Red_Panda.jpg",
                    "width": 2000,
                    "height": 1500,
                },
            }
        ]
    }
}

COMMONS_PAYLOAD = {
    "query": {
        "pages": [
            {
                "title": "File:Red panda in tree.jpg",
                "imageinfo": [
                    {
                        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/Rp.jpg/1280px-Rp.jpg",
                        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Rp.jpg",
                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Rp.jpg",
                        "mime": "image/jpeg",
                        "thumbwidth": 1280,
                        "thumbheight": 853,
                        "extmetadata": {"Artist": {"value": "<a href='#'>Jane Doe</a>"}},
                    }
                ],
            },
            {
                # Must be filtered out as noise, not shown as a "picture".
                "title": "File:Red panda logo.png",
                "imageinfo": [
                    {
                        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b2/Logo.png/1280px-Logo.png",
                        "mime": "image/png",
                    }
                ],
            },
        ]
    }
}

OPENVERSE_PAYLOAD = {
    "result_count": 2,
    "results": [
        {
            "title": "Red panda cub",
            "url": "https://live.staticflickr.com/65535/example_b.jpg",
            "foreign_landing_url": "https://www.flickr.com/photos/example/123",
            "creator": "A Photographer",
            "width": 1024,
            "height": 768,
        }
    ],
}

GOOGLE_PAYLOAD = {
    "items": [
        {
            "title": "Red panda - National Geographic",
            "link": "https://example.org/photos/red-panda.jpg",
            "mime": "image/jpeg",
            "image": {
                "contextLink": "https://example.org/animals/red-panda",
                "width": 1600,
                "height": 900,
            },
        }
    ]
}


# ---------------------------------------------------------------------------
# Fake aiohttp transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Serves canned payloads keyed by a substring of the request URL."""

    def __init__(self, routes, *, head=None, fail=()):
        self.routes = routes
        self.head_result = head
        self.fail = tuple(fail)
        self.requests: list[str] = []
        self.head_requests: list[str] = []

    def get(self, url, params=None):
        self.requests.append(url)
        for marker in self.fail:
            if marker in url:
                raise aiohttp.ClientError("boom")
        for marker, payload in self.routes.items():
            if marker in url:
                if isinstance(payload, int):
                    return FakeResponse(status=payload)
                return FakeResponse(payload=payload)
        return FakeResponse(status=404)

    def head(self, url, allow_redirects=True, timeout=None):
        self.head_requests.append(url)
        if self.head_result is None:
            raise aiohttp.ClientError("no head")
        return self.head_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def use_session(session):
    """Patch the service's pooled-session factory with *session*."""
    return patch.object(provider_module, "_session", lambda *a, **k: session)


def make_service(**config):
    defaults = {
        "enabled": True,
        "timeout": 5.0,
        "cache_ttl": 300,
        "cache_size": 16,
        "api_key": "",
        "engine_id": "",
    }
    defaults.update(config)
    return ImageSearchService(SimpleNamespace(**defaults))


# ---------------------------------------------------------------------------
# URL validation  (§ image safety / URL validation)
# ---------------------------------------------------------------------------


class TestUrlValidation(unittest.TestCase):
    def test_accepts_normal_web_urls(self):
        for url in (
            "https://upload.wikimedia.org/wikipedia/commons/a/a1/Rp.jpg",
            "http://example.com/image.png",
            "https://cdn.example.co.uk/a/b/c.webp?size=large",
        ):
            self.assertTrue(is_safe_image_url(url), url)

    def test_rejects_malformed_and_dangerous_urls(self):
        for url in (
            "",
            "   ",
            "not a url",
            "javascript:alert(1)",
            "data:image/png;base64,iVBORw0KGgo=",
            "file:///etc/passwd",
            "ftp://example.com/a.jpg",
            "https://user:pass@example.com/a.jpg",
            "https:// example.com/a.jpg",
            "https://example.com/a.jpg\nX-Injected: 1",
            "https://localhost/a.jpg",
            "https://127.0.0.1/a.jpg",
            "https://10.0.0.5/a.jpg",
            "https://192.168.1.1/secret.jpg",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/a.jpg",
            "https://nodot/a.jpg",
            "https://example.com/" + "a" * 3000,
        ):
            self.assertFalse(is_safe_image_url(url), url)

    def test_rejects_non_string_input(self):
        self.assertFalse(is_safe_image_url(None))
        self.assertFalse(is_safe_image_url(12345))


# ---------------------------------------------------------------------------
# Provider parsing
# ---------------------------------------------------------------------------


class TestProviders(unittest.IsolatedAsyncioTestCase):
    async def test_wikipedia_is_preferred_and_parsed(self):
        session = FakeSession({"en.wikipedia.org": WIKI_PAYLOAD})
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertTrue(response.has_results)
        best = response.best
        self.assertEqual(best.title, "Red panda")
        self.assertTrue(best.image_url.startswith("https://upload.wikimedia.org/"))
        self.assertEqual(best.source_url, "https://en.wikipedia.org/wiki/Red_panda")
        self.assertEqual(best.provider, "Wikipedia")

    async def test_commons_tops_up_and_filters_noise(self):
        session = FakeSession(
            {"en.wikipedia.org": WIKI_PAYLOAD, "commons.wikimedia.org": COMMONS_PAYLOAD}
        )
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=3)
        urls = [item.image_url for item in response.results]
        self.assertGreaterEqual(len(urls), 2)
        # The logo file must never be offered as "a picture of a red panda".
        self.assertFalse(any("Logo.png" in url for url in urls))
        # Commons results use the bounded thumbnail, not the huge original.
        self.assertTrue(any("1280px" in url for url in urls))
        commons = next(item for item in response.results if item.provider == "Wikimedia Commons")
        self.assertEqual(commons.attribution, "Jane Doe")

    async def test_openverse_used_when_earlier_providers_are_empty(self):
        session = FakeSession({"api.openverse.org": OPENVERSE_PAYLOAD})
        service = make_service()
        with use_session(session):
            response = await service.search("red panda cub", limit=1)
        self.assertTrue(response.has_results)
        self.assertEqual(response.best.provider, "Openverse")
        self.assertEqual(
            response.best.source_url, "https://www.flickr.com/photos/example/123"
        )

    async def test_google_provider_runs_first_when_configured(self):
        session = FakeSession(
            {"googleapis.com": GOOGLE_PAYLOAD, "en.wikipedia.org": WIKI_PAYLOAD}
        )
        service = make_service(api_key="k", engine_id="cx")
        self.assertTrue(service.has_api_provider)
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertEqual(response.best.provider, "Google")
        self.assertIn("googleapis.com", session.requests[0])

    async def test_keyless_by_default(self):
        service = make_service()
        self.assertFalse(service.has_api_provider)
        self.assertNotIn("google", service.describe())


# ---------------------------------------------------------------------------
# Failure handling  (§ API failure handling)
# ---------------------------------------------------------------------------


class TestFailureHandling(unittest.IsolatedAsyncioTestCase):
    async def test_provider_outage_falls_through_to_the_next(self):
        session = FakeSession(
            {"commons.wikimedia.org": COMMONS_PAYLOAD}, fail=("en.wikipedia.org",)
        )
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertTrue(response.has_results)

    async def test_all_providers_down_returns_empty_not_an_error(self):
        session = FakeSession(
            {}, fail=("en.wikipedia.org", "commons.wikimedia.org", "api.openverse.org")
        )
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertFalse(response.has_results)
        self.assertEqual(response.query, "red panda")

    async def test_http_error_status_is_handled(self):
        session = FakeSession({"en.wikipedia.org": 503, "commons.wikimedia.org": 429})
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertFalse(response.has_results)

    async def test_malformed_payloads_never_raise(self):
        for payload in ({}, {"query": None}, {"query": {"pages": "nope"}}, []):
            session = FakeSession({"en.wikipedia.org": payload})
            service = make_service()
            with use_session(session):
                response = await service.search("red panda", limit=1)
            self.assertFalse(response.has_results)

    async def test_timeout_is_contained(self):
        class HangingSession(FakeSession):
            def get(self, url, params=None):
                raise asyncio.TimeoutError()

        service = make_service()
        with use_session(HangingSession({})):
            response = await service.search("red panda", limit=1)
        self.assertFalse(response.has_results)

    async def test_unsafe_provider_urls_are_dropped(self):
        poisoned = {
            "query": {
                "pages": [
                    {
                        "title": "Evil",
                        "fullurl": "https://en.wikipedia.org/wiki/Evil",
                        "original": {"source": "javascript:alert(1)"},
                    },
                    {
                        "title": "Internal",
                        "original": {"source": "http://169.254.169.254/meta.jpg"},
                    },
                ]
            }
        }
        session = FakeSession({"en.wikipedia.org": poisoned})
        service = make_service()
        with use_session(session):
            response = await service.search("evil", limit=2)
        self.assertFalse(response.has_results)

    async def test_disabled_service_does_no_work(self):
        session = FakeSession({"en.wikipedia.org": WIKI_PAYLOAD})
        service = make_service(enabled=False)
        with use_session(session):
            response = await service.search("red panda")
        self.assertFalse(response.has_results)
        self.assertEqual(session.requests, [])

    async def test_empty_query_does_not_call_providers(self):
        session = FakeSession({"en.wikipedia.org": WIKI_PAYLOAD})
        service = make_service()
        with use_session(session):
            response = await service.search("   ")
        self.assertFalse(response.has_results)
        self.assertEqual(session.requests, [])


# ---------------------------------------------------------------------------
# Embeddability probing  (§ invalid image URL)
# ---------------------------------------------------------------------------


class TestProbing(unittest.IsolatedAsyncioTestCase):
    async def test_extension_free_url_is_probed_and_accepted(self):
        payload = {
            "items": [
                {
                    "title": "Mystery",
                    "link": "https://example.org/render?id=9",
                    "image": {"contextLink": "https://example.org/page"},
                }
            ]
        }
        session = FakeSession(
            {"googleapis.com": payload},
            head=FakeResponse(headers={"Content-Type": "image/jpeg", "Content-Length": "4096"}),
        )
        service = make_service(api_key="k", engine_id="cx")
        with use_session(session):
            response = await service.search("mystery", limit=1)
        self.assertTrue(response.has_results)
        self.assertEqual(response.best.mime, "image/jpeg")
        self.assertEqual(len(session.head_requests), 1)

    async def test_non_image_content_type_is_rejected(self):
        payload = {
            "items": [
                {"title": "HTML", "link": "https://example.org/page", "image": {}}
            ]
        }
        session = FakeSession(
            {"googleapis.com": payload},
            head=FakeResponse(headers={"Content-Type": "text/html"}),
        )
        service = make_service(api_key="k", engine_id="cx")
        with use_session(session):
            response = await service.search("html", limit=1)
        self.assertFalse(response.has_results)

    async def test_declared_mime_skips_the_probe_entirely(self):
        session = FakeSession({"commons.wikimedia.org": COMMONS_PAYLOAD})
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertTrue(response.has_results)
        self.assertEqual(session.head_requests, [])

    async def test_failed_probes_are_not_retried(self):
        payload = {
            "items": [
                {"title": f"n{i}", "link": f"https://example.org/r{i}", "image": {}}
                for i in range(8)
            ]
        }
        session = FakeSession({"googleapis.com": payload})  # head() raises
        service = make_service(api_key="k", engine_id="cx")
        with use_session(session):
            response = await service.search("many", limit=5)
        self.assertFalse(response.has_results)
        # Bounded: a flood of unprobeable results costs at most a few HEADs.
        self.assertLessEqual(len(session.head_requests), 3)


# ---------------------------------------------------------------------------
# Caching and deduplication  (§ memory efficiency / dedup)
# ---------------------------------------------------------------------------


class TestCachingAndDedup(unittest.IsolatedAsyncioTestCase):
    async def test_second_identical_query_is_served_from_cache(self):
        session = FakeSession({"en.wikipedia.org": WIKI_PAYLOAD})
        service = make_service()
        with use_session(session):
            first = await service.search("red panda", limit=1)
            second = await service.search("Red  Panda", limit=1)
        self.assertTrue(first.has_results)
        self.assertTrue(second.cached)
        self.assertEqual(len(session.requests), 1)

    async def test_concurrent_identical_queries_share_one_lookup(self):
        session = FakeSession({"en.wikipedia.org": WIKI_PAYLOAD})
        service = make_service()
        with use_session(session):
            results = await asyncio.gather(
                *(service.search("red panda", limit=1) for _ in range(5))
            )
        self.assertTrue(all(item.has_results for item in results))
        self.assertEqual(len(session.requests), 1)

    async def test_results_are_deduplicated_by_url(self):
        duplicate = {
            "query": {
                "pages": [
                    {
                        "title": "A",
                        "original": {"source": "https://cdn.example.com/x.jpg"},
                    },
                    {
                        "title": "B",
                        "original": {"source": "https://cdn.example.com/x.jpg/"},
                    },
                ]
            }
        }
        session = FakeSession({"en.wikipedia.org": duplicate})
        service = make_service()
        with use_session(session):
            response = await service.search("dupes", limit=4)
        self.assertEqual(len(response.results), 1)

    async def test_result_count_is_capped(self):
        many = {
            "results": [
                {
                    "title": f"img {i}",
                    "url": f"https://cdn.example.com/{i}.jpg",
                    "foreign_landing_url": "https://example.com/p",
                }
                for i in range(40)
            ]
        }
        session = FakeSession({"api.openverse.org": many})
        service = make_service()
        with use_session(session):
            response = await service.search("lots", limit=99)
        self.assertLessEqual(len(response.results), 5)

    async def test_cache_is_bounded(self):
        service = make_service(cache_size=8)
        for index in range(40):
            service._cache.set(f"q{index}", ImageResponse(query=f"q{index}"))
        self.assertLessEqual(len(service._cache), 8)


# ---------------------------------------------------------------------------
# Discord presentation
# ---------------------------------------------------------------------------


class TestImageEmbeds(unittest.TestCase):
    def _response(self, count=3):
        return ImageResponse(
            query="red panda",
            results=[
                ImageResult(
                    title=f"Red panda {index}",
                    image_url=f"https://cdn.example.com/{index}.jpg",
                    source_url="https://example.com/page",
                    provider="Wikimedia Commons",
                    attribution="Jane Doe",
                )
                for index in range(count)
            ],
        )

    def test_single_image_embed(self):
        embeds = build_image_embeds(self._response(1), 1)
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "🖼️ red panda")
        self.assertEqual(embeds[0].image.url, "https://cdn.example.com/0.jpg")
        self.assertIn("example.com", embeds[0].description)

    def test_multiple_images_are_limited(self):
        self.assertEqual(len(build_image_embeds(self._response(3), 3)), 3)
        # Never spam a channel, whatever was asked for.
        self.assertLessEqual(len(build_image_embeds(self._response(5), 99)), 4)

    def test_embed_uses_remote_url_without_downloading(self):
        embeds = build_image_embeds(self._response(1), 1)
        self.assertTrue(embeds[0].image.url.startswith("https://"))

    def test_source_is_optional(self):
        response = ImageResponse(
            query="thing",
            results=[ImageResult(title="t", image_url="https://cdn.example.com/a.jpg")],
        )
        embeds = build_image_embeds(response, 1)
        self.assertEqual(len(embeds), 1)


class TestHedgedProviders(unittest.IsolatedAsyncioTestCase):
    """A slow provider must not serialise the whole lookup.

    The preferred source gets a short head start; if it stalls, the next one
    is started in parallel. These tests pin that behaviour without relying on
    wall-clock timing: the hedge delay is patched to zero.
    """

    async def asyncSetUp(self):
        self._delay = provider_module.HEDGE_DELAY

    async def asyncTearDown(self):
        provider_module.HEDGE_DELAY = self._delay

    def _hedge(self) -> None:
        # A tiny head start instead of the production 0.35s: the fakes resolve
        # immediately, so one event-loop turn is all the winner needs.
        provider_module.HEDGE_DELAY = 0.01

    async def test_a_stalled_provider_does_not_block_a_working_one(self):
        release = asyncio.Event()

        class StallingSession(FakeSession):
            async def _get(self, url):  # type: ignore[override]
                if "wikipedia.org" in url:
                    await release.wait()
                return await super()._get(url)

        self._hedge()
        session = StallingSession({"api.openverse.org": OPENVERSE_PAYLOAD})
        service = make_service()
        try:
            with use_session(session):
                response = await asyncio.wait_for(
                    service.search("red panda cub", limit=1), timeout=2.0
                )
        finally:
            release.set()
        self.assertTrue(response.has_results)
        self.assertEqual(response.best.provider, "Openverse")

    async def test_the_preferred_provider_wins_when_it_is_fast(self):
        self._hedge()
        session = FakeSession(
            {"en.wikipedia.org": WIKI_PAYLOAD, "api.openverse.org": OPENVERSE_PAYLOAD}
        )
        service = make_service()
        with use_session(session):
            response = await service.search("red panda", limit=1)
        self.assertEqual(response.best.provider, "Wikipedia")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
