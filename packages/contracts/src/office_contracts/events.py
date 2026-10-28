"""Event envelope (spec §10).

Every event belongs to exactly one project and carries a per-project, gap-free
``sequence``. Consumers de-duplicate by ``event_id`` and replay by ``sequence``.
Large or sensitive content goes to object storage and is referenced from the
payload via ``content_ref``.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    PROJECT_CREATED = "project.created"
    AGENT_ASSIGNED = "agent.assigned"
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_BLOCKED = "task.blocked"
    TASK_COMPLETED = "task.completed"
    MESSAGE_SENT = "message.sent"
    MEETING_STARTED = "meeting.started"
    MEETING_ENDED = "meeting.ended"
    DECISION_RECORDED = "decision.recorded"
    ARTIFACT_CREATED = "artifact.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_FAILED = "run.failed"
    INTERVENTION_REQUESTED = "intervention.requested"
    INTERVENTION_APPLIED = "intervention.applied"


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", title="Event")

    event_id: str = Field(description="Globally unique; consumers de-dup on this.")
    project_id: str
    sequence: int = Field(ge=1, description="Per-project, strictly increasing, gap-free.")
    type: EventType
    actor_id: str = Field(description="Agent, principal or 'system' that caused the event.")
    conversation_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime = Field(description="RFC 3339 timestamp with offset.")
    payload: dict[str, Any] = Field(default_factory=dict)
