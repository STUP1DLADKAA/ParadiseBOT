# Changelog

All notable changes to Paradise are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- **The wake word can be spoken.** Paradise listens to the voice channel it is
  already in, waits for an utterance to end, transcribes it once
  (on-device Whisper, with Scribe/Gemini as an opt-in fallback), and only then checks for
  "hey paradise" — English, Hinglish and Devanagari, including the usual
  speech-to-text mis-hearings ("he paradise", "pair of dice", "अरे पैराडाइज़").
  Transcribed on-device by default (see above).
  `functions/voice_wake/`, `requirements-local-stt.txt`,
  `/voicewake status|stop|start|notice`,
  [docs/voice-wake.md](docs/voice-wake.md).
- Per-utterance endpointing: energy-based VAD, 100 ms pre-roll, 700 ms silence
  to close, 12 s hard cap, and down-mixing to 16 kHz mono before recognition.
- 22 `VOICE_WAKE_*` settings, a receive-tap hook on the voice receiver
  (`VoiceClipService.add_tap`) so other features can observe audio safely, and
  68 new tests including real Opus, real RTP-decryption and real Whisper paths.

### Changed
- The voice receiver now runs when **either** the clip buffer or the spoken wake
  word needs it (`VoiceClipService.receiving_wanted`), so `VOICE_CLIP_ENABLED`
  can stay off.
- The channel is told once, on first join, that Paradise is listening; anyone
  who opted out of voice capture is dropped before the listener sees a packet.
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
- **Speech recognition now runs on the bot's own CPU.** The wake word is
  transcribed by faster-whisper inside the process — no API key, no upload,
  works offline once the weights are downloaded — so `VOICE_WAKE_STT=auto`
  never touches ElevenLabs or Gemini while the local engine is installed.
  `pip install -r requirements-local-stt.txt` enables it (the Docker image does
  it by default); `VOICE_WAKE_STT=local` forbids the cloud entirely. Loading and
  inference run on a dedicated one-thread executor, the model is freed after 15
  minutes of silence, and a missing dependency, a failed download or a stuck
  decode is reported in plain words instead of going silent.
  New keys: `VOICE_WAKE_STT_MODEL`, `_DEVICE`, `_COMPUTE`, `_THREADS`,
  `_MODEL_DIR`, `_OFFLINE`, `_UNLOAD_AFTER`. [docs/voice-wake.md](docs/voice-wake.md).

### Fixed
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
