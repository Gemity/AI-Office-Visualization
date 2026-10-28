from office_contracts.adapter import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentInvocation,
    AgentResult,
    AgentStatus,
    ArtifactRef,
    CancelResult,
    Capability,
    CapabilityManifest,
    CostModel,
    CostUnit,
    InvocationHandle,
    InvocationLimits,
    InvocationMode,
    ObservabilityLevel,
    RunState,
)
from office_contracts.events import Event, EventType
from office_contracts.policy import ApprovalScope, RiskAction

# Models exported as JSON Schema files under packages/contracts/schemas/.
SCHEMA_MODELS = {
    "event": Event,
    "capability-manifest": CapabilityManifest,
    "agent-invocation": AgentInvocation,
    "invocation-handle": InvocationHandle,
    "agent-status": AgentStatus,
    "cancel-result": CancelResult,
    "agent-event": AgentEvent,
    "artifact-ref": ArtifactRef,
    "approval-scope": ApprovalScope,
}

__all__ = [
    "SCHEMA_MODELS",
    "AgentError",
    "AgentEvent",
    "AgentEventKind",
    "AgentInvocation",
    "AgentResult",
    "AgentStatus",
    "ApprovalScope",
    "ArtifactRef",
    "CancelResult",
    "Capability",
    "CapabilityManifest",
    "CostModel",
    "CostUnit",
    "Event",
    "EventType",
    "InvocationHandle",
    "InvocationLimits",
    "InvocationMode",
    "ObservabilityLevel",
    "RiskAction",
    "RunState",
]
