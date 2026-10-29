from datetime import datetime
from typing import Annotated, Any, Literal

from agent_adapters import ADAPTERS, config_fields, create_adapter
from fastapi import APIRouter, Header, Request
from office_contracts import Capability, CapabilityManifest, ConfigField, EventType
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from office_api import idempotency
from office_api.auth import CurrentPrincipal, Membership, OwnerMembership, Session
from office_api.db import new_id, utcnow
from office_api.errors import ApiError
from office_api.events import append_event
from office_api.idempotency import WriteResponse
from office_api.models import (
    AgentDefinition,
    AgentRun,
    OrchestratorAssignment,
    ProjectAgent,
)
from office_api.runtime import ACTIVE_RUN_STATES, active_orchestrator_id

router = APIRouter(tags=["agents"])


class AdapterInfo(BaseModel):
    adapter_type: str
    default_manifest: CapabilityManifest
    config_fields: list[ConfigField] = Field(
        description="Documented config keys; 'capabilities' present = capabilities are editable."
    )


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    adapter_type: str
    role: Literal["orchestrator", "worker"]
    config: dict[str, Any] = Field(
        default_factory=dict, description="Adapter config (non-secret), see adapter docs."
    )


class CurrentRun(BaseModel):
    run_id: str
    status: str
    task_id: str | None
    purpose: str
    provider_ref: str | None = Field(default=None, description="Provider job/session id.")
    progress: float | None = Field(default=None, description="As reported by the provider.")


class AgentOut(BaseModel):
    id: str
    name: str
    adapter_type: str
    provider: str
    role: str
    enabled: bool
    is_active_orchestrator: bool
    capabilities: list[str]
    config: dict[str, Any] = Field(description="Adapter config (non-secret).")
    current_runs: list[CurrentRun]
    created_at: datetime


@router.get("/adapters", response_model=list[AdapterInfo])
async def list_adapters(principal: CurrentPrincipal):
    return [
        AdapterInfo(
            adapter_type=t,
            default_manifest=cls().describe_capabilities(),
            config_fields=config_fields(t),
        )
        for t, cls in ADAPTERS.items()
    ]


@router.get("/capabilities", response_model=list[Capability])
async def list_capabilities(principal: CurrentPrincipal):
    """Every capability a worker can declare and a task can require."""
    return list(Capability)


