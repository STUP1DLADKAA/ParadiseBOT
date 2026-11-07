"""Build Mode configuration for ParadiseBOT."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _frameworks(value: object) -> tuple[str, ...]:
    if not value:
        return ("html", "react", "vanilla")
    parts = [p.strip().lower() for p in str(value).split(",") if p.strip()]
    return tuple(parts) if parts else ("html", "react", "vanilla")


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Configuration for AI Build Mode & Live Preview infrastructure."""

    # Master toggle for Build Mode
    enabled: bool = True

    # Storage directory for user projects and metadata
    projects_dir: str = "data/builds"
    db_path: str = "data/builds.db"

    # Preview server configuration
    preview_host: str = "0.0.0.0"
    preview_port: int = 8080
    # Public URL root sent to Discord users for preview links.
    # Defaults to preview_domain or auto-detected e2b preview URL or local fallback.
    preview_base_url: str = ""
    # Optional custom domain for previews (e.g. "https://preview.example.com")
    preview_domain: str = ""
    # Preview provider: "internal" (aiohttp server), "vercel", "cloudflare", etc.
    preview_provider: str = "internal"

    # Concurrency and rate limits
    max_concurrent_builds: int = 3
    max_user_concurrent_builds: int = 1
    build_queue_max_size: int = 20
    build_timeout: float = 120.0  # seconds

    # Preview lifecycle & limits
    preview_expiration_seconds: int = 7200  # 2 hours
    preview_cleanup_interval: int = 300  # 5 minutes
    max_projects_per_user: int = 10
    max_file_size_bytes: int = 1024 * 1024  # 1 MB per file
    max_total_project_bytes: int = 10 * 1024 * 1024  # 10 MB per project

    # Allowed frameworks
    allowed_frameworks: tuple[str, ...] = ("html", "react", "vanilla", "nextjs")

    # External deployment provider credentials (optional)
    vercel_token: str = ""
    vercel_team_id: str = ""
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""

    @property
    def configured(self) -> bool:
        return self.enabled

    def get_preview_url(self, project_id: str) -> str:
        """Resolve the publicly accessible preview URL for a project ID."""
        if self.preview_base_url:
            base = self.preview_base_url.rstrip("/")
            return f"{base}/preview/{project_id}/"
        if self.preview_domain:
            domain = self.preview_domain.rstrip("/")
            return f"{domain}/preview/{project_id}/"
        # Auto-detect E2B environment sandbox URL if available
        sandbox_id = os.environ.get("E2B_SANDBOX_ID", "").strip()
        if sandbox_id:
            return f"https://{self.preview_port}-{sandbox_id}.e2b.app/preview/{project_id}/"
        # Local fallback
        return f"http://localhost:{self.preview_port}/preview/{project_id}/"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BuildConfig":
        if env is None:
            env = os.environ
        return cls(
            enabled=_bool(env.get("BUILD_MODE_ENABLED"), True),
            projects_dir=env.get("BUILD_PROJECTS_DIR", "data/builds").strip() or "data/builds",
            db_path=env.get("BUILD_DB_PATH", "data/builds.db").strip() or "data/builds.db",
            preview_host=env.get("BUILD_PREVIEW_HOST", "0.0.0.0").strip() or "0.0.0.0",
            preview_port=max(1024, min(_int(env.get("BUILD_PREVIEW_PORT"), 8080), 65535)),
            preview_base_url=env.get("BUILD_PREVIEW_BASE_URL", "").strip(),
            preview_domain=env.get("BUILD_PREVIEW_DOMAIN", "").strip(),
            preview_provider=env.get("BUILD_PREVIEW_PROVIDER", "internal").strip().lower() or "internal",
            max_concurrent_builds=max(1, min(_int(env.get("BUILD_MAX_CONCURRENT"), 3), 10)),
            max_user_concurrent_builds=max(1, min(_int(env.get("BUILD_MAX_USER_CONCURRENT"), 1), 5)),
            build_queue_max_size=max(5, min(_int(env.get("BUILD_QUEUE_MAX_SIZE"), 20), 100)),
            build_timeout=max(30.0, min(_float(env.get("BUILD_TIMEOUT"), 120.0), 300.0)),
            preview_expiration_seconds=max(300, _int(env.get("BUILD_PREVIEW_EXPIRATION"), 7200)),
            preview_cleanup_interval=max(30, _int(env.get("BUILD_CLEANUP_INTERVAL"), 300)),
            max_projects_per_user=max(1, min(_int(env.get("BUILD_MAX_PROJECTS_PER_USER"), 10), 50)),
            max_file_size_bytes=max(1024, _int(env.get("BUILD_MAX_FILE_BYTES"), 1024 * 1024)),
            max_total_project_bytes=max(10240, _int(env.get("BUILD_MAX_PROJECT_BYTES"), 10 * 1024 * 1024)),
            allowed_frameworks=_frameworks(env.get("BUILD_ALLOWED_FRAMEWORKS")),
            vercel_token=env.get("VERCEL_TOKEN", "").strip(),
            vercel_team_id=env.get("VERCEL_TEAM_ID", "").strip(),
            cloudflare_api_token=env.get("CLOUDFLARE_API_TOKEN", "").strip(),
            cloudflare_account_id=env.get("CLOUDFLARE_ACCOUNT_ID", "").strip(),
        )
