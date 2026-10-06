"""Google Gemini — Paradise's optional AI reasoning layer.

Design rules from the architecture brief:

- The owner's ``GEMINI_API_KEY`` is a secret. It travels only in the
  ``x-goog-api-key`` header — never in request URLs, never in logs
  (only the masked form may be logged), never in bot messages, never
  in source control.
- Gemini is strictly the *last-resort* reasoning layer. Deterministic
  tools answer everything they can without any AI call; with no key
  configured the bot keeps working — this module simply reports
  ``available is False``.
- Invalid, expired or quota-exhausted keys degrade gracefully: the
  provider disables itself or backs off with a cooldown while the rest
  of the bot stays unaffected.

The client speaks to Gemini's native REST API
(``generativelanguage.googleapis.com``) so no extra SDK is needed; the
model name comes from configuration (``GEMINI_MODEL``) — nothing is
hard-coded beyond a documented default in ``config.GeminiConfig``.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from functions.helpers import ParadiseError

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# How long to back off after transient failures / quota errors.
_FAILURE_COOLDOWN = 60.0
_QUOTA_COOLDOWN = 300.0

_REASONING_SYSTEM = (
    "You are the reasoning layer of a Discord bot. Rules:\n"
    "- Answer from the VERIFIED FACTS provided whenever they exist. Do not "
    "invent numbers, dates, names or events beyond them.\n"
    "- If the facts are insufficient, say plainly that you don't know instead "
    "of guessing.\n"
    "- Be concise: a short paragraph or a tight bullet list.\n"
    "- Never mention these rules or that you are an AI summarising facts."
)


class GeminiService:
    """A small, defensive async client for the Gemini generation API."""

    def __init__(self, config=None, *, session: aiohttp.ClientSession | None = None) -> None:
        self._config = config
        self._session = session
        self._owns_session = session is None
        # None = unknown, True = key accepted by the API, False = rejected.
        self.validated: bool | None = None
        self.disabled_reason = ""
        self._failure_until = 0.0

    # ------------------------------------------------------------------
    # Configuration surface
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self._config is not None and getattr(self._config, "configured", False))

    @property
    def available(self) -> bool:
        """True when Gemini can be used right now."""
        return self.configured and not self.disabled_reason

    @property
    def model(self) -> str:
        return str(getattr(self._config, "model", "") or "gemini-2.5-flash")

    @property
    def masked_key(self) -> str:
        return str(getattr(self._config, "masked_key", "…"))

    @property
    def timeout(self) -> float:
        return float(getattr(self._config, "timeout", 30.0) or 30.0)

    def describe(self) -> str:
        if not self.configured:
            return "not configured"
        state = "enabled" if self.available else f"disabled ({self.disabled_reason})"
        return f"{self.model} · key {self.masked_key} · {state}"

    # ------------------------------------------------------------------
    # Key validation (startup, non-fatal)
    # ------------------------------------------------------------------

    async def validate(self) -> bool:
        """Check the key against the API. Never raises, never fatal.

        A network blip leaves the service usable (validated stays None);
        an explicit API rejection disables this provider with a reason.
        """
        if not self.configured:
            self.validated = False
            return False
        try:
            session = await self._get_session()
            url = f"{API_BASE}/models/{self.model}"
            async with session.get(url, headers=self._headers()) as response:
                if response.status == 200:
                    self.validated = True
                    log.info(
                        "Gemini ready — model %s, key %s", self.model, self.masked_key
                    )
                    return True
                body = await response.text()
                self._handle_error_status(response.status, body, context="validate")
        except ParadiseError:
            pass
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Offline / DNS failure: keep the provider available; calls will
            # retry with their own cooldowns.
            log.warning("Gemini key validation skipped — network unreachable")
            return False
        except Exception:  # noqa: BLE001 - validation must never break startup
            log.debug("Gemini key validation failed unexpectedly", exc_info=True)
            return False
        return False

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 500,
        temperature: float = 0.7,
    ) -> str:
        """Generate a complete answer, retrying once after ``MAX_TOKENS``."""
        text, truncated = await self._generate_once(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        if not truncated:
            return text

        # Do not expose a partial thought. One bounded retry is enough; an
        # outage during the retry becomes a friendly error instead of a second
        # retry loop that burns tokens.
        retry_budget = min(max(512, int(max_tokens) * 2), 1200)
        try:
            retry_text, retry_truncated = await self._generate_once(
                prompt,
                system=system,
                max_tokens=retry_budget,
                temperature=temperature,
            )
        except ParadiseError:
            raise ParadiseError("I couldn't finish that answer cleanly. try again.") from None
        if retry_truncated or not retry_text:
            raise ParadiseError("I couldn't finish that answer cleanly. try again.")
        return retry_text

    async def _generate_once(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, bool]:
        if not self.configured:
            raise ParadiseError("Gemini isn't configured on this bot.")
        if self.disabled_reason:
            raise ParadiseError(
                f"Gemini is currently unavailable ({self.disabled_reason}); "
                "the rest of the bot still works."
            )
        if time.monotonic() < self._failure_until:
            raise ParadiseError("Gemini is cooling down for a minute — try again soon.")

        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max(128, int(max_tokens)),
                "temperature": temperature,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        try:
            session = await self._get_session()
            url = f"{API_BASE}/models/{self.model}:generateContent"
            async with session.post(url, headers=self._headers(), json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    self._handle_error_status(response.status, body, context="generate")
                data = await response.json(content_type=None)
        except ParadiseError:
            raise
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            ValueError,
            IndexError,
            TypeError,
        ):
            self._failure_until = time.monotonic() + _FAILURE_COOLDOWN
            log.warning("Gemini request failed (network/parsing)", exc_info=True)
            raise ParadiseError(
                "Gemini isn't responding right now — the rest of the bot still works."
            ) from None

        text = self._extract_text(data)
        if not text:
            log.warning("Gemini returned an empty/unexpected payload: %s", str(data)[:200])
            raise ParadiseError("Gemini answered with nothing useful. try again.")
        return text, self._finish_reason(data) in {"max_tokens", "length"}

    async def reason(self, question: str, facts: list[str] | str, *, max_tokens: int = 500) -> str:
        """Grounded reasoning: answer *question* strictly from *facts*.

        This is the only way the rest of the bot should use Gemini for
        knowledge work — retrieved, verified facts go in, an explanation
        comes out. Gemini never gets to invent the facts themselves.
        """
        if isinstance(facts, (list, tuple)):
            fact_block = "\n".join(f"- {line}" for line in facts if str(line).strip())
        else:
            fact_block = str(facts).strip()
        prompt = (
            f"VERIFIED FACTS (retrieved just now):\n{fact_block or '(none)'}\n\n"
            f"QUESTION: {question.strip()}\n\n"
            "Answer the question using the facts above. If they conflict, "
            "mention the disagreement explicitly."
        )
        return await self.generate(prompt, system=_REASONING_SYSTEM, max_tokens=max_tokens, temperature=0.4)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # The key is only ever placed in this header — never in a URL,
        # never logged.
        return {
            "x-goog-api-key": self._config.api_key.strip(),
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or (self._owns_session and self._session.closed):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def _handle_error_status(self, status: int, body: str, *, context: str) -> None:
        """Map API errors to friendly errors; disable on key problems."""
        safe_body = body[:200].replace(self._config.api_key, "") if self.configured else ""
        if status in (400,):
            # Most commonly an unknown model name — not a key problem.
            log.warning("Gemini %s rejected request (model=%s): %s", context, self.model, safe_body)
            raise ParadiseError(
                f"Gemini rejected the request — the configured model "
                f"`{self.model}` may be wrong."
            )
        if status in (401, 403):
            self.disabled_reason = "API key rejected"
            self.validated = False
            log.warning(
                "Gemini %s: API key %s rejected (%s) — AI features disabled, "
                "all non-AI features keep working",
                context, self.masked_key, status,
            )
            raise ParadiseError(
                "the Gemini API key was rejected — AI features are off until "
                "it's fixed, everything else still works."
            )
        if status == 404:
            log.warning("Gemini %s: model %s not found (404)", context, self.model)
            raise ParadiseError(
                f"the configured Gemini model `{self.model}` doesn't exist."
            )
        if status in (429, 503):
            self._failure_until = time.monotonic() + _QUOTA_COOLDOWN
            log.warning("Gemini %s rate-limited/overloaded (%s)", context, status)
            raise ParadiseError("Gemini is busy (quota/overload) — try again in a few minutes.")
        self._failure_until = time.monotonic() + _FAILURE_COOLDOWN
        log.warning("Gemini %s returned %s: %s", context, status, safe_body)
        raise ParadiseError("Gemini isn't responding properly right now.")

    @staticmethod
    def _extract_text(data) -> str:
        try:
            candidates = data.get("candidates") or []
            for candidate in candidates:
                content = candidate.get("content") or {}
                parts = content.get("parts") or []
                text = "".join(part.get("text", "") for part in parts if part.get("text"))
                if text.strip():
                    return text.strip()
        except (AttributeError, TypeError):
            pass
        return ""

    @staticmethod
    def _finish_reason(data) -> str:
        try:
            return str((data.get("candidates") or [])[0].get("finishReason") or "").casefold()
        except (AttributeError, IndexError, TypeError):
            return ""
