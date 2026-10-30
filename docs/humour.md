# It talks back: the personality engine

Paradise is not a silent helper. In a friends-only server it is supposed to be
one of the funniest people in the channel — it reacts, it replies, it roasts, it
posts memes, and it never needs an API key to do any of it.

Everything on this page is one layer of `functions/personality.py`. Every layer
is a `PERSONALITY_*` setting, so a server that wants a quieter bot can turn any
of it down without touching code.

```text
message in
   │
   ├─ is it from a bot / a command / about something serious? → silence
   ├─ did they say "stop roasting me"?                        → silence, remembered
   │
   ├─ English typo?      → 🤓 nerd correction
   ├─ "roast me" / "1v1 me" → a roast, right now
   │
   └─ everything else (all gated by the channel cooldowns)
        one of: reply · roast · meme · GIF · sticker · quip · reaction
```

## The layers

| Layer | What it does | Setting | Default |
| --- | --- | --- | --- |
| Reaction | Emoji on a message that matched a known context (33 contexts) | `PERSONALITY_REACTION_CHANCE` | `0.22` |
| Ambient reaction | Emoji on a message that matched *nothing* — presence without words | `PERSONALITY_AMBIENT_REACTION_CHANCE` | `0.10` |
| Reply | A one-liner chosen for that context | `PERSONALITY_REPLY_CHANCE` | `0.10` |
| Roast | A jab aimed at whoever just spoke | `PERSONALITY_ROAST_CHANCE` | `0.12` |
| Meme | A real meme from the internet, or a card drawn locally | `PERSONALITY_MEME_CHANCE` | `0.10` |
| GIF | A reaction GIF (needs a free key) | `PERSONALITY_GIF_CHANCE` | `0.05` |
| Sticker | A native Discord sticker, or a drawn one | `PERSONALITY_STICKER_CHANCE` | `0.05` |
| Quip | An unprompted 200-IQ / 2-IQ observation | `PERSONALITY_QUIP_CHANCE` | `0.12` |
| Nerd | Grammar/spelling correction, English only | `PERSONALITY_GRAMMAR_CHANCE` | `0.70` |

At most **one** of these happens per message, and only when the channel is not
already firing (`PERSONALITY_CHANNEL_COOLDOWN`, default 35 s) and the bot has
not done anything recently anywhere (`PERSONALITY_GLOBAL_COOLDOWN`, default
10 s). Fast, flooded channels are left alone entirely — a bot that replies to a
burst is not funny, it is a wall.

## Roasts

`functions/roasts.py` is the book: three intensities, each with its own pool of
lines, plus comebacks for people being cocky and lines for the bot roasting
itself.

```env
PERSONALITY_ROAST_INTENSITY=spicy   # mild · spicy · nuclear
PERSONALITY_ROAST_CHANCE=0.12
PERSONALITY_ROAST_USER_COOLDOWN=600
```

* **mild** — friendly teasing.
* **spicy** — the default. Mean about what someone said or did.
* **nuclear** — for a server that asked for it.

Somebody can also just ask:

* `@Paradise roast me` or `/roast` — the bot roasts whoever asked.
* `/roast @friend` or `@Paradise roast @friend` — passed on to them.
* Anything cocky ("i'm him", "1v1 me", "no diff", "skill issue") gets a
  comeback aimed at *what they said*.

`PERSONALITY_ROAST_USER_COOLDOWN` is the anti-bullying valve: the same person
cannot be roasted twice inside that window. Nobody gets farmed by a bot, no
matter how much they deserve it.

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

## Memes from the internet

`functions/meme_feed.py` pulls real posts from real Reddit communities through
[meme-api.com](https://meme-api.com), which needs **no key at all**:

```env
MEME_SOURCES=dankmemes,memes,me_irl,wholesomememes
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

GIFs come from Tenor or Giphy, and both need a **free** key. Set one:

```env
TENOR_API_KEY=
GIPHY_API_KEY=
```

With a key, `/gif bruh` and the occasional automatic GIF reply work. Without
one, nothing breaks: `/gif` explains how to turn it on and sends a meme, and the
automatic GIF path is skipped entirely.

## Commands

| Command | What it does |
| --- | --- |
| `/roast [member]` | Roast someone, or yourself |
| `/meme [topic]` | A random internet meme, or a drawn card about `topic` |
| `/gif <query>` | A reaction GIF (free key required) |

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
providers — all with an injected fetcher, so the suite stays completely offline.
