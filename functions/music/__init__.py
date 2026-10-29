"""In-process music for Paradise: search, queue, mix and speak over it.

Why not Lavalink?
-----------------
Lavalink v4 owns the Discord voice connection itself: it logs the bot account
in to the voice server, receives the (encoded) audio over the gateway and
sends Opus straight to Discord. discord.py never gets a byte of it.

That is fine for a bot that only plays music, and it is *fatal* for this one:
Paradise answers out loud with an ElevenLabs voice, and the whole point of
this package is to **mix that voice over the song at 15 % volume**. With
Lavalink there is no local audio to mix, no local volume to duck, and the
voice client is a Lavalink player that cannot play a local source. You can
have Lavalink, or you can have "duck the song and speak over it" — not both.

So Paradise does the audio itself, and it is small:

    search  →  yt-dlp (YouTube / SoundCloud / any URL)      functions/music/search.py
    decode  →  FFmpeg pipes 48 kHz stereo s16le PCM         functions/music/decoder.py
    mix     →  numpy, gain ramp 1.0 → 0.15 → 1.0            functions/music/mixer.py
    play    →  one discord.AudioSource per guild            functions/music/player.py
    drive   →  voice intents, /cmd panel, text commands     functions/music/intents.py

Everything except the network and the Discord socket is a pure function, so
the important parts (ducking maths, queue order, intent parsing) are tested
without a token, a node or an internet connection.
"""

from .models import LoopMode, PlayerState, Track, format_duration
from .queue import MusicQueue, QueueFull
from .search import SearchUnavailable, YtDlpResolver
from .player import GuildPlayer, MusicError, MusicService, NotConnected
from .intents import MusicIntent, is_control_only, parse_music_intent

__all__ = [
    "GuildPlayer",
    "LoopMode",
    "MusicError",
    "MusicIntent",
    "MusicQueue",
    "MusicService",
    "NotConnected",
    "PlayerState",
    "QueueFull",
    "SearchUnavailable",
    "Track",
    "YtDlpResolver",
    "format_duration",
    "is_control_only",
    "parse_music_intent",
]
