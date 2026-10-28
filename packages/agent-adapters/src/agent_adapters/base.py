"""AgentAdapter interface (spec §7).

Required methods form ``AgentAdapter``. The optional ones from the spec
(``cancel?``, ``subscribeEvents?``, ``collectArtifacts?``) are separate
protocols so the runtime can check support with ``isinstance``. ``resume`` is
an extension for job-based providers: re-attach to a provider job by its
``provider_ref`` after a restart instead of submitting (and paying for) it again.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from office_contracts import (
    AgentEvent,
    AgentInvocation,
    AgentStatus,
    ArtifactRef,
    CancelResult,
    CapabilityManifest,
    InvocationHandle,
)


@runtime_checkable
class AgentAdapter(Protocol):
    def describe_capabilities(self) -> CapabilityManifest: ...

    async def invoke(self, request: AgentInvocation) -> InvocationHandle: ...

    async def get_status(self, handle_id: str) -> AgentStatus: ...


@runtime_checkable
class SupportsCancel(Protocol):
    async def cancel(self, handle_id: str) -> CancelResult: ...


@runtime_checkable
class SupportsEvents(Protocol):
    def subscribe_events(self, handle_id: str) -> AsyncIterator[AgentEvent]: ...


@runtime_checkable
class SupportsArtifacts(Protocol):
    async def collect_artifacts(self, handle_id: str) -> list[ArtifactRef]: ...


@runtime_checkable
class SupportsResume(Protocol):
    async def resume(self, request: AgentInvocation, provider_ref: str) -> InvocationHandle: ...
