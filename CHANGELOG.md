# Changelog

All notable changes to Paradise are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- **Real web research, by plain mention.** "search the latest Minecraft
  update", "latest Minecraft news", "who won the match" now run a keyless
  research pipeline: DuckDuckGo-lite + Wikipedia search, the top pages
  fetched and read (SSRF-guarded, text-extracted), then a grounded answer
  with clickable sources — the model is told to cite [n], never invent a
  citation, and treat page content as untrusted data (prompt-injection
  defence). Fetched/returned sources are the only ones ever listed; when
  the web is off or failing, the same question falls straight back to the
  direct model answer. Link summaries too: "summarize this link: <url>"
  (or a bare URL) fetches the page and summarises it, and says so honestly
  when a site refuses. Configurable via `WEB_SEARCH_*` in .env (all
  optional, on by default); every search and page is cached and
  single-flighted so five people asking the same thing cost one fetch.
- **Writing help, by mention.** "rewrite this to be formal: <text>",
  "make this sarcastic", "draft an announcement for movie night", "what
  should I reply to <message>", "action items from <text>", "tone of
  <text>", "shorten this: <text>" — copy-ready output that keeps the
  author's voice, on the deep model, with the deixis ("this to be formal:")
  stripped before the text reaches the model.
- **Dev & Minecraft help, by mention.** "explain this error: <traceback>",
  "review my code <snippet>", "regex for <goal>", "git undo last commit",
  "sql query to …", "minecraft command …", "what does http 429 mean" —
  text-only reasoning with an explicit never-executes rule; payloads are
  framed as untrusted data (BEGIN/END untrusted blocks).
- **Calculator learned lists and equations.** "average of 4 8 15 16 23 42",
  "sum of 3, 5, and 7", "median of …", "min/max/product of …", and
  single-variable linear equations ("solve 2x + 3 = 11" → x = 4). Linear
  equations are verified to be linear (f(0), f(1), f(2)) — x² = 4 is
  declined honestly instead of guessed; always-true and never-true
  equations are named as such.
