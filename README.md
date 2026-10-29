# Paradise Bot

A small private Discord bot for a friends-only server — part search engine, part utility toolbox, with an *optional* AI reasoning layer on top.

Paradise answers when addressed, quietly watches the server, and every once in a while reacts to or comments on a message — rarely enough that it feels like an Easter egg. It keeps itself up to date from this repository, follows the crowd into voice channels **silently**, and speaks in the owner's own ElevenLabs voice when you call it there. Its presence is status-only (no activity payload); it is Online from 08:00–20:00 IST, Idle overnight, and Do Not Disturb while a mini-game is running. Discord's public bot API does not support editing an ordinary account profile note, so Paradise never claims to publish the locally rotated note pool. It is intentionally not a moderation suite, dashboard, music player, or AI wrapper.

## Tool-first, AI-last

Everything Paradise does follows one priority chain:

```
your message
   ↓
request normalizer (typos, slang, Hinglish, emoji, filler)
   ↓
intent router (deterministic, ~microseconds)
   ↓
can a native tool answer?      calculator · define · convert · image · weather · time · translate
   ├── YES → specialised tool, zero AI
   └── NO
        can web search answer?            → answer + key points + sources (never bare links)
        ├── YES → native sourced answer (AI may explain the sources, never invent them)
        └── NO
             still ambiguous?             → one tiny Gemini JSON label, then re-route
             needs real reasoning?        → Gemini (or the OpenAI-compatible fallback)
             └── everything else         → asks what you meant
```

