"""Project session and persistent storage management for AI Build Mode."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("paradise.build.session")


@dataclass
class ProjectSession:
    """Represents a generated user project."""

    project_id: str
    owner_id: int
    channel_id: int
    title: str
    description: str
    framework: str  # "html", "react", etc.
    preview_url: str
    created_at: float
    updated_at: float
    build_count: int = 1
    deployment_status: str = "ready"  # "ready", "building", "failed", "expired"
    files: dict[str, str] = field(default_factory=dict)
    build_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return self.deployment_status == "expired"


class ProjectStore:
    """Thread-safe SQLite + filesystem project manager."""

    def __init__(self, db_path: str, projects_dir: str) -> None:
        self.db_path = db_path
        self.projects_dir = Path(projects_dir).resolve()
        self._init_storage()

    def _init_storage(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        os.makedirs(self.projects_dir, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    framework TEXT NOT NULL,
                    preview_url TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    build_count INTEGER NOT NULL DEFAULT 1,
                    deployment_status TEXT NOT NULL DEFAULT 'ready',
                    build_history TEXT NOT NULL DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_owner_updated 
                ON projects(owner_id, updated_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel_updated 
                ON projects(channel_id, updated_at DESC)
            """)
            conn.commit()

    def _sanitize_filename(self, filename: str) -> str:
        """Prevent path traversal and sanitize relative paths."""
        # Normalize slashes and strip leading/trailing slashes
        clean = filename.replace("\\", "/").strip("/ ")
        # Ensure parts don't contain ".."
        parts = [p for p in clean.split("/") if p and p != "." and p != ".."]
        if not parts:
            return "index.html"
        return "/".join(parts)

    def _project_dir(self, project_id: str) -> Path:
        """Resolve and isolate project directory."""
        safe_id = "".join(c for c in project_id if c.isalnum() or c in ("-", "_"))
        target = (self.projects_dir / safe_id).resolve()
        # Security: ensure directory is strictly inside projects_dir
        if not str(target).startswith(str(self.projects_dir)):
            raise ValueError(f"Path traversal detected in project_id {project_id}")
        return target

    def save_project(self, session: ProjectSession) -> None:
        """Persist project metadata to SQLite and project files to disk."""
        target_dir = self._project_dir(session.project_id)
        os.makedirs(target_dir, exist_ok=True)

        # Write files securely
        for rel_path, content in session.files.items():
            safe_rel = self._sanitize_filename(rel_path)
            file_path = (target_dir / safe_rel).resolve()
            if not str(file_path).startswith(str(target_dir)):
                log.warning("Skipping unsafe file path traversal: %s", rel_path)
                continue
            os.makedirs(file_path.parent, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

        # Write project metadata json inside directory as well for backup
        meta_file = target_dir / "_meta.json"
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump({
                    "project_id": session.project_id,
                    "owner_id": session.owner_id,
                    "title": session.title,
                    "framework": session.framework,
                    "updated_at": session.updated_at,
                }, f)
        except Exception:
            pass

        # Update SQLite database
        history_json = json.dumps(session.build_history)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO projects (
                    project_id, owner_id, channel_id, title, description,
                    framework, preview_url, created_at, updated_at,
                    build_count, deployment_status, build_history
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    framework = excluded.framework,
                    preview_url = excluded.preview_url,
                    updated_at = excluded.updated_at,
                    build_count = excluded.build_count,
                    deployment_status = excluded.deployment_status,
                    build_history = excluded.build_history
            """, (
                session.project_id,
                session.owner_id,
                session.channel_id,
                session.title,
                session.description,
                session.framework,
                session.preview_url,
                session.created_at,
                session.updated_at,
                session.build_count,
                session.deployment_status,
                history_json,
            ))
            conn.commit()

    def get_project(self, project_id: str) -> ProjectSession | None:
        """Load a project session from SQLite and disk."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT project_id, owner_id, channel_id, title, description,
                       framework, preview_url, created_at, updated_at,
                       build_count, deployment_status, build_history
                FROM projects WHERE project_id = ?
            """, (project_id,))
            row = cur.fetchone()

        if not row:
            return None

        # Load files from disk
        files: dict[str, str] = {}
        target_dir = self._project_dir(project_id)
        if target_dir.exists() and target_dir.is_dir():
            for root, _, filenames in os.walk(target_dir):
                for fname in filenames:
                    if fname.startswith("_meta."):
                        continue
                    full_p = Path(root) / fname
                    rel_p = str(full_p.relative_to(target_dir)).replace("\\", "/")
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="replace") as f:
                            files[rel_p] = f.read()
                    except Exception:
                        pass

        try:
            history = json.loads(row[11])
        except Exception:
            history = []

        return ProjectSession(
            project_id=row[0],
            owner_id=row[1],
            channel_id=row[2],
            title=row[3],
            description=row[4],
            framework=row[5],
            preview_url=row[6],
            created_at=row[7],
            updated_at=row[8],
            build_count=row[9],
            deployment_status=row[10],
            files=files,
            build_history=history,
        )

    def get_active_user_project(self, owner_id: int, channel_id: int | None = None) -> ProjectSession | None:
        """Find the user's most recently modified active project (preferably in this channel)."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            if channel_id:
                cur.execute("""
                    SELECT project_id FROM projects
                    WHERE owner_id = ? AND channel_id = ? AND deployment_status != 'expired'
                    ORDER BY updated_at DESC LIMIT 1
                """, (owner_id, channel_id))
                row = cur.fetchone()
                if row:
                    return self.get_project(row[0])

            # Fallback to any recent project by this owner
            cur.execute("""
                SELECT project_id FROM projects
                WHERE owner_id = ? AND deployment_status != 'expired'
                ORDER BY updated_at DESC LIMIT 1
            """, (owner_id,))
            row = cur.fetchone()
            if row:
                return self.get_project(row[0])

        return None

    def list_user_projects(self, owner_id: int, limit: int = 10) -> list[ProjectSession]:
        """List active projects owned by a user."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT project_id FROM projects
                WHERE owner_id = ?
                ORDER BY updated_at DESC LIMIT ?
            """, (owner_id, limit))
            rows = cur.fetchall()

        projects = []
        for r in rows:
            p = self.get_project(r[0])
            if p:
                projects.append(p)
        return projects

    def mark_expired(self, project_id: str) -> None:
        """Mark a project as expired."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE projects SET deployment_status = 'expired'
                WHERE project_id = ?
            """, (project_id,))
            conn.commit()

    def cleanup_expired(self, max_age_seconds: int) -> int:
        """Clean up projects that have expired past max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        cleaned = 0
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT project_id FROM projects
                WHERE updated_at < ? AND deployment_status != 'expired'
            """, (cutoff,))
            rows = cur.fetchall()
            for (pid,) in rows:
                self.mark_expired(pid)
                cleaned += 1

        return cleaned