- **Group utilities, all offline:** random balanced team splitter ("make
  two teams from A, B, C, D", any count 2–8), cross-timezone meeting
  planner ("what time is it in Tokyo when it's 8 pm in India", both
  phrasings, next-occurrence semantics), weekday lookup ("what day was
  15 august 1947"), copy-ready Discord timestamps ("discord timestamp for
  tomorrow 8 pm", `<t:…:F>`/`<t:…:R>`), JSON validation with
  position-precise errors + pretty-printing, list sorting and dedupe, and
  a Flesch reading-ease / grade-level score.
- **Channel history search.** "find the message where taher said hello"
  / "search this channel for pizza" scans the last 300 messages and
  replies with jump links.
- **Contextual follow-ups.** "what about london?" right after a
  time/weather/convert/translate/research ask re-runs the same tool with
  the new subject (five-minute window, per channel). Only fires when
  nothing else claims the message, so "roll a d20" still rolls a d20.
- **~24 new capabilities per plain @ping, all free and offline.** A
  deterministic toolkit (functions/toolkit.py) answers instantly, with no
  API key and no AI: dice (`roll 2d20+3`, `roll a d20`), magic 8-ball,
  rock-paper-scissors, random numbers (`pick a number between 1 and 100`),
  QR codes (rendered offline with the new `qrcode` dependency), strong
  passwords, UUIDs, lorem-ipsum, ROT13/Caesar ciphers, base64, SHA-256/MD5
  hashes, Morse code, reverse/mock-case/count/emojify/spoiler text tools,
  countdowns ("days until christmas", "how long until 25 dec 2026") and
  colour cards with a drawn swatch. All of them join the compound router
  ("weather in Delhi and roll a d20" answers both) and the AI classifier
  learned the labels, so loose phrasings still land on the right tool.
- **Reminders, polls and info cards by mention.** "remind me in 20 minutes
  to check the oven" uses the /remind engine (refactored into a shared
  `create_reminder`), "poll: pizza or burgers" deploys the reaction poll,
  and "who is @user" / "avatar" / "server info" post Discord-native cards.
- **Multi-model brains (`AI_MODELS`).** Several models can share one
  OpenAI-compatible endpoint, each tagged with a role:
  `AI_MODELS=fast:gpt-4o-mini,deep:gpt-4o`. Fast models answer labels,
  quips and anything latency-sensitive; deep models answer real questions
  and recaps. A model that 429s/500s is skipped for a minute and the next
  one answers mid-flight — then Gemini, as always. Single-model setups are
  unchanged (`AI_MODEL` still does everything).

### Changed
- **Search asks route through research, not just the model.** The router's
  search/fresh lanes now emit a RESEARCH kind (the assistant falls back to
  the AI when the web is off); the tolerant keyword stage got the same
  discipline — "sort of tired today" is conversation, not a web lookup
  (fresh words alone no longer trigger search unless the sentence also
  looks like an information ask). Classifier labels learned research,
  news, link, write/rewrite and devhelp.
- **Faster for everyone, fairer under load.** Up to 12 AI requests in
  flight at once (up from 8), and a per-member ceiling of 2 concurrent
  requests — one person machine-gunning questions can never starve the
  rest of the server. Per-user AI cooldown 8 s → 6 s.
- **Alive, but controlled.** Every ambient personality chance is lower
  (reactions 0.22 → 0.18, memes 0.16 → 0.10, quips 0.14 → 0.08, roasts
  0.12 → 0.08, GIFs 0.10 → 0.06, replies 0.12 → 0.08, ambient reactions
  0.10 → 0.06), the interjection chance halved (0.08 → 0.04) with a
  10-minute cooldown, and a new hard **ambient budget**
  (`PERSONALITY_AMBIENT_HOURLY_CAP`, default 6) caps *unprompted* chatter
  per channel per rolling hour. Mentions, invited roasts and grammar bait
  are never budget-limited: Paradise still answers instantly whenever
  someone actually talks to it.

### Removed
- **The "chat is silent" watcher is gone.** The aliveness feature — the
  lines Paradise posted when nobody had typed for a while or everyone had
  moved into a voice call — has been removed entirely, along with its
  `ALIVENESS_*` knobs and the optional Presence Intent (which existed only
  to count online members for those lines). The bot still follows everyone
  into voice channels silently; it just no longer narrates a quiet server.

### Changed
- **Way more context.** The AI now sees the last 32 messages of channel
  context (up from 16; 6,000 characters, up from 3,200) and keeps an
  80-message rolling history (up from 40), so answers track the actual
  conversation.
- **Faster and more parallel.** Up to 8 AI requests in flight at once
  (up from 4), a shorter per-user cooldown (12 s → 8 s) and per-channel
  cooldown (5 s → 3 s): a busy server no longer feels a queue.
- **Memories are relevant or silent.** The memory lookup no longer pads an
  unrelated question with the most recent stored facts — a memory now
  reaches the prompt only when it is about someone in the conversation or
  actually matches the words. No more dragging old business into random
  replies.
- **The bot never admits to remembering.** When a `/memory` fact surfaces —
  in an AI answer, a roast, or the personality's rare callback — it is framed
  as something Paradise *already knew*, not something it was taught: no "i
  remember", no "you told me", no mention of memory, and if someone asks how
  it knows, it stays smug and vague. The prompt block now presents facts as
  prior knowledge ("you just know this stuff — you pay attention to everyone
  in this server"), the roast ammunition likewise, and the personality
  callback frames were rewritten to match (`"spent a year reaching level 100
  in murder mystery" — i know everything about you people, Ash. everything.`).
  The `/memory` command itself still speaks plainly — it is the management
  screen. `functions/ai.py`, `functions/personality.py`.
- **The persona got a rewrite: short, human, funny-not-brutal.** The system
  prompt was rebuilt around how the bot should *talk* — 1–3 sentence replies,
  casual-but-correct grammar, plain words, no jargon, no corporate speak — and
  softened from "resident menace" to the server's funny friend (still cheeky,
  still never refuses a genuine question, still loyal to the lord). It is also
  ~70% smaller (8.4k → 2.4k characters), which makes every AI turn faster.
- **Faster replies across the board.** Smaller response budgets (200/300/500
  tokens by question size), a lower per-user (12 s) and per-channel (5 s) AI
  cooldown, shorter assistant (45 s) and classifier (6 s) timeouts, and a
  smaller `answer_quick` budget (220 tokens).
- **Better chat context.** The AI now sees 16 recent messages (up from 8) with
  3200 characters of channel context (up from 2400) and a 40-message rolling
  history, plus remembered facts from `/memory` when they are relevant to the
  question.
- More memes (`PERSONALITY_MEME_CHANCE` 0.10 → 0.16), more GIFs
  (`PERSONALITY_GIF_CHANCE` 0.05 → 0.10), more replies (0.10 → 0.12) and more
  quips (0.12 → 0.14) — the bot is louder and more alive by default.

### Added
- **More capabilities per plain @ping.** `@Paradise summarize` (also "recap",
  "tl;dr", "what did i miss", "catch me up") recaps the recent conversation
  with the same AI pipeline as /summarize; `@Paradise meme [about X]` and
  `@Paradise gif [of X]` (or just "bruh gif") post a meme or reaction GIF
  through the same tiers as the slash commands — keyless by default, with a
  locally drawn card as the meme fallback. The AI intent classifier learned
  the three new labels too, so loosely phrased asks ("drop something funny",
  "gif me something") route to the right tool instead of a chat answer.
- **Taher is a lord too.** User `1049231767585964032` (Taher) now stands at
  exactly the same level of authority as Stupid (`922359954025885737`): both
  are lords and masters of everyone, both addressed as "my lord", both
  honoured (crown reactions, loyal lines, name defence — disparage "taher" in
  a sentence and Paradise takes it personally) and both still teased, because
  that is what lords are for. `/memory`, `/edit`, `/clip` and the
  deafened-VC exemption now admit both of them. Configure the pair with
  `PARADISE_LORD_USER_IDS` (comma-separated; the legacy single-id
  `PARADISE_LORD_USER_ID` still works and merges in). `config.py`,
  `functions/ai.py`, `functions/personality.py`.
- **Long-term memory.** `/memory add|list|remove|clear` — the lord
  (`PARADISE_LORD_USER_ID`, default `922359954025885737`) teaches Paradise
  facts, preferences and server lore, stored in a tiny capped SQLite database
  (`data/memory.db`, `MEMORY_MAX_ENTRIES`). Every AI answer from then on folds
  the relevant memories into its prompt, so replies feel like they come from
  someone who has lived in the server. The command is the lord's alone;
  everyone else is told the memories aren't theirs to touch.
  `functions/memory.py`, `commands/memory.py`.
- **Memories are about people, and they surface at the right moment.**
  `/memory add <fact> user:<@member>` (or just mentioning them in the fact)
  files the memory *under that member*; the AI then sees it only when that
  member is the one talking, is mentioned in the question, or is the target of
  a roast — never force-dropped into unrelated replies. `/memory list` marks
  each entry `[about NAME]`, and old databases migrate in place.
- **Memories survive a reinstall — Supabase.** With the same
  `SUPABASE_URL`/`SUPABASE_SECRET_KEY` as the voice tracker and
  [`supabase/migrations/005_memory.sql`](supabase/migrations/005_memory.sql)
  applied, every memory is mirrored to the `paradise_memories` table (RLS on,
  service-role only) and back: new local rows are pushed, rows added in the
  Dashboard are pulled, rows deleted there disappear here, and the local ids
  *are* the remote primary keys. SQLite stays the working store; Supabase is
  the durable copy, reconciled every couple of minutes
  (`MEMORY_SUPABASE_INTERVAL_SECONDS`) and never on the reply path.
- **Parallel conversations.** Several members can talk to the bot at once and
  every one of them gets an answer: AI requests run concurrently up to a small
  ceiling (4), one member's cooldown no longer gates another's (per-user
  cooldowns only), short waits are queued instead of dropped, and the one-shot
  intent classifier never queues behind long answers — it fails over to its
  fallback the moment the provider is busy.
- **Memory callbacks in the personality.** Rarely (default 0.05, once every
  30 minutes per person at most) the bot quotes a remembered fact back at the
  person it is about — verbatim, cleaned, capped at 90 characters, no AI call.
  `PERSONALITY_MEMORY_CALLBACK_CHANCE`, `PERSONALITY_MEMORY_CALLBACK_COOLDOWN`.
- **The aliveness watcher.** Paradise now notices when its own server goes
  quiet. When nobody has typed for 25 minutes (`ALIVENESS_DEAD_AFTER_MINUTES`)
  — or when everyone who was chatting has moved into a voice call — it
  composes one short, situation-aware line from the real signals (minutes of
  silence, who is in VC and for how long, member count, IST time, optionally
  online count) and posts it in the home channel. AI-written when configured,
  canned-but-situational otherwise, always bounded by a 45-minute cooldown and
  01:00–07:00 IST quiet hours. `functions/aliveness.py`,
  `commands/aliveness.py`.
- **The lord is acknowledged.** When user `922359954025885737` himself speaks,
  a small chance (`PERSONALITY_LORD_REPLY_CHANCE`, 0.20) of a loyal,
  slightly-cheeky line ("my lord has spoken 👑") or a crown reaction — and the
  AI now addresses him as "my lord" whenever he asks something, while still
  teasing him.
- **Conversation interjections.** When two or more people are talking in a
  channel (ten-minute window), a small chance
  (`PERSONALITY_INTERJECT_CHANCE`, 0.08) of a "hello!"-style line of its own,
  with its own cooldown (`PERSONALITY_INTERJECT_COOLDOWN`, 240 s).
- **Context roasts.** When the roast target's last words are long enough, the
  roast quotes them back at them ("bro woke up today and chose to say …
  out loud."); roasting the lord uses loyal-but-cheeky frames instead.
  `@Paradise roast <someone>` got the same upgrade: the AI teases the target
  about what they actually just said, with the roast book as the fallback.
- **Keyless reaction GIFs.** GIFs work on a fresh `.env`: Tenor's legacy
  public endpoint is the keyless fallback tier behind the keyed Tenor/Giphy
  tiers, and if it ever dies, every call quietly degrades to a meme.
  `functions/meme_feed.py`.
- Two more meme communities in the default rotation:
  `terriblefacebookmemes`, `historymemes`.
- **The ParadiseBOT character bible.** The AI's system prompt
  (`functions/ai.py`) is the full personality: resident menace of the server,
  chronically online, terminally sarcastic, ragebait enthusiast, toxic (but
  never hateful) humour, deadpan/absurd/self-deprecating jokes, natural moods
  (menace, intellectual, sleep-deprived, competitive, disappointed…), openly
  flawed, and fiercely loyal to Stupid — the lord and founding father gets
  defended with signature aggression the moment an outsider disrespects him.
  Genuine questions are never refused: no moralising, no "as an AI", no
  watered-down non-answers. The same hard lines as the roast book still apply
  (behaviour, not identity; jokes off when somebody sounds hurt).
- **The supreme loyalty, passively.** A `loyalty` pattern at the top of the
  personality engine: any message containing "stupid" can earn a defence line
  ("watch your mouth. that's the founding father you're talking about."),
  and the greeting pattern learned the character bible's arrivals ("Oh look
  who's alive.", "There goes the neighbourhood.").
- `AIService.answer_question()` — the direct, in-character answer path that
  search requests are forwarded to.
- **It talks back now.** The personality engine went from "rare and polite" to
  actually present: more than double the chance of a reaction or a reply, an
  ambient reaction on messages that match nothing at all, a shorter global and
  per-channel cooldown, and eleven new conversation patterns (copium, ratio,
  sus, based, rizz, school, gym, birthdays, boredom…). Every knob is still a
  `PERSONALITY_*` setting. [docs/humour.md](docs/humour.md).
- **Roasts.** A roast book with three intensities (`PERSONALITY_ROAST_INTENSITY`
  = mild / spicy / nuclear), comebacks for cocky messages ("i'm him", "1v1 me",
  "skill issue"), and `/roast [member]`. Aimed at what somebody said or did —
  never at who they are — with no slurs in the book, a runtime filter, a
  six-hour "stop roasting me" opt-out and a per-person cooldown so nobody is
  farmed. `functions/roasts.py`.
- **Memes from the internet.** Real posts from Reddit communities through
  meme-api.com — no API key. HTTPS-only, CDN allow-list, image extensions only,
  NSFW/spoiler filtering, one-line titles, a small cache, prefetch and a
  backoff when the API is down. The locally drawn meme card remains the
  fallback, so the feature never goes dark. `functions/meme_feed.py`.
- **Reaction GIFs.** Tenor or Giphy with a free key (`TENOR_API_KEY` /
  `GIPHY_API_KEY`); without one the automatic path is skipped and `/gif`
  explains itself and sends a meme instead.
- `/meme [topic]`, `/gif <query>`, `/roast [member]`, and `@Paradise roast me`
  routed through the mention assistant (a new `roast` route kind, so it never
  costs a model call).
- `PERSONALITY_AMBIENT_REACTION_CHANCE`, `PERSONALITY_GIF_CHANCE`,
  `PERSONALITY_ONLINE_MEME_CHANCE`, `PERSONALITY_ROAST_CHANCE`,
  `PERSONALITY_ROAST_INTENSITY`, `PERSONALITY_ROAST_USER_COOLDOWN`,
  `MEME_SOURCES`, `MEME_TIMEOUT`, `TENOR_API_KEY`, `GIPHY_API_KEY`.

### Changed
- **The OpenAI-compatible provider is now the primary.** Every AI request
  (`chat`, `answer_quick`, `reason`, `answer_question`) goes to
  `AI_BASE_URL`/`AI_MODEL` first and falls back to Gemini only when that
  endpoint is unconfigured, rejected or cooling down. `provider_name` and
  `describe()` report the new order. Temperature for conversational calls rose
  to match the unhinged persona.
- **Search no longer reaches the internet.** "search …" (and anything the
  router shapes as a search) is forwarded straight to the AI, which answers
  in character; the placeholder card, the sourced-answer pipeline and the
  browse view are gone with it. Dictionary misses and failed image lookups
  fall back to the same AI path. `functions/search/` and its engines were
  removed; image search (real photos) is untouched.
- **The bot is lighter and faster to run.** With speech recognition gone, there
  is no model in memory, no per-utterance recognition on the CPU and no worker
  thread for it; the receive path only decrypts and buffers. One export runs at
  a time (a 60-second clip encodes in about a second, so the two-deep queue is
  invisible), the per-guild buffer ceiling dropped 48 → 32 MiB, and a bot with
  `VOICE_CLIP_ENABLED=false` connects with a plain voice client and decrypts
  nothing at all.
- **`/clip` sound is clearer at the same size.** The mastering chain is
  `highpass=f=70,lowpass=f=18500,alimiter=…`: the old 16 kHz cut was a telephone
  master that dulled sibilance, and 18.5 kHz keeps the top of the speech band
  while staying under Nyquist. 128 kbps AAC-LC mono at 48 kHz is untouched — a
  minute is ~950 KB, a full three-minute window ~2.8 MB, one encode, no
  resample, `+faststart` so Discord can start playing immediately.
- The AI persona is allowed to be funny on purpose: banter and roasts are
  explicitly in the system prompt, with the same hard limits as the roast book
  (no identity attacks, no slurs, no pile-ons, and jokes off entirely when
  somebody sounds hurt).
- Default personality pacing: reaction 0.045 → 0.22, reply 0.015 → 0.10, meme
  0.045 → 0.10, quip 0.035 → 0.12, global cooldown 45 s → 10 s, channel cooldown
  120 s → 35 s, humour cooldown 150 s → 45 s, reply cooldown 600 s → 150 s.

### Changed
- **The `/vc` leaderboard is a page now, not a poster.** White cards on a light
  grey page, one accent per period, a 44 px period title, one grouped list
  instead of a box per person, hairline separators, real (drawn) shadows and
  never a second headline in the embed. Contrast is tested: primary text is
  AAA on white, secondary is AA, and accents stay visible.
- **The daily DM reads like a report from a person.** One line naming the day,
  one about your own time, one comparison in words ("42m more than Tuesday"),
  then two numbers: where you landed and what the whole server did. No emoji
  labels, no "Keep talking. Paradise was definitely not quiet today." The daily
  channel post uses the same framing.
- `/vc stats` groups its numbers the way a settings screen would; leaderboard
  paging is a quiet "page 2 of 3" in the footer only.
### Removed
- **The "hey paradise" assistant, and everything that spoke.** `commands/wake.py`,
  `functions/voice_wake/` (listener, endpointing, local Whisper, cloud
  recognition, cue), `docs/voice-wake.md`, `requirements-local-stt.txt`,
  `tools/fetch_whisper_model.py`, both `WakeConfig` and `VoiceWakeConfig`, all
  22 `VOICE_WAKE_*` and 8 `WAKE_WORD_*` settings, the `/voicewake` command and
  the `Dockerfile` speech-recognition stage are gone. The bot no longer
  transcribes anything, no longer speaks in a voice channel, and has no wake
  word — type at it instead. This also removes a 75 MB model download, the CPU
  cost of recognising every utterance, and the dedicated worker thread that
  carried it.
- **The receive-tap hook.** `VoiceClipService.add_tap` / `remove_tap` existed
  only for the spoken wake word. The receiver now does exactly one thing with a
  packet — decrypt and buffer it — so the UDP reader thread has one less fan-out
  per packet.
- **Music, entirely.** `commands/music.py`, `functions/music/`,
  `docs/music.md`, `tests/test_music.py`, every `MUSIC_*` setting, the
  `yt-dlp` dependency and the `/cmd` panel are all deleted.

### Fixed
- **Clipped speech had a jitter.** Frames used to be placed on the wall clock
  they *arrived* at; a few milliseconds of network wobble made one frame overlap
  the next and the next leave a hole, which is heard as a flutter. Frames are
  now placed on the sender's own RTP clock, re-anchored only on a stream change,
  a sequence reset, a real silence gap or a long arrival pause.
- **A long stay in a voice channel was credited a few minutes short.** 24 hours
  in a channel landed in the database as ~23 h 51 m. The checkpoint credited a
  whole number of seconds and then moved its cursor to the requested instant,
  so the fraction each `floor()` discarded was lost — once per checkpoint
  window, ~720 times a day, which is minutes by the end of a day and scales
  with how long somebody stays. Migration `004_voice_credit_carry.sql` advances
  the cursor by the seconds that were actually credited, the close path rounds
  its final partial second, and the bot snaps its own boundaries to whole
  seconds so this cannot come back. Details:
  [docs/engineering/voice-accounting-incident.md §K](docs/engineering/voice-accounting-incident.md).

### Added
- `functions/voice_clip/mixer.py::apply_static_gain`: one constant gain per
  clip, levelled to −20 dBFS RMS with the loudest sample under the ceiling.

### Changed
- **`/clip` records everyone, silently.** Consent prompts, the channel notice,
  the opt-out list and `VOICE_CLIP_CONSENT_MODE` / `VOICE_CLIP_NOTICE_ENABLED`
  are gone; every human in the channel is captured, and the debug panel shows
  no per-speaker state to switch off.
- **Level differences are preserved.** The clip is levelled once, with a single
  gain, so a pause stays as quiet relative to speech as it really was; the
  dynamic `loudnorm` pass that rode the gain between sentences is gone from the
  FFmpeg chain.
- **Clips are smaller and still clear.** Default bitrate 192 → 128 kbps AAC-LC
  at 48 kHz mono: a three-minute clip is ~3 MB instead of ~4.5 MB.
- **Every slash command is guild-only.** `DISCORD_GUILD_ID` is now required in
  practice: commands are published to that guild alone and the global list is
  purged on start, so edits appear instantly and no command is listed twice.

### Added
- `DISCORD_GUILD_ID`: publish slash commands instantly to a chosen guild;
  leaving it blank registers them globally.
- Operator allow-lists in `.env`: `EDIT_ALLOWED_USER_IDS`,
  `CLIP_ALLOWED_USER_IDS`, `VOICE_DEAF_EXEMPT_USER_IDS`.
- `PREWARM_ON_START`: warm fonts, card backdrops and the render pool at boot.
- `functions/perf.py`: hardware-sized thread pools, one pooled HTTP session per
  service, a dedicated render executor, and shared-session shutdown.
- Hedged image search: the preferred source gets a head start, the next one
  starts in parallel, and the usual lookup stays a single request.
- Weather geocoding cache: repeat lookups of the same city skip the round trip.
- Leaderboard card: 2× supersampled drawing, gradient backdrop, glass cards,
  medal podium with a crown for first place, live "in voice" pills, share bars,
  a real empty state, and a 45-second render cache with single-flight dedupe.
- `tools/make_assets.py` regenerates the README images from the renderer itself.
- `functions/voice/ui.py::leaderboard_subtitle` for the card's context line.
- `docs/setup.md` (detailed setup guide) and `docs/performance.md`
  (card-rendering and HTTP measurements).
- `tests/conftest.py`: the suite is now provably offline — avatars are drawn,
  never fetched.

### Changed
- `tools/` reproducers moved to `tools/repro/`; engineering write-ups moved to
  `docs/engineering/`.
- README rewritten as a public showcase, including an explicit AI disclosure.
- Default `VOICE_ALERT_CHANNEL_ID` is now `0` (alerts off) instead of one
  deployment's channel.
- Default dictionary cache raised to 1024 entries; HTTP pools widened.

### Fixed
- `/edit` could report success while writing nothing when the first adjustment
  of a day had no existing row.
- `AutoUpdater` file backups crashed on every apply (a `@staticmethod` that
  still took `self`).
- Auto-update could re-apply the same bad commit forever on a crash loop; the
  start-up check is now skipped after three starts that never reached Discord.
- ElevenLabs `422` on optional voice settings no longer burns the whole model
  chain — the same model is retried with the safe payload.
- A flaky voice-clip end-to-end test now waits for observable buffer state.

## [2.0.0]

The "it actually behaves" release: every feature below was verified end to end
against the real services before landing.

### Added
- Owner-voice TTS (ElevenLabs) with OpenAI-compatible and free-voice fallbacks.
- "Hey Paradise …" in voice chat: typed and spoken answers.
- Silent voice presence, with the old arrival lines behind a flag.
- Self-update from GitHub: validation, backups, protected local state.
- Voice clips (`/clip`) with Opus decoding, mixing and AAC export.
- Supabase-backed voice tracking with crash-safe checkpoints and IST windows.

[Unreleased]: https://github.com/STUP1DLADKAA/ParadiseBOT/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/STUP1DLADKAA/ParadiseBOT/releases/tag/v2.0.0
