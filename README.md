# Paradise Bot

A small private Discord bot for a friends-only server. Paradise answers when addressed, quietly watches the server, and every once in a while reacts to or comments on a message — rarely enough that it feels like an Easter egg. It is intentionally not a moderation suite, dashboard, music player, or AI wrapper.

## Run it

Requires Python 3.11 or newer.

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On the bot page, enable the **Message Content Intent**. Paradise uses it for the passive personality, AFK handling and AI mentions.
3. Invite it with the `bot` and `applications.commands` scopes. Useful permissions: View Channels, Send Messages, Embed Links, Add Reactions, Read Message History.
4. Install and configure:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# fill in DISCORD_TOKEN (required); AI and personality tuning are optional
python bot.py
```

Slash commands are synced globally at startup; the exact synced set is logged. Discord can take a little while to show newly registered global commands.

### Configuration

Everything lives in `.env` (never committed). See `.env.example` for all options:

| Group | Variables | Notes |
| --- | --- | --- |
| Discord | `DISCORD_TOKEN` | required |
| AI | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | any OpenAI-compatible endpoint (OpenAI, Groq, Gemini's compat layer, local Ollama). Optional |
| Personality | `PERSONALITY_ENABLED`, `PERSONALITY_REACTION_CHANCE`, `PERSONALITY_REPLY_CHANCE`, `PERSONALITY_GLOBAL_COOLDOWN`, `PERSONALITY_CHANNEL_COOLDOWN`, `PERSONALITY_REPLY_CHANNEL_COOLDOWN` | tuning for the rare reactions/replies; defaults are deliberately conservative |

## Commands

- **Fun** — `/ping` (with the real latency), `/coinflip` (animated), `/choose`
- **Useful** — `/remind`, `/poll`, `/afk`, `/translate`, `/define`
- **AI** — `/summarize`, or just mention Paradise
- `/help` lists exactly what is registered — nothing more

Choices for `/choose` and `/poll` are separated with `|` or `,`:

```text
/choose options:pizza | burgers | something responsible
```

## Presence

Paradise's activity is always **👀 Watching Paradise** — it never rotates. What does rotate is the small status note attached to it ("Probably doing something.", "Monitoring the chaos.", "I saw that." …): one random note from a large Paradise-themed pool every 20 minutes, never the same note twice in a row. The rotation runs on a single timer (starting it twice is a no-op) and the presence is re-asserted after reconnects.

## The passive personality

Paradise has a small, deliberately rare passive personality (`functions/personality.py`):

- **Reactions** — occasionally a contextually fitting emoji on a normal message ("I finally fixed it" → 🏆, "this is so cursed" → 😭).
- **Replies** — even more rarely, a short, dry, context-aware reply ("bro I'm so tired" → "You chose to stay awake though.", "this server is dead" → "And yet here you are.").

Ground rules, enforced in code:

- Most messages get nothing. Chances are rolled only on messages matching a known pattern (greetings, wins, fails, confusion, food, sleep, gaming, Minecraft, "who broke this", "no way", "wtf", "lmao" …).
- Never a reaction **and** a reply on the same message.
- Global and per-channel cooldowns, plus an extra per-channel cooldown for replies.
- Bots, webhooks, system messages, command-looking messages (`!`, `/`, `.` …), messages addressed to Paradise, copy-paste spam and fast-moving channels are all ignored.
- Recently used reactions and replies are avoided, so the same joke doesn't repeat.

All probabilities and cooldowns are configurable in one place (`PersonalityConfig` in `config.py`, overridable via `.env`).

## Talking

Mention Paradise (or reply to it) and it answers via the configured AI — questions, explanations, Minecraft ideas, coding help, brainstorming, rewrites. It stays quiet otherwise; regular chat is never sent to the AI, only a small recent-context window used when it's actually addressed.

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
bot.py                    wiring, message flow (AFK / AI mentions / personality), sync
config.py                 all environment configuration and tuning in one place
commands/                 thin slash-command cogs (fun, utility, ai, system)
functions/
  ai.py                   AI client, activation rules, bounded context
  helpers.py              shared errors, colours, parsing, formatting
  personality.py          rare contextual reactions and short replies
  presence.py             fixed «Watching Paradise» activity + rotating note
tests/test_offline.py     offline validation: exact command set, every flow, limits
```

Design rules: all user-facing errors are friendly strings (`ParadiseError`), raw provider errors never reach users — they're logged with context instead. Secrets only ever come from the environment.

## Testing

The offline suite needs no token or network:

```bash
python -m unittest discover -s tests -v
```

It verifies the exact registered command set (and that the removed music/TTS commands stay gone from source and registration), the ping format, coinflip randomness, config parsing, the presence note pool and rotation rules (20-minute cadence, no back-to-back repeats, no duplicate timers), and the personality system: pattern matching, ignore rules for bots/webhooks/commands/spam, cooldowns, the never-react-and-reply guarantee, and reply variety.

## Notes & limits

- Reminders and AFK states live in memory; a restart clears them.
- The free translation and dictionary providers are best-effort; failures return clean "couldn't find" messages.
- Presence: Discord gives bot accounts one visible activity slot; Paradise keeps it on «Watching Paradise» and rotates only the attached note.
