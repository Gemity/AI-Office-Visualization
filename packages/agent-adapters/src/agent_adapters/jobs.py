"""Base for tool-based async job workers (spec §7: Suno/Tripo-style providers).

Such providers do not converse: a job is submitted, polled until it finishes,
and its outputs are downloaded. ``AsyncJobAdapter`` maps that onto the adapter
contract:
- the provider job id becomes the handle's ``provider_ref`` (announced with a
  ``status`` event as soon as the job is accepted);
- ``progress`` events and ``AgentStatus.progress`` reflect the job status the
  provider actually reports — never an estimate (BR-09);
- cancel asks the provider to cancel the job; the run is ``cancelled`` once
  that request returned;
- outputs are written to the invocation's ``workdir`` and become artifacts;
- ``resume(request, provider_ref)`` re-attaches to an existing job (after a
  restart) instead of submitting it again; ``detach()`` (on shutdown) stops
  following jobs *without* cancelling them at the provider.

Subclasses implement ``_submit`` / ``_poll`` / ``_cancel_job`` (and
``_attach`` if re-attaching needs provider-side lookup).
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from office_contracts import (
    AgentError,
    AgentEventKind,
    AgentInvocation,
    AgentResult,
    InvocationHandle,
)

from agent_adapters.runner import AdapterRunError, RunContext, TaskBackedAdapter

JOB_STATES = {"queued", "running", "succeeded", "failed", "cancelled"}


@dataclass
class JobStatus:
    state: str  # one of JOB_STATES, as reported by the provider
    progress: float | None = None
    message: str = ""
    error: AgentError | None = None
    outputs: dict[str, bytes] = field(default_factory=dict)  # file name -> content
    cost_usd: float | None = None


class AsyncJobAdapter(TaskBackedAdapter):
    poll_interval_s = 1.0

    def __init__(self, config=None):
        super().__init__(config)
        self._detached = False

    def detach(self) -> None:
        """The process is going away: leave provider jobs running for resume()."""
        self._detached = True

    async def _submit(self, invocation: AgentInvocation) -> str:
        raise NotImplementedError

    async def _poll(self, job_id: str) -> JobStatus:
        raise NotImplementedError

    async def _cancel_job(self, job_id: str) -> None:
        raise NotImplementedError

    async def _attach(self, invocation: AgentInvocation, job_id: str) -> None:
        """Raise AdapterRunError if the provider does not know ``job_id``."""

    async def resume(self, request: AgentInvocation, provider_ref: str) -> InvocationHandle:
        async def execute(ctx: RunContext) -> AgentResult:
            await self._attach(ctx.invocation, provider_ref)
            ctx.provider_ref = provider_ref
            await ctx.emit(AgentEventKind.STATUS, state="resumed", provider_ref=provider_ref)
            return await self._follow_job(ctx, provider_ref)

        return self._start(request, execute)

    async def _execute(self, ctx: RunContext) -> AgentResult:
        if not ctx.invocation.workdir:
            raise AdapterRunError("no_workdir", "async job outputs need a workdir")
        job_id = await self._submit(ctx.invocation)
        ctx.provider_ref = job_id
        await ctx.emit(AgentEventKind.STATUS, state="submitted", provider_ref=job_id)
        return await self._follow_job(ctx, job_id)

    async def _follow_job(self, ctx: RunContext, job_id: str) -> AgentResult:
        workdir = ctx.invocation.workdir
        interval = float(self.config.get("poll_s", self.poll_interval_s))
        last = None
        try:
            while True:
                status = await self._poll(job_id)
                if status.state not in JOB_STATES:
                    raise AdapterRunError(
                        "bad_job_state", f"provider reported {status.state!r}", retryable=True
                    )
                if (status.state, status.progress) != last:
                    last = (status.state, status.progress)
                    ctx.progress = status.progress
                    await ctx.emit(
                        AgentEventKind.PROGRESS,
                        provider_ref=job_id,
                        job_state=status.state,
                        progress=status.progress,
                    )
                if status.state == "succeeded":
                    for name, content in status.outputs.items():
                        path = Path(workdir) / Path(name).name  # provider names are untrusted
                        path.write_bytes(content)
                    return AgentResult(text=status.message, cost_usd=status.cost_usd)
                if status.state == "failed":
                    error = status.error or AgentError(code="job_failed", message="job failed")
                    raise AdapterRunError(error.code, error.message, error.retryable)
                if status.state == "cancelled":
                    raise AdapterRunError("job_cancelled", "job cancelled by the provider")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            if not self._detached:
                # Make sure the provider stops (and stops billing) before reporting cancelled.
                await asyncio.shield(self._cancel_job(job_id))
            raise
