"""Writing help: rewriting, drafting and tone work — in Paradise's voice.

None of these tools change who Paradise is. They add a small overlay to
the existing system prompt that says, in effect, "for the next answer you
are also a sharp editor": the humour stays, the loyalty stays, and the
*output* the user asked for comes back clean and copy-ready.

Everything here is prompt assembly — no network, no extra dependencies,
one ``chat`` call per request on the deep tier.
"""

from __future__ import annotations

import re
from typing import Any

#: "rewrite this to be formal: <text>" — the part before the colon is
#: deixis + tone, not content.
_DEIXIS_PREFIX = re.compile(
    r"^(?:this|that|it)\s+(?:to\s+be\s+|in\s+(?:a\s+)?|more\s+|a\s+bit\s+)?"
    r"(?:" + "|".join(_t.replace(" ", r"\s+") for _t in (
        "formal", "casual", "friendly", "professional", "sarcastic", "playful",
        "serious", "polite", "blunt", "excited", "sad", "dramatic", "gen z",
        "shakespearean", "corporate", "hinglish", "shorter", "longer",
    )) + r")"
    r"\s*(?:tone|style)?\s*[:,]?\s*",
    re.IGNORECASE,
)


def strip_deixis(payload: str) -> str:
    """Drop leading 'this to be formal:' style prefixes from writing input."""
    cleaned = _DEIXIS_PREFIX.sub("", " ".join(str(payload or "").split()), count=1)
    # A lone "this:" / "that:" leftover.
    cleaned = re.sub(r"^(?:this|that|it)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

# The writing modes the router can ask for.
WRITE_MODES = frozenset(
    {"rewrite", "announcement", "reply", "actions", "tone", "shorten", "expand"}
)

_VALID_TONES = (
    "formal", "casual", "friendly", "professional", "sarcastic", "playful",
    "serious", "polite", "blunt", "excited", "sad", "dramatic", "gen z",
    "shakespearean", "corporate", "hinglish",
)


def detect_tone(text: str) -> str:
    """Pull a requested tone out of a rewrite request, defaulting to casual."""
    value = " ".join(str(text or "").split()).casefold()
    for tone in _VALID_TONES:
        if tone in value:
            return tone
    return "casual"


def writing_system_prompt(mode: str) -> str:
    """The editor overlay on top of Paradise's normal personality."""
    from functions.ai import SYSTEM_PROMPT

    overlay = {
        "rewrite": (
            "WRITING MODE — REWRITE. Rewrite the user's text in the requested "
            "tone. Preserve the author's meaning and any facts; keep it roughly "
            "the same length unless asked otherwise. Reply with ONLY the "
            "rewritten text, copy-ready, no introduction, no quotes around it, "
            "no explanation. If the text is too short or empty, ask for the text."
        ),
        "announcement": (
            "WRITING MODE — ANNOUNCEMENT. Draft a Discord announcement for the "
            "user. Rules: a bold first line with the essentials, 2–5 short lines "
            "of detail, one emoji at most per line, an @-mention only if the "
            "user asked for one, and end with a one-line call to action. Reply "
            "with ONLY the announcement, copy-ready."
        ),
        "reply": (
            "WRITING MODE — REPLY SUGGESTIONS. The user shows you a message "
            "they need to answer. Offer 3 candidate replies as a short list: "
            "one friendly, one funny (your specialty), one that says no or "
            "pushes back politely. Each reply is one or two lines, in the "
            "user's voice, not yours, and copy-ready as-is. Keep your own "
            "commentary to a single short line at the end at most."
        ),
        "actions": (
            "WRITING MODE — ACTION ITEMS. Extract the action items from the "
            "user's text as a checklist: `- [ ] task — who/when if stated`. "
            "Only real tasks (something someone must do), never opinions. If "
            "there are none, say so in one line. Reply with ONLY the list, copy-ready."
        ),
        "tone": (
            "WRITING MODE — TONE ANALYSIS. Analyse the tone of the user's text "
            "in 2–3 short lines: the dominant tone, the cues that give it "
            "away, and how it will likely land on the reader. Be specific, "
            "not a list of adjectives. If the text is sarcastic, say so — "
            "you of all bots would know."
        ),
        "shorten": (
            "WRITING MODE — SHORTEN. Cut the user's text to its essentials "
            "without losing meaning: keep every fact, drop every filler word. "
            "Aim for at most half the length. Reply with ONLY the shortened "
            "text, copy-ready."
        ),
        "expand": (
            "WRITING MODE — EXPAND. Elaborate the user's text to about twice "
            "the length: add useful detail and structure, but invent no new "
            "facts. Reply with ONLY the expanded text, copy-ready."
        ),
    }
    return SYSTEM_PROMPT + "\n\n" + overlay.get(mode, overlay["rewrite"])


def build_prompt(mode: str, payload: str, *, asker: str = "someone") -> str:
    """Assemble the user prompt for one writing request."""
    text = " ".join(str(payload or "").split())
    if mode in ("rewrite", "actions", "shorten", "expand"):
        text = strip_deixis(text)
    if mode == "rewrite":
        tone = detect_tone(payload or text)
        return (
            f"{asker} wants this rewritten in a {tone} tone. "
            f"If the tone isn't stated in the text, keep it casual:\n\n{text}"
        )
    if mode == "announcement":
        return f"{asker} needs a Discord announcement about:\n\n{text}"
    if mode == "reply":
        return f"{asker} needs to answer this message:\n\n{text}"
    if mode == "actions":
        return f"{asker} wants the action items extracted from:\n\n{text}"
    if mode == "tone":
        return f"{asker} wants the tone of this analysed:\n\n{text}"
    if mode == "shorten":
        return f"{asker} wants this shortened:\n\n{text}"
    if mode == "expand":
        return f"{asker} wants this expanded:\n\n{text}"
    return f"{asker} asks:\n\n{text}"


async def run_writing(
    mode: str, payload: str, ai: Any, *, asker: str = "someone", asker_id: int = 0
) -> str:
    """One writing request → one polished answer. Raises on AI failure."""
    answer = await ai.chat(
        [
            {"role": "system", "content": writing_system_prompt(mode)},
            {"role": "user", "content": build_prompt(mode, payload, asker=asker)},
        ],
        max_tokens=450,
        role="deep",
        caller_id=int(asker_id or 0) or None,
    )
    return str(answer or "").strip()


__all__ = [
    "WRITE_MODES", "build_prompt", "detect_tone", "run_writing",
    "writing_system_prompt", "strip_deixis",
]
