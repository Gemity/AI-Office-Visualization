"""Shared machinery for adapters whose work runs as a local asyncio task
(SDK sessions, CLI subprocesses, mocks).

Subclasses implement ``_execute(ctx)`` and return an ``AgentResult``; this base
provides invoke/get_status/cancel/subscribe_events/collect_artifacts with the
contract's semantics:
- invoking twice with the same idempotency key returns the same handle;
- cancel only *requests* cancellation; ``cancelled`` is reported once the
  task has actually stopped (BR-06, BR-09);
- files created or modified in ``workdir`` during the run are artifacts.
"""

import asyncio
import hashlib
import mimetypes
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from office_contracts import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentInvocation,
    AgentResult,
    AgentStatus,
    ArtifactRef,
    CancelResult,
    CapabilityManifest,
    InvocationHandle,
    RunState,
)

TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


class AdapterRunError(Exception):
    """Raised by ``_execute`` to fail a run with a coded error."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.error = AgentError(code=code, message=message, retryable=retryable)


class RunContext:
    def __init__(self, handle_id: str, invocation: AgentInvocation):
        self.handle_id = handle_id
        self.invocation = invocation
        self.provider_ref: str | None = None
        self.progress: float | None = None
        self._events: list[AgentEvent] = []
        self._changed = asyncio.Condition()

    async def emit(self, kind: AgentEventKind, **data: Any) -> None:
        event = AgentEvent(
            handle_id=self.handle_id, kind=kind, occurred_at=datetime.now(UTC), data=data
        )
        async with self._changed:
            self._events.append(event)
            self._changed.notify_all()


class _Run:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.state = RunState.QUEUED
        self.error: AgentError | None = None
        self.result: AgentResult | None = None
        self.artifacts: list[ArtifactRef] = []
        self.started_at = datetime.now(UTC).timestamp()
        self.task: asyncio.Task | None = None


class TaskBackedAdapter:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self._runs: dict[str, _Run] = {}
        self._by_key: dict[str, str] = {}

    # --- to implement -------------------------------------------------------

    def describe_capabilities(self) -> CapabilityManifest:
        raise NotImplementedError

    async def _execute(self, ctx: RunContext) -> AgentResult:
        raise NotImplementedError

    # --- AgentAdapter -------------------------------------------------------

    async def invoke(self, request: AgentInvocation) -> InvocationHandle:
        return self._start(request, self._execute)

    def _start(self, request: AgentInvocation, execute) -> InvocationHandle:
        if (existing := self._by_key.get(request.idempotency_key)) is not None:
            run = self._runs[existing]
        else:
            handle_id = f"hdl_{uuid.uuid4().hex}"
            run = _Run(RunContext(handle_id, request))
            self._runs[handle_id] = run
            self._by_key[request.idempotency_key] = handle_id
            run.task = asyncio.create_task(self._drive(run, execute))
        return InvocationHandle(
            handle_id=run.ctx.handle_id,
            invocation_id=request.invocation_id,
            state=run.state,
            provider_ref=run.ctx.provider_ref,
        )

    async def get_status(self, handle_id: str) -> AgentStatus:
        run = self._get(handle_id)
        return AgentStatus(
            handle_id=handle_id,
            state=run.state,
            progress=run.ctx.progress,
            error=run.error,
            result=run.result,
            observed_at=datetime.now(UTC),
        )

    async def cancel(self, handle_id: str) -> CancelResult:
        run = self._get(handle_id)
        if run.state in TERMINAL:
            return CancelResult(handle_id=handle_id, accepted=False, state=run.state)
        run.state = RunState.CANCEL_REQUESTED
        run.task.cancel()
        return CancelResult(handle_id=handle_id, accepted=True, state=run.state)

    async def subscribe_events(self, handle_id: str) -> AsyncIterator[AgentEvent]:
        """Replays buffered events, then yields live ones until the run ends."""
        run = self._get(handle_id)
        ctx, sent = run.ctx, 0
        while True:
            async with ctx._changed:
                await ctx._changed.wait_for(
                    lambda sent=sent: len(ctx._events) > sent or run.state in TERMINAL
                )
                batch = ctx._events[sent:]
                done = run.state in TERMINAL
            for event in batch:
                yield event
            sent += len(batch)
            if done and not batch:
                return

    async def collect_artifacts(self, handle_id: str) -> list[ArtifactRef]:
        return list(self._get(handle_id).artifacts)

    def forget(self, handle_id: str) -> None:
        """Drop a terminal run's bookkeeping."""
        run = self._runs.pop(handle_id, None)
        if run is not None:
            self._by_key.pop(run.ctx.invocation.idempotency_key, None)

    # --- internals ----------------------------------------------------------

    def _get(self, handle_id: str) -> _Run:
        try:
            return self._runs[handle_id]
        except KeyError:
            raise KeyError(f"unknown handle {handle_id}") from None

    async def _drive(self, run: _Run, execute) -> None:
        run.state = RunState.RUNNING
        await run.ctx.emit(AgentEventKind.STATUS, state=run.state.value)
        try:
            run.result = await execute(run.ctx)
            run.artifacts = _scan_artifacts(run.ctx.invocation.workdir, run.started_at)
            run.state = RunState.SUCCEEDED
        except asyncio.CancelledError:
            run.state = RunState.CANCELLED
        except AdapterRunError as exc:
            run.error, run.state = exc.error, RunState.FAILED
        except Exception as exc:  # adapter bug or provider failure
            run.error = AgentError(code="adapter_error", message=repr(exc), retryable=True)
            run.state = RunState.FAILED
        async with run.ctx._changed:
            run.ctx._changed.notify_all()


def _scan_artifacts(workdir: str | None, since: float) -> list[ArtifactRef]:
    if not workdir or not Path(workdir).is_dir():
        return []
    refs = []
    for path in sorted(Path(workdir).rglob("*")):
        if not path.is_file() or path.stat().st_mtime < since:
            continue
        if any(part.startswith(".") for part in path.relative_to(workdir).parts):
            continue
        data = path.read_bytes()
        refs.append(
            ArtifactRef(
                uri=path.resolve().as_uri(),
                type=_artifact_type(path),
                mime_type=mimetypes.guess_type(path.name)[0],
                checksum="sha256:" + hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return refs


_CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".json", ".sh", ".go"}


def _artifact_type(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if path.suffix in _CODE_SUFFIXES:
        return "code"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if path.suffix in {".glb", ".gltf", ".obj", ".fbx"}:
        return "3d"
    if path.suffix == ".log":
        return "log"
    return "document"
