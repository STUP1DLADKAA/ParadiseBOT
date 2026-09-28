#!/usr/bin/env python3
"""Verify that a real Lavalink node starts cleanly with the declared plugins.

This is Paradise's node startup validation (see lavalink/README.md). It never
fakes success: it starts the actual Lavalink server with the actual config,
watches the startup logs, and then interrogates the node over its REST API.

What it checks, in order:

  1. the node process starts, boots both Spring contexts and serves /version;
  2. every plugin declared under ``lavalink.plugins`` in the config is
     classloaded during startup ("Loaded '<jar>' (N classes)") AND is reported
     by ``/v4/info`` with the declared version — a plugin that silently failed
     to load, or drifted from the pin, fails this script;
  3. no plugin bootstrap errors appear in the startup log;
  4. ``/version`` answers;
  5. the http source can load and decode a direct audio link (the path
     Paradise's TTS uses), verified with a locally served WAV file.

Offline runs: Lavalink normally downloads declared plugins from
maven.lavalink.dev. If that host is unreachable (air-gapped CI, sandbox), point
the declaration's ``repository`` at a local stand-in that serves the plugin's
``maven-metadata.xml`` and pass the jar via ``--plugin-jar`` — classloading,
Spring wiring and the REST surface are still fully exercised.

Usage (from the lavalink/ directory):

    python verify_node.py                       # downloads Lavalink.jar if missing
    python verify_node.py --lavalink-jar /path/to/Lavalink.jar
    python verify_node.py --plugin-jar /path/to/some-plugin.jar   # offline plugins
    python verify_node.py --password … --port 2333

Exit code 0 = every check passed. Anything else prints why.

Only Python's standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "application.yml")
LAVALINK_RELEASE_JAR = "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar"

READY_MARKER = re.compile(r"Started Launcher in")
PLUGIN_JAR_LOADED = re.compile(r"Loaded '(?P<jar>[^']+)' \(\d+ classes\)")
DECLARED_PLUGIN = re.compile(
    r"-\s*dependency:\s*[\"']?(?P<group>[^:\"'\s]+):(?P<name>[^:\"'\s]+):(?P<version>[^\"'\s]+)"
)
FATAL_LOG = re.compile(
    r"APPLICATION FAILED TO START|Application run failed|PluginBootException|"
    r"NoClassDefFoundError|ClassNotFoundException|BeanInstantiationException|"
    r"FileNotFoundException|Failed to download plugin"
)


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok: bool = False
        self.detail: str = ""

    def passed(self, detail: str = "") -> None:
        self.ok = True
        self.detail = detail

    def failed(self, detail: str) -> None:
        self.ok = False
        self.detail = detail


def log(message: str) -> None:
    print(f"[verify] {message}", flush=True)


def parse_declared_plugins(config_path: str) -> list[tuple[str, str]]:
    """Extract (artifact-name, version) pairs from lavalink.plugins declarations."""
    declared: list[tuple[str, str]] = []
    with open(config_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:  # commented-out entries don't count
                continue
            match = DECLARED_PLUGIN.search(stripped)
            if match:
                declared.append((match.group("name"), match.group("version")))
    return declared


def make_silent_wav(path: str, seconds: float = 1.0, rate: int = 16000) -> None:
    """A tiny, valid, boring WAV file for the http-source load check."""
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = int(rate * seconds)
        wav.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))


def serve_file(directory: str) -> tuple[ThreadingHTTPServer, str]:
    """Serve ``directory`` on an ephemeral localhost port; returns (server, url)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = os.path.join(directory, urllib.parse.unquote(self.path.lstrip("/")))
            if not os.path.isfile(path):
                self.send_error(404)
                return
            with open(path, "rb") as handle:
                data = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # keep the console quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/test.wav"


@lru_cache(maxsize=None)
def auth_header(password: str) -> str:
    # Lavalink's documented auth: the raw password in the Authorization header.
    return password


def rest_get(url: str, password: str, timeout: float = 15.0) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"Authorization": auth_header(password)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            try:
                return response.status, json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return response.status, body.decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except urllib.error.URLError as error:
        return 0, str(error.reason)


