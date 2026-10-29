#!/usr/bin/env python3
"""Download the on-device Whisper model without starting the bot.

The bot already fetches its own weights at start-up, so most people never need
this. It exists for the two cases where waiting for start-up is the wrong
moment:

* **building an image** — bake the weights in, then run with
  ``VOICE_WAKE_STT_OFFLINE=true`` and no network at all::

      docker build --build-arg WHISPER_MODEL=small -t paradisebot .
      docker run … -e VOICE_WAKE_STT_MODEL_DIR=/opt/whisper \\
                     -e VOICE_WAKE_STT_OFFLINE=true paradisebot

* **a box with a slow link** — fetch the model over the fast connection
  (laptop, CI) and copy the directory to the server::

      python tools/fetch_whisper_model.py small --dir ./whisper-models

Usage::

    python tools/fetch_whisper_model.py                 # VOICE_WAKE_STT_MODEL, else small
    python tools/fetch_whisper_model.py base --dir /opt/whisper
    python tools/fetch_whisper_model.py small --check    # only report, never fetch

Exit codes: 0 the files are in place, 1 they are not (and the reason is on
stderr), 2 the dependency is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.voice_wake.local_stt import (  # noqa: E402
    DEFAULT_MODEL,
    INSTALL_HINT,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_NOT_INSTALLED,
    STATUS_OFFLINE,
    STATUS_READY,
    directory_size,
    download_model_files,
    find_cached_model,
    human_size,
    looks_like_model_dir,
)


def default_model() -> str:
    """The same default the bot uses: the env setting, else ``small``."""
    env = os.environ.get("VOICE_WAKE_STT_MODEL", "").strip()
    return env or DEFAULT_MODEL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download the Whisper model used by the on-device wake word."
    )
    parser.add_argument(
        "model",
        nargs="?",
        default=default_model(),
        help="tiny, base, small (default), medium, large-v3-turbo, or a model id",
    )
    parser.add_argument(
        "--dir",
        dest="model_dir",
        default=os.environ.get("VOICE_WAKE_STT_MODEL_DIR", "").strip(),
        help="where to put the weights (default: the Hugging Face cache)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether the files are already here; never download",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print progress and errors only, not the result (for build logs)",
    )
    args = parser.parse_args(argv)

    cache_dir = args.model_dir or ""
    if args.check:
        cached = (
            args.model
            if looks_like_model_dir(args.model)
            else find_cached_model(args.model, cache_dir)
        )
        if cached:
            if not args.quiet:
                print(f"whisper {args.model}: on disk ({human_size(directory_size(cached))})")
                print(f"  {cached}")
            return 0
        if not args.quiet:
            print(f"whisper {args.model}: not on this machine")
        return 1

    print(f"whisper {args.model}: checking…", file=sys.stderr if args.quiet else sys.stdout)
    status, path, message = download_model_files(args.model, model_dir=cache_dir)
    if status == STATUS_NOT_INSTALLED:
        print(f"faster-whisper is not installed — run `{INSTALL_HINT}`", file=sys.stderr)
        return 2
    if status in {STATUS_READY, STATUS_DOWNLOADED}:
        if not args.quiet:
            verb = "downloaded" if status == STATUS_DOWNLOADED else "already on disk"
            print(f"whisper {args.model}: {verb} ({human_size(directory_size(path))})")
            print(f"  {path or 'the Hugging Face cache'}")
            if args.model_dir:
                print(
                    "use it without touching the network:\n"
                    f"  VOICE_WAKE_STT_MODEL_DIR={args.model_dir}\n"
                    "  VOICE_WAKE_STT_OFFLINE=true"
                )
        return 0
    if status == STATUS_OFFLINE:
        if not args.quiet:
            print(f"whisper {args.model}: not present — {message}")
        return 1
    if status == STATUS_FAILED:
        print(f"whisper {args.model}: download failed — {message}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
