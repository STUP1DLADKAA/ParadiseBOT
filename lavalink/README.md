# Paradise's Lavalink node

This directory is the **node-side half** of Paradise's music system: the
configuration for the Lavalink v4 server Paradise connects to. The bot-side
half is `functions/music/` (one `MusicService`, one queue, one panel — buttons
and commands share it) and the connection credentials live in `../.env`
(`LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_PASSWORD`, `LAVALINK_SECURE`).

```
                 PARADISE (bot.py)
                        │
              ┌─────────┴─────────┐
              │                   │
           MUSIC                 TTS
              │                   │
        MusicService          TTSService
              │                   │
         lavalink.py      edge-tts + upload
              │                   │
      ══════════════════════════════════  ← this directory
              │                   │
        ┌─────┴─────┐        http source
        │           │        (direct link)
  youtube-plugin  built-ins
        │      (soundcloud, …)
        └─────┬─────┘
              │
        Lavalink node (application.yml)
```

Both music and TTS go through the same single Lavalink connection and the same
voice connection — TTS clips are synthesised separately (edge-tts), uploaded,
and played through the node's `http` source, so TTS never depends on YouTube
or any music source plugin.

## What's configured

| Piece | Status | Notes |
| --- | --- | --- |
| `dev.lavalink.youtube:youtube-plugin:f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3` | **declared** | The requested YouTube source plugin, pinned to the exact requested snapshot build (commit [`f45bbb7`](https://github.com/lavalink-devs/youtube-source/commit/f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3), "Recover the total length for formats that omit contentLength"). Served from `https://maven.lavalink.dev/snapshots`. |
| `chanom-plugin:1.0.6` | **documented, not declared** | See [below](#chanom-plugin). No repository currently publishes it; declaring guesses would prevent the node from starting. |
| built-in `youtube` source | disabled | Required by the youtube-plugin — it replaces it and serves `ytsearch:` / `ytmsearch:`. |
| `http` source | enabled | Paradise's TTS playback path (uploaded clips as direct links). |
| SoundCloud, Bandcamp, Twitch, Vimeo | enabled | SoundCloud doubles as Paradise's search fallback (`scsearch:`). |
| OAuth / poToken for YouTube | off (optional) | Uncomment in `application.yml` if YouTube starts demanding sign-in. Uses burner credentials only. |

## Running the node

```bash
cd lavalink
LAVALINK_PASSWORD='<a real password>' docker compose up -d
python verify_node.py          # confirms the node + plugins are actually healthy
```

Then point the bot at it (in `../.env`):

```env
LAVALINK_HOST=localhost
LAVALINK_PORT=2333
LAVALINK_PASSWORD=<the same password>
LAVALINK_SECURE=false
LAVALINK_EXPECTED_PLUGINS=youtube-plugin:f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3
```

The port is published on `127.0.0.1` only by default — change the `ports:`
mapping in `docker-compose.yml` deliberately if the bot runs elsewhere, and
never without a real password.

Without Docker (Java 17+):

```bash
java -jar Lavalink.jar   # from this directory; downloads plugins on first start
```

## Verifying startup (do not trust file existence)

`verify_node.py` is the startup validation for this node. It starts the real
server with the real config and checks, in order:

1. the node boots **both** Spring contexts and serves `/version`;
2. every plugin declared under `lavalink.plugins` is actually classloaded
   (`Loaded '<jar>' (N classes)`) **and** reported by `/v4/info` with the
   declared version — silent load failures and version drift both fail;
3. no fatal plugin errors (`FileNotFoundException`, `NoClassDefFoundError`,
   `BeanInstantiationException`, …) appear in the startup log;
4. `/version` answers;
5. the `http` source can load and decode a direct audio link (the TTS path),
   verified with a locally served WAV file.

Exit code 0 means all five passed. It has no dependencies beyond the Python
standard library.

**Offline / air-gapped runs:** Lavalink normally fetches declared plugins from
`maven.lavalink.dev`. If that host is unreachable, point the declaration's
`repository` at a local stand-in serving the plugin's `maven-metadata.xml` and
pass the jar via `--plugin-jar` — classloading, Spring wiring and the REST
surface are still fully exercised. (This is exactly how the configuration in
this directory was verified during development: Lavalink 4.2.1, Java 17, the
plugin jar, all five checks green, plus the negative tests below.)

## chanom-plugin

`chanom-plugin` **v1.0.6** is requested as part of Paradise's Lavalink setup,
and the client side is already wired for it: set

```env
LAVALINK_EXPECTED_PLUGINS=youtube-plugin:f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3,chanom-plugin:1.0.6
```

and Paradise will check for it on every node connection, log clearly when it
is missing, and surface it in `/status`.

It is **not declared in `application.yml`** because no repository currently
publishes it. This was not assumed — it was checked:

- `https://maven.lavalink.dev/releases` and `/snapshots` (the official plugin
  repositories): no `chanom-plugin` under any plausible group (`dev.lavalink`,
  `dev.chanom`, `com.github.chanom`, …);
- JitPack (`com.github.chanom:chanom-plugin`): "Repo not found";
- Maven Central / mvnrepository: no results;
- GitHub (repositories, code search for `chanom-plugin`, `chanom` + lavalink):
  no such plugin project exists — "Chanom" only matches an unrelated Thai
  Discord bot and bubble-tea shops.

Declaring guessed coordinates anyway is not an option: **Lavalink hard-fails
at startup when a declared plugin cannot be resolved** (`FileNotFoundException`
in `PluginManager` → `pluginManager` bean creation fails → the whole node
dies, music and TTS included). This was verified live:

```text
Downloading http://…/com/github/chanom/chanom-plugin/1.0.6/chanom-plugin-1.0.6.jar
Caused by: java.io.FileNotFoundException: …/chanom-plugin-1.0.6.jar
    at lavalink.server.bootstrap.PluginManager.downloadJar(PluginManager.kt:135)
Error creating bean with name 'pluginManager' … Application run failed
```

A node that silently cannot start is the one failure mode this setup must
avoid, so the plugin stays documented-but-undeclared instead of guessed.

**When the real coordinates exist** (from the plugin's own documentation or
the node host), add them under `lavalink.plugins`:

```yaml
lavalink:
  plugins:
    - dependency: "dev.lavalink.youtube:youtube-plugin:f45bbb7aebfcbc1c553769e04af6cd43afa8b7c3"
      snapshot: true
      repository: "https://maven.lavalink.dev/snapshots"
    - dependency: "<group>:chanom-plugin:1.0.6"        # ← real group goes here
      repository: "<the plugin's documented repository>"
```

…restart the node, run `python verify_node.py` again, and that's it: any
sources the plugin registers become available to `/play` automatically —
Paradise resolves queries through whatever sources the node reports, so no bot
changes are needed. If you use the hosted node `free-lava.heavencloud.in`
instead of this one, ask its operator to install both plugins and keep the
`LAVALINK_EXPECTED_PLUGINS` line so drift stays visible.

## How the bot uses the node's sources

- `/play <url>` sends the URL to the node as-is; whatever source claims it
  (YouTube, SoundCloud, http, …) resolves it.
- `/play <text>` tries search sources **in the order the node supports them**:
  `ytsearch:` → `ytmsearch:` → `scsearch:`. Each source is tried exactly once,
  there are no retries or loops, and the node's reported source list (from
  `/v4/info`) decides what is available — a plugin that isn't loaded is never
  queried.
- If a source fails or returns nothing everywhere, the user gets a short
  friendly message; the raw Lavaplayer/Lavalink exception goes to the console
  log only.
- `/join`, `/say` and natural TTS never touch source resolution at all — they
  keep working with an empty queue, a dead YouTube source, or no plugin.
- If the whole node is unreachable, music commands answer "music is
  unavailable right now — the music node isn't responding" and `/status`
  shows Lavalink as **down** (plus which expected plugins are missing, when
  the node is up but incomplete).

## Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `Failed to download plugin … FileNotFoundException` at startup | A declared plugin coordinate doesn't exist (or the repository is unreachable). Remove or fix the declaration — the node will not start while it's there. |
| `/status` shows `⚠️ missing: chanom-plugin` | The node came up without a plugin listed in `LAVALINK_EXPECTED_PLUGINS`. Harmless for everything else; fix on the node side. |
| `A newer version of 'youtube-plugin' was found` in the node log | Lavalink noticed a newer build upstream. Bump the pin in `application.yml` deliberately — and in `LAVALINK_EXPECTED_PLUGINS` — then re-run `verify_node.py`. |
| YouTube searches return "couldn't find anything" with `Something went wrong while looking up the track` in the console | YouTube is blocking the node's IP/clients. Configure `oauth` or `pot` in the `plugins.youtube` section (burner account!). |
| `SSLHandshakeException` from `checkPluginForUpdates` at startup | The node host cannot reach `maven.lavalink.dev` at all (firewall/air-gap). Lavalink treats even the update-metadata check as fatal in this case — mirror the metadata locally or wait for an upstream fix. |
| TTS says "isn't available on the music node" | The node's `http` source is disabled. It's enabled in this config. |

## Files

- `application.yml` — the node configuration (plugins, sources, filters, logging).
- `docker-compose.yml` — the recommended way to run it.
- `verify_node.py` — startup + plugin verification (run it after every change).
- `plugins/`, `logs/`, `Lavalink.jar` — runtime artifacts, git-ignored.
