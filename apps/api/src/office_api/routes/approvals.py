from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from office_api import idempotency
from office_api.approvals import ApprovalError
from office_api.auth import Membership, OwnerMembership, Session
from office_api.errors import ApiError
from office_api.idempotency import WriteResponse
from office_api.models import Approval

router = APIRouter(prefix="/projects/{project_id}/approvals", tags=["approvals"])


class DecisionCreate(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=5000)


class ApprovalOut(BaseModel):
    id: str
    task_id: str | None
    actions: list[str]
    agent_id: str | None = Field(description="Worker the approval is scoped to.")
    summary: str | None
    status: str = Field(description="pending | approved | rejected | expired | cancelled")
    requester_id: str
    approver_id: str | None
    comment: str | None
    run_id: str | None = Field(description="Run that used this (single-use) approval.")
    expires_at: datetime
    resolved_at: datetime | None
    created_at: datetime


def _out(a: Approval) -> ApprovalOut:
    return ApprovalOut(
        id=a.id,
        task_id=a.task_id,
        actions=a.action.split(","),
        agent_id=a.scope.get("agent_id"),
        summary=a.scope.get("summary"),
        status=a.status,
        requester_id=a.requester_id,
        approver_id=a.approver_id,
        comment=a.comment,
        run_id=a.run_id,
        expires_at=a.expires_at,
        resolved_at=a.resolved_at,
        created_at=a.created_at,
    )


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(project_id: str, member: Membership, session: Session):
    rows = await session.scalars(
        select(Approval).where(Approval.project_id == project_id).order_by(Approval.created_at)
    )
    return [_out(a) for a in rows]


@router.post("/{approval_id}/decision", response_model=WriteResponse[ApprovalOut])
async def decide(
    project_id: str,
    approval_id: str,
    body: DecisionCreate,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Approve or reject a pending approval (BR-07). Only project owners decide;
    an expired or already decided approval cannot be changed (409)."""
    runtime = request.app.state.runtime
    turns: list[str] = []

    async def handler() -> ApprovalOut:
        try:
            approval, staged = await runtime.decide_approval(
                session,
                project_id=project_id,
                approval_id=approval_id,
                decision=body.decision,
                approver_id=member.principal_id,
                comment=body.comment,
            )
        except ApprovalError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None
        turns.extend(staged)
        return _out(approval)

    response, data = await idempotency.write(
        request, session, member.principal_id, idempotency_key, body, handler, status_code=200
    )
    if data is not None:
        runtime._after(turns)
    return response
