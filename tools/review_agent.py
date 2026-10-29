#!/usr/bin/env python3
"""Paradise review agent — static review of a code change.

Reviews the *diff* (not the whole repository), so pre-existing patterns do not
flood the report, and focuses on the failure modes that actually matter for
this bot:

    data safety      destructive SQL, schema changes, dropped pending writes
    async safety     blocking calls on the event loop, unbounded task creation
    reliability      swallowed exceptions, unsafe retries, unbounded reads
    security         committed credentials or private keys
    coverage         production changes without a matching test change

Every finding carries a severity, the file, the line in the head revision, an
explanation and a recommended fix. ``blocking`` findings fail the run (and the
CI job); ``warning`` findings are printed but do not fail unless asked to.

The agent never edits, merges, deploys or talks to GitHub. It is deliberately
dependency-free (stdlib only) so it can run in any CI runner.

Usage::

    python3 tools/review_agent.py --base origin/main --head HEAD
    python3 tools/review_agent.py --worktree          # review uncommitted work
    python3 tools/review_agent.py --base HEAD~1 --json
    python3 tools/review_agent.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"
_SEVERITY_ORDER = {BLOCKING: 0, WARNING: 1, INFO: 2}


@dataclass
class Finding:
    severity: str
    rule: str
    file: str
    line: int
    message: str
    fix: str
    snippet: str = ""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class ReviewReport:
    base: str
    head: str
    files_changed: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.file, f.line or 0),
        )

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def blocked(self) -> bool:
        return self.count(BLOCKING) > 0


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_CHANGED = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class FileDiff:
    path: str
    status: str = "modified"
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout


def worktree_files() -> list[FileDiff]:
    """Tracked changes plus untracked files, so local review sees everything."""
    files = parse_diff(_git("diff", "--unified=0", "--no-color", "HEAD"))
    seen = {diff.path for diff in files}
    for rel in _git("ls-files", "--others", "--exclude-standard").splitlines():
        if rel in seen:
            continue
        path = REPO_ROOT / rel
        if not path.is_file() or path.stat().st_size > 400_000:
            continue
        diff = FileDiff(path=rel, status="added")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        diff.added = [(number, line) for number, line in enumerate(text.splitlines(), 1)]
        files.append(diff)
    return files


def parse_diff(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    new_line = 0
    for raw in diff_text.splitlines():
        match = _CHANGED.match(raw)
        if match:
            current = FileDiff(path=match.group(2))
            files.append(current)
            continue
        if current is None:
            continue
        if raw.startswith("new file mode"):
            current.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.added.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current.removed.append(raw[1:])
        elif raw.startswith(" "):
            new_line += 1
    return files


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

#: Values that are obviously documentation, not a credential: ``<your-key>``,
#: ``${VAR}``, ``xxx``, ``changeme``, ``...``. A doc showing the *shape* of a
#: setting must not be reported as a leaked secret.
PLACEHOLDER_VALUE = re.compile(
    r"""(?ix)
    ^(?:
        <.*                       # <service_role key>  (closing > optional)
      | \$\{.*\}                  # ${SUPABASE_SECRET_KEY}
      | \[.*\]                    # [your key]
      | \.{3,}                     # ...
      | [x*]{3,}                  # xxx / ***
      | (?:your|my|example)[-_\w]*
      | (?:change|replace)[-_]?me
      | (?:redacted|placeholder|insert[-_]?here|todo)
      | (?:key|token|secret|value)[-_]?(?:here|goes[-_]?here)
    )
    """,
)

#: An explicit, greppable opt-out for a *test fixture* that has to look like a
#: credential. It is deliberately verbose so it stands out in review, and it is
#: only ever appropriate inside tests/.
SECRET_FIXTURE_MARKER = "review-agent: allow-secret-fixture"

SECRET_PATTERNS = (
    (re.compile(r"sb_secret_[A-Za-z0-9_\-]{10,}"), "Supabase secret key"),
    (re.compile(r"sbp_[A-Za-z0-9]{20,}"), "Supabase personal token"),
    (re.compile(r"[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27}"), "Discord bot token"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "Google API key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
    (
        re.compile(r"(?:SUPABASE_SECRET_KEY|SUPABASE_SERVICE_KEY|DISCORD_TOKEN|"
                   r"GEMINI_API_KEY|AI_API_KEY|IMAGE_SEARCH_API_KEY)\s*=\s*[^\s\"']{8,}"),
        "filled-in credential",
    ),
)

DESTRUCTIVE_SQL = re.compile(
    r"\b(drop\s+table|truncate\s+table|truncate\b|drop\s+column|drop\s+schema|"
    r"alter\s+table\s+public\.\w+\s+drop|delete\s+from\s+public\.\w+|"
    r"update\s+public\.voice_\w+\s+set)\b",
    re.IGNORECASE,
)

# Tables that hold the record of what happened. Anything that can remove or
# rewrite a row here is destructive by definition.
HISTORY_TABLES = (
    "voice_sessions",
    "voice_daily_stats",
    "voice_member_profiles",
    "voice_session_corrections",
    "voice_migration_state",
)
# The only table holding "who is in voice right now". Ending a session
# removes one keyed row from it; that is normal operation, not data loss,
# because the interval has already been written to voice_sessions.
TRANSIENT_TABLE = "voice_active_sessions"

SQL_COMMENT = re.compile(r"--[^\n]*")
ROW_KEYED = re.compile(r"where\b(?=[^;]*\bguild_id\b)(?=[^;]*\buser_id\b)", re.I | re.S)


def _sql_body_lines(content: str) -> set[int]:
    """1-based line numbers that fall inside a ``$$ ... $$`` function body.

    Operations inside a function body only run when the bot calls the
    function, with the arguments the bot passes. Top-level operations run
    against the whole table the moment the migration is applied, which is
    what the owner has to be protected from.
    """
    bodies: set[int] = set()
    inside = False
    for index, line in enumerate(content.splitlines(), start=1):
        if "$$" in line and not inside:
            inside = True
            continue
        if "$$" in line and inside:
            inside = False
            continue
        if inside:
            bodies.add(index)
    return bodies


def _is_placeholder(text: str) -> bool:
    """True when the credential-shaped values on this line are documented filler.

    A *quoted* value is code, not documentation: ``KEY = "sb_secret_…"`` keeps
    being reported even if it reads like a sample. Only bare ``KEY=<placeholder>``
    forms are excused, and only when every value on the line is one.
    """
    values = re.findall(r"=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'#]+)", text)
    if not values:
        return False
    for raw in values:
        value = raw.strip()
        if value[:1] in ("\"", "'"):
            return False
        if not PLACEHOLDER_VALUE.match(value):
            return False
    return True


def _strip_sql_comments(line: str) -> str:
    """Blank out ``--`` comments so prose cannot trip a code rule."""
    return SQL_COMMENT.sub(" ", line)

# Sync-only calls that must never run inside a coroutine. Detected with the
# AST (only inside `async def`) so module-level CLI code never trips it.
BLOCKING_CALLS = {
    "time.sleep": "time.sleep() blocks the event loop",
    "requests.get": "synchronous requests call in async code",
    "requests.post": "synchronous requests call in async code",
    "requests.put": "synchronous requests call in async code",
    "requests.request": "synchronous requests call in async code",
    "urllib.request.urlopen": "blocking urlopen() in async code",
    "subprocess.run": "blocking subprocess call in async code",
    "subprocess.call": "blocking subprocess call in async code",
    "subprocess.check_output": "blocking subprocess call in async code",
    "os.system": "blocking os.system() call in async code",
}

# The agent's own source embeds every banned pattern as a rule definition, so
# it cannot meaningfully review itself.
SKIP_FILES = frozenset({"tools/review_agent.py"})

SWALLOWED = re.compile(r"except\s*(?:\([^)]*\)|[\w.]+)\s*:\s*(?:pass|continue)\s*(?:#.*)?$")

BUFFER_NAMES = re.compile(r"self\.(_dirty_\w+|_pending_\w+|_outbox_\w*queue\w*)\s*[:=]")


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def async_blocking_calls(node: ast.AST) -> dict[int, str]:
    """Map line numbers of blocking calls *inside coroutines* to a reason."""
    found: dict[int, str] = {}

    def scan(body: list[ast.stmt]) -> None:
        for child in body:
            for sub in ast.walk(child):
                if isinstance(sub, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)):
                    continue
                if isinstance(sub, ast.Call):
                    name = _dotted_name(sub.func)
                    reason = BLOCKING_CALLS.get(name)
                    if reason:
                        found[sub.lineno] = reason

    for node_ in ast.walk(node):
        if isinstance(node_, ast.AsyncFunctionDef):
            # Nested sync defs inside a coroutine are not executed on loop call.
            scan([stmt for stmt in node_.body if not isinstance(stmt, (ast.FunctionDef,))])
    return found


def _load_python(path: str) -> ast.AST | None:
    try:
        source = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        return ast.parse(source, filename=path)
    except (OSError, SyntaxError, ValueError):
        return None


def review_file(diff: FileDiff, report: ReviewReport) -> None:
    path = diff.path
    if path in SKIP_FILES:
        return
    if diff.status == "deleted":
        report.add(
            Finding(
                WARNING,
                "REPO001",
                path,
                0,
                "File deleted — confirm nothing still imports or calls it.",
                "Grep for imports/usages before merging; deletions of voice "
                "storage or command modules are behaviour changes.",
            )
        )
        return

    is_test = path.startswith("tests/") or "/tests/" in path
    is_sql = path.endswith(".sql")
    is_python = path.endswith(".py")
    is_env = Path(path).name.startswith(".env") and Path(path).name != ".env.example"
    blocking_calls = async_blocking_calls(tree) if (is_python and (tree := _load_python(path))) else {}

    sql_source = ""
    sql_bodies: set[int] = set()
    all_lines: list[str] = []
    if is_sql:
        candidate = REPO_ROOT / path
        if candidate.exists():
            sql_source = candidate.read_text(encoding="utf-8", errors="replace")
            all_lines = sql_source.splitlines()
            sql_bodies = _sql_body_lines(sql_source)

    if is_env and diff.status == "added":
        report.add(
            Finding(
                BLOCKING,
                "SEC002",
                path,
                0,
                "A real .env file is being committed.",
                "Remove it from the change and rotate any credential it contained.",
            )
        )

    for line_no, text in diff.added:
        snippet = text.strip()[:160]

        # --- secrets ---------------------------------------------------
        for pattern, label in SECRET_PATTERNS:
            allowed_fixture = SECRET_FIXTURE_MARKER in text
            if (
                pattern.search(text)
                and ".env.example" not in path
                and not allowed_fixture
                and not _is_placeholder(text)
            ):
                report.add(
                    Finding(
                        BLOCKING,
                        "SEC001",
                        path,
                        line_no,
                        f"Possible {label} committed to the repository.",
                        "Remove the value, rotate the credential, and load it "
                        "from the environment instead.",
                        snippet,
                    )
                )
                break

        # --- destructive SQL / schema changes ---------------------------
        # Comments are stripped first: a migration that documents "NO drop,
        # NO truncate, NO delete" must not be flagged for saying so.
        if is_sql and DESTRUCTIVE_SQL.search(_strip_sql_comments(text)):
            target = re.search(
                r"(?:delete\s+from|update)\s+public\.(\w+)", text, re.I
            )
            table = target.group(1) if target else ""
            # Ending a session removes one keyed row from the liveness table
            # from inside voice_close_active_session. That is the normal end
            # of a session, not a migration, and it is worth a warning so the
            # scope stays visible rather than being silently allowed.
            keyed_transient = (
                table == TRANSIENT_TABLE
                and line_no in sql_bodies
                and bool(ROW_KEYED.search(" ".join(all_lines[line_no - 1 : line_no + 3])))
            )
            if table and table not in HISTORY_TABLES and not keyed_transient:
                severity, why = BLOCKING, (
                    "Voice data must never be renamed, truncated, migrated or "
                    "recalculated as part of an optimisation. Revert this and "
                    "raise it separately for explicit owner approval."
                )
                title = "Destructive or data-affecting SQL in the change."
            elif keyed_transient:
                severity, why = WARNING, (
                    "Confirm this only ever removes the single row for the "
                    "session being closed, and that the interval has already "
                    "been written to voice_sessions."
                )
                title = (
                    "Keyed delete on the transient liveness table — verify "
                    "its scope."
                )
            else:
                severity, why = BLOCKING, (
                    "Voice data must never be renamed, truncated, migrated or "
                    "recalculated as part of an optimisation. Revert this and "
                    "raise it separately for explicit owner approval."
                )
                title = "Destructive or data-affecting SQL in the change."
            report.add(
                Finding(
                    severity,
                    "DB001",
                    path,
                    line_no,
                    title,
                    why,
                    snippet,
                )
            )

        # --- SQL access from Python -----------------------------------
        if is_python and not is_test:
            if re.search(r"\.delete\s*\(\s*\)", text) and "supabase" not in path:
                report.add(
                    Finding(
                        WARNING,
                        "DB004",
                        path,
                        line_no,
                        "Unfiltered delete() — verify it cannot remove more rows "
                        "than intended.",
                        "Add an explicit eq() filter or a guarded RPC, and confirm "
                        "the deleted scope in a test.",
                        snippet,
                    )
                )
            if re.search(r"\.table\s*\(", text) and "select\(" in text:
                pass  # filter check below
            if re.search(r"select\s*\(\s*[\"']\*[\"']\s*\)", text):
                report.add(
                    Finding(
                        WARNING,
                        "PERF001",
                        path,
                        line_no,
                        "select('*') loads more columns than needed.",
                        "Select only the columns the caller reads.",
                        snippet,
                    )
                )

        # --- blocking calls in async code ------------------------------
        if is_python and not is_test:
            reason = blocking_calls.get(line_no)
            if reason:
                report.add(
                    Finding(
                        BLOCKING,
                        "ASY001",
                        path,
                        line_no,
                        reason,
                        "Use an async API, asyncio.sleep(), or asyncio.to_thread() "
                        "for unavoidable blocking work.",
                        snippet,
                    )
                )

        # --- error handling ------------------------------------------
        if is_python and SWALLOWED.search(text):
            report.add(
                Finding(
                    WARNING,
                    "ERR001",
                    path,
                    line_no,
                    "Exception swallowed without logging or handling.",
                    "Log at debug/warning with exc_info, or handle the specific "
                    "failure explicitly.",
                    snippet,
                )
            )

        # --- retries without backoff --------------------------------
        if is_python and re.search(r"while\s+True\s*:", text):
            pass  # the backoff check runs on the whole file below

        # --- unbounded concurrency ----------------------------------
        unbounded_names = ("rows", "members", "users", "ids", "messages", "channels")
        if (
            is_python
            and re.search(r"asyncio\.gather\s*\(", text)
            and (" for " in text or any(name in text for name in unbounded_names))
        ):
            report.add(
                Finding(
                    INFO,
                    "ASY002",
                    path,
                    line_no,
                    "gather() over a dynamic collection — confirm it is bounded.",
                    "Cap the input or use a semaphore when the size comes from "
                    "Discord/Supabase data.",
                    snippet,
                )
            )

        # --- task lifecycle -----------------------------------------
        if is_python and re.search(r"asyncio\.create_task\s*\(", text):
            lookahead = " ".join(
                line for number, line in diff.added if line_no <= number <= line_no + 3
            )
            if "name=" not in lookahead:
                report.add(
                    Finding(
                        INFO,
                        "ASY003",
                        path,
                        line_no,
                        "Background task created without a name or lifetime check.",
                        "Name the task and ensure it is cancelled/awaited on shutdown.",
                        snippet,
                    )
                )

    # --- review context rules (whole file after the change) -------------
    if is_python and not is_test and diff.added:
        try:
            content = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""

        if content:
            adds_buffer = any(BUFFER_NAMES.search(text) for _, text in diff.added)
            has_flush = re.search(r"async def (flush|_flush)\w*\s*\(", content)
            has_shutdown = re.search(r"async def (stop|close|shutdown)\w*\s*\(", content)
            bounded = re.search(
                r"wait_for\([\s\S]{0,300}?timeout\s*=", content
            )
            if adds_buffer and not has_flush:
                report.add(
                    Finding(
                        BLOCKING,
                        "DATA001",
                        path,
                        diff.added[0][0],
                        "A bounded write buffer was added but no flush path exists.",
                        "Pending voice-time updates must never be silently lost: "
                        "add an explicit flush used by the writer, shutdown and "
                        "reconciliation.",
                    )
                )
            if adds_buffer and has_flush and (not has_shutdown or not bounded):
                report.add(
                    Finding(
                        BLOCKING,
                        "DATA002",
                        path,
                        diff.added[0][0],
                        "Buffered writes have no bounded shutdown flush.",
                        "Stop() must flush pending rows within a timeout, and the "
                        "timeout path must leave the data in the durable outbox.",
                    )
                )
            if re.search(r"while\s+True\s*:", content) and diff.added:
                for line_no, text in diff.added:
                    if re.search(r"await\s+\w+\(.*\)", text):
                        backoff = bool(re.search(r"asyncio\.sleep\(", content))
                        if not backoff:
                            report.add(
                                Finding(
                                    WARNING,
                                    "ERR002",
                                    path,
                                    line_no,
                                    "Retry loop without backoff detected.",
                                    "Add a bounded sleep or exponential backoff, and "
                                    "cap the number of attempts.",
                                    text.strip()[:160],
                                )
                            )
                            break

    # --- schema changes ---------------------------------------------------
    if "migrations/" in path and path.endswith(".sql"):
        verb = "modified" if diff.status != "added" else "added"
        # A migration that can only add things still needs the owner to run
        # it in the Dashboard, but it cannot destroy data, so it should not
        # hard-block a change the owner asked for. Anything that can remove
        # or rewrite a row at the top level stays blocking.
        top_level = [
            (line_no, text)
            for line_no, text in diff.added
            if sql_source and line_no not in sql_bodies
        ]
        destructive_top_level = any(
            DESTRUCTIVE_SQL.search(_strip_sql_comments(text))
            for _line_no, text in top_level
        )
        touches_history = any(
            re.search(r"\b%s\b" % table, _strip_sql_comments(text), re.I)
            for table in HISTORY_TABLES
            for _line_no, text in top_level
            if DESTRUCTIVE_SQL.search(_strip_sql_comments(text))
        )
        if diff.status == "added" and not destructive_top_level and not touches_history:
            report.add(
                Finding(
                    WARNING,
                    "DB002",
                    path,
                    0,
                    f"Additive database migration {verb} in this change.",
                    "It adds no destructive top-level statement, but it still "
                    "changes the schema: the owner must apply it in the "
                    "Supabase Dashboard (SQL Editor) and confirm "
                    "voice_schema_version() afterwards. Prove it is "
                    "non-destructive with supabase/validate_locally.mjs.",
                )
            )
        else:
            report.add(
                Finding(
                    BLOCKING,
                    "DB002",
                    path,
                    0,
                    f"Database migration {verb} in this change.",
                    "Routine optimisation must not alter the Supabase schema. Revert "
                    "it and ask the owner to apply (or approve) any SQL separately.",
                )
            )


def review(report: ReviewReport, files: list[FileDiff]) -> ReviewReport:
    report.files_changed = len(files)
    for diff in files:
        review_file(diff, report)

    production = [
        f
        for f in files
        if f.path.endswith(".py")
        and not f.path.startswith("tests/")
        and "/tests/" not in f.path
    ]
    tests = [f for f in files if f.path.startswith("tests/") or "/tests/" in f.path]
    if production and not tests:
        report.add(
            Finding(
                WARNING,
                "TEST001",
                production[0].path,
                0,
                f"{len(production)} production file(s) changed with no test change.",
                "Add or update a focused test for the behaviour change; passing "
                "tests are not by themselves evidence of correctness.",
            )
        )
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: ReviewReport) -> None:
    print(f"Review agent: {report.base}...{report.head} ({report.files_changed} file(s))")
    findings = report.sorted_findings()
    if not findings:
        print("No findings. (Static review only — it does not replace tests.)")
        return
    for finding in findings:
        tag = finding.severity.upper()
        print(f"\n[{tag}] {finding.rule} {finding.location}")
        print(f"  {finding.message}")
        if finding.snippet:
            print(f"  code: {finding.snippet}")
        print(f"  fix:  {finding.fix}")
    print(
        f"\nSummary: {report.count(BLOCKING)} blocking, "
        f"{report.count(WARNING)} warning, {report.count(INFO)} info"
    )


def run_self_test() -> int:
    """A tiny sanity suite so the agent itself is testable.

    The fixtures are materialised in a temporary directory (with ``REPO_ROOT``
    pointed at it) because the blocking-call rule parses real files.
    """
    import tempfile

    global REPO_ROOT
    original_root = REPO_ROOT
    sample_diff = """diff --git a/supabase/migrations/001_voice_activity.sql b/supabase/migrations/001_voice_activity.sql
