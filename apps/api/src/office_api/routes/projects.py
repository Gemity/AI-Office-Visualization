from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from office_contracts import EventType
from pydantic import BaseModel, Field
from sqlalchemy import select

from office_api import idempotency
from office_api.auth import CurrentPrincipal, Membership, OwnerMembership, Session
from office_api.budget import ProjectBudget
from office_api.db import new_id
from office_api.errors import request_id
from office_api.events import append_event
from office_api.idempotency import WriteResponse
from office_api.models import AuditLog, Project, ProjectMember

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    budget: ProjectBudget = Field(default_factory=ProjectBudget)


class ProjectOut(BaseModel):
    id: str
    name: str
    status: str
    config: dict[str, Any]
    budget: dict[str, Any]
    created_by: str
    created_at: datetime
    role: str


def _out(project: Project, role: str) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        status=project.status,
        config=project.config,
        budget=project.budget,
        created_by=project.created_by,
        created_at=project.created_at,
        role=role,
    )


@router.post("", status_code=201, response_model=WriteResponse[ProjectOut])
async def create_project(
    body: ProjectCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    async def handler() -> ProjectOut:
        project = Project(
            id=new_id("prj"),
            name=body.name,
            config=body.config,
            budget=body.budget.model_dump(),
            created_by=principal.id,
        )
        session.add(project)
        await session.flush()  # no ORM relationships: parent rows are flushed explicitly
        session.add(ProjectMember(project_id=project.id, principal_id=principal.id, role="owner"))
        await session.flush()
        await append_event(
            session,
            project_id=project.id,
            type=EventType.PROJECT_CREATED,
            actor_id=principal.id,
            payload={"name": project.name},
        )
        session.add(
            AuditLog(
                id=new_id("aud"),
                project_id=project.id,
                principal=principal.id,
                action="project.create",
                resource=project.id,
                outcome="success",
                request_id=request_id(request),
            )
        )
        return _out(project, "owner")

    response, _ = await idempotency.write(
        request, session, principal.id, idempotency_key, body, handler
    )
    return response


@router.get("", response_model=list[ProjectOut])
async def list_projects(principal: CurrentPrincipal, session: Session):
    rows = await session.execute(
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.principal_id == principal.id)
        .order_by(Project.created_at)
    )
    return [_out(project, role) for project, role in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, member: Membership, session: Session):
    project = await session.get(Project, project_id)
    return _out(project, member.role)


@router.put("/{project_id}/budget", response_model=WriteResponse[ProjectOut])
async def set_budget(
    project_id: str,
    body: ProjectBudget,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Sets cost limits (null = unlimited). Exceeding the project limit pauses the
    project (circuit breaker); resuming needs a higher limit (ADR-0006)."""

    async def handler() -> ProjectOut:
        project = await session.get(Project, project_id)
        project.budget = body.model_dump()
        session.add(
            AuditLog(
                id=new_id("aud"),
                project_id=project_id,
                principal=member.principal_id,
                action="project.budget",
                resource=project_id,
                outcome="success",
                request_id=request_id(request),
            )
        )
        return _out(project, member.role)

    response, data = await idempotency.write(
        request, session, member.principal_id, idempotency_key, body, handler, status_code=200
    )
    if data is not None:
        request.app.state.runtime.wake()
    return response
