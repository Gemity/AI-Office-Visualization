"""Database schema (spec §9).

Rules:
- Every project-scoped table has ``project_id`` and a composite index on
  ``(project_id, created_at)`` and/or ``(project_id, status)``.
- References between project-scoped rows use composite foreign keys
  ``(project_id, <ref>_id) -> (project_id, id)`` so a row can never point at
  data in another project (BR-04).
- Large/sensitive content lives in object storage, referenced by ``content_ref``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from office_api.db import Base, utcnow

ID = String(64)


def _pk() -> Mapped[str]:
    return mapped_column(ID, primary_key=True)


def _project_fk() -> Mapped[str]:
    return mapped_column(ID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)


def _created_at() -> Mapped[datetime]:
    return mapped_column(default=utcnow, nullable=False)


# --- Principals & auth (project-scoped RBAC) ---------------------------------


class Principal(Base):
    """A human user (Owner/observer). Agents are agent_definitions, not principals."""

    __tablename__ = "principals"

    id: Mapped[str] = _pk()
    display_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = _pk()
    principal_id: Mapped[str] = mapped_column(
        ID, ForeignKey("principals.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None]


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    created_at: Mapped[datetime] = _created_at()
    # Last assigned event sequence; incremented under row lock in append_event.
    event_seq: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (CheckConstraint("status IN ('active', 'paused', 'archived')"),)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[str] = mapped_column(
        ID, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    principal_id: Mapped[str] = mapped_column(
        ID, ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'observer')"),
        Index("ix_project_members_principal", "principal_id"),
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    principal_id: Mapped[str] = mapped_column(
        ID, ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = _created_at()


# --- Agents ------------------------------------------------------------------


class AgentDefinition(Base):
    """Logical agent identity; not project-scoped, assigned via project_agents."""

    __tablename__ = "agent_definitions"

    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(64))
    adapter_type: Mapped[str] = mapped_column(String(64))
    capability_manifest: Mapped[dict[str, Any]]
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)  # non-secret adapter config
    config_ref: Mapped[str | None] = mapped_column(String(500))  # secret-vault reference
    created_at: Mapped[datetime] = _created_at()


class ProjectAgent(Base):
    __tablename__ = "project_agents"

    project_id: Mapped[str] = mapped_column(
        ID, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(ID, ForeignKey("agent_definitions.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(64))  # 'orchestrator' | 'worker'
    permissions: Mapped[list[str]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created_at()


class OrchestratorAssignment(Base):
    """BR-01: at most one active (active_to IS NULL) orchestrator per project."""

    __tablename__ = "orchestrator_assignments"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    agent_id: Mapped[str] = mapped_column(ID)
    active_from: Mapped[datetime] = mapped_column(default=utcnow)
    active_to: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "agent_id"], ["project_agents.project_id", "project_agents.agent_id"]
        ),
        UniqueConstraint("project_id", "version"),
        Index(
            "uq_orchestrator_active",
            "project_id",
            unique=True,
            postgresql_where=text("active_to IS NULL"),
            sqlite_where=text("active_to IS NULL"),
        ),
    )


# --- Conversations -----------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    type: Mapped[str] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="open")
    task_id: Mapped[str | None] = mapped_column(ID)  # for type='task_thread'
    created_by: Mapped[str] = mapped_column(ID)  # agent or principal id
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        CheckConstraint("type IN ('owner_chat', 'group_meeting', 'dm', 'task_thread')"),
        CheckConstraint("visibility IN ('project', 'participants')"),
        Index("ix_conversations_project_created", "project_id", "created_at"),
        Index("ix_conversations_project_status", "project_id", "status"),
    )


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"

    conversation_id: Mapped[str] = mapped_column(
        ID, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(ID, primary_key=True)  # agent or principal id
    role: Mapped[str] = mapped_column(String(32))  # moderator | member | observer


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    conversation_id: Mapped[str] = mapped_column(ID)
    sender_id: Mapped[str] = mapped_column(ID)
    content_ref: Mapped[str] = mapped_column(String(500))
    parent_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        ForeignKeyConstraint(
            ["project_id", "conversation_id"], ["conversations.project_id", "conversations.id"]
        ),
        ForeignKeyConstraint(["project_id", "parent_id"], ["messages.project_id", "messages.id"]),
        Index("ix_messages_conversation_created", "project_id", "conversation_id", "created_at"),
    )


# --- Tasks -------------------------------------------------------------------


class Task(Base):
    """``kind='plan'`` is the root of one Orchestrator plan (never dispatched; it
    finishes when all its children are terminal). ``kind='work'`` tasks are
    dispatched to workers; ``kind='meeting'`` / ``'dm'`` tasks run a conversation
    between ``participants`` (spec §8.2).

    Status flow: waiting (deps unmet) → pending (ready; ``retry_at`` delays a
    retry) → active (run in progress) → completed | failed; a task whose
    dependency failed, or for which no capable worker exists, is blocked.
    Owner interventions add paused (resumable) and cancelled.
    """

    __tablename__ = "tasks"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    parent_id: Mapped[str | None] = mapped_column(ID)
    kind: Mapped[str] = mapped_column(String(16), default="work")
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    required_capabilities: Mapped[list[str]] = mapped_column(default=list)
    participants: Mapped[list[str]] = mapped_column(default=list)  # meeting / dm agent ids
    turn_limit: Mapped[int | None] = mapped_column(Integer)
    assignee_hint: Mapped[str | None] = mapped_column(ID)
    assignee_id: Mapped[str | None] = mapped_column(ID)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    blocked_reason: Mapped[str | None] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    deadline: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    retry_at: Mapped[datetime | None]
    result_ref: Mapped[str | None] = mapped_column(String(500))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        ForeignKeyConstraint(["project_id", "parent_id"], ["tasks.project_id", "tasks.id"]),
        CheckConstraint(
            "status IN ('pending', 'waiting', 'active', 'paused', 'blocked', 'completed',"
            " 'failed', 'cancelled')"
        ),
        CheckConstraint("kind IN ('plan', 'work', 'meeting', 'dm')"),
        Index("ix_tasks_project_created", "project_id", "created_at"),
        Index("ix_tasks_project_status", "project_id", "status"),
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    project_id: Mapped[str] = mapped_column(
        ID, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(ID, primary_key=True)
    depends_on_id: Mapped[str] = mapped_column(ID, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        ForeignKeyConstraint(["project_id", "depends_on_id"], ["tasks.project_id", "tasks.id"]),
        CheckConstraint("task_id <> depends_on_id"),
    )


class Meeting(Base):
    """Lifecycle (§8.2): proposed → scheduled → active → decision_recorded →
    action_items_created → closed."""

    __tablename__ = "meetings"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    conversation_id: Mapped[str] = mapped_column(ID)
    task_id: Mapped[str | None] = mapped_column(ID)
    agenda: Mapped[str] = mapped_column(Text)
    moderator_id: Mapped[str] = mapped_column(ID)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    turn_limit: Mapped[int | None] = mapped_column(Integer)
    time_limit_s: Mapped[int | None] = mapped_column(Integer)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    minutes_ref: Mapped[str | None] = mapped_column(String(500))
    decisions_ref: Mapped[str | None] = mapped_column(String(500))
    outcome: Mapped[str | None] = mapped_column(String(16))  # completed | failed | cancelled
    created_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None]

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "conversation_id"], ["conversations.project_id", "conversations.id"]
        ),
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        CheckConstraint(
            "status IN ('proposed', 'scheduled', 'active', 'decision_recorded',"
            " 'action_items_created', 'closed')"
        ),
        Index("ix_meetings_project_status", "project_id", "status"),
    )


# --- Artifacts, runs ---------------------------------------------------------


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    task_id: Mapped[str | None] = mapped_column(ID)
    run_id: Mapped[str | None] = mapped_column(ID)
    type: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(1000))
    checksum: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        ForeignKeyConstraint(["project_id", "run_id"], ["agent_runs.project_id", "agent_runs.id"]),
        Index("ix_artifacts_project_created", "project_id", "created_at"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    task_id: Mapped[str | None] = mapped_column(ID)  # None for orchestrator turns
    purpose: Mapped[str] = mapped_column(String(32), default="task")
    trigger: Mapped[
        dict[str, Any] | None
    ]  # orchestrator turn input, e.g. {"kind": "owner_message"}
    agent_id: Mapped[str] = mapped_column(ID)
    adapter: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))  # office_contracts.RunState
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    trace_id: Mapped[str] = mapped_column(String(64))
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6, asdecimal=False))
    error: Mapped[dict[str, Any] | None]
    output_ref: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        Index("ix_agent_runs_project_created", "project_id", "created_at"),
        Index("ix_agent_runs_project_status", "project_id", "status"),
        CheckConstraint("purpose IN ('task', 'orchestrator_turn', 'conversation_turn')"),
    )


# --- Events, approvals, audit ------------------------------------------------


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(ID)
    conversation_id: Mapped[str | None] = mapped_column(ID)
    task_id: Mapped[str | None] = mapped_column(ID)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "seq"),  # also the replay index
        Index("ix_events_project_occurred", "project_id", "occurred_at"),
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    action: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict[str, Any]] = mapped_column(default=dict)
    requester_id: Mapped[str] = mapped_column(ID)
    approver_id: Mapped[str | None] = mapped_column(ID)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    expires_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired')"),
        Index("ix_approvals_project_status", "project_id", "status"),
    )


class Intervention(Base):
    """Owner (or Orchestrator on the Owner's behalf) control request (spec §8.3).

    ``requested`` until the runtime has observed the effect (BR-06, AC-06), then
    ``applied``; ``rejected`` when it cannot apply (e.g. task already finished).
    """

    __tablename__ = "interventions"

    id: Mapped[str] = _pk()
    project_id: Mapped[str] = _project_fk()
    task_id: Mapped[str | None] = mapped_column(ID)  # None = whole project
    action: Mapped[str] = mapped_column(String(16))
    instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="requested")
    outcome: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None]

    __table_args__ = (
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"]),
        CheckConstraint("action IN ('pause', 'resume', 'cancel', 'redirect')"),
        CheckConstraint("status IN ('requested', 'applied', 'rejected')"),
        Index("ix_interventions_project_status", "project_id", "status"),
    )


class AuditLog(Base):
    """Append-only; never updated or deleted by application code."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = _pk()
    project_id: Mapped[str | None] = mapped_column(ID, ForeignKey("projects.id"))
    principal: Mapped[str] = mapped_column(ID)
    action: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_audit_logs_project_created", "project_id", "created_at"),)
