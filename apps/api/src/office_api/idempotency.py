"""Idempotency keys for write endpoints (spec §11).

Same principal + key + identical request → stored response is replayed.
Same key with a different request → 409.
"""

import hashlib
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.errors import ApiError
from office_api.models import IdempotencyKey


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
