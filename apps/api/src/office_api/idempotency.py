"""Idempotency keys for write endpoints (spec §11).

Same principal + key + identical request → stored response is replayed.
Same key with a different request → 409.
"""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.errors import ApiError, request_id
from office_api.events import commit_and_publish
from office_api.models import IdempotencyKey

T = TypeVar("T")


def request_hash(request: Request, body: BaseModel) -> str:
    raw = f"{request.method} {request.url.path}\n{body.model_dump_json()}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def replay(
    session: AsyncSession, principal_id: str, key: str, req_hash: str
) -> JSONResponse | None:
    stored = await session.get(IdempotencyKey, (principal_id, key))
    if stored is None:
        return None
    if stored.request_hash != req_hash:
        raise ApiError(
            409, "idempotency_key_reused", "Idempotency key used for a different request"
        )
    return JSONResponse(
        stored.response_body,
        status_code=stored.response_status,
        headers={"Idempotent-Replayed": "true"},
    )


def remember(
    session: AsyncSession,
    principal_id: str,
    key: str,
    req_hash: str,
    status: int,
    body: dict[str, Any],
) -> None:
    session.add(
        IdempotencyKey(
            principal_id=principal_id,
            key=key,
            request_hash=req_hash,
            response_status=status,
            response_body=body,
        )
    )


class WriteResponse(BaseModel, Generic[T]):  # noqa: UP046 (keeps OpenAPI names stable)
    """Envelope for write endpoints (spec §11)."""

    request_id: str
    status: str  # completed | accepted
    data: T


async def write(
    request: Request,
    session: AsyncSession,
    principal_id: str,
    key: str,
    body: BaseModel,
    handler: Callable[[], Awaitable[BaseModel]],
    *,
    status_code: int = 201,
    op_status: str = "completed",
) -> tuple[JSONResponse, BaseModel | None]:
    """Runs ``handler`` (which stages DB changes and events) exactly once per key,
    commits, publishes events, and returns ``(response, data)``. ``data`` is None
    when a stored response was replayed, so callers skip post-commit side effects."""
    req_hash = request_hash(request, body)
    if (replayed := await replay(session, principal_id, key, req_hash)) is not None:
        return replayed, None
    data = await handler()
    payload = {
        "request_id": request_id(request),
        "status": op_status,
        "data": data.model_dump(mode="json"),
    }
    remember(session, principal_id, key, req_hash, status_code, payload)
    try:
        await commit_and_publish(session, request.app.state.broker)
    except IntegrityError:
        # A concurrent request with the same key won the race.
        await session.rollback()
        raise ApiError(
            409, "idempotency_conflict", "Request with this key is in progress"
        ) from None
    return JSONResponse(payload, status_code=status_code), data
