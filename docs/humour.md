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
        ├─ a lord himself spoke?   → a "my lord 👑" line or a crown
        ├─ two people talking?     → a small chance of "hello!"
        └─ one of: reply · roast · meme · GIF · sticker · quip · reaction
```

## The layers

| Layer | What it does | Setting | Default |
| --- | --- | --- | --- |
| Reaction | Emoji on a message that matched a known context (34 contexts) | `PERSONALITY_REACTION_CHANCE` | `0.22` |
| Ambient reaction | Emoji on a message that matched *nothing* — presence without words | `PERSONALITY_AMBIENT_REACTION_CHANCE` | `0.10` |
| Reply | A one-liner chosen for that context | `PERSONALITY_REPLY_CHANCE` | `0.12` |
| Roast | A jab aimed at whoever just spoke — quoting their own words when there are enough to quote | `PERSONALITY_ROAST_CHANCE` | `0.12` |
| Meme | A real meme from the internet, or a card drawn locally | `PERSONALITY_MEME_CHANCE` | `0.16` |
| GIF | A reaction GIF — keyless by default | `PERSONALITY_GIF_CHANCE` | `0.10` |
| Sticker | A native Discord sticker, or a drawn one | `PERSONALITY_STICKER_CHANCE` | `0.05` |
| Quip | An unprompted 200-IQ / 2-IQ observation | `PERSONALITY_QUIP_CHANCE` | `0.14` |
| Nerd | Grammar/spelling correction, English only | `PERSONALITY_GRAMMAR_CHANCE` | `0.70` |
| Interjection | A "hello!" when two people are talking (10-minute window) | `PERSONALITY_INTERJECT_CHANCE` | `0.08` |
| Lord | A loyal acknowledgement when a lord and master speaks | `PERSONALITY_LORD_REPLY_CHANCE` | `0.20` |
| Memory callback | One fact the bot "already knew" quoted back at the person it's about | `PERSONALITY_MEMORY_CALLBACK_CHANCE` | `0.05` |

New layers:

* **Interjection** — when two or more distinct humans have spoken in a channel
  within ten minutes, the conversation is "live" and Paradise may drop one
  short line of its own ("hello!", "continue, i'm invested now"). Its own
  cooldown (`PERSONALITY_INTERJECT_COOLDOWN`, default 240 s) keeps it a
  surprise rather than a photobombber.
* **Lords** — Stupid (`922359954025885737`, the owner) and Taher
  (`1049231767585964032`) are the lords and masters, at the same level of
  authority (configurable with `PARADISE_LORD_USER_IDS`). When either speaks,
  Paradise occasionally answers with a loyal, slightly cheeky line ("my lord
  has spoken 👑") or just drops a crown reaction on the message. They are also
  the only people who can teach the bot long-term memories (`/memory`), and
  the AI addresses each of them as "my lord" — while still teasing them,
  because they expect it. Each lord's *name* is defended too: disparage
  "stupid" or "taher" in a sentence and Paradise takes it personally.
* **Context roasts** — when the roast target's last words are long enough, the
  roast *quotes them back* ("bro woke up today and chose to say \"pineapple
  belongs on pizza\" out loud."). Roasting the lord uses the loyal-but-cheeky
  frames instead. Short messages fall back to the classic book.
* **Memory callback** — the long memory has a life outside the AI, too. When
  somebody the bot has a fact on (`/memory add … user: @them`) speaks, there
  is a rare chance (`PERSONALITY_MEMORY_CALLBACK_CHANCE`, default 0.05 —
  roughly once every few hours per person) that Paradise just *quotes the
  fact back at them*: `"spent a year reaching level 100 in murder mystery"
  — i know everything about you people, Ash. everything.` No AI call, no
  rewriting — the fact is quoted verbatim (cleaned, capped at 90 characters)
  and only the frame around it is chosen. And the frame **never admits to
  remembering**: no "i remember", no "you told me", no mention of memory —
  Paradise knew already, because it pays attention to everyone in this
  server. The same person is safe for half an hour afterwards
  (`PERSONALITY_MEMORY_CALLBACK_COOLDOWN`, default 1800 s). The joke is the
  timing, not the volume: it works precisely because it is rare.

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

## The aliveness watcher

The part that makes the bot feel like it *lives* here, not just answers here.
`functions/aliveness.py` + `commands/aliveness.py` watch the server and, when
the picture says so, post one short, situation-aware line in the home channel
(`ALIVENESS_CHANNEL_ID`, default `1181574439893217280`):

* **dead** — no human message anywhere for `ALIVENESS_DEAD_AFTER_MINUTES`
  (default 25) minutes;
* **everyone's in VC** — everyone who chatted in the last couple of hours has
  gone quiet and is sitting in a voice call together (at least
  `ALIVENESS_MIN_VC_GROUP`, quiet for `ALIVENESS_VC_QUIET_MINUTES`).

The line is written by the AI from the *real* signals — minutes of silence,
who is in which voice channel and for how long (`VoiceState.joined_at`),
member count, IST time of day, and (only when `PRESENCE_INTENT_ENABLED=true`
**and** the portal toggle is on) how many members are online. No AI
configured? A canned, still-situational line is posted instead.

Guard rails: one post per `ALIVENESS_POST_COOLDOWN_MINUTES` (45), nothing at
all during `ALIVENESS_QUIET_HOURS` (01:00–07:00 IST by default), and a fresh
boot never calls a server dead until it has actually been watching for
`dead_after` minutes (it also seeds "last message" from the channel's history,
so a restart can't produce a false alarm).

## Commands

| Command | What it does |
| --- | --- |
| `/roast [member]` | Roast someone, or yourself |
| `/meme [topic]` | A random internet meme, or a drawn card about `topic` |
| `/gif <query>` | A reaction GIF (works keyless) |
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
mirror (against a fake client); `tests/test_aliveness.py` the dead-server
watcher — all with an injected clock and fetcher, so the suite stays
completely offline.
