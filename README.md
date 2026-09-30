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
| Dictionary | `DICTIONARY_TEXT_TRIGGERS`, `DICTIONARY_TIMEOUT`, `DICTIONARY_CACHE_TTL`, `DICTIONARY_CACHE_SIZE`, `DICTIONARY_CARD_THEME` | all optional — the dictionary needs no keys and works out of the box |
| Pronunciation | `PRONUNCIATION_PREFER_NATIVE`, `PRONUNCIATION_VOICE_PLAYBACK`, `SPEECH_BASE_URL`, `SPEECH_API_KEY`, `SPEECH_MODEL`, `SPEECH_VOICE` | optional. `SPEECH_*` adds a neural voice for words with no human recording; without it a free voice is used |

## Commands

- **Fun** — `/ping` (with the real latency), `/coinflip` (animated), `/choose`
- **Useful** — `/remind`, `/poll`, `/afk`, `/translate`, `/dictionary` (or `/define`), `/recipe`
- **AI** — `/summarize`, or just mention Paradise
- `/help` lists exactly what is registered — nothing more

Choices for `/choose` and `/poll` are separated with `|` or `,`:

```text
/choose options:pizza | burgers | something responsible
```

## Dictionary

`/dictionary promise` (or `/define`, or just **`define promise`** in chat) answers with a rendered Material Expressive card instead of a wall of text.

![The dictionary card for "hello"](assets/dictionary-card.png)

```text
/dictionary word: serendipity
define serendipity          ← plain chat works too
what does serendipity mean?
```

- **No API key, no external service.** Word data comes from two free, key-less sources: [dictionaryapi.dev](https://dictionaryapi.dev) (Wiktionary-backed definitions, IPA and human recordings) and [Datamuse](https://www.datamuse.com/api/) (synonyms, antonyms, related words, spelling correction). If the first is down the second still produces a usable card, clearly marked as partial.
- **Asked in plain English.** `define hello`, `dictionary hello`, `Define "hello"`, `meaning of hello`, `what does hello mean?` — case and quotes don't matter. Ordinary conversation is left alone: "I need to define a function" is not a lookup. Set `DICTIONARY_TEXT_TRIGGERS=false` for slash-commands-only.
- **A real Material Expressive card, drawn per word.** Nothing is a static template: the layout measures every string and grows or shrinks to fit, so a one-line definition and a twelve-synonym entry both look deliberate. The headword is the focal point; pronunciation, part of speech, definition, example, word families and source information each have their own calm visual layer. Tonal containers, organic background shapes and restrained elevation keep it polished without turning it into an infographic. Dark is tuned for Discord by default; set `DICTIONARY_CARD_THEME=light` for a light surface. Long words shrink to fit on one line, text is wrapped and clipped rather than allowed to overflow, and IPA renders properly (`/həˈləʊ/`) thanks to a bundled Inter subset with full phonetic coverage. Without Pillow installed the card degrades to clean formatted text.
- **🔊 Pronounce speaks the word** — never the definition. It prefers the dictionary's own human recording (UK/US/AU where available) and falls back to a synthesised voice, optionally your own neural endpoint via `SPEECH_*`. Audio arrives as a private attachment; with PyNaCl and ffmpeg installed, `PRONUNCIATION_VOICE_PLAYBACK=true` makes Paradise join your voice channel, say the word and leave.
- **Everything else on demand.** The card stays uncluttered; **📚 More** adds all meanings, every pronunciation variant, inflected forms, synonyms, antonyms, related words and etymology, and **📖 Examples** shows the example sentences. Navigation belongs to whoever asked; 🔊 works for everyone.
- **Spelling-aware, but never presumptuous.** Inputs are trimmed, normalised and checked exactly first. On a miss, RapidFuzz Damerau-Levenshtein matching ranks real candidates from Datamuse, handling transposed, missing, extra and repeated letters (`definately` → **definitely**, `recieve` → **receive**). A clear one-edit match is verified through the normal dictionary pipeline and opens its card immediately; an ambiguous match asks **Did you mean…?** with **Yes, search _word_** and **Search exactly what I typed**. Nothing is ever invented: missing fields are omitted or labelled, never filled in.
- **Built to not fall over.** Lookups, rendered cards and audio clips are cached with bounded, expiring caches; identical simultaneous requests are coalesced into one fetch; everything has a deadline; and a failure in any layer (API, renderer, voice) is contained — you still get whatever the other layers produced.

## Recipes

`/recipe` turns "here's what's in my kitchen" into something to actually cook. Give it a plain list of ingredients and Paradise suggests the recipes you can realistically make, in a small Components V2 interface that behaves like a cooking app inside Discord.

```text
/recipe ingredients: chicken, rice, onion, tomato, garlic
```

- **No API key, no external service.** Matching runs entirely on a bundled recipe book (`functions/recipes/recipes.json`) — it works the moment you clone and run. AI is *not* required; it stays an optional future enhancement.
- **Understands messy input.** Quantities and units are stripped (`2 eggs`, `4 slices of bread`, `one tomato`), plurals and spellings are folded together, and common Hindi/Hinglish names are recognised (`aloo`→potato, `pyaaz`→onion, `tamatar`→tomato, `hari mirchi`→green chilli, `anda`→egg, `dahi`→yogurt, `lahsun`→garlic, `adrak`→ginger…). All of this vocabulary lives in `functions/recipes/ingredients.json`, easy to expand.
- **Smart matching.** For every recipe Paradise works out what you have, what's missing, and what's optional, then ranks by how much of your stuff it uses and how little you'd need to buy. Pantry staples (salt, water, oil, basic spices — configurable in `ingredients.json`) are assumed on hand and never counted as missing; unusual items (soy sauce, cheese, cream, coconut milk) are.
- **Interactive.** The suggestion list has a picker plus **📖 View recipe**, **🔄 Another** and **🧺 Missing ingredients**. The detail view shows time, servings, difficulty, descriptive tags (High protein, Vegetable-rich, Lower-oil, Quick, Balanced — never medical claims), a *You have / Missing* breakdown, quantities and numbered steps. The missing-ingredients view lists only what you lack, with conservative substitution suggestions where they make sense. Cycling and picking reuse the original results — no repeated searching.
- **Optional filters:** `diet` (vegetarian / vegan / non-veg), `cuisine` (indian / chinese / italian / …) and `max_time` (minutes). All optional — the bare `ingredients` form is always the easiest.
- **Graceful when nothing fits.** Instead of a flat "no recipes found", Paradise explains what you gave it, suggests how to broaden the search, and lists the closest near-misses from what you have.

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
  dictionary/             dictionary engine (pure data/render; only playback.py
                          touches discord, because it drives a voice client)
    models.py               WordEntry / Sense / Pronunciation / Clip data models
    normalize.py            query cleaning, chat-trigger patterns, edit distance
    http.py                 one shared aiohttp session, retries, deadlines
    sources.py              provider interface + dictionaryapi.dev and Datamuse
    service.py              lookup pipeline: cache → coalesce → fetch → merge
    cache.py                bounded TTL cache + single-flight helper
    render.py               the card renderer: measure, lay out, paint (Pillow)
    fonts.py                font discovery and glyph fallback (IPA coverage)
    pronounce.py            speech providers: native recording → neural → free
    playback.py             optional voice-channel playback (join, speak, leave)
    feature.py              the façade the cog talks to; owns every cache
  recipes/                recipe engine (no discord, no AI, unit-testable)
    normalize.py            ingredient parsing/normalization (aliases, units, Hinglish)
    engine.py               matching, ranking, filters, substitutions, fallbacks
    source.py               recipe source abstraction + bundled loader
    models.py               Recipe / MatchResult data models
    ingredients.json        aliases, pantry staples, substitutions (data, editable)
    recipes.json            the bundled recipe book (data, editable)
commands/recipe.py        /recipe command + Components V2 recipe UI
commands/dictionary.py    /dictionary + /define, chat triggers, card UI, buttons
assets/fonts/             bundled Inter subset (Latin + IPA) for the card
tests/test_offline.py     offline validation: exact command set, every flow, limits
tests/test_dictionary.py  the dictionary: parsing, pipeline, renderer, cog flows
```

Design rules: all user-facing errors are friendly strings (`ParadiseError`), raw provider errors never reach users — they're logged with context instead. Secrets only ever come from the environment.

## Testing

The offline suite needs no token or network:

```bash
python -m unittest discover -s tests -v
```

It verifies the exact registered command set (and that the removed music/TTS commands stay gone from source and registration), the ping format, coinflip randomness, config parsing, the presence note pool and rotation rules (20-minute cadence, no back-to-back repeats, no duplicate timers), and the personality system: pattern matching, ignore rules for bots/webhooks/commands/spam, cooldowns, the never-react-and-reply guarantee, and reply variety.

`tests/test_dictionary.py` covers the dictionary end to end without a network or a gateway: recorded API payloads through the real parsers, the cache/coalescing/fallback pipeline, spelling suggestions, every renderer edge case (very long words, many synonyms, missing fields, Unicode and IPA — asserting no text ever overflows), the pronunciation provider chain, and the cog itself — slash command, chat triggers, buttons, permissions, cooldowns, expired interactions and concurrency — driven with fake interactions.

## Notes & limits

- Reminders and AFK states live in memory; a restart clears them.
- The free translation and dictionary providers are best-effort; failures return clean "couldn't find" messages.
- Dictionary cards need [Pillow](https://pypi.org/project/Pillow/) (in `requirements.txt`); without it the feature still works and answers with formatted text.
- Voice-channel pronunciation additionally needs PyNaCl (`pip install "discord.py[voice]"`) and ffmpeg on `PATH`. Without them it stays off and the audio is attached to the reply instead.
- Presence: Discord gives bot accounts one visible activity slot; Paradise keeps it on «Watching Paradise» and rotates only the attached note.
