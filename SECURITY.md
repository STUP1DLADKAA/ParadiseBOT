# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private reporting
(**Security → Report a vulnerability**) on
[STUP1DLADKAA/ParadiseBOT](https://github.com/STUP1DLADKAA/ParadiseBOT/security/advisories/new),
or contact the maintainer directly on Discord.

Include what you did, what happened, and what you expected. A minimal
reproduction is worth more than a long report. You will get an acknowledgement
within a few days; please give us a reasonable window before disclosure.

## What this project treats as a secret

| Secret | Where it may live | Never |
| --- | --- | --- |
| `DISCORD_TOKEN` | your private `.env` | committed, logged, sent to a user |
| `SUPABASE_SECRET_KEY` (service_role) | your private `.env` | used in a client, exposed to a browser, committed |
| `GEMINI_API_KEY`, `AI_API_KEY` | `.env` | shown in a message, logged in full |
| `ELEVENLABS_API_KEY` | `.env` | in a URL, in a payload, in a log line |
| `IMAGE_SEARCH_API_KEY` | `.env` | in a rendered card or embed |
| `GITHUB_TOKEN` | `.env` | logged |

CI enforces the obvious version of this: `tools/review_agent.py` fails a pull
request that contains anything shaped like a credential. Keys are masked in
logs, travel only in headers, and are never interpolated into user-facing text.

## Design decisions worth knowing

* **Voice audio** stays in a bounded in-memory ring buffer; it is exported only
  on request, only to an allow-listed operator, and never uploaded elsewhere.
* **Self-update** fetches from a configured GitHub repository, requires the
  archive to contain this bot, compiles every `.py` before writing, rejects
  path traversal, protects `.env`/`.git`/`data/`/databases/logs, and keeps a
  recoverable copy of every replaced file under `data/autoupdate_backup/`.
  Set `AUTO_UPDATE_ENABLED=false` if your deployment must be immutable.
* **User content** is never sent to an AI provider unless the message actually
  needs reasoning (the router decides); prompts are bounded and the system
  prompt forbids revealing configuration.
* **Permissions**: operator-only features (`/edit`, `/clip`) are gated by
  Discord user ID allow-lists in `.env`, checked on the server, and reply with
  a vague error to everyone else.

## Supported versions

Only the latest commit on `main` is supported. This is a hobby project: fixes
land on `main`, and there are no backports.
