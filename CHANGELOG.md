# Changelog

All notable changes to Paradise are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
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
