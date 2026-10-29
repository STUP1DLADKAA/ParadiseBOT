# Music, and talking over it

Paradise plays music in the voice channel you're sitting in and **answers you
mid-song**: the song drops to 15 % for the length of the sentence, then comes
straight back up. It does not stop, it does not restart, and the position is
never lost.

```
you │  hey paradise, play billie jean
bot │  🎵 playing **Billie Jean** (4:53)
    │  …music at 50 %…
you │  hey paradise, who sang this?
    │  ┌ song fades 1.0 → 0.15 over 240 ms
bot │  🗣 "Michael Jackson wrote and performed it in 1982."
    │  └ song fades 0.15 → 1.0
```

## How it is built

```
"play billie jean"                       functions/music/intents.py
        │                                (English + Hinglish parser)
        ▼
yt-dlp  ──►  Track(title, duration, url) functions/music/search.py
        │                                (runs in the thread pool)
        ▼
FFmpeg  ──►  48 kHz stereo s16le PCM     functions/music/decoder.py
        │    in exact 20 ms frames
        ▼
numpy   ──►  music × gain + speech       functions/music/mixer.py
        │    gain ramps 1.0 ↔ duck level
        ▼
discord.AudioSource  ──►  the voice socket  functions/music/player.py
```

Everything is in-process. There is no external music service to configure and
no second bot to run.

### Why not Lavalink v4?

Lavalink is a good piece of software, and it is the right answer for a bot
that **only** plays music. It is the wrong answer for this bot, for one
specific reason:

> Lavalink v4 owns the Discord voice connection. It logs the bot account into
> the voice server itself and sends Opus straight to Discord. discord.py never
> receives a single byte of audio.

That means, with Lavalink:

* there is no local audio to mix the ElevenLabs voice into;
* there is no local volume to drop to 15 % — the volume control belongs to the
  node, and only for the track it is playing;
* `guild.voice_client` is a Lavalink player, not a `discord.VoiceClient`, so it
  cannot play a locally generated clip at all.

In other words: **"Lavalink" and "duck the song and speak over it" are mutually
exclusive.** Since speaking over music is the whole feature, Paradise does the
audio itself. The upside is that the queue, the ducking and the spoken answers
are all ordinary Python that can be unit-tested — including the exact gain
maths (`tests/test_music.py::test_ducking_on_a_real_stream`).

If YouTube ever blocks the host's IP, the fix is a cookie file, not a node:

```env
MUSIC_COOKIES_FILE=/path/to/cookies.txt
```

Export cookies from a browser where you are logged in to YouTube, in Netscape
format (`yt-dlp --cookies-from-browser chrome --cookies cookies.txt`).

## Setup

1. **Install the new dependency** (already in `requirements.txt`):

   ```bash
   pip install -r requirements.txt     # adds yt-dlp
   ```

2. **FFmpeg** — the bundled `imageio-ffmpeg` binary is used automatically; a
   system `ffmpeg` on `PATH` is preferred when present. Check with
   `/music play` — if it says ffmpeg is missing, install it.

3. **Voice permissions** for the bot in the channel: *View Channel*,
   *Connect*, *Speak*. PyNaCl is installed by `discord.py[voice]`.

4. **ElevenLabs** — the same key used for everything else
   (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`). Without it answers are typed
   but not spoken.

Nothing else is required. `MUSIC_ENABLED=false` turns the whole feature off.

## Using it

### By voice (in a voice channel's text chat)

| You type | It does |
| --- | --- |
| `Hey Paradise play billie jean` | plays it |
| `billie jean baja do` | same, Hindi word order |
| `अगला गाना लगाओ` | skips |
| `Hey Paradise pause` / `pause karo` / `रोक दो` | pauses |
| `Hey Paradise resume` / `chalu karo` | resumes |
| `Hey Paradise stop` / `band karo` / `ruko` | stops and clears the queue |
| `Hey Paradise skip` / `agla` / `next song` | next track |
| `Hey Paradise volume 30` / `awaaz kam karo` | volume |
| `Hey Paradise what's playing` / `kaun sa gaana` | says the current song |
| `Hey Paradise loop` / `loop off` / `loop this song` | loop mode |
| `Hey Paradise leave vc` / `nikal jao` | disconnects |

While a song is playing, a bare `skip` / `pause` / `stop` in that voice
channel's text chat works too — nobody types a full sentence to skip a song.

### Slash commands

```
/music play <song>     /music pause        /music resume
/music skip [count]    /music stop         /music queue
/music nowplaying      /music volume <0-200>
/music loop <off|this song|the queue>
/music shuffle         /music leave        /music panel
/cmd                   ← the button panel
```

### Text commands

`!p`, `!play`, `!pause`, `!r`, `!s`, `!skip`, `!stop`, `!q`, `!np`,
`!vol 40`, `!loop`, `!shuffle`, `!leave`, `!cmd` — the prefix is
`MUSIC_PREFIX` (default `!`).

### The panel

`/cmd` posts a persistent panel: **Pause/Resume · Skip · Stop · Vol − · Vol + ·
Queue · Loop**, with the current song, progress, volume and the text-command
cheat-sheet. It keeps working after a restart.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `MUSIC_ENABLED` | `true` | master switch |
| `MUSIC_VOLUME` | `0.5` | music level on its own (0.0–2.0) |
| `MUSIC_DUCK_VOLUME` | `0.15` | level while Paradise speaks (15 %) |
| `MUSIC_FADE_MS` | `240` | fade length for the drop and the return |
| `MUSIC_MAX_QUEUE` | `50` | queue length |
| `MUSIC_SEARCH_RESULTS` | `1` | how many hits a text query enqueues |
| `MUSIC_SEARCH_SOURCE` | `ytsearch` | `ytsearch` (YouTube) or `scsearch` (SoundCloud) |
| `MUSIC_SEARCH_TIMEOUT` | `25` | seconds before a search gives up |
| `MUSIC_MAX_TRACK_SECONDS` | `0` | refuse longer tracks (0 = no limit) |
| `MUSIC_COOKIES_FILE` | — | Netscape cookies for yt-dlp |
| `MUSIC_PROXY` | — | proxy for yt-dlp only |
| `MUSIC_IDLE_DISCONNECT` | `300` | leave after this many idle seconds (0 = stay) |
| `MUSIC_PREFIX` | `!` | text-command prefix |
| `MUSIC_TEXT_COMMANDS` | `true` | enable `!p`-style commands |
| `MUSIC_AUTO_PANEL` | `false` | post the panel when a song starts |

## Troubleshooting

**"music needs yt-dlp installed"** — `pip install yt-dlp`, then restart.

**Nothing plays, or it stops after a second.** Run the bot from a terminal and
watch the log. `yt-dlp` prints the reason (usually *"Sign in to confirm you're
not a bot"* → set `MUSIC_COOKIES_FILE`, or *"Video unavailable"* → the track is
region-locked).

**It joins but nobody hears anything.** Check that the bot is not
server-deafened (right-click → untick *Deafen*) and that it has *Speak* in that
channel.

**The song is too loud / too quiet.** `/music volume 30`. The spoken voice is
always at full level regardless of the music volume — that asymmetry is what
makes the answer audible.

**Answers are typed but not spoken.** No ElevenLabs key, or PyNaCl/FFmpeg is
missing. The log line at start-up says which:
`Wake word ready — speech: elevenlabs` is what you want.

**The bot leaves after a few minutes.** That is `MUSIC_IDLE_DISCONNECT`
(default 5 min). Set it to `0` to stay put.
