"""Which voice client class a join should use — a dependency-free policy.

This lives in its own leaf module (importing nothing from the rest of the
package) so every join path — voice presence, dictionary pronunciation
playback — can share one rule without import cycles.
"""

from __future__ import annotations

from typing import Any

import discord


def voice_client_class_for(bot: Any) -> type[discord.VoiceClient]:
    """The voice client class a join should use for ``bot``.

    The receive-capable :class:`~functions.voice_clip.receiver.ClipVoiceClient`
    whenever the clip buffer needs audio (``VOICE_CLIP_ENABLED``, on by
    default); the plain client otherwise, so nothing decrypts and the bot
    stays as light as a music-free voice client can be.  Shared by every join
    path so a re-join can never silently swap the recording receiver out.
    """
    clip_cfg = getattr(getattr(bot, "config", None), "voice_clip", None)
    if bool(getattr(clip_cfg, "enabled", False)):
        # Local import: the receiver module pulls in the whole capture stack.
        from .receiver import ClipVoiceClient

        return ClipVoiceClient
    return discord.VoiceClient
