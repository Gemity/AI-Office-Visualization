import os
from dataclasses import dataclass, field


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