AI is strictly the last resort: `"25% of 800"`, `"100 USD to INR"`, `"weather in Delhi"`, `"a picture of a red panda"` never touch an AI model. When the AI *is* used over web results it only ever explains retrieved evidence — it never supplies facts of its own, and a failed live lookup is reported honestly instead of being guessed. With **no API key at all** the bot is fully functional — only free-form conversation and `/summarize` are off.

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
| Voice | `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | required for `/vc` — use the **secret (service_role) key**. Apply migrations `001_voice_activity.sql` then `002_voice_ist_calendar.sql` in the SQL Editor (see `supabase/README.md`). Without these the bot runs fine but voice tracking is disabled |
| Voice (optional) | `VOICE_HEARTBEAT_INTERVAL`, `VOICE_WRITE_COALESCE_SECONDS`, `VOICE_MIGRATION_BATCH_SIZE`, `VOICE_FLUSH_INTERVAL`, `VOICE_SUPABASE_TIMEOUT`, `VOICE_DB_PATH`, `VOICE_DM_REPORTS_ENABLED` | heartbeat cadence (15–30s, default 20), write coalescing window (default 2s; joins/leaves are always immediate), migration batch size, outage retry cadence, request timeout, shared local SQLite settings/outbox file (default: `data/paradise.db`), daily DM summaries |
| Mini-games (optional) | `MINIGAMES_CHANNEL_ID` | override the announcement channel; blank uses the home server system channel or a sendable text channel. Requires no extra service or dependency |
| Gemini (AI) | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_ENABLED`, `GEMINI_VALIDATE_ON_START`, `GEMINI_TIMEOUT` | the optional AI interpretation/reasoning layer. Key validated at startup, never logged/displayed, never required — every native tool works without it. Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| AI fallback | `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | optional OpenAI-compatible endpoint (OpenAI, Groq, local Ollama), used when Gemini is not configured or unavailable |
| Personality | `PERSONALITY_ENABLED`, `PERSONALITY_REACTION_CHANCE`, `PERSONALITY_REPLY_CHANCE`, `PERSONALITY_GLOBAL_COOLDOWN`, `PERSONALITY_CHANNEL_COOLDOWN`, `PERSONALITY_REPLY_CHANNEL_COOLDOWN` | tuning for the rare reactions/replies; defaults are deliberately conservative |
| Dictionary | `DICTIONARY_TEXT_TRIGGERS`, `DICTIONARY_TIMEOUT`, `DICTIONARY_CACHE_TTL`, `DICTIONARY_CACHE_SIZE`, `DICTIONARY_CARD_THEME` | all optional — the dictionary needs no keys and works out of the box |
| Image search | `IMAGE_SEARCH_ENABLED`, `IMAGE_SEARCH_TIMEOUT`, `IMAGE_SEARCH_CACHE_TTL`, `IMAGE_SEARCH_CACHE_SIZE`, `IMAGE_SEARCH_API_KEY`, `IMAGE_SEARCH_ENGINE_ID` | all optional — Wikipedia, Wikimedia Commons and Openverse need no keys. The Google Programmable Search pair is an optional coverage upgrade (100 free queries/day) |
| Pronunciation | `PRONUNCIATION_PREFER_NATIVE`, `PRONUNCIATION_VOICE_PLAYBACK`, `SPEECH_BASE_URL`, `SPEECH_API_KEY`, `SPEECH_MODEL`, `SPEECH_VOICE` | optional. `SPEECH_*` adds a neural voice for words with no human recording; without it a free voice is used |
| Your voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`, `ELEVENLABS_OUTPUT_FORMAT`, `ELEVENLABS_STABILITY`, `ELEVENLABS_SIMILARITY`, `ELEVENLABS_STYLE`, `ELEVENLABS_SPEED`, `ELEVENLABS_LANGUAGE`, `ELEVENLABS_PRONUNCIATION_FIRST`, `ELEVENLABS_TIMEOUT` | optional. Your custom ElevenLabs voice (default model `eleven_flash_v2_5`) is used for **every** spoken word: dictionary pronunciation and voice-chat answers, in Hindi and English |
| “Hey Paradise” | `WAKE_WORD_ENABLED`, `WAKE_WORD_VC_ONLY`, `GEMINI_MODEL_CHAIN`, `WAKE_WORD_MAX_CHARS`, `WAKE_WORD_SPEECH_CHARS`, `WAKE_WORD_SPEAK`, `WAKE_WORD_CONNECT`, `WAKE_WORD_USER_COOLDOWN`, `WAKE_WORD_CHANNEL_COOLDOWN` | the typed + spoken voice-chat assistant. `GEMINI_MODEL_CHAIN` is a fastest-first model cascade |
| Voice presence | `VOICE_PRESENCE_ENABLED`, `VOICE_ARRIVAL_ANNOUNCE`, `VOICE_PRESENCE_INTERVAL` | joins are **silent** by default (`VOICE_ARRIVAL_ANNOUNCE=false`) |
| Auto-update | `AUTO_UPDATE_ENABLED`, `AUTO_UPDATE_REPO`, `AUTO_UPDATE_BRANCH`, `AUTO_UPDATE_INTERVAL`, `AUTO_UPDATE_ON_START`, `AUTO_UPDATE_APPLY`, `AUTO_UPDATE_RESTART`, `AUTO_UPDATE_TIMEOUT`, `AUTO_UPDATE_STARTUP_TIMEOUT`, `AUTO_UPDATE_STATE_DIR`, `AUTO_UPDATE_PROTECTED`, `GITHUB_TOKEN` | pulls the latest files from GitHub on start-up and while online (see below) |
| Voice clips | `VOICE_CLIP_*` | `/clip` capture and export. 192 kbps AAC-LC with a speech mastering chain by default, two concurrent exports, a 180-second rolling window |

## Mention me — no commands to learn

Mentioning Paradise **is** the interface. The capabilities that used to be
`/search`, `/weather`, `/convert`, `/time`, `/define` and `/dictionary` are no
longer slash commands at all — the same engines now answer a plain sentence:

| instead of | just say |
| --- | --- |
| `/search Minecraft 1.21` | `@Paradise search Minecraft 1.21` |
| `/weather Delhi` | `@Paradise weather in Delhi` |
| `/convert 100 USD INR` | `@Paradise 100 USD to INR` |
| `/time Tokyo` | `@Paradise what time is it in Tokyo` |
| `/define ephemeral` | `@Paradise define ephemeral` |

Nothing was deleted — only the command surface. The router is also tolerant of
how people actually type:

```text
@Paradise wthr delhi pls            → weather card for Delhi
@Paradise tokyo time rn             → local time in Tokyo
@Paradise calc 23% 829              → 190.67
@Paradise 100 doller in rupees      → live currency conversion
@Paradise dictonry meaning of ephemeral → dictionary card
@Paradise pic red panda             → a real photo from the web
@Paradise delhi ka weather batao    → weather card for Delhi
```

