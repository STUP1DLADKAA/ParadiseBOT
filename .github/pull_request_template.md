## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- The behaviour or bug this addresses. -->

## How it was verified

- [ ] `python -m pytest -q` is green
- [ ] `python tools/review_agent.py --self-test` passes
- [ ] `python tools/review_agent.py --base origin/main --head HEAD` reports no blocking findings
- [ ] New behaviour has a test (or this is docs/chore only)

## Checklist

- [ ] No secret, token, key or real user/server ID is included
- [ ] New settings appear in both `config.py` and `.env.example`
- [ ] `CHANGELOG.md` updated under `Unreleased` (user-visible changes)
- [ ] Screenshots attached for anything visual (`python tools/make_assets.py`)

## AI-assisted?

<!-- The project welcomes AI-assisted work; say so, and confirm you ran and understand it. -->

- [ ] This change involved AI assistance, and I have run and understood the diff
