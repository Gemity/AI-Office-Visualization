import os
from dataclasses import dataclass, field
from pathlib import Path

# apps/api/src/office_api/config.py -> apps/web
_DEFAULT_WEB_DIR = Path(__file__).resolve().parents[3] / "web"


@dataclass(frozen=True)
class Settings:
    # SQLite for local dev; Postgres target: postgresql+asyncpg://user:pass@host/db
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "OFFICE_DATABASE_URL", "sqlite+aiosqlite:///./office.db"
        )
    )
    # Max events buffered per WebSocket subscriber before it is told to resync.
    stream_queue_size: int = 1000
    # Frontend served at "/" for local runs; empty string disables it.
    web_dir: str = field(
        default_factory=lambda: os.environ.get("OFFICE_WEB_DIR", str(_DEFAULT_WEB_DIR))
    )
    # Content store (content_ref) and per-task agent workspaces.
    data_dir: str = field(default_factory=lambda: os.environ.get("OFFICE_DATA_DIR", "./data"))

    # --- runtime ---
    max_parallel_runs: int = 5  # per project (NFR: >= 5 concurrent workers)
    run_timeout_s: float = 1800
    retry_backoff_s: float = 5.0  # delay before attempt n+1 = backoff * 2**(n-1)
    max_turn_attempts: int = 3  # orchestrator turns
    conversation_time_limit_s: float = 900  # whole meeting / dm
    # How long the Owner has to decide on (and the runtime to use) an approval;
    # a project can override it with config["approval_ttl_s"].
    approval_ttl_s: float = 3600
    # OS sandbox for adapters that run local CLIs: "bwrap" (default) or "none" (ADR-0006).
    sandbox: str = field(default_factory=lambda: os.environ.get("OFFICE_SANDBOX", "bwrap"))

    def __post_init__(self):
        if self.sandbox not in {"bwrap", "none"}:
            raise ValueError("OFFICE_SANDBOX must be 'bwrap' or 'none'")

    dispatch_interval_s: float = 1.0  # dispatcher also wakes immediately on changes