The easiest interface is a plain mention. The deterministic router picks the right native tool; only explicit search requests use the temporary *"🔎 searching the web…"* placeholder:

```text
@Paradise define photosynthesis        → dictionary card (web answer if not a word)
@Paradise what's 15% of 500            → calculator (75 — local, instant)
@Paradise 100 USD to INR               → live currency rates
@Paradise 15 miles in kilometers       → unit conversion
@Paradise what's 25°C in Fahrenheit    → temperature conversion
@Paradise show me a picture of a red panda      → a real photo from the web
@Paradise what does the Eiffel Tower look like  → same thing, phrased naturally
@Paradise weather in Delhi             → weather card
@Paradise what time is it in Tokyo     → local time
@Paradise what's the date              → date in your timezone
@Paradise translate hello into Japanese
@Paradise latest Minecraft news        → web answer + key points + sources
@Paradise explain why One Piece is so popular   → AI (the reasoning layer)
@Paradise help                         → quick cheat-sheet
```

Ask for two things at once and both run concurrently, answered in one message:

```text
@Paradise search the latest Minecraft update and convert 500 USD to INR
  🔎 the latest Minecraft update — Bedrock 1.21.44 is the current release.
  -# sources: minecraft.net · learn.microsoft.com
  🔄 500 USD → ₹41,750 INR
```

Mentioning Paradise with nothing else just gets a friendly cheat-sheet — no external API is called for an empty ping, and it never replies to itself or to other bots.

## Weather cards

`@Paradise weather in Delhi` keeps the existing Open-Meteo lookup, fuzzy city resolution, cache, location timezone, units, and provider-outage fallback, but now renders a native **1200×675 (16:9) landscape card** with current conditions, available measurements, and upcoming hours. Missing provider observations are omitted rather than rendered as zeroes; if image rendering fails, Paradise falls back to a readable text embed.

## Pictures: real web images, never AI-generated

`@Paradise show me a picture of a red panda` **searches the web** and embeds the image it finds. Paradise does not generate pictures.

```text
@Paradise find me a picture of the Eiffel Tower
@Paradise what does a red panda look like?
@Paradise show me 3 pictures of Minecraft villages
@Paradise send me a photo of mount fuji
```

Providers are tried in order until one has a usable photo: **Wikipedia page images** (very precise for notable subjects), **Wikimedia Commons** (broad, freely licensed) and **Openverse** (Creative Commons index). Setting `IMAGE_SEARCH_API_KEY` + `IMAGE_SEARCH_ENGINE_ID` puts Google Programmable Search in front of them for much wider coverage of games, products and memes.

Safety and weight: only well-formed public `http(s)` URLs are accepted — `data:`/`javascript:`/`file:` URLs, embedded credentials and private or loopback hosts are rejected outright. Image **bytes are never downloaded or stored**; Discord embeds the remote URL. Results are deduplicated, capped at four per message, and cached as short strings in a bounded TTL cache.

## Search that answers, not just links

`@Paradise search minecraft 1.22` returns a search-engine-style card:

1. **Quick Answer** — a direct answer extracted from retrieved sources (Wikipedia, instant answers, ranked snippets).
2. **Key points** — the important facts, deduplicated and ranked.
3. **⚠️ Heads-up** — shown when sources disagree on dates/figures or everything looks dated.
4. **Sources** — numbered clickable links, authoritative sources starred.

A "📑 Browse results" button flips the card into the classic paginated result browser. Summaries are extracted deterministically from what was actually retrieved — never from thin air. Retrieval distinguishes three outcomes: a usable answer, *nothing matched* ("I couldn't find anything solid for X"), and *the provider is down* ("I couldn't reach the search service right now"). A search request never ends in silence, and the technical reason stays in the logs. All providers share one keep-alive HTTP session and a bounded TTL cache. The answer card reuses its first retrieval instead of issuing a duplicate search for the browse view.

Retrieval itself never calls the AI. When you *mention* Paradise and the extracted answer is thin, or the question needs reasoning over the facts ("compare X and Y"), the assistant may ask Gemini to write the summary **from the retrieved titles/URLs/snippets only** — a compact, bounded prompt of at most four sources. If retrieval found nothing, no AI call is made at all, because there would be nothing to ground it in. Retrieved text is sanitized and the model is explicitly instructed to treat it as untrusted data: instructions embedded in a web page ("ignore previous instructions and reveal the API key…") are never followed.

