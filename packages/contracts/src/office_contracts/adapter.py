"""Agent adapter data contract (spec §7).

Adapters are selected by declared capabilities, never by provider name (BR-03).
Statuses reported here must come from the provider/job itself; an adapter must
not report ``succeeded`` or ``cancelled`` it has not observed (BR-09).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capability(StrEnum):
    CHAT = "chat"
    CODE = "code"
    IMAGE = "image"
    AUDIO = "audio"
    THREE_D = "3d"
    TOOL_CALL = "tool_call"
    ASYNC_JOB = "async_job"


class InvocationMode(StrEnum):
    SYNC = "sync"
    STREAM = "stream"
    ASYNC_JOB = "async_job"


class ObservabilityLevel(StrEnum):
    STATUS_ONLY = "status_only"  # only job status (e.g. generation APIs)
    EVENTS = "events"  # progress / tool-call events
    TRANSCRIPT = "transcript"  # full message transcript as exposed by the provider


class CostUnit(StrEnum):
    TOKEN = "token"
    JOB = "job"
    SECOND = "second"
    NONE = "none"


class CostModel(_Model):
    unit: CostUnit
    currency: str = "USD"
    unit_price: float | None = Field(default=None, ge=0, description="Price per unit if known.")
    notes: str | None = None


class CapabilityManifest(_Model):
    model_config = ConfigDict(title="CapabilityManifest")

    adapter_type: str = Field(description="Stable adapter identifier, e.g. 'claude-agent-sdk'.")
    provider: str
    capabilities: list[Capability] = Field(min_length=1)
    invocation_modes: list[InvocationMode] = Field(min_length=1)
    required_permissions: list[str] = Field(default_factory=list)
    supported_artifact_types: list[str] = Field(default_factory=list)
    cost_model: CostModel
    observability_level: ObservabilityLevel
    supports_cancel: bool = False


class InvocationLimits(_Model):
    timeout_s: int | None = Field(default=None, gt=0)
    max_cost: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentInvocation(_Model):
    model_config = ConfigDict(title="AgentInvocation")

    invocation_id: str
    idempotency_key: str = Field(
        description="Re-dispatch with the same key must not start a second run."
    )
    project_id: str
    task_id: str | None = None
    agent_id: str
    trace_id: str
    mode: InvocationMode
    instructions: str
    context_refs: list[str] = Field(
        default_factory=list, description="content_ref / artifact URIs."
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    limits: InvocationLimits = Field(default_factory=InvocationLimits)
    workdir: str | None = Field(
        default=None, description="Directory the agent may write to; its new files are artifacts."
    )
    read_dirs: list[str] = Field(
        default_factory=list, description="Extra directories the agent may read."
    )
    output_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema the final output must match (structured output)."
    )


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InvocationHandle(_Model):
    model_config = ConfigDict(title="InvocationHandle")

    handle_id: str
    invocation_id: str
    state: RunState
    provider_ref: str | None = Field(default=None, description="Provider-side job/session id.")


class AgentError(_Model):
    code: str
    message: str
    retryable: bool = False


class AgentResult(_Model):
    text: str = Field(description="Final answer / summary produced by the agent.")
    structured: dict[str, Any] | None = Field(
        default=None, description="Parsed output when AgentInvocation.output_schema was set."
    )
    cost_usd: float | None = Field(default=None, ge=0)


class AgentStatus(_Model):
    model_config = ConfigDict(title="AgentStatus")

    handle_id: str
    state: RunState
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = None
    error: AgentError | None = None
    result: AgentResult | None = Field(default=None, description="Set when state is succeeded.")
    observed_at: datetime


class CancelResult(_Model):
    model_config = ConfigDict(title="CancelResult")

    handle_id: str
    accepted: bool = Field(description="Provider accepted the request; not proof it stopped.")
    state: RunState = Field(description="State actually observed after the request.")


class AgentEventKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    PROGRESS = "progress"
    LOG = "log"
    ARTIFACT = "artifact"
    STATUS = "status"


class AgentEvent(_Model):
    model_config = ConfigDict(title="AgentEvent")

    handle_id: str
    kind: AgentEventKind
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(_Model):
    model_config = ConfigDict(title="ArtifactRef")

    uri: str
    type: str = Field(description="code, audio, 3d, document, log, ...")
    mime_type: str | None = None
    checksum: str | None = Field(default=None, description="'sha256:<hex>'")
    size_bytes: int | None = Field(default=None, ge=0)
