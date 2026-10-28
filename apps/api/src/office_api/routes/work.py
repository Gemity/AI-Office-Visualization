"""Read models for the task board, conversations and artifacts (spec §11-12)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from office_api.auth import Membership, Session
from office_api.errors import ApiError
from office_api.models import (
    AgentRun,
    Artifact,
    Conversation,
    ConversationParticipant,
    Message,
    Task,
    TaskDependency,
)
from office_api.storage import preview

router = APIRouter(prefix="/projects/{project_id}", tags=["work"])


class RunOut(BaseModel):
    id: str
    agent_id: str
    status: str
    attempt: int
    error: dict | None
    cost: float | None
    started_at: datetime | None
    finished_at: datetime | None


class TaskOut(BaseModel):
    id: str
    kind: str
    parent_id: str | None
    title: str
    description: str
    status: str
    blocked_reason: str | None
    required_capabilities: list[str]
    assignee_id: str | None
    depends_on: list[str]
    attempts: int
    retry_at: datetime | None
    result_preview: str | None
    runs: list[RunOut]
    created_at: datetime
    updated_at: datetime


class ConversationOut(BaseModel):
    id: str
    type: str
    visibility: str
    status: str
    task_id: str | None
    participants: list[str]
    created_at: datetime


class MessageOut(BaseModel):
    id: str
    sender_id: str
    content: str | None
    parent_id: str | None
    created_at: datetime


class ArtifactOut(BaseModel):
    id: str
    task_id: str | None
    run_id: str | None
    type: str
    uri: str
    checksum: str | None
    version: int
    created_at: datetime


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    project_id: str,
    request: Request,
    member: Membership,
    session: Session,
    status: Annotated[str | None, Query()] = None,
):
    stmt = select(Task).where(Task.project_id == project_id).order_by(Task.created_at)
    if status:
        stmt = stmt.where(Task.status == status)
    tasks = (await session.scalars(stmt)).all()
    deps = (
        await session.execute(
            select(TaskDependency.task_id, TaskDependency.depends_on_id).where(
                TaskDependency.project_id == project_id
            )
        )
    ).all()
    runs = (
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.project_id == project_id, AgentRun.task_id.is_not(None))
            .order_by(AgentRun.created_at)
        )
    ).all()
    store = request.app.state.runtime.store
    return [
        TaskOut(
            id=t.id,
            kind=t.kind,
            parent_id=t.parent_id,
            title=t.title,
            description=t.description,
            status=t.status,
            blocked_reason=t.blocked_reason,
            required_capabilities=t.required_capabilities,
            assignee_id=t.assignee_id,
            depends_on=[d for task_id, d in deps if task_id == t.id],
            attempts=t.attempts,
            retry_at=t.retry_at,
            result_preview=preview(store.get_text(t.result_ref), 500) if t.result_ref else None,
            runs=[
                RunOut(
                    id=r.id,
                    agent_id=r.agent_id,
                    status=r.status,
                    attempt=r.attempt,
                    error=r.error,
                    cost=r.cost,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                )
                for r in runs
                if r.task_id == t.id
            ],
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tasks
    ]


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(project_id: str, member: Membership, session: Session):
    convs = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.project_id == project_id)
            .order_by(Conversation.created_at)
        )
    ).all()
    parts = (
        await session.execute(
            select(ConversationParticipant.conversation_id, ConversationParticipant.agent_id).where(
                ConversationParticipant.conversation_id.in_([c.id for c in convs])
            )
        )
    ).all()
    return [
        ConversationOut(
            id=c.id,
            type=c.type,
            visibility=c.visibility,
            status=c.status,
            task_id=c.task_id,
            participants=[a for cid, a in parts if cid == c.id],
            created_at=c.created_at,
        )
        for c in convs
    ]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    project_id: str,
    conversation_id: str,
    request: Request,
    member: Membership,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.project_id != project_id:
        raise ApiError(404, "conversation_not_found", "Conversation not found")
    msgs = (
        await session.scalars(
            select(Message)
            .where(Message.project_id == project_id, Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
            .limit(limit)
        )
    ).all()
    store = request.app.state.runtime.store
    return [
        MessageOut(
            id=m.id,
            sender_id=m.sender_id,
            content=store.get_text(m.content_ref),
            parent_id=m.parent_id,
            created_at=m.created_at,
        )
        for m in msgs
    ]


@router.get("/artifacts", response_model=list[ArtifactOut])
async def list_artifacts(project_id: str, member: Membership, session: Session):
    rows = await session.scalars(
        select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created_at)
    )
    return [
        ArtifactOut(
            id=a.id,
            task_id=a.task_id,
            run_id=a.run_id,
            type=a.type,
            uri=a.uri,
            checksum=a.checksum,
            version=a.version,
            created_at=a.created_at,
        )
        for a in rows
    ]