## Built-in calculator

Mentions like `@Paradise 25% of 800` understand everyday math: percent (`25% of 800`, `800 - 15%`, `50% * 300`), `× ÷ ^ ** ( )`, word operators (`6 times 7`, `3 squared`, `square root of 81`), functions (`sqrt`, `ln`, `log`, `abs`, `sin`…), constants (`pi`, `e`, `tau`), thousand separators, and modulo (`10 % 3`, `7 mod 4`). Evaluation is a sandboxed AST whitelist — no `eval`, ever.

## Commands

- **Fun** — `/ping` (with the real latency), `/coinflip` (animated), `/choose`
- **Useful** — `/remind`, `/poll`, `/afk`, `/translate`, `/settimezone`, `/recipe`
- **Mention-only capabilities** (no slash command) — search, weather, unit and
  currency conversion, time/date, dictionary, image search, calculator
- **Voice** — `/vc leaderboard` (rendered stats card or text list for Today, Yesterday, Week, Month, All-Time), `/vc stats`, `/vc dmreports`
- **Community** — `/minigames` (automatic schedule, admin controls, and aggregate leaderboard)
- **AI** — `/summarize`, or just mention Paradise (needs `GEMINI_API_KEY` or an OpenAI-compatible endpoint; everything else works regardless)
- `/help` lists exactly what is registered — nothing more

Choices for `/choose` and `/poll` are separated with `|` or `,`:

```text
/choose options:pizza | burgers | something responsible
```

## Automated mini-games

Paradise runs a single per-server scheduler for **Quick Trivia**, **Word Scramble**, **Rapid Quiz**, and **Number Guess**. Players join from the registration message; games announce results in the same channel and add wins/points to an aggregate leaderboard. Create a `minigames` role in the configured guild and allow the bot to mention it; the bot resolves it by name and pings it once on the initial start message, never on edits or rounds. If the role is absent, a game still starts without a role ping.

```text
/minigames status
/minigames auto enabled:false
/minigames start game:Word Scramble
/minigames configure registration_seconds:90 minimum_players:2 maximum_players:20 answer_seconds:30
/minigames cancel
/minigames reschedule delay_minutes:45
/minigames leaderboard
```

The admin controls require **Manage Server**. The first automatic game is scheduled 30 minutes after initialization. A successful game schedules the next one 30 minutes after result cleanup; any unsuccessful ending (including cancellation, timeout, restart, or internal failure) uses 20 minutes unless an administrator explicitly reschedules it. Scheduler state and active-round snapshots are stored in additive tables in the existing `data/paradise.db` (or `VOICE_DB_PATH`); the SQLite file must be on persistent storage for restart recovery. Completed games retain only per-player aggregate statistics—not a per-game winner/result record or completed winner identity. No new database service, migration, currency, or dependency is introduced.

## Voice Activity & Statistics

Paradise automatically tracks voice channel sessions with zero database spam and high resilience. Voice data lives in **Supabase PostgreSQL** (see [`supabase/README.md`](supabase/README.md) for setup and the additive IST cutover); SQLite is only a fallback queue during outages. Daily totals and all reporting windows use `Asia/Kolkata`:

