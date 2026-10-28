from datetime import UTC, datetime

from agent_adapters import AgentAdapter, SupportsArtifacts, SupportsCancel, SupportsEvents
from office_contracts import (
    AgentInvocation,
    AgentStatus,
    CapabilityManifest,
    InvocationHandle,
    RunState,
)


class StatusOnlyAdapter:
    """Shape of a tool-based async worker (e.g. a mocked Suno/Tripo)."""

    def describe_capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            adapter_type="mock-async",
            provider="mock",
            capabilities=["audio", "async_job"],
            invocation_modes=["async_job"],
            supported_artifact_types=["audio"],
            cost_model={"unit": "job"},
            observability_level="status_only",
        )

    async def invoke(self, request: AgentInvocation) -> InvocationHandle:
        return InvocationHandle(handle_id="h1", invocation_id=request.invocation_id, state="queued")

    async def get_status(self, handle_id: str) -> AgentStatus:
        return AgentStatus(
            handle_id=handle_id, state=RunState.QUEUED, observed_at=datetime.now(UTC)
        )


def test_required_methods_satisfy_protocol():
    adapter = StatusOnlyAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert not isinstance(adapter, SupportsCancel)
    assert not isinstance(adapter, SupportsEvents)
    assert not isinstance(adapter, SupportsArtifacts)


def test_manifest_requires_capabilities():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CapabilityManifest(
            adapter_type="x",
            provider="x",
            capabilities=[],
            invocation_modes=["sync"],
            cost_model={"unit": "none"},
            observability_level="status_only",
        )
