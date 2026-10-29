# The spoken wake word

Type it or say it — Paradise answers either way.

```
you │ 🎙  "hey paradise, what's the capital of Rajasthan?"
    │        ↓ Opus → PCM 48 kHz → endpointing → WAV 16 kHz mono
    │        ↓ speech-to-text: "Hey Paradise, what's the capital of Rajasthan?"
    │        ↓ wake phrase found → the question is the rest of the sentence
bot │ 🗣  "Jaipur is the capital of Rajasthan."
```

If a song is playing, see [music](music.md): the song ducks to 15 % for the
length of the answer and comes back up.

## What it costs, and when

Nothing is uploaded until **you have finished speaking**:

* the bot decodes the channel's audio locally and watches 20 ms frames for
  speech energy (the same threshold that a "is anyone talking" meter uses);
* when a sentence starts, frames are kept in memory — 100 ms of pre-roll so the
  first syllable is never clipped;
* when the sentence ends (700 ms of quiet, or 12 s, whichever comes first) the
  utterance is down-mixed to 16 kHz mono and sent **once** to speech-to-text;
* the transcript is matched against the wake phrase locally. Only if it is a
  call does Gemini get the question and ElevenLabs speak the answer.

Silence, background noise, coughs and sentences that do not start with the wake
phrase cost one STT request and nothing else.

## Privacy, honestly

This is the one feature that sends **microphone audio to a third party**. It is
built to be visible rather than hidden:

| | |
| --- | --- |
| When it listens | whenever Paradise is in a voice channel and `VOICE_WAKE_ENABLED=true` |
| What is uploaded | one WAV per finished utterance, to ElevenLabs Scribe (or Gemini) |
| What is stored | nothing on disk; the rolling buffers are in memory and bounded |
| Who is ignored | anyone who opted out of voice capture (`/clip` consent) — dropped before the listener sees a packet |
| How it is announced | a one-time notice in the channel when Paradise joins (`VOICE_WAKE_ANNOUNCE`) |
| How to turn it off | `VOICE_WAKE_ENABLED=false`, or `/voicewake stop` per server |
| How to see what it heard | `/voicewake status` shows the last transcript and counters |

The audio never touches the bot's database, is never written to a file, and is
discarded as soon as it has been transcribed.

## Setup

1. **A speech-to-text provider.** Either key already used by the bot works:

   ```env
   ELEVENLABS_API_KEY=...   # Scribe v1 — best Hindi/Hinglish, uses the same key
   GEMINI_API_KEY=...       # audio inline in the normal Gemini request
   VOICE_WAKE_STT=auto      # try ElevenLabs, then Gemini (default)
   ```

   `VOICE_WAKE_STT=elevenlabs` or `gemini` pins one; `off` disables listening
   while leaving the typed wake word working.

2. **A receive-capable voice connection.** Paradise connects with its own
   `ClipVoiceClient` whenever the clip buffer *or* the wake word is enabled, so
   `VOICE_CLIP_ENABLED` may stay `false`. PyNaCl and FFmpeg are already required
   for the rest of the bot.

3. **Do not server-deafen the bot.** Discord sends a deafened client no audio at
   all — that is a Discord rule, not a limitation of this code. The bot connects
   with `self_deaf=False`.

4. **Voice permissions:** *View Channel*, *Connect*, *Speak*.

## Tuning

| Key | Default | Meaning |
| --- | --- | --- |
| `VOICE_WAKE_ENABLED` | `true` | master switch |
| `VOICE_WAKE_STT` | `auto` | `auto`, `elevenlabs`, `gemini` or `off` |
| `VOICE_WAKE_LANGUAGE` | — | ISO-639-1 hint (`hi`, `en`); blank auto-detects |
| `VOICE_WAKE_THRESHOLD` | `0.012` | how loud a frame must be to count as voice (~-38 dBFS) |
| `VOICE_WAKE_ONSET_MS` | `60` | how much voice starts a recording |
| `VOICE_WAKE_SILENCE_MS` | `700` | quiet gap that ends a sentence |
| `VOICE_WAKE_MIN_SECONDS` | `0.6` | shorter utterances are ignored |
| `VOICE_WAKE_MAX_SECONDS` | `12` | hard cap on one utterance |
| `VOICE_WAKE_COOLDOWN` | `3` | per-user gap between requests |
| `VOICE_WAKE_MAX_PER_MINUTE` | `8` | per-server ceiling |
| `VOICE_WAKE_FOLLOWUP_SECONDS` | `6` | after a bare "hey paradise", how long the question may follow |
| `VOICE_WAKE_MAX_SPEAKERS` | `32` | speakers tracked at once |
| `VOICE_WAKE_ANNOUNCE` | `true` | post the listening notice once per channel join |
| `VOICE_WAKE_POST_TEXT` | `true` | also post the answer in that channel's text chat |
| `VOICE_WAKE_ACKNOWLEDGE` | `true` | say "haan?" when only the wake word was heard |

**It cuts me off.** Raise `VOICE_WAKE_SILENCE_MS` (900–1200) — the endpointing
closes a sentence after that much quiet.

**It never triggers.** Lower `VOICE_WAKE_THRESHOLD` to `0.006` and check
`/voicewake status`: if the last transcript is empty, the STT provider is
refusing (usually a missing or rate-limited key). The log line at start-up says
which providers are live:

```
Spoken wake word listening — speech-to-text: elevenlabs, gemini (off) · threshold 0.012
```

**It transcribes everything and answers nothing.** That is normal: the wake
phrase is what decides. Watch `/voicewake status`, which shows the last
transcript — if it reads "he paradise" instead of "hey paradise", that is
already handled (the matcher tolerates it); if it reads something unrelated, the
utterance is being cut, so raise `VOICE_WAKE_SILENCE_MS`.

## What the matcher accepts

English, Hinglish and Devanagari, including the ways speech-to-text engines
mis-hear it:

```
hey paradise …        he paradise …         hey paradaise …
hey pair of dice …    okay paradise …       arey paradise …
अरे पैराडाइज़ …       हैलो पैराडाइस …        पैराडाइज़, …
```

…and rejects sentences that merely contain the word:

```
paradise is a nice word      → no
I said hey to paradise       → no
the paradise hotel is nice   → no
```

## How it fits together

```
functions/voice_clip/receiver.py   decrypts RTP + DAVE, feeds the buffer, calls taps
        │  tap(user_id, packet)                    (on the UDP thread, cheap only)
        ▼
functions/voice_wake/listener.py   queue → Opus decode → endpointing
        │  Segment(48 kHz int16)
        ▼
functions/voice_wake/segmenter.py  silence/onset rules, pre-roll, trimming
        │  WAV (16 kHz mono)
        ▼
functions/voice_wake/stt.py        ElevenLabs Scribe → Gemini
        │  "hey paradise …"
        ▼
functions/voice_wake/phrases.py    wake phrase + question
        │
        ▼
commands/wake.py                   music first, then Gemini, then speak (ElevenLabs)
```

Every hop except the network calls is a pure function with tests:
`tests/test_voice_wake.py` covers the endpointing boundaries, 17 phrase
variants, the WAV encoder, the provider chain, rate limits, a failing
provider, real Opus packets, real RTP decryption into the tap, and the cog
wiring.

Lavalink is absent for the same reason as in [music](music.md#why-not-lavalink-v4):
it owns the voice connection, and a voice connection it owns delivers no audio
to this code.
