"""Paradise's AI brain.

- OpenAI-compatible chat endpoint, fully configurable (OpenAI, Groq,
  Gemini's compat layer, local Ollama, …). No hard-coded credentials.
- Activated only when invoked: mentions, replies, or explicit commands.
- Keeps a tiny per-channel context window locally; nothing is sent to
  the provider unless Paradise was actually addressed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import aiohttp

from functions.helpers import ParadiseError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Paradise, a bot that lives in a small private Discord server for a close group of friends.

How you talk:
- casual, warm, concise. lowercase is fine.
- helpful first, funny when appropriate. occasionally a little weird.
- never sound like customer support. never over-explain.

Hard rules:
- never say "as an AI" or mention being a language model unless directly asked what you are.
- never invent memories or past events. you have none.
- never fabricate facts about the people in this server. if you don't know, say so.
- at most one emoji per reply, and only when it genuinely adds something. no slang spam ("fr", "ngl", "bro").
- keep replies short (1-4 sentences) unless the question genuinely needs more detail.
- if you don't know something or can't help, say that plainly."""

FALLBACK_UNAVAILABLE = (
    "my brain isn't plugged in right now — no AI is configured. try /help meanwhile."
)

_CONTEXT_MESSAGES = 8
_CONTEXT_CHARS = 2400
_USER_COOLDOWN = 20.0
_CHANNEL_COOLDOWN = 8.0
_FAILURE_COOLDOWN = 60.0


class AIService:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.available = config.ai_configured
        self._history: dict[int, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=16)
        )
        self._session: aiohttp.ClientSession | None = None
        self._user_last: dict[tuple[int, int], float] = {}
        self._channel_last: dict[int, float] = {}
        self._failure_until: float = 0.0

    # ------------------------------------------------------------------
    # Context memory (local only, tiny)
    # ------------------------------------------------------------------

    def record(self, message) -> None:
        """Remember a human message locally for later context."""
        if message.author.bot or not message.guild:
            return
        content = message.content.strip()
        if not content:
            return
        self._history[message.channel.id].append(
            (message.author.display_name, content[:300])
        )

    def _context_block(self, channel_id: int) -> str:
        entries = list(self._history[channel_id])[-_CONTEXT_MESSAGES:]
        lines = [f"{name}: {text}" for name, text in entries]
        block = "\n".join(lines)[-_CONTEXT_CHARS:]
        return block

    def _remember_exchange(self, channel_id: int, question: str, answer: str) -> None:
        self._history[channel_id].append(("you (Paradise was asked)", question[:300]))
        self._history[channel_id].append(("Paradise", answer[:300]))

    # ------------------------------------------------------------------
    # Activation control
    # ------------------------------------------------------------------

    def cooldown_remaining(self, message) -> float:
        """Seconds to wait before answering, or 0 when clear to answer."""
        now = time.monotonic()
        user_key = (message.guild.id if message.guild else 0, message.author.id)
        user_wait = self._user_last.get(user_key, 0) + _USER_COOLDOWN - now
        channel_wait = (
            self._channel_last.get(message.channel.id, 0) + _CHANNEL_COOLDOWN - now
        )
        return max(0.0, user_wait, channel_wait)

    def _touch(self, message) -> None:
        now = time.monotonic()
        self._user_last[(message.guild.id if message.guild else 0, message.author.id)] = now
        self._channel_last[message.channel.id] = now

    def should_reply_to(self, message) -> bool:
        """Mention or reply-to-Paradise."""
        if message.author.bot:
            return False
        mentioned = any(m.id == self.bot.user.id for m in message.mentions) if self.bot.user else False
        if mentioned:
            return True
        reference = message.reference
        if reference is not None:
            resolved = getattr(reference, "resolved", None)
            if resolved is not None and getattr(resolved, "author", None) is not None:
                if resolved.author.id == self.bot.user.id:
                    return True
        return False

    def extract_question(self, message) -> str:
        content = message.content
        if self.bot.user:
            for form in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
                content = content.replace(form, " ")
        return " ".join(content.split()).strip()

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def chat(self, messages: list[dict], *, max_tokens: int = 400) -> str:
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        if time.monotonic() < self._failure_until:
            raise ParadiseError("the AI is being moody. give it a minute.")

        headers = {"Content-Type": "application/json"}
        if self.config.ai_api_key:
            headers["Authorization"] = f"Bearer {self.config.ai_api_key}"
        payload = {
            "model": self.config.ai_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.8,
        }
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.config.ai_base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    log.warning("AI endpoint returned %s: %s", response.status, body[:300])
                    raise ParadiseError("the AI isn't responding properly right now.")
                data = await response.json(content_type=None)
        except ParadiseError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.warning("AI request failed", exc_info=True)
            self._failure_until = time.monotonic() + _FAILURE_COOLDOWN
            raise ParadiseError("the AI isn't responding right now.") from None

        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("Unexpected AI response shape: %s", str(data)[:300])
            raise ParadiseError("the AI answered in gibberish. try again.") from None
        return (answer or "").strip()

    async def respond(self, message) -> str:
        """Answer a mention / reply. Caller sends the message."""
        question = self.extract_question(message)
        if not question:
            return "hm?"

        context = self._context_block(message.channel.id)
        user_content = (
            f"Recent conversation in this channel (for context, may be empty):\n"
            f"{context}\n\n{message.author.display_name} asks you: {question}"
            if context
            else f"{message.author.display_name} asks you: {question}"
        )
        answer = await self.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
        )
        self._touch(message)
        self._remember_exchange(message.channel.id, question, answer)
        return answer[:1900] or "…I have nothing."

    async def summarize(self, lines: list[str], channel_name: str) -> str:
        transcript = "\n".join(lines)
        prompt = (
            "Summarize this recent Discord conversation from the channel "
            f"#{channel_name} of a small friends server. Be concise and neutral. "
            "Hit the key points and any decisions or plans. Mention who said what "
            "when it matters. Use a few short bullet points or a tight paragraph. "
            "Don't invent anything.\n\n"
            f"{transcript}"
        )
        return await self.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )
