# Paradise Bot

A small private Discord bot for a friends-only server — part search engine, part utility toolbox, part entertainment guide, with an *optional* AI reasoning layer on top.

Paradise answers when addressed, quietly watches the server, and every once in a while reacts to or comments on a message — rarely enough that it feels like an Easter egg. It is intentionally not a moderation suite, dashboard, music player, or AI wrapper.

## Tool-first, AI-last

Everything Paradise does follows one priority chain:

```
your message
   ↓
intent router (deterministic, ~microseconds)
   ↓
can a native tool answer?      calculator · define · convert · weather · time · translate
   ├── YES → specialised tool, zero AI
   └── NO
        can the entertainment engine answer?   movies · anime · series · trending · upcoming · recommendations
        ├── YES → verified cards, zero AI
        └── NO
             can web search answer?            → answer + key points + sources (never bare links)
             ├── YES → sourced summary, optionally rephrased by AI *from retrieved facts*
             └── NO
                  needs real reasoning?        → Gemini (or the OpenAI-compatible fallback)
                  └── everything else         → asks what you meant
```

AI is strictly the last resort: `"25% of 800"`, `"100 USD to INR"`, `"weather in Delhi"` or `"upcoming anime"` never touch an AI model. With **no API key at all** the bot is fully functional — only free-form conversation and AI-polished summaries are off.

## Run it

Requires Python 3.11 or newer.

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On the bot page, enable the **Message Content Intent** and **Server Members / Voice States**. Paradise uses them for voice activity tracking, passive personality, AFK handling and AI mentions.
3. Invite it with the `bot` and `applications.commands` scopes. Useful permissions: View Channels, Send Messages, Embed Links, Attach Files, Add Reactions, Read Message History, Connect.
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
| Voice | `VOICE_DB_PATH`, `VOICE_DM_REPORTS_ENABLED` | SQLite voice database path (default: `data/paradise.db`) and daily DM summaries |
| Gemini (AI) | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_ENABLED`, `GEMINI_VALIDATE_ON_START`, `GEMINI_TIMEOUT`, `GEMINI_SYNTHESIZE_SEARCH` | the optional AI reasoning layer. Key validated at startup, never logged/displayed, never required — every tool works without it. Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| AI fallback | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | optional OpenAI-compatible endpoint (OpenAI, Groq, local Ollama), used when Gemini is not configured or unavailable |
| Personality | `PERSONALITY_ENABLED`, `PERSONALITY_REACTION_CHANCE`, `PERSONALITY_REPLY_CHANCE`, `PERSONALITY_GLOBAL_COOLDOWN`, `PERSONALITY_CHANNEL_COOLDOWN`, `PERSONALITY_REPLY_CHANNEL_COOLDOWN` | tuning for the rare reactions/replies; defaults are deliberately conservative |
| Dictionary | `DICTIONARY_TEXT_TRIGGERS`, `DICTIONARY_TIMEOUT`, `DICTIONARY_CACHE_TTL`, `DICTIONARY_CACHE_SIZE`, `DICTIONARY_CARD_THEME` | all optional — the dictionary needs no keys and works out of the box |
| Pronunciation | `PRONUNCIATION_PREFER_NATIVE`, `PRONUNCIATION_VOICE_PLAYBACK`, `SPEECH_BASE_URL`, `SPEECH_API_KEY`, `SPEECH_MODEL`, `SPEECH_VOICE` | optional. `SPEECH_*` adds a neural voice for words with no human recording; without it a free voice is used |

## Mention me — no commands to learn

The easiest interface is a plain mention. The deterministic router picks the right tool; a temporary *"🔎 searching the web…"* placeholder is edited into the final answer when something takes a moment:

```text
@Paradise define photosynthesis        → dictionary card (web answer if not a word)
@Paradise what's 15% of 500            → calculator (75 — local, instant)
@Paradise 100 USD to INR               → live currency rates
@Paradise 15 miles in kilometers       → unit conversion
@Paradise weather in Delhi             → weather card
@Paradise what time is it in Tokyo     → local time
@Paradise what's the date              → date in your timezone
@Paradise translate hello into Japanese
@Paradise latest Minecraft news        → web answer + key points + sources
@Paradise upcoming anime               → the entertainment engine
@Paradise recommend a movie like Interstellar
@Paradise what's trending in anime
@Paradise explain why One Piece is so popular   → AI (the reasoning layer)
@Paradise help                         → quick cheat-sheet
```

## Search that answers, not just links

`/search minecraft 1.22` (or `@Paradise search minecraft 1.22`) returns a search-engine-style card:

1. **Quick Answer** — a direct answer extracted from retrieved sources (Wikipedia, instant answers, ranked snippets).
2. **Key points** — the important facts, deduplicated and ranked.
3. **⚠️ Heads-up** — shown when sources disagree on dates/figures or everything looks dated.
4. **Sources** — numbered clickable links, authoritative sources starred.

A "📑 Browse results" button flips the card into the classic paginated result browser. Summaries are extracted deterministically from what was actually retrieved — never from thin air; if the owner configured Gemini *and* the query genuinely benefits from synthesis, the retrieved facts are handed to the model for rephrasing (footer marks the card `summary rephrased by Gemini`).

## Built-in calculator

Mentions like `@Paradise 25% of 800` understand everyday math: percent (`25% of 800`, `800 - 15%`, `50% * 300`), `× ÷ ^ ** ( )`, word operators (`6 times 7`, `3 squared`, `square root of 81`), functions (`sqrt`, `ln`, `log`, `abs`, `sin`…), constants (`pi`, `e`, `tau`), thousand separators, and modulo (`10 % 3`, `7 mod 4`). Evaluation is a sandboxed AST whitelist — no `eval`, ever.

## Commands

- **Fun** — `/ping` (with the real latency), `/coinflip` (animated), `/choose`
- **Useful**
  - `/search` — search-engine-style answers: quick answer, key points, disagreement warnings, clickable sources, plus a classic results browser
  - `/weather` — current weather, feels-like, wind, humidity, sun times, and forecast for any city or country
  - `/convert` — natural language unit conversions (length, weight, temp, speed, time, volume, area, data) and live currency exchange rates
  - `/time` — local time, date, and UTC offset for any location or configured member
  - `/settimezone` — set your personal timezone for `/time` and reports
  - `/remind`, `/poll`, `/afk`, `/translate`, `/dictionary` (or `/define`), `/recipe`
- **Voice** — `/vc leaderboard` (rendered stats card or text list for Today, Yesterday, Week, Month, All-Time), `/vc stats`, `/vc dmreports`
- **Entertainment** — `/example` (movie/anime/web-series info card with live autocomplete), `/trending`, `/upcoming`, `/recommend`
- **AI** — `/summarize`, or just mention Paradise (needs `GEMINI_API_KEY` or an OpenAI-compatible endpoint; everything else works regardless)
- `/help` lists exactly what is registered — nothing more

Choices for `/choose` and `/poll` are separated with `|` or `,`:

```text
/choose options:pizza | burgers | something responsible
```

## Voice Activity & Statistics

Paradise automatically tracks voice channel sessions with zero database spam and high resilience:

- **Session Tracking**: Tracks join, leave, channel move, and disconnects safely.
- **Restart- & Crash-Safe**: In-progress sessions are persisted with periodic heartbeats. Someone still in VC through a bot restart keeps their *original* join time (nothing is re-counted from zero); if a user left while the bot was down, their session is closed at the last confirmed heartbeat.
- **Real Names, Always**: Member names/avatars are remembered in the database and refreshed opportunistically, so the leaderboard renders real identities even after restarts, cold member caches, or members leaving voice (requires the **Server Members Intent**).
- **Midnight Splitting**: Long sessions spanning across midnight are accurately split across days (e.g. 23:50 → 00:20 becomes 10m on Day 1, 20m on Day 2).
- **Daily DM Reports**: Automatically sends each active member their own personal daily VC summary at day's end.
- **Rendered Leaderboard Card**: Programmatic Pillow renderer producing Material Expressive statistics cards with podium highlights (#1 gold, #2 silver, #3 bronze), Discord avatars, time duration pills, and percentage shares.

```text
/vc leaderboard period:Today image:True
/vc stats member:@Alex
/settimezone Asia/Kolkata
/time member:@Alex
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

## Movies, Anime & Web Series Discovery

`/example` (with live autocomplete while you type) and natural chat questions answer with a clean card: title, ⭐ ratings *per named source* (IMDb, MyAnimeList, Rotten Tomatoes, AniList, Kitsu… shown separately, never blended), 📅 release dates, 🎭 genres, 🎞️ episodes/seasons, ⏱️ runtime, 🎬 director, 🌟 cast, 🏭 studio, 📡 where to watch, and 🍿 similar titles.

```text
/example name:Interstellar
/example naruto          ← autocomplete kicks in after 2 characters
/trending category:anime mode:trending
/upcoming category:movies days:30
/recommend query:anime like Jujutsu Kaisen

what's trending right now?          ← plain chat works too
where can I watch Oppenheimer in India?
is Chainsaw Man season 2 coming?
give me 5 anime like Jujutsu Kaisen
```

- **Zero setup.** No API keys, accounts, or datasets. Every source is a key-less public API — IMDb suggestions, TVMaze, Kitsu, AniList, Jikan (MyAnimeList), Wikipedia/Wikidata, the Wikimedia pageview charts — with automatic fallback between them; the movie/TV/anime layer also reuses the bot's web search engine for streaming availability and ratings cross-checks. No single point of failure.
- **Fresh over stored.** Stable facts (synopsis, cast, genres) cache for a day; volatile facts (ratings, trending, streaming, release dates) refresh every ~45 minutes. If every source is unreachable the bot says so honestly instead of guessing — a stale card is served clearly labelled as stale.
- **Honest dates.** "2027" (a year-only announcement) is never dressed up as an exact day; approximate windows are labelled as windows.
- **Trending ≠ popular ≠ highest rated** — three separate engines, each with its own live signals (Wikimedia most-read charts, AniList trending scores, TVMaze schedule premieres, Wikidata future release dates).
- **Typo-tolerant everywhere** — `naurto` still finds Naruto; Japanese romaji and English titles are interchangeable (`shingeki no kyojin` ↔ `Attack on Titan`); ambiguous short queries like `it` offer a disambiguation of buttons instead of guessing.

## Architecture

```
bot.py                    wiring, message flow (AFK / assistant / personality), sync
config.py                 all environment configuration and tuning in one place
commands/
  fun.py                  ping, coinflip, choose
  utility.py              remind, poll, afk, translate, search, weather, convert, time, settimezone
  vc.py                   /vc leaderboard, /vc stats, /vc dmreports, voice state listeners
  dictionary.py           /dictionary + /define, chat triggers, card UI, buttons
  recipe.py               /recipe command + Components V2 recipe UI
  entertainment.py        /example /trending /upcoming /recommend + chat & mention triggers
  ai.py                   /summarize command
  system.py               /help command
  assistant.py            mention router: "@Paradise <anything>" → tools first, AI last
functions/
  router.py               deterministic intent router (no AI): calc/define/convert/weather/
                          time/translate/entertainment/search/AI priority chain
  calculator.py           safe AST-whitelisted calculator (percent idioms, word operators)
  gemini.py               Gemini REST client (key in header only, masked logs, graceful
                          invalid/quota handling, grounded reason() synthesis)
  ai.py                   provider chain: Gemini → OpenAI-compatible → none; activation
                          rules, bounded context, reason() over retrieved facts
  search/                 web search engine (DuckDuckGo, Wikipedia, caching) + answer
                          pipeline (direct answer, key points, conflict/staleness warnings,
                          source evidence, optional grounded AI rephrasing)
  translate.py            shared key-less translation helper
  weather/                Open-Meteo weather provider, fuzzy geocoding, forecast embed
  conversion/             units conversion (9 categories), currency exchange rates, natural parser
  time/                   IANA timezone resolver, city maps, time formatting
  voice/                  voice activity tracker, SQLite persistence, midnight splitting,
                          daily DM reporter, programmatic Pillow leaderboard card renderer
  entertainment/          movies/anime/web-series discovery: search & resolver, details
                          builder, verification, recommendation/trending/upcoming engines,
                          autocomplete, NLU chat parser, cache layer, response formatter
  helpers.py              shared errors, colours, parsing, formatting
  personality.py          rare contextual reactions and short replies
  presence.py             fixed «Watching Paradise» activity + rotating note
  dictionary/             dictionary engine & card renderer
  recipes/                recipe engine & bundled recipe book
assets/fonts/             bundled Inter subset for card renderers
tests/                    comprehensive offline and feature test suites
```

Design rules: all user-facing errors are friendly strings (`ParadiseError`), raw provider errors never reach users — they're logged with context instead. Secrets only ever come from the environment.

## Testing

The offline suite needs no token or network:

```bash
pytest
# or: python -m unittest discover -s tests -v
```

All 456+ test cases run completely offline and validate every subsystem: search (raw engine *and* the answer pipeline with conflict/staleness detection), the mention router and assistant dispatch, the calculator sandbox, the Gemini client and AI provider chain (key-masking, rejection/quota/fallback handling), weather, conversions, timezones, voice tracking, midnight session splitting, daily reports, image rendering, and the full entertainment pipeline (source parsers tested against real captured API payloads, resolver ranking, autocomplete, natural-language parsing, cache expiry, and end-to-end lookup flows over a fake transport).
