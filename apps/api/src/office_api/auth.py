"""Project-scoped auth.

Bearer token → principal → ``project_members`` row. A principal that is not a
member of a project gets 404 (not 403) so project existence is not leaked
(BR-04, AC-01). All project routes must depend on ``get_membership``.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.db import get_session, new_id
from office_api.errors import ApiError
from office_api.models import ApiToken, Principal, ProjectMember

TOKEN_PREFIX = "po_"

Session = Annotated[AsyncSession, Depends(get_session)]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_principal_with_token(
    session: AsyncSession, display_name: str
) -> tuple[Principal, str]:
    principal = Principal(id=new_id("usr"), display_name=display_name)
    token = await issue_token(session, principal)
    return principal, token


async def issue_token(session: AsyncSession, principal: Principal) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    session.add(principal)
    await session.flush()  # no ORM relationship, so insert the principal first
    session.add(ApiToken(id=new_id("tok"), principal_id=principal.id, token_hash=hash_token(token)))
    await session.commit()
    return token


async def authenticate(session: AsyncSession, token: str | None) -> Principal | None:
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    stmt = (
        select(Principal)
        .join(ApiToken, ApiToken.principal_id == Principal.id)
        .where(ApiToken.token_hash == hash_token(token), ApiToken.revoked_at.is_(None))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_membership(
    session: AsyncSession, project_id: str, principal_id: str
) -> ProjectMember | None:
    return await session.get(ProjectMember, (project_id, principal_id))


async def get_principal(
    session: Session, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    principal = await authenticate(session, token) if scheme.lower() == "bearer" else None
    if principal is None:
        raise ApiError(401, "unauthenticated", "Missing or invalid bearer token")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_membership(
    project_id: str, principal: CurrentPrincipal, session: Session
) -> ProjectMember:
    member = await find_membership(session, project_id, principal.id)
    if member is None:
        raise ApiError(404, "project_not_found", "Project not found")
    return member


Membership = Annotated[ProjectMember, Depends(get_membership)]
