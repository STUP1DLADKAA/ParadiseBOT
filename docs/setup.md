# Setup guide

The README gets you running in five minutes. This page is the detail behind the
optional parts: voice data, the owner's voice, the wake word, silent presence,
clips, self-update and the operator commands.

Everything here is optional except the Discord token.

---

## 1. Discord application

1. [Developer Portal](https://discord.com/developers/applications) → **New
   Application** → **Bot**.
2. Enable **Message Content Intent** (mentions, chat triggers) and **Server
   Members Intent** (names/avatars on leaderboards, AFK handling, personality).
3. Invite with scopes `bot` + `applications.commands` and these permissions:

   | Permission | Needed for |
   | --- | --- |
   | View Channels | everything |
   | Send Messages, Embed Links, Attach Files | replies and cards |
   | Read Message History | mention context, AFK detection |
   | Add Reactions | personality |
   | Connect, Speak | voice presence, wake word, pronunciation |
   | Use Voice Activity | receiving audio for `/clip` |

4. Copy the token into `.env` as `DISCORD_TOKEN`.

### Instant slash commands

Set `DISCORD_GUILD_ID` to your server's ID (right-click the server with
Developer Mode on → *Copy Server ID*). Guild commands appear immediately; global
ones can take up to an hour to propagate.

Leave it unset to register globally — the bot logs which mode it used.

---

## 2. Voice activity and leaderboards (Supabase)

Voice statistics live in Postgres because they are read far more often than
they are written, and because a leaderboard should survive a bot restart.

1. Create a project at [supabase.com](https://supabase.com) (pick the region
   closest to your members).
2. SQL Editor → run, in order:
   - [`supabase/migrations/001_voice_activity.sql`](../supabase/migrations/001_voice_activity.sql)
   - [`supabase/migrations/002_voice_ist_calendar.sql`](../supabase/migrations/002_voice_ist_calendar.sql)
   - [`supabase/migrations/003_voice_checkpoint.sql`](../supabase/migrations/003_voice_checkpoint.sql)
   - [`supabase/migrations/004_voice_credit_carry.sql`](../supabase/migrations/004_voice_credit_carry.sql)
     — apply this one even if you already have 003: without it every
     checkpoint can shave a fraction of a second off a long session, which adds
     up to minutes over a day.

   Each file is transactional and safe to re-run. The bot requires schema
   version 4 and refuses to track until it sees it (it says so in the log).
3. Project Settings → API → copy the **project URL** and the **service_role
   secret key** into `.env`:

   ```env
   SUPABASE_URL=   # your project URL, from Project Settings → API
   SUPABASE_SECRET_KEY=   # the service_role secret key from the same page
   ```

   > The service-role key bypasses row-level security. It belongs only in this
   > private `.env` — never in a client, a dashboard, a screenshot or a commit.

4. Restart. The first boot verifies the schema, creates the tables' indexes and
   starts tracking. Existing local SQLite history migrates automatically in
   idempotent, verified batches.

Without these two values the bot still runs — `/vc` explains what is missing
and every other feature works.

Optional knobs (defaults are sane): `VOICE_HEARTBEAT_INTERVAL`,
`VOICE_CHECKPOINT_SECONDS`, `VOICE_WRITE_COALESCE_SECONDS`, `VOICE_DB_PATH`,
`VOICE_DM_REPORTS_ENABLED`, `VOICE_ALERT_CHANNEL_ID`,
`VOICE_DEAF_WARNING_SECONDS`.

---

## 3. Your own voice (ElevenLabs)

Set two values and Paradise speaks with **your** voice everywhere — dictionary
pronunciation, `/define` playback and every voice-chat answer:

```env
ELEVENLABS_API_KEY=...          # elevenlabs.io → Settings → API keys
ELEVENLABS_VOICE_ID=...         # Voice Lab → your voice → ID
ELEVENLABS_MODEL=eleven_flash_v2_5
```

* `eleven_flash_v2_5` is the fastest multilingual model (~75 ms first byte) and
  handles Hindi, English and Hinglish in one voice. `eleven_turbo_v2_5` sounds
  slightly richer; `eleven_multilingual_v2` is the highest quality and slowest.