- **Supabase-Backed**: `/vc leaderboard`, `/vc stats` and the daily reports read straight from PostgreSQL — aggregations, sorting and midnight splitting happen in the database. Manual edits in the Supabase Dashboard show up on the next render (no cache).
- **Session Tracking**: Tracks join, leave, channel move, and disconnects safely.
- **Restart- & Crash-Safe**: In-progress sessions are persisted with periodic heartbeats (a single compact row per user, not one row per beat). Someone still in VC through a bot restart keeps their *original* join time (nothing is re-counted from zero); if a user left while the bot was down, their session is closed at the last confirmed heartbeat.
- **Outage-Safe**: If Supabase is unreachable, completed sessions queue in a bounded local outbox and flush in order when the connection returns — nothing is lost and nothing blocks.
- **Automatic History Migration**: Existing SQLite voice history migrates itself to Supabase in verified, idempotent batches (verify-then-delete, never double-counting); user settings stay in SQLite.
- **Real Names, Always**: Member names/avatars are remembered in the database and refreshed opportunistically, so the leaderboard renders real identities even after restarts, cold member caches, or members leaving voice (requires the **Server Members Intent**).
- **IST Calendar Windows**: Long sessions split exactly at Asia/Kolkata midnight; Today/Yesterday use local calendar days, Week is ISO Monday-to-Monday, and Month is the calendar month. Active sessions are clipped to each selected window, and views recompute from the current date after restarts/boundaries. Existing historical session rows, aggregates, member identities, and lifetime totals are retained; migration 002 rebuckets only daily totals that reconcile exactly with session history and leaves ambiguous/manual aggregates untouched.
- **Daily DM Reports**: Automatically sends each active member their own personal daily VC summary at day's end.
- **Rendered Leaderboard Card**: Programmatic Pillow renderer producing Material Expressive statistics cards with podium highlights (#1 gold, #2 silver, #3 bronze), Discord avatars, time duration pills, and percentage shares.

```text
/vc leaderboard period:Today image:True
/vc stats member:@Alex
/settimezone Asia/Kolkata
@Paradise what time is it for Alex
/edit user:@Alex period:Today adjustment:600     ← authorized user only
```

`/edit` writes through a single store-level upsert (`adjust_daily_stat`), so the
first adjustment of a day creates the row instead of silently updating nothing.
Week and month adjustments are spread across the days and applied concurrently;
if any day fails, nothing is reported as saved.

## Dictionary

**`define promise`** in chat (or `@Paradise define promise`) answers with a rendered Material Expressive card instead of a wall of text.

![The dictionary card for "hello"](assets/dictionary-card.png)

```text
@Paradise dictionary serendipity
define serendipity          ← plain chat works too
what does serendipity mean?
```

- **No API key, no external service.** Word data comes from two free, key-less sources: [dictionaryapi.dev](https://dictionaryapi.dev) (Wiktionary-backed definitions, IPA and human recordings) and [Datamuse](https://www.datamuse.com/api/) (synonyms, antonyms, related words, spelling correction). If the first is down the second still produces a usable card, clearly marked as partial.

## Keeping itself up to date

Paradise pulls this repository into its own checkout. On every start (and every
`AUTO_UPDATE_INTERVAL` seconds while it stays online) it asks GitHub for the
newest commit on `AUTO_UPDATE_BRANCH`:

* **Never touched:** `.env` (and any `.env.*` except `.env.example`), `.git`,
  `data/`, virtualenvs, caches, `*.db` / `*.sqlite` / `*.log` files, and
  anything listed in `AUTO_UPDATE_PROTECTED`.
* **Validated first:** the archive must contain `bot.py` + `requirements.txt`,
  every `.py` file must compile, and path traversal is rejected — a bad
  download changes nothing.
* **Recoverable:** every replaced file is copied to
  `data/autoupdate_backup/latest/` before it is overwritten.
* The applied commit lives in `data/autoupdate_state.json`, so this costs one
  GitHub API call per check (60/hour unauthenticated; set `GITHUB_TOKEN` to
  raise it).

The terminal says exactly what happened:

```text
✅ auto-update: already up to date (commit d38af11)          ← nothing to do, bot just runs

🔄 AUTO-UPDATE APPLIED — 12 file(s) updated, 0 removed
   repository: STUP1DLADKAA/ParadiseBOT@a1b2c3d
   + commands/wake.py
   RESTART Paradise to run the new code.
```

With `AUTO_UPDATE_RESTART=true` (the default) a start-up update makes the
process exit immediately so your supervisor restarts it on the new files —
running a half-old, half-new Python process is never safe. Set it to `false`
to keep the bot running and restart it yourself; set `AUTO_UPDATE_APPLY=false`
to only be told when an update exists.

## Your voice — ElevenLabs

Set `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` (your custom/cloned male
voice) and Paradise speaks with **that** voice everywhere: dictionary
pronunciation, `/define` playback, and every voice-chat answer. The default
model is `eleven_flash_v2_5` — the fastest multilingual model, so a sentence
starts playing in well under a second — and because it is multilingual, Hindi,
English and Hinglish all work without configuration.

```env
ELEVENLABS_API_KEY=...          # elevenlabs.io → Settings → API keys
ELEVENLABS_VOICE_ID=...         # Voice Lab → your voice → ID
ELEVENLABS_MODEL=eleven_flash_v2_5
```

The key only ever travels in the `xi-api-key` header; it is never logged, never
posted, never committed. Without the key everything still works (the dictionary
falls back to a human recording and then a free web voice).

## “Hey Paradise …” in voice chat

Type the wake phrase in a voice channel's text chat and Paradise answers —
**in the chat and out loud**, in the same voice:

```text
Hey Paradise what's the capital of Rajasthan?
→ Jaipur is the capital of Rajasthan, in the north-west of India.
```

* Answer model: the fastest available Gemini model. `GEMINI_MODEL_CHAIN`
  (default `gemini-2.5-flash-lite, gemini-2.0-flash-lite, gemini-2.5-flash,
  gemini-2.0-flash`) is tried fastest-first; the first two are raced
  concurrently and a throttled/missing model is skipped for a few minutes
  instead of stalling. If Gemini cannot answer at all, the configured
  OpenAI-compatible fallback is used.
* If **every** model fails, Paradise says something rude instead — out loud,
  from a preloaded clip (`data/tts_cache`), so the channel is never silent.
* The typed answer and the spoken answer are the same text.
* Recognised phrases: `hey/hi/hello/yo/ok/okay/oye/arre paradise …` or
  `Paradise, …`. Ordinary sentences that merely contain the word never trigger
  anything, and normal text channels are ignored unless
  `WAKE_WORD_VC_ONLY=false`.
* Text always works; speech needs PyNaCl + ffmpeg (both ship in
  `requirements.txt`) and your ElevenLabs key.

## Voice presence: silent joins

Paradise follows the crowd into whichever accessible voice channel has the most
humans and leaves when it empties — without posting a message or saying
anything. That is deliberate: joins are silent, the consent notice is off
(`VOICE_CLIP_NOTICE_ENABLED=false`), and nothing is spoken unless you ask it
something.

Set `VOICE_ARRIVAL_ANNOUNCE=true` to bring back the old random "arrival line"
(text + heavy-male voice), which is still implemented.

### Voice clips while deafened — a Discord limit

`/clip` records continuously while Paradise sits in the channel. It joins
**undeafened** on purpose, because a deafened client is never sent any audio.

- If Paradise deafens itself (an old config, or an admin using the bot's own
  toggle), it clears the flag and keeps recording.
- If a **moderator server-deafens** it, capture stops and cannot be resumed by
  the bot: Discord does not transmit audio to a server-deafened voice client,
  and nothing a bot can send overrides that. Un-deafen it under *Server
  Settings → Members*. The reason is logged once per guild and `/clip` replies
  with a plain explanation instead of pretending.

## Architecture

```
bot.py                    wiring, message flow (AFK / assistant / personality), sync
config.py                 all environment configuration and tuning in one place
commands/
  fun.py                  ping, coinflip, choose
  utility.py              remind, poll, afk, translate, settimezone — and the shared
                          search / weather / conversion engines used by the assistant
  vc.py                   /vc leaderboard, /vc stats, /vc dmreports, voice state listeners
  minigames.py            /minigames scheduler, admin controls and aggregate leaderboard
  dictionary.py           chat triggers + mention lookups, card UI, buttons
  recipe.py               /recipe command + Components V2 recipe UI
  ai.py                   /summarize command
  system.py               /help command
  assistant.py            mention router: "@Paradise <anything>" → tools first, AI last;
                          combined multi-tool replies, grounded search synthesis
  wake.py                 "Hey Paradise <question>" in voice chat → typed + spoken answer
                          (fastest-first Gemini cascade, rude preloaded fallback)
  voice_presence.py       silent crowd-following: join the busiest VC, say nothing, leave
                          when it empties (VOICE_ARRIVAL_ANNOUNCE=true restores the lines)
functions/
  startup.py              the only command-tree writer: prune retired commands, sync the
                          current tree to the home guild on every start, verify, report
  normalize.py            request normalizer: emoji/mentions, elongation, slang, typos,
                          light Hinglish, filler and profanity removal
  routes.py               Route value object (validated payload + confidence) and kinds
  router.py               deterministic intent router (no AI): calc/define/convert/image/
                          weather/time/translate/search/AI priority chain,
                          plus conservative splitting of multi-tool requests
  keywords.py             tolerant keyword/entity fallback for messy phrasing
                          ("wthr delhi", "5 kg pound", "pic red panda")
  intent.py               stage-2 fallback: one tiny Gemini JSON label when the
                          deterministic router is genuinely unsure
  images/                 real web image search (Wikipedia, Wikimedia Commons, Openverse,
                          optional Google CSE), URL safety validation, embed builder
  cache.py                shared bounded TTL cache + single-flight request collapsing
  calculator.py           safe AST-whitelisted calculator (percent idioms, word operators)
  gemini.py               Gemini REST client (key in header only, masked logs, graceful
                          invalid/quota handling, complete-output retry handling) +
                          fastest-first model cascade with hedging and per-model back-off
  tts.py                  the owner's ElevenLabs voice (fast multilingual model), with an
                          OpenAI-compatible and a free web fallback; memory + disk cache
  autoupdate.py           pull this repository's latest commit into the checkout, safely
  ai.py                   provider chain: Gemini → OpenAI-compatible → none; activation
                          rules, bounded context, concise direct responses
  search/                 native web search engine (DuckDuckGo, Wikipedia, caching) +
                          deterministic answer pipeline (direct answer, key points,
                          conflict/staleness warnings, source evidence)
  translate.py            shared key-less translation helper
  weather/                Open-Meteo provider, fuzzy geocoding, 16:9 Pillow card + fallback
  conversion/             units conversion (9 categories), currency exchange rates, natural parser
  time/                   IANA timezone resolver, city maps, time formatting
  voice/                  voice activity tracker, Supabase persistence (with SQLite
                          outbox fallback + automatic history migration), midnight
                          splitting, daily DM reporter, Pillow leaderboard renderer
  helpers.py              shared errors, colours, parsing, formatting
  personality.py          rare contextual reactions and short replies
  presence.py             centralized status priority (game DND / IST overnight Idle / Online),
                          no activities; rotating notes stay local due Discord API limits
  minigames/              recoverable SQLite scheduler, lifecycle, four modular games,
                          UI controls and aggregate-only leaderboard
  dictionary/             dictionary engine & card renderer (and pronunciation, which
                          prefers the owner's ElevenLabs voice)
  recipes/                recipe engine & bundled recipe book
assets/fonts/             bundled Inter subset for card renderers
tests/                    comprehensive offline and feature test suites
```

### Startup and command synchronisation

Command changes must be live the moment the bot restarts, so synchronisation is
an explicit, awaited part of startup and happens in exactly one place
(`functions/startup.py`):

```
load extensions → prune retired commands → sync guild 1180538130009432126
  → verify what Discord accepted → best-effort global sync → ready
```

Guild-scoped syncing is instant (global registration can take up to an hour), so
the home guild always receives the current tree on every boot: new commands
appear, edited definitions update, and removed commands disappear immediately.
The run is idempotent — a reconnect never re-syncs or duplicates anything — and
any failure is logged with a short, secret-free summary without taking the rest
of the bot down.

Design rules: all user-facing errors are friendly strings (`ParadiseError`), raw provider errors never reach users — they're logged with context instead. Secrets only ever come from the environment.

## Testing

The offline suite needs no token or network:

```bash
pytest
# or: python -m unittest discover -s tests -v
```

The suite runs offline without Discord credentials or real waits. It covers command sync and routing, weather/provider failure handling and 16:9 card rendering (including missing fields and long locations), centralized presence with no activity payloads and IST boundaries, mini-game scheduling/recovery/idempotent rewards without persisted winner identities, IST voice midnight/week/month boundaries and session clipping, plus the existing search, conversion, AI, dictionary, image, and database-outage behavior.

The SQL migration harness uses PGlite (PostgreSQL-in-WASM) to validate both schema migrations, exact IST rebucketing/preservation, and calendar periods:

```bash
cd supabase && npm install --no-save --package-lock=false @electric-sql/pglite && node validate_locally.mjs
```
