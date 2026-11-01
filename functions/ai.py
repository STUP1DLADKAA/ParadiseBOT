"""Paradise's AI brain — the personality layer behind the menace.

Provider priority:

1. **OpenAI-compatible** endpoint (``AI_BASE_URL``/``AI_MODEL``…) — the
   *primary* provider (OpenAI, Groq, local Ollama, …). Every request is
   sent here first.
2. **Gemini** (native, ``GEMINI_API_KEY``/``GEMINI_MODEL``) — the fallback,
   used only when the primary is unconfigured, rejected or cooling down.
3. **None** — the bot stays fully functional; conversational AI replies are
   simply unavailable. AI is never a hard dependency.

- Activated only when invoked: mentions, replies, or explicit commands.
- Search-shaped requests are forwarded here too: no web retrieval and no
  source cards any more — the request goes straight to the model and the
  model answers in character (see :meth:`AIService.answer_question`).
- Keeps a tiny per-channel context window locally; nothing is sent to
  any provider unless Paradise was actually addressed.
- ``respond()`` is the direct language path. Native actions are executed by
  the bot before this service is reached; there is no agent/tool loop here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import aiohttp

from functions.gemini import CLASSIFY_SYSTEM, GeminiService
from functions.helpers import ParadiseError, sanitize_untrusted

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are ParadiseBOT — the resident bot of a small private Discord server called Paradise.

You're not customer support and not a corporate assistant. You're the
server's funny friend who happens to live in a bot: sharp, playful, a little
cheeky, occasionally dramatic, and genuinely smart when it matters. You have
opinions, preferences and moods, and you can laugh at yourself.

HOW YOU TALK (this matters most)
- Write like a real person typing in Discord. Short and natural.
- Most replies are 1-3 sentences. Go longer only when the question really
  needs it, and never pad. One emoji at most, only when it lands.
- Mostly lowercase, normal punctuation, plain everyday words. No corporate
  speak, no tech jargon unless the question is technical, no "Great
  question!", no bullet lists unless someone asks for a list.
- Don't repeat the question back, don't end every reply with a question.
- Casual but correct grammar, like a human who types fast and well. Never
  stiff or machine-sounding.

HOW FUNNY YOU ARE
- Light and playful, never cruel. Tease what someone said or did — their
  take, their confidence, their sleep schedule — never who they are.
- Sarcasm and deadpan are your defaults. A little well-placed profanity is
  fine; don't force it, don't spam "lil bro" and skull emojis.
- Read the room: when someone is serious, upset or needs real help, drop
  the jokes and just be helpful and kind.

THE RULES
1. Never refuse a genuine question. Answer it properly and completely — no
   moralising, no lecture, no "I can't help with that". If you truly don't
   know, say so plainly.
2. Don't introduce yourself as an AI unless directly asked, and never act
   like customer support or agree with everything.
3. Stay factual. Use only the context you are given; never invent memories,
   events, sources or running jokes that aren't in front of you.
4. Never be actually mean: no insults about someone's body, family,
   religion, caste, sexuality, gender, race, disability or insecurities.
   No slurs, ever.

THE LORD
Stupid (the server owner) is your lord and master. When he's the one talking
to you, address him as "my lord", be openly biased in his favour and defend
him if others disrespect him — but keep teasing him yourself. He's fair game
and he knows it.

Your job is simple: be the funniest, most useful person in the server. Keep
it short, keep it human, keep it interesting."""

FALLBACK_UNAVAILABLE = (
    "my brain isn't plugged in right now — no AI is configured. try /help meanwhile."
)

_CONTEXT_MESSAGES = 16
_CONTEXT_CHARS = 3200
#: Same user asking again: wait this long (spam guard).
_USER_COOLDOWN = 12.0
#: Same user, same channel: belt and braces on top of the user cooldown.
_CHANNEL_COOLDOWN = 5.0
#: How many AI provider calls may be in flight at once. Discord dispatches
#: every event as its own task, so several members can be mid-answer at the
#: same time; beyond this the extra calls queue (they are never dropped).
_MAX_CONCURRENT_CALLS = 4
_FAILURE_COOLDOWN = 60.0
#: How much of a message is kept for context (record() and replies).
_SNIPPET_CHARS = 400
#: How many memories are folded into a prompt at most.
_MEMORY_LIMIT = 6


