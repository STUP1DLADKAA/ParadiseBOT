# Passive reactions & on-demand banter

Paradise stays out of unprompted conversation: ordinary messages never trigger
automatic text, GIF, image, meme or sticker replies. The passive engine in
`functions/personality.py` adds only rare, context-fitting emoji reactions,
while roasts, memes and reaction GIFs remain available on demand through
`/roast`, `/meme`, `/gif` and `@Paradise` pings.

```text
unaddressed message in
   │
   ├─ bot / command / bare URL / spam burst / serious topic? → silence
   ├─ user opted out ("stop roasting me" / "leave me alone")? → silence
   ├─ global, channel, per-user or hourly cap active?        → silence
   │
   └─ at most one rare emoji reaction (never a text/GIF/image/sticker reply)
        ├─ a lord spoke (cooldown-gated)? → 👑 / 🗿 / 🫡
        ├─ matched a known context?       → one contextual emoji
        └─ unmatched casual chat?         → very rare ambient emoji
```

## Passive reaction settings

| Layer | What it does | Setting | Default |
| --- | --- | --- | --- |
| Contextual reaction | Emoji on a message matching a known context | `PERSONALITY_REACTION_CHANCE` | `0.06` |
| Ambient reaction | Subtle emoji on casual chat matching no pattern | `PERSONALITY_AMBIENT_REACTION_CHANCE` | `0.015` |
| Lord reaction | Loyalty emoji when a lord speaks | `PERSONALITY_LORD_REACTION_CHANCE` | `0.06` |
| Global cooldown | Minimum seconds between any two passive reactions | `PERSONALITY_GLOBAL_COOLDOWN` | `45` |
| Channel cooldown | Minimum seconds between reactions in the same channel | `PERSONALITY_CHANNEL_COOLDOWN` | `240` |
| User cooldown | Minimum seconds before reacting to the same user again | `PERSONALITY_USER_COOLDOWN` | `300` |
| Lord cooldown | Minimum seconds between lord reactions in one channel | `PERSONALITY_LORD_COOLDOWN` | `600` |
| **Hourly budget** | Hard ceiling on passive reactions per channel per rolling hour (`0` disables) | `PERSONALITY_AMBIENT_HOURLY_CAP` | `4` |

Set `PERSONALITY_ENABLED=false` to disable passive emoji reactions completely.

## On-demand roasts

`functions/roasts.py` backs explicit `/roast` and `@Paradise roast …` requests
with three intensities:

```env
PERSONALITY_ROAST_INTENSITY=spicy   # mild · spicy · nuclear
```

* **mild** — friendly teasing.
* **spicy** — the default. Aimed at what someone said or did.
* **nuclear** — sharper, still keeping the same safety rules.

How to invoke:

* `@Paradise roast me` or `/roast` — roasts whoever asked.
* `/roast @friend` or `@Paradise roast @friend` (or reply to a message with `@Paradise roast this`) — roasts the target.

### The line that never moves

A roast is about the **message**, the ego, the aim, the sleep schedule, the
"one more game". It is never about a person's body, family, religion, caste,
sexuality, gender, race or disability, there are no slurs in the book, and
there is a runtime check that drops any line containing one. Hate is not a
joke; it is hate with a laugh track.

Two hard stops, both before any joke is chosen:

* **Real distress.** Grief, illness, hospital, self-harm, abuse, money trouble,
  breakups — the bot goes completely silent. See `roasts.looks_sensitive`.
* **"Stop roasting me."** Say it once and that person is left alone for the
  rest of the session, no argument, no comment.

## The supreme loyalty

