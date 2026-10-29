# Changelog

All notable changes to Paradise are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- **Music, in-process.** `/music …`, `!p`-style text commands, a `/cmd` button
  panel, and voice requests in English, Hinglish and Devanagari
  ("Hey Paradise play billie jean", "billie jean baja do", "अगला गाना लगाओ").
  yt-dlp searches, FFmpeg decodes to 48 kHz stereo PCM, and a numpy mixer
  plays it through one `discord.AudioSource` per guild
  (`functions/music/`, `commands/music.py`, [docs/music.md](docs/music.md)).
- **Ducking.** When Paradise answers while a song is playing, the music gain
  ramps 1.0 → 0.15 → 1.0 around the sentence, so the answer is always
  intelligible and the song never stops. Verified on a real FFmpeg stream
  (`tests/test_music.py::test_ducking_on_a_real_stream`: 0.150 and 1.000).
- `MUSIC_*` settings (15 keys), `yt-dlp` in `requirements.txt`, 71 new tests.

### Changed
- **"Hey Paradise" now answers in far more situations.** `WAKE_WORD_VC_ONLY`
  used to mean "only in a voice channel's text chat"; sitting in a voice
  channel now counts as well, so typing in `#general` while in the VC is
  answered *and* spoken in the channel. Every ignored wake phrase is logged
  at INFO, and a missing ElevenLabs key is reported at start-up instead of
  silently producing text-only replies.
- Wake-word answers are mixed into a playing song instead of waiting for it
  to end.

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