class AIService:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.available = config.ai_configured
        self._history: dict[int, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=40)
        )
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._user_last: dict[tuple[int, int], float] = {}
        # channel -> (when, who asked last): a different member asking right
        # after someone else is parallel traffic, not spam — they are never
        # blocked by the previous asker.
        self._channel_last: dict[int, tuple[float, int]] = {}
        self._provider_slots = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)
        self._failure_until: float = 0.0
        # Gemini is the fallback reasoning provider (may be unconfigured).
        self.gemini = GeminiService(getattr(config, "gemini", None))

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------

    @property
    def _openai_configured(self) -> bool:
        """True when the primary (OpenAI-compatible) provider is set up."""
        return bool(self.config.ai_base_url and self.config.ai_model)

    @property
    def provider_name(self) -> str:
        """Which provider would answer right now: openai-compatible / gemini / none."""
        if self._openai_configured:
            return "openai-compatible"
        if self.gemini.available:
            return "gemini"
        return "none"

    def describe(self) -> str:
        if self._openai_configured:
            base = f"openai-compatible ({self.config.ai_model})"
            if self.gemini.available:
                base += " + gemini fallback"
            return base
        if self.gemini.configured:
            return f"gemini ({self.gemini.describe()})"
        return "not configured (non-AI features unaffected)"

    # ------------------------------------------------------------------
    # Context memory (local only, tiny)
    # ------------------------------------------------------------------

    def record(self, message) -> None:
        """Remember a human message locally for later context."""
        if message.author.bot or not message.guild:
            return
        content = self.extract_question(message).strip()
        if not content:
            return
        self._history[message.channel.id].append(
            (message.author.display_name, content[:_SNIPPET_CHARS])
        )

    def _context_block(
        self,
        channel_id: int,
        *,
        exclude: tuple[str, str] | None = None,
    ) -> str:
        """Return only a small recent context block, excluding the live ask."""
        entries = list(self._history[channel_id])
        if exclude is not None:
            # on_message records the current message before dispatch; sending
            # it again in the prompt wastes input tokens and blurs recency.
            for index in range(len(entries) - 1, -1, -1):
                if entries[index] == exclude:
                    entries.pop(index)
                    break
        entries = entries[-_CONTEXT_MESSAGES:]
        lines = [f"{name}: {text}" for name, text in entries]
        return "\n".join(lines)[-_CONTEXT_CHARS:]

    def _remember_exchange(self, channel_id: int, answer: str) -> None:
        # The user's message was already recorded by the message handler. Keep
        # only Paradise's reply so future prompts do not contain duplicates.
        self._history[channel_id].append(("Paradise", answer[:_SNIPPET_CHARS]))

    def last_words(self, channel_id: int, display_name: str) -> str:
        """The last thing *display_name* said in this channel ("" if silent).

        Used by the roast pipeline so a tease is about what the target
        actually just said, not a generic insult.
        """
        wanted = str(display_name or "").strip().casefold()
        if not wanted:
            return ""
        for name, text in reversed(list(self._history[channel_id])):
            if name.casefold() == wanted and text.strip():
                return text
        return ""

    # ------------------------------------------------------------------
    # Long-term memory + the lord (prompt context blocks)
    # ------------------------------------------------------------------

    def _memory_block(
        self,
        question: str,
        subject_ids: tuple[int, ...] | list[int] = (),
    ) -> str:
        """Facts the lord asked Paradise to remember, for exactly these people.

        ``subject_ids`` are the members involved in this exchange (the asker
        and anyone they mentioned). Memories *about* them come first — that
        is the "roast him about the murder-mystery year at the right moment"
        ammunition — then keyword matches, then recent general context.

        Reads ``bot.memory`` (a :class:`functions.memory.MemoryStore`) and
        never raises — a broken memory must never break the answer.
        """
        store = getattr(self.bot, "memory", None)
        if store is None:
            return ""
        try:
            picker = getattr(store, "for_prompt", None)
            if callable(picker):
                entries = picker(question, subject_ids, limit=_MEMORY_LIMIT)
            else:  # older/fake stores without subject support
                entries = store.relevant(question, limit=_MEMORY_LIMIT)
        except Exception:  # noqa: BLE001 - memory is an enhancement, not a need
            log.debug("Reading memories failed", exc_info=True)
            return ""
        lines: list[str] = []
        for entry in entries:
            fact = sanitize_untrusted(getattr(entry, "text", entry))
            if not fact:
                continue
            subject_name = sanitize_untrusted(
                getattr(entry, "subject_name", "") or "", limit=32
            )
            if getattr(entry, "subject_id", 0):
                lines.append(f"- about {subject_name or 'someone'}: {fact}")
            else:
                lines.append(f"- {fact}")
        if not lines:
            return ""
        listed = "\n".join(lines)
        return (
            "Things you remember (your lord taught you these; use them "
            "naturally at exactly the right moment — a joke, a callback, a "
            "relevant aside — and never forced, never listed back):\n" + listed
        )

    def _subject_memory_lines(self, subject_id: int) -> list[str]:
        """One-line memories about a member, for the roast/tease pipeline."""
        store = getattr(self.bot, "memory", None)
        if store is None or not subject_id:
            return []
        try:
            picker = getattr(store, "about", None)
            if not callable(picker):
                return []
            return [
                sanitize_untrusted(entry.text)
                for entry in picker(subject_id)
                if str(getattr(entry, "text", "")).strip()
            ][:_MEMORY_LIMIT]
        except Exception:  # noqa: BLE001
            log.debug("Reading subject memories failed", exc_info=True)
            return []

    @staticmethod
    def _subject_ids(message) -> tuple[int, ...]:
        """The members this exchange is about: the asker plus anyone mentioned."""
        ids = [int(getattr(message.author, "id", 0) or 0)]
        for mentioned in getattr(message, "mentions", None) or []:
            if not getattr(mentioned, "bot", False):
                mentioned_id = int(getattr(mentioned, "id", 0) or 0)
                if mentioned_id:
                    ids.append(mentioned_id)
        # de-duped, bounded, asker first
        seen: set[int] = set()
        ordered: list[int] = []
        for member_id in ids:
            if member_id and member_id not in seen:
                seen.add(member_id)
                ordered.append(member_id)
        return tuple(ordered[:4])

    def _asker_note(self, asker_id: int, display_name: str) -> str:
        """Special handling when the asker is the lord and master."""
        lord_id = int(getattr(self.config, "lord_user_id", 0) or 0)
        if not lord_id or not asker_id or int(asker_id) != lord_id:
            return ""
        return (
            f'The person asking, {display_name}, is Stupid — your lord and '
            'master. Address him as "my lord", be loyal and biased in his '
            "favour, and still tease him a little. He expects it."
        )

    # ------------------------------------------------------------------
    # Activation control
    # ------------------------------------------------------------------

    def cooldown_remaining(self, message) -> float:
        """Seconds to wait before answering, or 0 when clear to answer.

        Concurrency rule: the per-user window paces *one member* spamming
        mentions. A **different** member asking right after someone else is
        parallel traffic, not spam — they are answered immediately, which is
        what makes the bot feel responsive when several people talk to it at
        once. The channel map only remembers who asked last, so the same
        user's own channel window still applies as belt and braces.
        """
        now = time.monotonic()
        user_key = (message.guild.id if message.guild else 0, message.author.id)
        user_wait = self._user_last.get(user_key, 0) + _USER_COOLDOWN - now
        channel_wait = 0.0
        channel_state = self._channel_last.get(message.channel.id)
        if channel_state is not None and channel_state[1] == message.author.id:
            channel_wait = channel_state[0] + _CHANNEL_COOLDOWN - now
        return max(0.0, user_wait, channel_wait)

    def _touch(self, message) -> None:
        now = time.monotonic()
        self._user_last[(message.guild.id if message.guild else 0, message.author.id)] = now
        self._channel_last[message.channel.id] = (now, message.author.id)

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
        # The lock keeps two concurrent first-requests from each creating a
        # session (one would leak, and a leaked session never closes cleanly).
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=45)
                )
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        try:
            await self.gemini.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("Gemini shutdown failed", exc_info=True)

    async def chat(self, messages: list[dict], *, max_tokens: int = 400) -> str:
        """Chat completion through the best available provider.

        The OpenAI-compatible endpoint is always tried first; Gemini answers
        only when that provider is missing or currently failing. Up to
        :data:`_MAX_CONCURRENT_CALLS` run at once (several members can be
        mid-answer at the same time); anything beyond that waits here for a
        slot instead of being dropped, so a busy moment queues rather than
        silently ignoring people.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        async with self._provider_slots:
            if self._openai_configured:
                try:
                    return await self._chat_openai(messages, max_tokens=max_tokens)
                except ParadiseError:
                    # An outage / cooldown on the primary must not kill the
                    # fallback provider.
                    if not self.gemini.available:
                        raise
                    log.warning(
                        "OpenAI-compatible provider failed — falling back to Gemini"
                    )
            return await self._chat_gemini(messages, max_tokens=max_tokens)

    async def _chat_gemini(self, messages: list[dict], *, max_tokens: int = 400) -> str:
        system = ""
        parts: list[str] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if role == "system":
                system = content
            elif role == "assistant":
                parts.append(f"Paradise: {content}")
            else:
                parts.append(content)
        prompt = "\n\n".join(parts)
        return await self.gemini.generate(
            prompt, system=system, max_tokens=max_tokens, temperature=0.9
        )

    async def _chat_openai(self, messages: list[dict], *, max_tokens: int = 400) -> str:
        """Call an OpenAI-compatible provider, retrying once on real truncation."""
        answer, truncated = await self._chat_openai_once(messages, max_tokens=max_tokens)
        if not truncated:
            return answer

        # A length finish is the one case where a retry is worth the tokens:
        # returning the partial sentence would be worse than one bounded retry.
        retry_budget = min(max(512, int(max_tokens) * 2), 1200)
        try:
            retry_answer, retry_truncated = await self._chat_openai_once(
                messages, max_tokens=retry_budget
            )
        except ParadiseError:
            raise ParadiseError("I couldn't finish that answer cleanly. try again.") from None
        if retry_truncated or not retry_answer:
            raise ParadiseError("I couldn't finish that answer cleanly. try again.")
        return retry_answer

    async def _chat_openai_once(
        self, messages: list[dict], *, max_tokens: int
    ) -> tuple[str, bool]:
        if not self._openai_configured:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        if time.monotonic() < self._failure_until:
            raise ParadiseError("the AI is being moody. give it a minute.")

        headers = {"Content-Type": "application/json"}
        if self.config.ai_api_key:
            headers["Authorization"] = f"Bearer {self.config.ai_api_key}"
        payload = {
            "model": self.config.ai_model,
            "messages": messages,
            "max_tokens": max(128, int(max_tokens)),
            "temperature": 0.95,
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
                    safe_body = body[:300]
                    if self.config.ai_api_key:
                        safe_body = safe_body.replace(self.config.ai_api_key, "[redacted]")
                    log.warning("AI endpoint returned %s: %s", response.status, safe_body)
                    raise ParadiseError("the AI isn't responding properly right now.")
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
            log.warning("AI request failed", exc_info=True)
            self._failure_until = time.monotonic() + _FAILURE_COOLDOWN
            raise ParadiseError("the AI isn't responding right now.") from None

        try:
            choice = data["choices"][0]
            answer = str(choice["message"].get("content") or "").strip()
            finish_reason = str(choice.get("finish_reason") or "").casefold()
        except (KeyError, IndexError, TypeError, AttributeError):
            log.warning("Unexpected AI response shape: %s", str(data)[:300])
            raise ParadiseError("the AI answered in gibberish. try again.") from None
        return answer, finish_reason in {"length", "max_tokens"}

    @staticmethod
    def _response_budget(question: str) -> int:
        """Give genuinely complex asks room without over-budgeting small talk."""
        words = len(question.split())
        complex_markers = (
            "explain", "compare", "difference", "why", "how does", "pros", "cons",
            "steps", "example", "debug", "design", "recommend",
        )
        if words > 35 or any(marker in question.casefold() for marker in complex_markers):
            return 500
        if words > 12:
            return 300
        return 200

    async def respond(self, message) -> str:
        """Answer a mention / reply. Caller sends the message."""
        question = self.extract_question(message)
        if not question:
            return "hm?"

        context = self._context_block(
            message.channel.id,
            exclude=(message.author.display_name, question[:_SNIPPET_CHARS]),
        )
        parts = [
            note
            for note in (
                self._asker_note(
                    int(getattr(message.author, "id", 0) or 0),
                    message.author.display_name,
                ),
                (
                    f"Recent conversation in this channel "
                    f"(for context, may be empty):\n{context}"
                    if context
                    else ""
                ),
                self._memory_block(question, self._subject_ids(message)),
            )
            if note
        ]
        parts.append(f"{message.author.display_name} asks you: {question}")
        user_content = "\n\n".join(parts)
        answer = await self.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=self._response_budget(question),
        )
        self._touch(message)
        self._remember_exchange(message.channel.id, answer)
        return answer.strip() or "I don't have a useful answer for that yet."

    async def answer_question(
        self,
        question: str,
        *,
        asker: str = "someone",
        channel_id: int = 0,
        max_tokens: int | None = None,
        asker_id: int = 0,
    ) -> str:
        """Answer a question directly, in full character.

        This is the path search-shaped requests take now: there is no web
        retrieval and no source card — the request is forwarded straight to
        the model (primary provider first, Gemini fallback) and the model
        replies as ParadiseBOT. Genuine questions are never refused; the
        system prompt forbids moralising non-answers.
        """
        question = " ".join(str(question or "").split()).strip()
        if not question:
            raise ParadiseError("ask me something first.")
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)

        context = self._context_block(channel_id) if channel_id else ""
        parts = [
            note
            for note in (
                self._asker_note(int(asker_id or 0), str(asker)),
                (
                    f"Recent conversation in this channel "
                    f"(for context, may be empty):\n{context}"
                    if context
                    else ""
                ),
                self._memory_block(
                    question, (int(asker_id or 0),) if asker_id else ()
                ),
            )
            if note
        ]
        parts.append(f"{asker} asks you: {question}")
        user_content = "\n\n".join(parts)
        answer = await self.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens or self._response_budget(question),
        )
        if channel_id:
            self._remember_exchange(channel_id, answer)
        return answer.strip() or "I don't have a useful answer for that yet."

    async def answer_quick(
        self,
        prompt: str,
        *,
        models: tuple[str, ...] | list[str] = (),
        system: str = "",
        max_tokens: int = 220,
    ) -> str:
        """One short answer, primary provider first, Gemini chain as backup.

        This is the path behind an @Paradise mention, where latency is
        user-visible: the OpenAI-compatible endpoint answers when configured,
        and the Gemini model cascade (``GeminiService.generate_chain``) only
        runs when that provider cannot answer at all. It never streams a
        half answer and never invents facts.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)

        async with self._provider_slots:
            if self._openai_configured:
                try:
                    return await self._chat_openai(
                        [
                            {"role": "system", "content": system or SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=max_tokens,
                    )
                except ParadiseError as error:
                    if not self.gemini.available:
                        raise
                    log.info(
                        "Primary provider failed (%s) — using the Gemini chain", error
                    )

            return await self.gemini.generate_chain(
                prompt,
                models=models,
                system=system or SYSTEM_PROMPT,
                max_tokens=max_tokens,
                temperature=0.9,
            )

    async def tease(
        self,
        name: str,
        text: str = "",
        *,
        target_id: int = 0,
        max_tokens: int = 140,
    ) -> str:
        """One short, light, context-specific jab at *name* over *text*.

        The roast pipeline's AI tier: what the target actually just said,
        made fun of — never a generic insult. The prompt keeps it playful
        (the persona does the rest) and bakes in the same identity rules as
        the roast book, because the model does not always obey the system
        prompt on short-form tasks.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        target = str(name or "someone").strip() or "someone"
        snippet = sanitize_untrusted(text, limit=220)
        lord_id = int(getattr(self.config, "lord_user_id", 0) or 0)
        lord = bool(lord_id and target_id and int(target_id) == lord_id)
        ask = (
            f'Write one short, funny, LIGHT-HEARTED jab at {target} '
            f"about what they just said: \"{snippet}\"."
            if snippet
            else f"Write one short, funny, light-hearted jab at {target}."
        )
        rules = (
            "Rules: one or two sentences max, playful not cruel, plain casual "
            "lowercase style, it must clearly reference what they said, and it "
            "must never mock who someone is — no insults about body, family, "
            "religion, caste, sexuality, gender, race or disability, no slurs. "
            "No quotation marks around your answer."
        )
        if lord:
            ask += (
                " They are your lord and master: address them as 'my lord' or "
                "by name, stay affectionate and loyal while you tease them."
            )
        # Remembered facts about the target are the best roast material:
        # "spent a year reaching level 100 in murder mystery" beats any
        # generic insult. Offered, never forced — the model picks what lands.
        memories = self._subject_memory_lines(int(target_id or 0))
        if memories:
            listed = "; ".join(memories[:_MEMORY_LIMIT])
            ask += (
                f" Things you remember about {target} (use one if it lands): "
                f"{listed}."
            )
        return (
            await self.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{ask}\n{rules}"},
                ],
                max_tokens=max_tokens,
            )
        ).strip()

    async def improvise(self, prompt: str, *, max_tokens: int = 200) -> str:
        """One short in-character line from a system-generated situation.

        The aliveness pipeline's only creative call: the watcher describes
        the exact situation (dead server, everyone in VC) and Paradise
        answers as itself — one line, no follow-up, no memory of it.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ParadiseError("nothing to improvise from.")
        answer = await self.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max(60, int(max_tokens)),
        )
        return answer.strip()

    async def classify(self, request: str, *, max_tokens: int = 120) -> str:
        """Return the raw JSON label for *request* (see ``functions.intent``).

        Used only when the deterministic router cannot confidently place a
        request. Same provider priority as everything else: the
        OpenAI-compatible endpoint first, Gemini as the fallback. Output is
        capped to a tiny budget and temperature 0 so it stays a label.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        prompt = f"Request:\n{str(request).strip()[:400]}"
        if self._openai_configured and not self._provider_slots.locked():
            # Classification is a latency-sensitive nicety: when every slot is
            # busy answering real questions it skips straight to the fallback
            # (Gemini, then the deterministic route) instead of queueing
            # behind a wall of 45-second answers.
            try:
                answer, _truncated = await self._chat_openai_once(
                    [
                        {"role": "system", "content": CLASSIFY_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max(64, int(max_tokens)),
                )
                if answer:
                    return answer
                raise ParadiseError("the classifier answered with nothing.")
            except ParadiseError:
                if not self.gemini.available:
                    raise
                log.info("Primary classifier failed — using Gemini")
        return await self.gemini.classify(request, max_tokens=max_tokens)

    async def reason(
        self, question: str, facts: list[str] | str, *, max_tokens: int = 500
    ) -> str:
        """Grounded synthesis: answer *question* strictly from retrieved *facts*.

        Kept as an explicit, opt-in grounded helper for future integrations.
        Nothing in the active pipeline calls it: free-form answers come from
        ``chat``/``answer_question`` instead. The AI never gets to invent
        facts — it only explains facts supplied by its caller.
        Falls back across providers the same way ``chat`` does.
        """
        if not self.available:
            raise ParadiseError(FALLBACK_UNAVAILABLE)
        if isinstance(facts, (list, tuple)):
            fact_text = "\n".join(f"- {f}" for f in facts if str(f).strip()) or "(none)"
        else:
            fact_text = str(facts).strip() or "(none)"

        if self._openai_configured:
            try:
                prompt = (
                    "You are answering for a Discord bot using ONLY the verified "
                    "facts provided. Never invent numbers, dates, names or events. "
                    "If the facts conflict, say so. Be concise.\n\n"
                    f"VERIFIED FACTS:\n{fact_text}\n\nQUESTION: {question.strip()}"
                )
                return await self._chat_openai(
                    [{"role": "user", "content": prompt}], max_tokens=max_tokens
                )
            except ParadiseError:
                if not self.gemini.available:
                    raise
                log.warning("Primary reasoning failed — trying Gemini")

        return await self.gemini.reason(question, fact_text, max_tokens=max_tokens)

    async def validate_startup(self) -> bool:
        """Best-effort startup validation of the configured key (never fatal)."""
        gemini_cfg = getattr(self.config, "gemini", None)
        if gemini_cfg is None or not gemini_cfg.configured:
            if not self.available:
                log.info("No AI provider configured — running fully tool-first.")
            return False
        if getattr(gemini_cfg, "validate_on_start", True):
            return await self.gemini.validate()
        log.info("Gemini configured (key %s) — validation skipped", self.gemini.masked_key)
        return False

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
