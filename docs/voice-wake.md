# The spoken wake word

Type it or say it — Paradise answers either way.

```
you │ 🎙  "hey paradise, what's the capital of Rajasthan?"
    │        ↓ Opus → PCM 48 kHz → endpointing → WAV 16 kHz mono
    │        ↓ Whisper, on THIS machine: no key, no upload, works offline
    │        "Hey Paradise, what's the capital of Rajasthan?"
    │        ↓ wake phrase found → the question is the rest of the sentence
bot │ 🗣  "Jaipur is the capital of Rajasthan."
```

The instant the phrase is recognised, a two-note chime plays in the channel —
before the answer is composed, so the pause while the model thinks never looks
like being ignored (`VOICE_WAKE_CUE=false` turns it off).

## On-device by default

Recognition runs **inside the bot**, on the bot's own CPU, through
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — a CTranslate2
build of OpenAI's Whisper. There is no API key, no account, no request, and no
audio leaving the machine. It also works with the network unplugged, once the
weights are on disk:

```bash
python -m pip install -r requirements-local-stt.txt   # once, ~250 MB of wheels
python bot.py                                         # start-up fetches the model
```

That is the whole setup: `pip` brings the *engine*, and the bot brings the
weights. No requirements file can ship them — they are 75 MB of data, not a
package — so the first start-up downloads them once, in the background, while
Discord is still connecting:

```
On-device model: whisper small: on disk · /root/.cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots/…
```

By the time anyone says "hey paradise" the files are already there, and they
are never fetched again. To do it before the bot ever starts — for an image, or
a slow link — use the same logic from the command line:

```bash
python tools/fetch_whisper_model.py small --dir /opt/whisper   # fetch now
python tools/fetch_whisper_model.py small --check              # is it here?
```

Start-up is never held hostage to the download: it runs on the Whisper worker
thread, a failure is a log line rather than a crash, and `/voicewake download`
retries on demand. `VOICE_WAKE_STT=auto` (the default) means *local
first*; `VOICE_WAKE_STT=local` means **local only** — the cloud providers are
then never constructed, so a bug could not reach them.

| | |
| --- | --- |
| Model | `small` — the smallest size that is good at Hindi/Hinglish (`VOICE_WAKE_STT_MODEL`) |
| Speed | int8 on CPU; roughly 0.5–1.5× real time for a 3–5 s sentence on a normal VPS |
| Memory | ~600 MB while loaded, freed after 15 minutes of silence (`VOICE_WAKE_STT_UNLOAD_AFTER`) |
| Network | none, ever, after the one-time download (at start-up) |
| Cost | nothing |

The weights are fetched by a background task at start-up (`VOICE_WAKE_STT_PRELOAD`,
on by default), and loaded into memory on a background thread the first time
somebody speaks, so start-up stays instant and the event loop is never blocked. Loading and
inference both run on a dedicated one-thread executor: cards, commands and
heartbeats keep running while Whisper works.

### Why not something smaller?

Whisper `tiny` and `base` are ~3× faster and fine for English, but they turn
Hinglish into nonsense. `small` is the point where "hey paradise, kal ka
mausam kaisa hai" comes back intact. `medium` and `large-v3-turbo` are more
accurate and considerably slower on CPU — try `VOICE_WAKE_STT_MODEL=large-v3-turbo`
if the host has spare cores.

## What it costs, and when

Nothing is uploaded, so the only thing worth bounding is CPU:

* the bot decodes the channel's audio locally and watches 20 ms frames for
  speech energy;
* when a sentence starts, frames are kept in memory — 100 ms of pre-roll so the
  first syllable is never clipped;
* when the sentence ends (700 ms of quiet, or 12 s, whichever comes first) the
  utterance is down-mixed to 16 kHz mono and handed to Whisper **once**;
* the transcript is matched against the wake phrase. Only if it is a call does
  Gemini get the question and ElevenLabs speak the answer.

Silence, background noise, coughs and sentences that do not start with the wake
phrase cost a few hundred milliseconds of CPU and nothing else.

## Privacy, honestly

| | |
| --- | --- |
| When it listens | whenever Paradise is in a voice channel and `VOICE_WAKE_ENABLED=true` |
| Where audio goes | **nowhere** — it is decoded and transcribed in this process |
| What is written | nothing on disk; the rolling buffers are in memory and bounded |
| What is stored | nothing; the audio is dropped as soon as it has been transcribed |
| Who is ignored | anyone who opted out of voice capture (`/clip` consent) — dropped before the listener sees a packet |
| How it is announced | a one-time notice in the channel when Paradise joins (`VOICE_WAKE_ANNOUNCE`) |
| How to turn it off | `VOICE_WAKE_ENABLED=false`, or `/voicewake stop` per server |
| How to see what it heard | `/voicewake status` shows the engine, the last transcript and counters |

The `Gemini`/`ElevenLabs` recognisers still exist — some hosts (a tiny shared
CPU, or an ARM board) cannot run Whisper — but they are only used when you ask
for them explicitly, or when the local engine is not installed and
`VOICE_WAKE_STT=auto` is left alone. `/voicewake status` always says which one
is live:

```
speech-to-text: local whisper small (int8 on cpu, loaded) · threshold 0.012
speech-to-text: local whisper (not installed), elevenlabs · threshold 0.012
```

## Setup

1. **The dependency** (only if you want the wake word at all):

   ```bash
   python -m pip install -r requirements-local-stt.txt
   ```

   Docker users get it automatically (`--build-arg INSTALL_LOCAL_STT=0` opts out).
   Without it the bot starts normally, `/voicewake status` explains, and typing
   "hey paradise" keeps working.

