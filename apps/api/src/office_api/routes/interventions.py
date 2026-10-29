from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from office_api import idempotency
from office_api.auth import Membership, Session
from office_api.errors import ApiError
from office_api.idempotency import WriteResponse
from office_api.interventions import InterventionError
from office_api.models import Intervention, ProjectMember

router = APIRouter(prefix="/projects/{project_id}/interventions", tags=["interventions"])


class InterventionCreate(BaseModel):
    action: Literal["pause", "resume", "cancel", "redirect"]
    task_id: str | None = Field(
        default=None, description="Target task; null = whole project (pause/resume only)."
    )
    instructions: str | None = Field(default=None, max_length=20_000)


class InterventionOut(BaseModel):
    id: str
    task_id: str | None
    action: str
    instructions: str | None
    status: str = Field(description="requested | applied | rejected")
    outcome: str | None
    requested_by: str
    created_at: datetime
    resolved_at: datetime | None


def _out(iv: Intervention) -> InterventionOut:
    return InterventionOut(
        id=iv.id,
        task_id=iv.task_id,
        action=iv.action,
        instructions=iv.instructions,
        status=iv.status,
        outcome=iv.outcome,
        requested_by=iv.requested_by,
        created_at=iv.created_at,
        resolved_at=iv.resolved_at,
    )


async def may_intervene(member: Membership, request: Request) -> ProjectMember:
    """Rejects roles with no intervention right at all before the body is read."""
    policy = request.app.state.runtime.settings.policy
    if member.role != "owner" and not policy.interventions.observer_actions:
        raise ApiError(403, "forbidden", "Owner role required")
    return member


@router.post("", status_code=202, response_model=WriteResponse[InterventionOut])
async def create_intervention(
    project_id: str,
    body: InterventionCreate,
    request: Request,
    member: Annotated[ProjectMember, Depends(may_intervene)],
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Pause/resume/cancel/redirect a task or the whole project (§8.3).

    ``status`` stays ``requested`` until the runtime observes the effect; watch
    for ``intervention.applied`` (BR-06, AC-06). Owners may always intervene;
    observers only for the actions in ``interventions.observer_actions``."""
    runtime = request.app.state.runtime
    if not runtime.settings.policy.can_intervene(member.role, body.action):
        raise ApiError(403, "forbidden", f"Role {member.role!r} may not {body.action}")
    staged: list = []

    async def handler() -> InterventionOut:
        try:
            iv, turns = await runtime.request_intervention(
                session,
                project_id=project_id,
                action=body.action,
                task_id=body.task_id,
                instructions=body.instructions,
                requested_by=member.principal_id,
            )
        except InterventionError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None
        staged.extend([iv, turns])
        return _out(iv)

    response, data = await idempotency.write(
        request,
        session,
        member.principal_id,
        idempotency_key,
        body,
        handler,
        status_code=202,
        op_status="accepted",
    )
    if data is not None:
        await runtime.after_intervention_commit(*staged)
    return response


@router.get("", response_model=list[InterventionOut])
async def list_interventions(project_id: str, member: Membership, session: Session):
    rows = await session.scalars(
        select(Intervention)
        .where(Intervention.project_id == project_id)
        .order_by(Intervention.created_at)
    )
    return [_out(iv) for iv in rows]