One context sits above all the others in the pattern list: the word "stupid"
is the owner's name, and Paradise treats every use of it as a potential act
of disrespect towards its lord. The `loyalty` pattern is checked first, so a
message containing "stupid" can be answered with a defence line ("watch your
mouth. that's the founding father you're talking about.") instead of whatever
else it might have matched. It is still probabilistic and still bound by the
same cooldowns — the loyalty is religious, the pacing is not.

The same loyalty lives in the AI's system prompt (`functions/ai.py`): when
somebody disrespects Stupid in a conversation with the bot, it gets
significantly more argumentative, and it favours him in ordinary
disagreements while still being allowed to call him an idiot itself.

## Memes from the internet

`functions/meme_feed.py` pulls real posts from real Reddit communities through
[meme-api.com](https://meme-api.com), which needs **no key at all**:

```env
MEME_SOURCES=dankmemes,memes,me_irl,wholesomememes,terriblefacebookmemes,historymemes
MEME_TIMEOUT=6
```

Up to six memes are kept cached, one is prefetched after each use, and a dead
API puts the feed on a 90-second backoff instead of being hammered. Everything
returned is validated before it is shown:

* https only, and only on a known image CDN;
* the URL must end in an image extension — an `.mp4` or a web page is not a
  picture and Discord will not render it;
* NSFW and spoiler posts are dropped;
* titles are flattened to one line, stripped of mentions and markdown, capped.

If any of that fails — no internet, blocked host, weird payload — the bot draws
a meme card locally instead. The feature never goes dark.

## Reaction GIFs

GIFs work **without any key**: a free Tenor (or Giphy) key makes the results
better, but a fresh `.env` already gets keyless GIFs through Tenor's legacy
public endpoint. If that endpoint ever goes away, every request quietly
returns nothing and the bot falls back to memes — the feature never shows a
broken link.

```env
TENOR_API_KEY=     # optional; improves quality and speed
GIPHY_API_KEY=     # optional; same deal
```

With any of the three tiers working, `/gif bruh` and the occasional automatic
GIF reply fire. Without one, `/gif` says so and sends a meme instead.

## Commands

| Command | What it does |
| --- | --- |
| `/roast [member]` | Roast someone, or yourself |
| `/meme [topic]` | A random internet meme, or a drawn card about `topic` |
| `/gif <query>` | A reaction GIF (works keyless) |
| `@Paradise meme [about X]` | The same meme pipeline, by plain mention |
| `@Paradise gif [of X]` / "bruh gif" | The same GIF tiers, by plain mention |
| `@Paradise summarize` | An AI recap of the recent chat ("recap", "tl;dr", "what did i miss" all work) |
| `@Paradise roll 2d20+3` · `8ball` · `rps` · `pick a number between 1 and 100` | The free fun toolkit — dice, 8-ball, rock-paper-scissors, RNG |
| `@Paradise qr code for <link>` · `password` · `uuid` · `lorem 2` | Offline generators — QR codes, strong passwords, IDs, filler text |
| `@Paradise rot13 …` · `caesar 3 …` · `base64 …` · `hash …` · `morse …` | Ciphers and encodings |
| `@Paradise reverse/mock/count/emojify/spoiler …` | The text utilities |
| `@Paradise days until christmas` · `color #5865F2` | Countdowns and colour cards |
| `@Paradise remind me in 20 minutes to …` · `poll: pizza or burgers` | The reminder and poll engines, by mention |
| `@Paradise who is @user` · `avatar` · `server info` | Discord-native info cards |
| `/memory add/list/remove/clear` | Teach the bot facts it remembers (the lords only) |

## Quiet mode

One switch, no code changes:

```env
PERSONALITY_ENABLED=false
```

Everything on this page stops. Commands, AI, voice and everything else keep
working.

## Tests

`tests/test_banter.py` covers the book (every intensity, anti-repetition, no
slurs), the guards (distress, opt-out, per-person cooldown), the meme feed
(validation, NSFW filtering, caching, backoff, garbage payloads) and the GIF
providers (including the keyless tier and its graceful failure).
`tests/test_humour.py` covers the interjections, the lord acknowledgements and
the context roasts; `tests/test_banter.py` the memory callbacks;
`tests/test_memory.py` the long-term memory, its command and the Supabase
mirror (against a fake client) — all with an injected clock and fetcher, so
the suite stays completely offline.