* If your plan rejects the optional voice settings, the same model is retried
  with the safe payload instead of falling down the chain.
* The key travels only in the `xi-api-key` header. It is never logged, never
  placed in a URL and never echoed into a message.
* Without a key: the OpenAI-compatible `SPEECH_*` provider is used if
  configured, otherwise the free web voice, otherwise text only.

---

## 4. "Hey Paradise …" in voice chat

Type the phrase in a voice channel's text chat:

```text
Hey Paradise what's the capital of Rajasthan?
→ Jaipur is the capital of Rajasthan, in the north-west of India.   (and spoken aloud)
```

* The answer model is the fastest one with capacity:
  `GEMINI_MODEL_CHAIN` (default `gemini-2.5-flash-lite, gemini-2.0-flash-lite,
  gemini-2.5-flash, gemini-2.0-flash`) is tried fastest-first, the first two are
  hedged, and a throttled model is skipped for a few minutes.
* If every model fails, Paradise says something rude instead — from a clip
  preloaded at start-up, so the channel is never silent.
* Recognised phrases: `hey/hi/hello/yo/ok/okay/oye/arre paradise …` or
  `Paradise, …`. Ordinary sentences containing the word never trigger anything.
* Only voice-channel text chats are watched (`WAKE_WORD_VC_ONLY=true`); normal
  channels are ignored.
* Text always works. Speech needs PyNaCl + ffmpeg (both in `requirements.txt`).

### Saying it out loud — transcribed on your own CPU

The wake word also works when **spoken** in the channel, and the recognition
runs inside the bot (faster-whisper, no key, no upload):

```bash
python -m pip install -r requirements-local-stt.txt   # once, ~250 MB
```

* The first utterance downloads the model (75 MB for `small`) into the Hugging
  Face cache; after that it is fully offline. `VOICE_WAKE_STT_MODEL_DIR` points
  at weights you already have, `VOICE_WAKE_STT_OFFLINE=true` forbids the network.
* `VOICE_WAKE_STT=local` guarantees no audio ever leaves the machine; `auto`
  (the default) tries the local engine first and only falls back to
  `ELEVENLABS_API_KEY`/`GEMINI_API_KEY` recognition when Whisper is not
  installed.
* It joins undeafened because a deafened client receives no audio at all.
* Full guide: [docs/voice-wake.md](voice-wake.md).

---

## 5. Voice presence: silent joins

Paradise follows the crowd into whichever accessible voice channel has the most
humans and leaves when it empties — without posting anything or speaking.

* `VOICE_PRESENCE_INTERVAL` (default 20 s) controls how quickly it reacts.
* `VOICE_ARRIVAL_ANNOUNCE=true` restores the old random arrival line in the
  channel text chat and in speech.
* `VOICE_PRESENCE_ENABLED=false` turns the behaviour off entirely.

### Voice clips while deafened — a Discord limit

`/clip` records continuously while Paradise sits in the channel. It joins
**undeafened on purpose**, because a deafened client is never sent any audio.

* If Paradise deafens itself (an old config, or an admin toggling the bot's own
  state), it clears the flag and keeps recording.
* If a **moderator server-deafens** it, capture stops and the bot cannot resume
  it: Discord does not transmit audio to a server-deafened voice client, and
  nothing a bot sends overrides that. Un-deafen it under *Server Settings →
  Members*. The bot logs the reason once and `/clip` replies with a plain
  explanation instead of pretending.

---

## 6. Voice clips (`/clip`)

```env
VOICE_CLIP_ENABLED=true
CLIP_ALLOWED_USER_IDS=123456789012345678   # who may request/receive clips
VOICE_CLIP_MAX_SECONDS=180                 # rolling window (clamped to 180)
VOICE_CLIP_BITRATE_KBPS=192                # 64–320
```

* Audio is decoded from Discord's Opus stream, kept in a bounded in-memory ring
  buffer, mixed per speaker and exported once as 48 kHz mono AAC-LC.
