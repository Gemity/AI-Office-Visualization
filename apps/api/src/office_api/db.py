import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator
from starlette.requests import HTTPConnection


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    gen = getattr(uuid, "uuid7", uuid.uuid4)  # time-ordered on Python 3.14+
    return f"{prefix}_{gen().hex}"


class UTCDateTime(TypeDecorator):
    """Stores aware datetimes as UTC; SQLite returns naive values, so re-attach UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: JSON().with_variant(JSONB(), "postgresql"),
        list[str]: JSON().with_variant(JSONB(), "postgresql"),
    }


def create_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_fk(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: rows are serialized after commit (responses, events).
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(conn: HTTPConnection) -> AsyncIterator[AsyncSession]:
    async with conn.app.state.sessionmaker() as session:
        yield session
