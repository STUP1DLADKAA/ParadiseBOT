# Contributing to Paradise

Thanks for taking a look. Paradise is a hobby bot for one friend group, so the
bar is simple: **be useful, be testable, don't break the quiet server.**

## Quick start

```bash
git clone https://github.com/STUP1DLADKAA/ParadiseBOT.git
cd ParadiseBOT
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Nothing in the test suite needs a token, a key or a network connection. If a
test you add needs any of those, it belongs in `tools/` instead.

## Where code goes

| Change | Lives in |
| --- | --- |
| A new Discord command or view | `commands/` (thin: parse input, call `functions/`, render a reply) |
| The engine behind it | `functions/<topic>/` |
| A new setting | `config.py` **and** `.env.example` (the tests check they agree) |
| Tests | `tests/test_<topic>.py` |
| Diagnostics, reproducers, fuzzers | `tools/` (`tools/repro/` for incident scripts) |

Rules of the house:

* `commands/` may import from `functions/`, never the other way round.
* Every `functions/` module must be importable without a Discord gateway.
* Only `config.py` reads the environment.
* User-facing errors are friendly strings (raise `ParadiseError`); raw provider
  errors are logged, never shown.
* Card rendering draws in *design* coordinates through the `_Painter` wrapper,
  so it stays resolution independent.

## Before you open a pull request

```bash
python -m pytest -q                       # must be green
python tools/review_agent.py --self-test  # static-review rules still pass
python tools/review_agent.py --base origin/main --head HEAD   # no blocking findings
```

CI runs the same three things plus an end-to-end voice-pipeline smoke run.
Blocking findings from the review agent (committed secrets, destructive SQL,
blocking calls inside coroutines, unbounded shutdown flushes) will fail the
check — fix them rather than silencing them.

## Tests

* One file per topic: `tests/test_<topic>.py`.
* `unittest` classes run by `pytest` — `IsolatedAsyncioTestCase` for async code.
* Replace the network at the seam, not the function under test: pool sessions
  are injected through `functions.perf.shared_session` (see
  `tests/test_images.py::use_session`), Supabase through `tests/voice_fakes.py`.
* Never assert on wall-clock timing; wait for observable state
  (`tests/test_voice_clip.py::_wait_until`).
* Bug fixes come with a regression test that fails without the fix.

## Style

* Python 3.11+, 4-space indentation, `from __future__ import annotations`.
* Type hints on public functions; dataclasses (`slots=True`) for records.
* Prefer a small pure function to a clever one.
* Comments explain *why*. If a constant looks odd, say where it came from.
* `ruff` config lives in `pyproject.toml`; `pyflakes` is enough for CI.

## Commits and pull requests

* Small, focused commits with a sentence-long summary in the imperative mood.
* Describe the behaviour change and how you verified it; screenshots for
  anything visual (`python tools/make_assets.py` regenerates the README images
  from the renderer itself).
* One concern per pull request. Refactors and behaviour changes stay separate.
* New behaviour that a user can notice goes in `CHANGELOG.md` under
  `Unreleased`.

## AI-assisted contributions

They are welcome — the project itself was built that way. Two conditions:

1. **You have run it.** Tests green locally, and you understand the diff.
2. **You can explain it.** If a reviewer asks why a branch exists, "the model
   wrote it" is not an answer.

## Security

Please do not open a public issue for a vulnerability — see
[SECURITY.md](SECURITY.md).