* Default mastering chain: `highpass=f=75,lowpass=f=16000,
  loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=…` — override with
  `VOICE_CLIP_AUDIO_FILTER`.
* The file is DM'd to the authorized operator only. It is never uploaded
  anywhere else, and the buffer is purged the moment recording stops.

---

## 7. Operator commands

```env
EDIT_ALLOWED_USER_IDS=123456789012345678     # /edit
CLIP_ALLOWED_USER_IDS=123456789012345678     # /clip
VOICE_DEAF_EXEMPT_USER_IDS=123456789012345678  # still credited while deafened
```

`/edit` adjusts a member's tracked voice time (`period`, `adjustment` in
seconds, positive or negative). Week and month adjustments are spread across the
days and sum exactly; a store outage is reported as a failure, never as success.

Everyone outside the allow-list gets a vague "something went wrong" reply — no
hint that the command exists.

---

## 8. Self-update from GitHub

On boot (and every `AUTO_UPDATE_INTERVAL` seconds while online) Paradise asks
GitHub for the newest commit on `AUTO_UPDATE_BRANCH`:

```env
AUTO_UPDATE_ENABLED=true
AUTO_UPDATE_REPO=STUP1DLADKAA/ParadiseBOT
AUTO_UPDATE_BRANCH=main
AUTO_UPDATE_APPLY=true          # false = only report that an update exists
AUTO_UPDATE_RESTART=true        # exit after applying, so your supervisor restarts
GITHUB_TOKEN=                   # optional: raises the API rate limit
```

* **Never touched:** `.env` (and every `.env.*` except `.env.example`), `.git`,
  `data/`, virtualenvs, caches, `*.db` / `*.sqlite` / `*.log`, plus anything in
  `AUTO_UPDATE_PROTECTED`.
* **Validated first:** the archive must contain `bot.py` and
  `requirements.txt`, every `.py` file must compile, and path traversal is
  rejected. A bad download changes nothing.
* **Recoverable:** every replaced file is copied to
  `data/autoupdate_backup/latest/` before it is overwritten.
* **Crash-loop guard:** if the process keeps starting without reaching Discord,
  the start-up update is skipped so a bad commit cannot own the bot.
* The applied commit is recorded in `data/autoupdate_state.json`, so this costs
  one GitHub API call per check.

Terminal output:

```text
✅ auto-update: already up to date (commit d38af11)

🔄 AUTO-UPDATE APPLIED — 12 file(s) updated, 0 removed
   repository: STUP1DLADKAA/ParadiseBOT@a1b2c3d
   + commands/wake.py
   RESTART Paradise to run the new code.
```

---

## 9. Performance

Paradise sizes its own pools at boot:

| Setting | What it does |
| --- | --- |
| `PREWARM_ON_START=true` | loads fonts, builds the card backdrops and starts the render pool before the first command |
| — | default executor: `max(8, min(32, cpu × 4))` worker threads |
| — | render pool: `max(2, min(16, cpu))` dedicated drawing threads |
| — | HTTP: one pooled session per service, up to 256 connections with 600 s DNS cache |

Turn `PREWARM_ON_START` off only on a very small or already-exhausted host; the
bot then warms lazily on first use.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `DISCORD_TOKEN is missing` | copy `.env.example` to `.env` and fill it in |
| Commands don't appear | you set `DISCORD_GUILD_ID` to the wrong server, or wait up to an hour for global commands |
| `/vc` says it isn't configured | `SUPABASE_URL` / `SUPABASE_SECRET_KEY` missing, or the migrations were not applied |
| Leaderboard shows `User 123…` | Server Members Intent is off in the Developer Portal |
| No sound from `/clip` capture | another admin server-deafened the bot (see §5) |
| Wake word answers but says nothing | PyNaCl/ffmpeg missing, or no speech provider configured |
| `auto-update: skipped (crash loop guard)` | the previous three starts never reached Discord — fix the error, then delete `data/autoupdate_state.json` |
| Cards look pixelated | the bundled fonts are missing from `assets/fonts/` |

Still stuck? Open an issue with the template — and redact tokens, keys, user IDs
and server names first.