new file mode 100644
--- a/supabase/migrations/001_voice_activity.sql
+++ b/supabase/migrations/001_voice_activity.sql
@@ -0,0 +1,2 @@
+drop table public.voice_sessions;
+select 1;
diff --git a/supabase/migrations/009_additive.sql b/supabase/migrations/009_additive.sql
new file mode 100644
--- /dev/null
+++ b/supabase/migrations/009_additive.sql
@@ -0,0 +1,11 @@
+--   * NO drop, NO truncate, NO delete, NO blanket update
+alter table public.voice_active_sessions
+    add column if not exists credited_until timestamptz;
+create or replace function public.voice_close_active_session()
+returns integer language plpgsql as $$
+begin
+    delete from public.voice_active_sessions
+    where guild_id = p_guild_id and user_id = p_user_id;
+    return 1;
+end;
+$$;
diff --git a/supabase/migrations/010_destructive.sql b/supabase/migrations/010_destructive.sql
new file mode 100644
--- /dev/null
+++ b/supabase/migrations/010_destructive.sql
@@ -0,0 +1,6 @@
+create or replace function public.voice_purge()
+returns void language plpgsql as $$
+begin
+    delete from public.voice_daily_stats where duration_seconds < 60;
+end;
+$$;
diff --git a/functions/x.py b/functions/x.py
--- a/functions/x.py
+++ b/functions/x.py
@@ -1,2 +1,5 @@
 import time
