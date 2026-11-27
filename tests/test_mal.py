"""Targeted tests for the MyAnimeList integration.

Offline by design: no network, no Supabase, no Discord. Uses fakes and
mocks at every seam.
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet

from functions.mal.api import MalClient, MalNotFound, MalUnauthorized
from functions.mal.callback import MalCallbackServer
from functions.mal.encryption import MalEncryption
from functions.mal.oauth import (
    MalOAuth,
    generate_code_challenge,
    generate_code_verifier,
    generate_state,
)
from functions.mal.store import MalService, MalStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSupabaseTable:
    """In-memory stand-in for a Supabase table builder."""

    def __init__(self, rows=None):
        self._rows = dict(rows or {})
        self._filters = []
        self._upsert_rows = []
        self._on_conflict = None
        self._op = None  # "select", "upsert", or "delete"

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def select(self, columns="*"):
        self._op = "select"
        return self

    def upsert(self, rows, on_conflict=None):
        self._op = "upsert"
        self._upsert_rows = [rows] if isinstance(rows, dict) else rows
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    async def execute(self):
        if self._op == "upsert" and self._upsert_rows:
            for row in self._upsert_rows:
                key = row.get("discord_user_id")
                if self._on_conflict == "discord_user_id" and key in self._rows:
                    self._rows[key].update(row)
                else:
                    self._rows[key] = row
            self._upsert_rows = []
        elif self._op == "delete" and self._filters:
            for col, val in self._filters:
                self._rows = {
                    k: v for k, v in self._rows.items() if v.get(col) != val
                }

        results = list(self._rows.values())
        for col, val in self._filters:
            results = [r for r in results if r.get(col) == val]
        return MagicMock(data=results)


class FakeSupabaseClient:
    """In-memory stand-in for supabase.AsyncClient."""

    def __init__(self):
        self._tables = {}

    def table(self, name):
        if name not in self._tables:
            self._tables[name] = FakeSupabaseTable()
        return self._tables[name]


class FakeAiohttpResponse:
    """Fake aiohttp client response (async context manager)."""

    def __init__(self, status=200, json_data=None, text_data="", headers=None):
        self.status = status
        self._json = json_data or {}
        self._text = text_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._json


class FakeAiohttpSession:
    """Fake aiohttp ClientSession.

    ``request``/``post``/``get`` are regular methods (like real aiohttp)
    that return an async context manager directly.
    """

    def __init__(self, responses=None):
        self._responses = responses or []
        self.calls = []
        self.closed = False

    def _make_response(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return FakeAiohttpResponse(
            status=200, json_data={"id": 12345, "name": "TestUser"}
        )

    def request(self, method, url, **kwargs):
        return self._make_response(method, url, **kwargs)

    def post(self, url, **kwargs):
        return self._make_response("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._make_response("GET", url, **kwargs)

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEncryption(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        key = Fernet.generate_key().decode()
        enc = MalEncryption(key)
        plaintext = "super-secret-access-token"
        ciphertext = enc.encrypt(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(enc.decrypt(ciphertext), plaintext)

    def test_invalid_key_raises(self):
        with self.assertRaises(Exception):
            MalEncryption("not-a-valid-key")

    def test_wrong_key_decrypt_fails(self):
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        enc1 = MalEncryption(key1)
        enc2 = MalEncryption(key2)
        ct = enc1.encrypt("secret")
        with self.assertRaises(Exception):
            enc2.decrypt(ct)


class TestPKCE(unittest.TestCase):
    def test_code_verifier_length(self):
        verifier = generate_code_verifier()
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_code_challenge_deterministic(self):
        verifier = "test_verifier_12345"
        self.assertEqual(generate_code_challenge(verifier), generate_code_challenge(verifier))
        self.assertNotIn("=", generate_code_challenge(verifier))

    def test_state_unique(self):
        self.assertNotEqual(generate_state(), generate_state())


class TestOAuthFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = FakeAiohttpSession()
        self.oauth = MalOAuth(
            client_id="test_client_id",
            client_secret="test_client_secret",
            callback_url="https://example.com/mal/oauth/callback",
            session=self.session,
        )

    def test_start_flow(self):
        url, state = self.oauth.start_flow(discord_user_id=987654321)
        self.assertIn("myanimelist.net/v1/oauth2/authorize", url)
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=test_client_id", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("state=" + state, url)
        self.assertIn("code_challenge=", url)
        self.assertIn(987654321, self.oauth._flows)

    def test_consume_flow_valid(self):
        url, state = self.oauth.start_flow(discord_user_id=987654321)
        verifier = self.oauth.consume_flow(987654321, state)
        self.assertIsNotNone(verifier)
        self.assertNotIn(987654321, self.oauth._flows)

    def test_consume_flow_wrong_state(self):
        self.oauth.start_flow(discord_user_id=987654321)
        self.assertIsNone(self.oauth.consume_flow(987654321, "wrong_state"))

    def test_consume_flow_expired(self):
        self.oauth.start_flow(discord_user_id=987654321)
        flow = self.oauth._flows[987654321]
        flow.created_at = time.time() - 700
        self.assertIsNone(self.oauth.consume_flow(987654321, flow.state))

    async def test_exchange_code_success(self):
        self.session._responses = [
            FakeAiohttpResponse(
                status=200,
                json_data={
                    "access_token": "access_123",
                    "refresh_token": "refresh_456",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        ]
        pair = await self.oauth.exchange_code("auth_code", "verifier")
        self.assertIsNotNone(pair)
        self.assertEqual(pair.access_token, "access_123")
        self.assertEqual(pair.refresh_token, "refresh_456")
        self.assertFalse(pair.is_expired)

    async def test_exchange_code_failure(self):
        self.session._responses = [FakeAiohttpResponse(status=400, text_data="invalid_grant")]
        self.assertIsNone(await self.oauth.exchange_code("bad_code", "verifier"))

    async def test_refresh_tokens_success(self):
        self.session._responses = [
            FakeAiohttpResponse(
                status=200,
                json_data={
                    "access_token": "new_access_789",
                    "refresh_token": "new_refresh_012",
                    "expires_in": 3600,
                },
            )
        ]
        pair = await self.oauth.refresh_tokens("old_refresh")
        self.assertIsNotNone(pair)
        self.assertEqual(pair.access_token, "new_access_789")

    async def test_revoke_token(self):
        self.session._responses = [FakeAiohttpResponse(status=200)]
        self.assertTrue(await self.oauth.revoke_token("some_token"))


class TestMalStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = Fernet.generate_key().decode()
        self.encryption = MalEncryption(key)
        self.store = MalStore(
            url="https://test.supabase.co",
            key="secret_key",
            encryption=self.encryption,
        )
        self.fake = FakeSupabaseClient()

        async def _mock_client(_self):
            return self.fake

        self.store._client = types.MethodType(_mock_client, self.store)

    async def test_store_and_get_tokens(self):
        ok = await self.store.store_tokens(
            discord_user_id=555,
            mal_user_id=12345,
            access_token="at",
            refresh_token="rt",
            expires_in=3600,
        )
        self.assertTrue(ok)
        tokens = await self.store.get_tokens(555)
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens["mal_user_id"], 12345)
        self.assertEqual(tokens["access_token"], "at")
        self.assertEqual(tokens["refresh_token"], "rt")

    async def test_get_tokens_nonexistent(self):
        self.assertIsNone(await self.store.get_tokens(999999))

    async def test_delete_tokens(self):
        await self.store.store_tokens(
            discord_user_id=555,
            mal_user_id=12345,
            access_token="at",
            refresh_token="rt",
            expires_in=3600,
        )
        self.assertTrue(await self.store.delete_tokens(555))
        self.assertIsNone(await self.store.get_tokens(555))

    async def test_has_tokens(self):
        self.assertFalse(await self.store.has_tokens(555))
        await self.store.store_tokens(
            discord_user_id=555,
            mal_user_id=12345,
            access_token="at",
            refresh_token="rt",
            expires_in=3600,
        )
        self.assertTrue(await self.store.has_tokens(555))


class TestMalService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = Fernet.generate_key().decode()
        self.encryption = MalEncryption(key)
        self.store = MalStore(
            url="https://test.supabase.co",
            key="secret_key",
            encryption=self.encryption,
        )
        self.fake = FakeSupabaseClient()

        async def _mock_client(_self):
            return self.fake

        self.store._client = types.MethodType(_mock_client, self.store)
        self.session = FakeAiohttpSession()
        self.service = MalService(
            client_id="cid",
            client_secret="csecret",
            callback_url="https://cb.example.com/mal/oauth/callback",
            encryption=self.encryption,
            store=self.store,
            session=self.session,
        )

    async def test_start_connect_flow(self):
        url, state = self.service.start_connect_flow(discord_user_id=42)
        self.assertIn("myanimelist.net", url)
        self.assertIsInstance(state, str)

    async def test_get_valid_access_token_no_tokens(self):
        self.assertIsNone(await self.service.get_valid_access_token(discord_user_id=999))

    async def test_exchange_and_store(self):
        self.session._responses = [
            FakeAiohttpResponse(
                status=200,
                json_data={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            ),
            FakeAiohttpResponse(status=200, json_data={"id": 12345, "name": "TestUser"}),
        ]
        mal_user_id = await self.service.exchange_and_store(
            discord_user_id=42, code="auth_code", code_verifier="verifier"
        )
        self.assertEqual(mal_user_id, 12345)

    async def test_get_valid_access_token_refresh(self):
        await self.store.store_tokens(
            discord_user_id=42,
            mal_user_id=12345,
            access_token="old_at",
            refresh_token="old_rt",
            expires_in=1,
        )
        self.session._responses = [
            FakeAiohttpResponse(
                status=200,
                json_data={"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600},
            )
        ]
        token = await self.service.get_valid_access_token(discord_user_id=42)
        self.assertEqual(token, "new_at")

    async def test_disconnect(self):
        await self.store.store_tokens(
            discord_user_id=42,
            mal_user_id=12345,
            access_token="at",
            refresh_token="rt",
            expires_in=3600,
        )
        self.session._responses = [
            FakeAiohttpResponse(status=200),
            FakeAiohttpResponse(status=200),
        ]
        self.assertTrue(await self.service.disconnect(discord_user_id=42, revoke=True))
        self.assertFalse(await self.store.has_tokens(42))


class TestMalClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeAiohttpSession()
        self.client = MalClient(client_id="test_cid", session=self.session)

    async def test_search_anime(self):
        self.session._responses = [
            FakeAiohttpResponse(
                status=200,
                json_data={
                    "data": [
                        {
                            "node": {
                                "id": 1,
                                "title": "Test Anime",
                                "main_picture": {"medium": "https://example.com/img.jpg"},
                                "synopsis": "A test anime.",
                                "num_episodes": 12,
                                "media_type": "TV",
                                "status": "finished_airing",
                                "mean": 8.5,
                                "popularity": 100,
                            }
                        }
                    ],
                    "paging": {},
                },
            )
        ]
        result = await self.client.search_anime("test", limit=5)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["data"]), 1)

    async def test_get_anime_not_found(self):
        self.session._responses = [FakeAiohttpResponse(status=404)]
        with self.assertRaises(MalNotFound):
            await self.client.get_anime(99999)

    async def test_unauthorized_raises(self):
        self.session._responses = [FakeAiohttpResponse(status=401)]
        with self.assertRaises(MalUnauthorized):
            await self.client.get_user("bad_token")

    async def test_rate_limited_retry(self):
        self.session._responses = [
            FakeAiohttpResponse(status=429, headers={"Retry-After": "0"}, text_data="rate limited"),
            FakeAiohttpResponse(status=200, json_data={"data": [], "paging": {}}),
        ]
        result = await self.client.search_anime("test")
        self.assertIsNotNone(result)


class TestCallbackServer(unittest.TestCase):
    def test_callback_url(self):
        service = MagicMock()
        service.oauth._flows = {}
        server = MalCallbackServer(service, host="0.0.0.0", port=8765)
        self.assertEqual(server.callback_url, "http://0.0.0.0:8765/mal/oauth/callback")


if __name__ == "__main__":
    unittest.main()
