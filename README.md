# Paradise Bot

A small private Discord bot for a friends-only server. Paradise plays music through Lavalink, can speak in voice channels, answers when addressed, and otherwise stays out of the way. It is intentionally not a moderation suite, dashboard, or AI wrapper.

## Run it

Requires Python 3.11 or newer.

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On the bot page, enable the **Message Content Intent**. Paradise uses it for conversational reactions, AFK handling, TTS triggers and AI mentions.
3. Invite it with the `bot` and `applications.commands` scopes. Useful permissions: View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, and **Connect / Speak** for voice.
4. Install and configure:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# fill in DISCORD_TOKEN (required) and the Lavalink credentials
python bot.py
```

Slash commands are synced globally at startup; the exact synced set is logged. Discord can take a little while to show newly registered global commands.

### Configuration

Everything lives in `.env` (never committed). See `.env.example` for all options:

| Group | Variables | Notes |
| --- | --- | --- |
| Discord | `DISCORD_TOKEN` | required |
| Lavalink | `LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_PASSWORD`, `LAVALINK_SECURE`, `LAVALINK_REGION` | any Lavalink v4 node; music and voice TTS are disabled without it |
| AI | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | any OpenAI-compatible endpoint (OpenAI, Groq, Gemini's compat layer, local Ollama). Optional |
| TTS | `TTS_ENABLED`, `TTS_VOICE_HI`, `TTS_VOICE_EN`, `TTS_MAX_LENGTH`, `TTS_USER_COOLDOWN`, `TTS_GLOBAL_COOLDOWN` | free neural voices via edge-tts |
| Music | `MUSIC_MAX_VOLUME`, `AUTO_DISCONNECT_MINUTES` | |
| Lyrics | `LYRICS_ENABLED` | free provider; set `false` and `/lyrics` is not even registered |

## Commands

27 commands, one `/ping`, no filler:

- **Fun** — `/ping` (with the real latency), `/8ball`, `/coinflip` (animated), `/choose`
- **Music** — `/join`, `/play`, `/pause`, `/resume`, `/skip`, `/stop`, `/queue`, `/nowplaying`, `/volume`, `/clearqueue`, `/shuffle`, `/leave`, `/lyrics`
- **Useful** — `/remind`, `/poll`, `/afk`, `/translate`, `/define`, `/status`, `/settings`
- **Voice** — `/say`
- **AI** — `/summarize`, or just mention Paradise

Choices for `/choose` and `/poll` are separated with `|` or `,`:

```text
/choose options:pizza | burgers | something responsible
```

### The music panel

Every music command opens or updates one interactive panel per server (edit-in-place, no embed spam). The panel shows status, track, requester, progress, queue length, volume and voice channel, with buttons for **Play / Pause / Resume / Skip / Stop / Queue / Now Playing / Volume / Join**. Buttons run the exact same validation and service calls as the slash commands — they're the same code paths, not decorations. Panels survive restarts: Paradise re-binds them on startup.

Voice rules, deliberately simple:

- Music controls require being in Paradise's voice channel (or having Move Members).
- `/join` keeps Paradise in the channel until `/stop`, `/leave`, or Discord removes it — it will not leave an empty channel by itself.
- Sessions started by `/play` (when Paradise wasn't connected) will auto-disconnect after `AUTO_DISCONNECT_MINUTES` alone in a channel. Servers can toggle this in `/settings`.

### Talking and speaking

- **Talk:** mention Paradise (or reply to it) and it answers via the configured AI — questions, explanations, Minecraft ideas, coding help, brainstorming, rewrites. It stays quiet otherwise; regular chat is never sent to the AI, only a small recent-context window used when it's actually addressed.
- **Speak:** with Paradise in your voice channel, write `@Paradise say hello everyone` (or just `paradise say hello everyone`, or `paradise bolo नमस्ते`). Paradise speaks it in voice — a natural Hindi-accented neural voice, using a Hindi voice for Devanagari text and an Indian-English voice otherwise. `/say` does the same with the same limits: short messages only, per-user and global cooldowns, one line at a time, music paused and resumed around each spoken line.

TTS playback works by synthesizing the clip with edge-tts, uploading it as a Discord attachment and streaming it through the Lavalink node's http source. If a node doesn't support direct audio links, TTS reports itself unavailable in `/status` and retries periodically. The provider is modular (`functions/tts.py`) and can be swapped.

### AI setup

Point `AI_BASE_URL` at any OpenAI-compatible `/chat/completions` endpoint. Examples:

```env
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=sk-...
AI_MODEL=gpt-4o-mini
```

```env
AI_BASE_URL=http://localhost:11434/v1
AI_MODEL=llama3.1
```

Without AI credentials, mentions get an occasional honest "my brain isn't plugged in" note, and `/summarize` explains that it needs configuration.

## Architecture

```
bot.py                    wiring, message flow (AFK / TTS triggers / AI mentions), sync
config.py                 all environment configuration in one place
commands/                 thin slash-command cogs (fun, music, utility, voice, ai, system)
functions/
  music/service.py        MusicService — the single source of truth for player state,
                          queue, panel lifecycle, auto-disconnect and TTS playback
  music/panel.py          panel/queue rendering, buttons, modals, pagination
  music/voice.py          discord.py <-> Lavalink voice bridge
  tts.py                  TTS provider + cooldowns + per-guild speech queue
  ai.py                   AI client, activation rules, bounded context
  settings.py             per-guild settings (JSON, atomic writes)
  lyrics.py               optional lyrics provider
  helpers.py              shared errors, colours, parsing, formatting
  personality.py          rare unprompted reactions
  presence.py             slow presence rotation
tests/test_offline.py     offline validation: exact command set, every flow, limits
```

Design rules: one music service owns all state (buttons and commands share it), all user-facing errors are friendly strings (`MusicError` / `TTSError` / `ParadiseError`), raw provider or Lavalink errors never reach users — they're logged with context instead. Secrets only ever come from the environment.

## Testing

The offline suite needs no token, node, or network:

```bash
python -m unittest discover -s tests -v
```

It verifies the exact registered command set (and that removed commands stay gone), the ping format, coinflip randomness, TTS limits and cooldowns, panel rendering for every state, queue pagination, settings persistence, lyrics pagination, and the full music flow (join/play/pause/skip/volume/clearqueue/shuffle/stop/auto-disconnect rules) against a fake Lavalink client.

## Notes & limits

- Reminders, AFK states and the music queue live in memory; music panels and server settings persist to `data/settings.json`.
- The free lyrics and dictionary providers are best-effort; failures return clean "couldn't find" messages.
- Presence: Discord gives bot accounts one visible activity slot; Paradise rotates a custom status and activities slowly, never repeating back-to-back.