+SUPABASE_SECRET_KEY = "sb_secret_abcdefghijklmnop"
+async def handler():
+    time.sleep(1)
+    return 1
"""
    fixtures = {
        "supabase/migrations/001_voice_activity.sql": (
            "drop table public.voice_sessions;\nselect 1;\n"
        ),
        # Purely additive: comments that merely mention destructive verbs, a
        # new column, and a keyed single-row delete of a liveness row inside
        # a function body. Must not hard-block.
        "supabase/migrations/009_additive.sql": (
            "--   * NO drop, NO truncate, NO delete, NO blanket update\n"
            "alter table public.voice_active_sessions\n"
            "    add column if not exists credited_until timestamptz;\n"
            "create or replace function public.voice_close_active_session()\n"
            "returns integer language plpgsql as $$\n"
            "begin\n"
            "    delete from public.voice_active_sessions\n"
            "    where guild_id = p_guild_id and user_id = p_user_id;\n"
            "    return 1;\n"
            "end;\n"
            "$$;\n"
        ),
        # Deletes from the history table. Must block, function body or not.
        "supabase/migrations/010_destructive.sql": (
            "create or replace function public.voice_purge()\n"
            "returns void language plpgsql as $$\n"
            "begin\n"
            "    delete from public.voice_daily_stats where duration_seconds < 60;\n"
            "end;\n"
            "$$;\n"
        ),
        "functions/x.py": (
            "import time\n"
            'SUPABASE_SECRET_KEY = "sb_secret_abcdefghijklmnop"\n'
            "async def handler():\n"
            "    time.sleep(1)\n"
            "    return 1\n"
        ),
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            REPO_ROOT = Path(tmp)
            for rel, content in fixtures.items():
                target = REPO_ROOT / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            files = parse_diff(sample_diff)
            report = review(ReviewReport(base="a", head="b"), files)
    finally:
        REPO_ROOT = original_root

    rules = {f.rule for f in report.findings}
    expected = {"SEC001", "DB001", "DB002", "ASY001"}
    missing = expected - rules
    if missing:
        print(f"self-test FAILED: missing rules {sorted(missing)}", file=sys.stderr)
        return 1

    # The narrowing must not have opened a hole: severity per fixture is
    # asserted explicitly, so a future relaxation has to be deliberate.
    by_path: dict[str, list[Finding]] = {}
    for finding in report.findings:
        by_path.setdefault(finding.file, []).append(finding)

    def sev(path: str, rule: str) -> set[str]:
        return {f.severity for f in by_path.get(path, []) if f.rule == rule}

    cases = [
        (
            "top-level drop table blocks",
            BLOCKING in sev("supabase/migrations/001_voice_activity.sql", "DB001"),
        ),
        (
            "migration with a destructive statement blocks",
            BLOCKING in sev("supabase/migrations/001_voice_activity.sql", "DB002"),
        ),
        (
            "a comment mentioning delete is not destructive",
            "supabase/migrations/009_additive.sql"
            not in {f.file for f in report.findings if f.rule == "DB001"}
            or BLOCKING not in sev("supabase/migrations/009_additive.sql", "DB001"),
        ),
        (
            "keyed liveness delete is a warning, not silent",
            WARNING in sev("supabase/migrations/009_additive.sql", "DB001"),
        ),
        (
            "purely additive migration downgrades to a warning",
            WARNING in sev("supabase/migrations/009_additive.sql", "DB002")
            and BLOCKING not in sev("supabase/migrations/009_additive.sql", "DB002"),
        ),
        (
            "deleting from the history table still blocks",
            BLOCKING in sev("supabase/migrations/010_destructive.sql", "DB001"),
        ),
    ]
    failures = [name for name, ok in cases if not ok]
    if failures:
        for name in failures:
            print(f"self-test FAILED: {name}", file=sys.stderr)
        return 1

    print(
        f"self-test OK ({len(report.findings)} findings, "
        f"rules {sorted(rules)}, {len(cases)} severity cases)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="base revision")
    parser.add_argument("--head", default="HEAD", help="head revision")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--worktree", action="store_true", help="review uncommitted changes"
    )
    parser.add_argument("--fail-on", choices=[BLOCKING, WARNING, "never"], default=BLOCKING)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if args.worktree:
        base, head = "HEAD (working tree)", ""
        try:
            files = worktree_files()
        except RuntimeError as err:
            print(f"review agent: {err}", file=sys.stderr)
            return 2
    else:
        base, head = args.base, args.head
        try:
            files = parse_diff(
                _git("diff", "--unified=0", "--no-color", f"{args.base}...{args.head}")
            )
        except RuntimeError as err:
            print(f"review agent: {err}", file=sys.stderr)
            return 2

    report = review(ReviewReport(base=base, head=head), files)

    if args.json:
        print(
            json.dumps(
                {
                    "base": report.base,
                    "head": report.head,
                    "files_changed": report.files_changed,
                    "counts": {
                        "blocking": report.count(BLOCKING),
                        "warning": report.count(WARNING),
                        "info": report.count(INFO),
                    },
                    "findings": [asdict(f) for f in report.sorted_findings()],
                },
                indent=2,
            )
        )
    else:
        print_report(report)

    if args.fail_on == "never":
        return 0
    if args.fail_on == WARNING:
        return 1 if report.count(BLOCKING) or report.count(WARNING) else 0
    return 1 if report.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