2. **A receive-capable voice connection.** Paradise connects with its own
   `ClipVoiceClient` whenever the clip buffer *or* the wake word is enabled, so
   `VOICE_CLIP_ENABLED` may stay `false`. PyNaCl and FFmpeg are already required
   for the rest of the bot.

3. **Do not server-deafen the bot.** Discord sends a deafened client no audio at
   all — that is a Discord rule, not a limitation of this code. The bot connects
   with `self_deaf=False`.

4. **Voice permissions:** *View Channel*, *Connect*, *Speak*.

### Air-gapped or pre-downloaded models

The weights are ordinary files. Copy a faster-whisper model directory (the one
containing `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.txt`) to the
host and point at it:

```env
VOICE_WAKE_STT_MODEL_DIR=/opt/whisper/small
VOICE_WAKE_STT_OFFLINE=true      # never touch the network, even on a typo
```

`VOICE_WAKE_STT_OFFLINE=false` with an *empty* `MODEL_DIR` restores the default:
download into the Hugging Face cache on the first start-up.

An image can carry the weights instead of fetching them at run time:

```bash
docker build --build-arg WHISPER_MODEL=small -t paradisebot .
docker run … -e VOICE_WAKE_STT_MODEL_DIR=/opt/whisper \
             -e VOICE_WAKE_STT_OFFLINE=true paradisebot
```

Without that build argument the container keeps the model on its `/app/data`
volume (`HF_HOME`), so a restart or a re-created container does not fetch it
again.

### The cloud fallback (optional)

```env
VOICE_WAKE_STT=elevenlabs        # Scribe v1 with ELEVENLABS_API_KEY
VOICE_WAKE_STT=gemini            # audio inline with GEMINI_API_KEY
VOICE_WAKE_STT=auto              # local → elevenlabs → gemini (default)
```

## Tuning

| Key | Default | Meaning |
| --- | --- | --- |
| `VOICE_WAKE_ENABLED` | `true` | master switch |
| `VOICE_WAKE_STT` | `auto` | `auto`, `local`, `elevenlabs`, `gemini` or `off` |
| `VOICE_WAKE_STT_MODEL` | `small` | Whisper size, or a path to a converted model |
| `VOICE_WAKE_STT_DEVICE` | `cpu` | `cpu` or `cuda` |
| `VOICE_WAKE_STT_COMPUTE` | `int8` | `int8`, `int8_float32` or `float32` |
| `VOICE_WAKE_STT_THREADS` | `0` | CPU threads for inference; 0 = let CTranslate2 decide |
| `VOICE_WAKE_STT_MODEL_DIR` | — | where the weights live / download to |
| `VOICE_WAKE_STT_OFFLINE` | `false` | never touch the network |
| `VOICE_WAKE_STT_PRELOAD` | `true` | fetch the weights at start-up, not on the first utterance |
| `VOICE_WAKE_STT_UNLOAD_AFTER` | `900` | seconds of silence before the weights leave RAM; 0 = keep |
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

**It never triggers.** Check `/voicewake status` first:

* `local whisper (not installed)` → run `pip install -r requirements-local-stt.txt`;
* `local whisper small (… , loaded)` and an empty last transcript → the audio is
  below `VOICE_WAKE_THRESHOLD`; lower it to `0.006`;
* `not downloaded yet` / `download failed — …` in the `model —` line → run
  `/voicewake download`, or `python tools/fetch_whisper_model.py`;
* `could not be loaded … not on this machine yet` → the one-time download has
  never succeeded; run once with internet, or set `VOICE_WAKE_STT_MODEL_DIR`.

**It transcribes everything and answers nothing.** That is normal: the wake
phrase is what decides. `/voicewake status` shows the last transcript — if it
reads "he paradise" instead of "hey paradise" that is already handled; if it
reads something unrelated, the utterance is being cut, so raise
`VOICE_WAKE_SILENCE_MS`.

**The first start-up is quiet for a minute.** That is the one-time download;
`/voicewake status` shows `model — whisper small: on disk · …` when it is done.
Set `VOICE_WAKE_STT_PRELOAD=false` to move it to first use instead.

**It is slow on my VPS.** Use `VOICE_WAKE_STT_MODEL=base` (English-heavy
servers) or `tiny`, and set `VOICE_WAKE_STT_THREADS` to the number of real
cores. A 3 s question at `small`/int8 is typically 2–4 s on two shared vCPUs.

**It eats 600 MB.** That is Whisper `small` in memory. `VOICE_WAKE_STT_UNLOAD_AFTER`
lowers the idle window; `VOICE_WAKE_STT_MODEL=base` halves the footprint.

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

Whisper is also given "Hey Paradise." as a decoder prompt, so the phrase comes
back spelled the way the matcher expects.

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
functions/voice_wake/local_stt.py  faster-whisper on a one-thread executor
        │  "hey paradise …"            (stt.py: ElevenLabs / Gemini, opt-in)
        ▼
functions/voice_wake/phrases.py    wake phrase + question
        │
        ▼
commands/wake.py                   chime, then Gemini, then speak (ElevenLabs)
```

Every hop except the model itself is a pure function with tests:
`tests/test_voice_wake.py` covers the endpointing boundaries, 17 phrase
variants, the WAV encoder, the provider chain, the on-device engine (threading,
timeouts, load retry, idle unload, a missing dependency), rate limits, a failing
provider, real Opus packets, real RTP decryption into the tap, and the cog
wiring. Set `VOICE_WAKE_STT_MODEL_DIR` and run the suite to exercise real
Whisper weights too.

The answer still needs a brain: Gemini (or an OpenAI-compatible endpoint) writes
it, ElevenLabs speaks it. Only the *listening* became local.

Nothing else owns the voice connection: Paradise connects with its own
`ClipVoiceClient`, which is the only way this code can see RTP at all.
