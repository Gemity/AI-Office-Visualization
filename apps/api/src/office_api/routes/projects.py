from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from office_contracts import EventType
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from office_api import idempotency
from office_api.auth import CurrentPrincipal, Membership, Session
from office_api.db import new_id
from office_api.errors import ApiError, request_id
from office_api.events import append_event, commit_and_publish
from office_api.models import AuditLog, Project, ProjectMember

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)


class ProjectOut(BaseModel):
    id: str
    name: str
    status: str
    config: dict[str, Any]
    budget: dict[str, Any]
    created_by: str
    created_at: datetime
    role: str


class ProjectWriteResponse(BaseModel):
    request_id: str
    status: str = "completed"
    data: ProjectOut


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


@router.post("", status_code=201, response_model=ProjectWriteResponse)
async def create_project(
    body: ProjectCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    req_hash = idempotency.request_hash(request, body)
    if replayed := await idempotency.replay(session, principal.id, idempotency_key, req_hash):
        return replayed

    project = Project(
        id=new_id("prj"),
        name=body.name,
        config=body.config,
        budget=body.budget,
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
    rid = request_id(request)
    session.add(
        AuditLog(
            id=new_id("aud"),
            project_id=project.id,
            principal=principal.id,
            action="project.create",
            resource=project.id,
            outcome="success",
            request_id=rid,
        )
    )
    response = ProjectWriteResponse(request_id=rid, data=_out(project, "owner"))
    payload = response.model_dump(mode="json")
    idempotency.remember(session, principal.id, idempotency_key, req_hash, 201, payload)
    try:
        await commit_and_publish(session, request.app.state.broker)
    except IntegrityError:
        # A concurrent request with the same key won the race.
        await session.rollback()
        raise ApiError(
            409, "idempotency_conflict", "Request with this key is in progress"
        ) from None
    return JSONResponse(payload, status_code=201)


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