def download(url: str, destination: str) -> None:
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="application.yml to verify (default: the one next to this script)")
    parser.add_argument("--lavalink-jar", default=os.path.join(HERE, "Lavalink.jar"), help="path to Lavalink.jar; if missing, the latest release is downloaded")
    parser.add_argument("--plugin-jar", action="append", default=[], help="pre-seed the plugins dir with this jar (repeatable; for offline runs)")
    parser.add_argument("--java", default="java", help="java executable (needs Java 17+)")
    parser.add_argument("--java-arg", action="append", default=[], help="extra JVM argument, repeatable (e.g. -Xmx1G)")
    parser.add_argument("--host", default="127.0.0.1", help="node host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="node port (default $LAVALINK_PORT or 2333)")
    parser.add_argument("--password", default=os.environ.get("LAVALINK_PASSWORD", "youshallnotpass"), help="node password (default $LAVALINK_PASSWORD or youshallnotpass)")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for node startup (default 180)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch workdir for debugging")
    args = parser.parse_args()

    if args.port is None:
        args.port = int(os.environ.get("LAVALINK_PORT", "2333"))

    config_path = os.path.abspath(args.config)
    if not os.path.isfile(config_path):
        print(f"[verify] config not found: {config_path}", file=sys.stderr)
        return 2
    java = shutil.which(args.java) or args.java  # the node runs with a different cwd
    if shutil.which(java) is None:
        print(f"[verify] java not found: {args.java} (Lavalink needs Java 17+)", file=sys.stderr)
        return 2
    declared = parse_declared_plugins(config_path)
    log(f"declared plugins in {os.path.basename(config_path)}: "
        + (", ".join(f"{n}@{v}" for n, v in declared) or "(none)"))

    lavalink_jar = os.path.abspath(args.lavalink_jar)
    if not os.path.isfile(lavalink_jar):
        try:
            download(LAVALINK_RELEASE_JAR, lavalink_jar)
        except Exception as error:  # noqa: BLE001 - report any download failure
            print(f"[verify] could not obtain Lavalink.jar ({error}). Pass --lavalink-jar.",
                  file=sys.stderr)
            return 2

    workdir = tempfile.mkdtemp(prefix="paradise-lavalink-verify-")
    plugins_dir = os.path.join(workdir, "plugins")
    os.makedirs(plugins_dir)
    shutil.copyfile(config_path, os.path.join(workdir, "application.yml"))
    for jar in args.plugin_jar:
        shutil.copyfile(os.path.abspath(jar), os.path.join(plugins_dir, os.path.basename(jar)))
    make_silent_wav(os.path.join(workdir, "test.wav"))

    checks = [Check(n) for n in (
        "node starts", "declared plugins load", "no fatal plugin errors",
        "/version answers", "http source loads audio",
    )]
    by_name = {check.name: check for check in checks}

    env = dict(os.environ)
    env["LAVALINK_PASSWORD"] = args.password
    env["LAVALINK_PORT"] = str(args.port)

    log(f"workdir: {workdir}")
    log(f"starting: {java} -jar {os.path.basename(lavalink_jar)} (port {args.port})")
    process = subprocess.Popen(
        [java, *args.java_arg, "-jar", lavalink_jar],
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )

    server = None
    log_lines: list[str] = []
    started_at = time.monotonic()
    try:
        ready = False
        fatal: str | None = None
        loaded_jars: list[str] = []

        def read_stream() -> None:
            nonlocal ready, fatal
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                log_lines.append(line)
                if "lavalink" in line.lower() or "plugin" in line.lower() or ready is False:
                    print(f"    {line}", flush=True)
                if READY_MARKER.search(line):
                    ready = True
                match = PLUGIN_JAR_LOADED.search(line)
                if match:
                    loaded_jars.append(match.group("jar"))
                if fatal is None and FATAL_LOG.search(line):
                    fatal = line

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()

        while not ready and time.monotonic() - started_at < args.timeout:
            if process.poll() is not None:
                break
            time.sleep(0.2)

        # Lavalink 4 boots Spring TWICE: a bootstrap context (which scans and
        # classloads plugin jars) and then the real application context, where
        # Undertow binds the REST port a couple of seconds after the first
        # "Started Launcher" line. The log alone is therefore not proof the
        # node is serving — poll /version until it actually answers.
        base = f"http://{args.host}:{args.port}"
        version_status, version_body = 0, ""
        poll_deadline = time.monotonic() + 30
        while time.monotonic() < poll_deadline:
            version_status, version_body = rest_get(f"{base}/version", args.password, timeout=3.0)
            if version_status == 200 or process.poll() is not None:
                break
            time.sleep(0.5)

        if ready and version_status == 200:
            by_name["node starts"].passed(
                "startup log shows Spring Boot ready and the REST port is serving"
            )
        else:
            tail = "\n".join(log_lines[-25:])
            by_name["node starts"].failed(
                f"node did not finish starting within {args.timeout}s "
                f"(exit code {process.poll()}, /version HTTP {version_status})\n"
                f"--- log tail ---\n{tail}"
            )

        if fatal:
            by_name["no fatal plugin errors"].failed(fatal)
        else:
            by_name["no fatal plugin errors"].passed("no plugin bootstrap failures in the log")

        # Fetch /v4/info once; it is the authoritative statement of what the
        # node really loaded (what Paradise's /play resolves through).
        status, info = rest_get(f"{base}/v4/info", args.password)
        reported_plugins: dict[str, str] = {}
        if status == 200 and isinstance(info, dict):
            reported_plugins = {
                str(p.get("name")): str(p.get("version"))
                for p in (info.get("plugins") or [])
                if p.get("name")
            }
            log(f"/v4/info plugins: {reported_plugins or '(none)'}")
            log(f"/v4/info sources: {', '.join(info.get('sourceManagers') or []) or '(none)'}")

        if version_status == 200:
            by_name["/version answers"].passed(f"Lavalink {str(version_body).strip()}")
        else:
            by_name["/version answers"].failed(f"HTTP {version_status}: {version_body}")

        if not declared:
            by_name["declared plugins load"].passed("(nothing declared — nothing to check)")
        else:
            problems = []
            if version_status != 200 or not isinstance(info, dict) or status != 200:
                problems.append(
                    "node is not answering on its REST port — plugin state cannot be verified"
                )
            else:
                for name, version in declared:
                    # Note: "Found plugin" log lines only appear for jars that
                    # were already on disk; downloaded jars go straight to the
                    # classloading step. The authoritative evidence is the
                    # "Loaded '<jar>' (N classes)" line plus /v4/info.
                    if not any(f"{name}-{version}.jar" in jar for jar in loaded_jars):
                        problems.append(
                            f"{name}-{version}.jar was never classloaded (no 'Loaded' "
                            f"line) — the plugin did not actually initialize"
                        )
                    reported = reported_plugins.get(name)
                    if reported is None:
                        problems.append(
                            f"{name}@{version}: /v4/info does not list it "
                            f"(node reports: {reported_plugins or 'no plugins'})"
                        )
                    elif reported != version:
                        problems.append(
                            f"{name}: /v4/info reports version {reported}, config declares {version}"
                        )
            if problems:
                by_name["declared plugins load"].failed("; ".join(problems))
            else:
                by_name["declared plugins load"].passed(
                    ", ".join(f"{n}@{reported_plugins.get(n, '?')}" for n, _ in declared)
                    + " (log + /v4/info agree)"
                )

        if status == 200 and isinstance(info, dict):
            server, url = serve_file(workdir)
            try:
                load_status, load = rest_get(
                    f"{base}/v4/loadtracks?identifier={urllib.parse.quote(url, safe='')}",
                    args.password,
                )
            finally:
                server.shutdown()
            load_type = load.get("loadType") if isinstance(load, dict) else None
            if load_status == 200 and load_type in ("track", "playlist"):
                by_name["http source loads audio"].passed(f"loadType={load_type} via {url}")
            else:
                detail = json.dumps(load)[:300] if not isinstance(load, str) else load[:300]
                by_name["http source loads audio"].failed(
                    f"HTTP {load_status} loadType={load_type} {detail}"
                )
        else:
            by_name["http source loads audio"].failed(
                f"/v4/info unreachable (HTTP {status}) — cannot confirm sources over REST"
            )
    finally:
        if server is not None:
            server.shutdown()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    print()
    failed = False
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        if not check.ok:
            failed = True
        detail = f" — {check.detail}" if check.detail else ""
        print(f"  [{mark}] {check.name}{detail}")
    print()
    if args.keep:
        print(f"[verify] workdir kept: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    if failed:
        print("[verify] node verification FAILED — see above. The music node is NOT healthy.")
        return 1
    print("[verify] node verification passed — Lavalink started and every declared plugin loaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