@router.post(
    "/projects/{project_id}/agents",
    status_code=201,
    response_model=WriteResponse[AgentOut],
)
async def add_agent(
    project_id: str,
    body: AgentCreate,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Adds an agent to the project. Adding an orchestrator replaces the active one
    (new assignment version; BR-01)."""
    try:
        manifest = create_adapter(body.adapter_type, body.config).describe_capabilities()
    except (ValueError, TypeError) as exc:
        raise ApiError(422, "invalid_agent_config", str(exc)) from None

    async def handler() -> AgentOut:
        agent = AgentDefinition(
            id=new_id("agt"),
            name=body.name,
            provider=manifest.provider,
            adapter_type=body.adapter_type,
            capability_manifest=manifest.model_dump(mode="json"),
            config=body.config,
        )
        session.add(agent)
        await session.flush()
        session.add(ProjectAgent(project_id=project_id, agent_id=agent.id, role=body.role))
        await session.flush()
        payload: dict[str, Any] = {"agent_id": agent.id, "role": body.role, "name": agent.name}
        if body.role == "orchestrator":
            current = await session.scalar(
                select(OrchestratorAssignment).where(
                    OrchestratorAssignment.project_id == project_id,
                    OrchestratorAssignment.active_to.is_(None),
                )
            )
            if current is not None:
                current.active_to = utcnow()
                payload["replaced_agent_id"] = current.agent_id
                await session.flush()
            version = (
                await session.scalar(
                    select(func.max(OrchestratorAssignment.version)).where(
                        OrchestratorAssignment.project_id == project_id
                    )
                )
                or 0
            ) + 1
            session.add(
                OrchestratorAssignment(
                    id=new_id("orc"), project_id=project_id, agent_id=agent.id, version=version
                )
            )
            payload["version"] = version
        await append_event(
            session,
            project_id=project_id,
            type=EventType.AGENT_ASSIGNED,
            actor_id=member.principal_id,
            payload=payload,
        )
        return _out(agent, body.role, True, body.role == "orchestrator", [])

    response, _ = await idempotency.write(
        request, session, member.principal_id, idempotency_key, body, handler
    )
    return response


@router.get("/projects/{project_id}/agents", response_model=list[AgentOut])
async def list_agents(project_id: str, member: Membership, session: Session):
    orchestrator_id = await active_orchestrator_id(session, project_id)
    rows = (
        await session.execute(
            select(AgentDefinition, ProjectAgent)
            .join(ProjectAgent, ProjectAgent.agent_id == AgentDefinition.id)
            .where(ProjectAgent.project_id == project_id)
            .order_by(ProjectAgent.created_at)
        )
    ).all()
    runs = (
        await session.scalars(
            select(AgentRun).where(
                AgentRun.project_id == project_id, AgentRun.status.in_(ACTIVE_RUN_STATES)
            )
        )
    ).all()
    out = []
    for agent, assignment in rows:
        current = [
            CurrentRun(
                run_id=r.id,
                status=r.status,
                task_id=r.task_id,
                purpose=r.purpose,
                provider_ref=r.provider_ref,
                progress=r.progress,
            )
            for r in runs
            if r.agent_id == agent.id
        ]
        out.append(
            _out(agent, assignment.role, assignment.enabled, agent.id == orchestrator_id, current)
        )
    return out


def _out(
    agent: AgentDefinition,
    role: str,
    enabled: bool,
    is_active_orchestrator: bool,
    current_runs: list[CurrentRun],
) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        adapter_type=agent.adapter_type,
        provider=agent.provider,
        role=role,
        enabled=enabled,
        is_active_orchestrator=is_active_orchestrator,
        capabilities=agent.capability_manifest.get("capabilities", []),
        config=agent.config,
        current_runs=current_runs,
        created_at=agent.created_at,
    )


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = Field(
        default=None, description="Replaces the whole adapter config (non-secret)."
    )


class _NoBody(BaseModel):
    pass


async def _editable_worker(
    session, project_id: str, agent_id: str
) -> tuple[AgentDefinition, ProjectAgent]:
    """Workers only, and only while idle: a running run keeps using the adapter
    instance (and config) it started with, and cancel must reach that instance."""
    assignment = await session.get(ProjectAgent, (project_id, agent_id))
    if assignment is None or not assignment.enabled:
        raise ApiError(404, "agent_not_found", "Agent not found")
    if assignment.role != "worker":
        raise ApiError(
            409, "orchestrator_managed", "The Orchestrator is configured by office-launch.py"
        )
    busy = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.project_id == project_id,
            AgentRun.agent_id == agent_id,
            AgentRun.status.in_(ACTIVE_RUN_STATES),
        )
    )
    if busy:
        raise ApiError(409, "agent_busy", "Agent has a run in progress; retry when it is idle")
    return await session.get(AgentDefinition, agent_id), assignment


@router.patch("/projects/{project_id}/agents/{agent_id}", response_model=WriteResponse[AgentOut])
async def update_agent(
    project_id: str,
    agent_id: str,
    body: AgentUpdate,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Renames a worker and/or replaces its adapter config (applies to its next runs)."""

    async def handler() -> AgentOut:
        agent, assignment = await _editable_worker(session, project_id, agent_id)
        changed = []
        if body.name is not None and body.name != agent.name:
            agent.name = body.name
            changed.append("name")
        if body.config is not None and body.config != agent.config:
            try:
                manifest = create_adapter(agent.adapter_type, body.config).describe_capabilities()
            except (ValueError, TypeError) as exc:
                raise ApiError(422, "invalid_agent_config", str(exc)) from None
            agent.config = body.config
            agent.capability_manifest = manifest.model_dump(mode="json")
            changed.append("config")
        if changed:
            await append_event(
                session,
                project_id=project_id,
                type=EventType.AGENT_UPDATED,
                actor_id=member.principal_id,
                payload={"agent_id": agent.id, "name": agent.name, "changed": changed},
            )
        return _out(agent, assignment.role, True, False, [])

    response, data = await idempotency.write(
        request, session, member.principal_id, idempotency_key, body, handler, status_code=200
    )
    if data is not None:
        request.app.state.runtime.forget_adapter(agent_id)
        request.app.state.runtime.wake()
    return response


@router.delete("/projects/{project_id}/agents/{agent_id}", response_model=WriteResponse[AgentOut])
async def remove_agent(
    project_id: str,
    agent_id: str,
    request: Request,
    member: OwnerMembership,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    """Removes a worker from the project. Its history (runs, messages, events) stays,
    so it is listed with ``enabled=false``; it gets no new work, and pending
    meetings/DMs that need it are blocked (``participant_unavailable``)."""

    async def handler() -> AgentOut:
        agent, assignment = await _editable_worker(session, project_id, agent_id)
        assignment.enabled = False
        await append_event(
            session,
            project_id=project_id,
            type=EventType.AGENT_REMOVED,
            actor_id=member.principal_id,
            payload={"agent_id": agent.id, "name": agent.name},
        )
        return _out(agent, assignment.role, False, False, [])

    response, data = await idempotency.write(
        request, session, member.principal_id, idempotency_key, _NoBody(), handler, status_code=200
    )
    if data is not None:
        request.app.state.runtime.forget_adapter(agent_id)
        request.app.state.runtime.wake()
    return response
