"""Orchestration runtime (spec §5, ADR-0003): durable task execution on the DB.

All state lives in ``tasks`` / ``agent_runs`` / ``events``; in-memory objects
(adapter instances, asyncio tasks) are disposable. On startup ``recover()``
fails runs left unfinished by a previous process and retries them per policy,
so a restart never loses committed tasks (AC-10).

Flow:
  owner message ─► orchestrator turn ─► plan (tasks DAG) ─► dispatcher
  ─► worker runs (parallel) ─► completion/retry/blocking ─► plan finished
  ─► orchestrator synthesis turn ─► reply to Owner
"""

import asyncio
import logging
from collections import Counter
from datetime import timedelta
from typing import Any

from agent_adapters import TERMINAL, AgentAdapter, SupportsEvents, create_adapter
from office_contracts import (
    AgentError,
    AgentEventKind,
    AgentInvocation,
    AgentStatus,
    ArtifactRef,
    EventType,
    InvocationLimits,
    RunState,
)
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from office_api import orchestrator as orch
from office_api.config import Settings
from office_api.db import new_id, utcnow
from office_api.events import EventBroker, append_event, commit_and_publish
from office_api.hub import ConversationMixin
from office_api.interventions import InterventionError, InterventionMixin
from office_api.models import (
    AgentDefinition,
    AgentRun,
    Artifact,
    Conversation,
    ConversationParticipant,
    Intervention,
    Message,
    OrchestratorAssignment,
    Project,
    ProjectAgent,
    ProjectMember,
    Task,
    TaskDependency,
)
from office_api.storage import ContentStore, preview, workspace_root

log = logging.getLogger(__name__)

ACTIVE_RUN_STATES = [
    RunState.QUEUED,
    RunState.RUNNING,
    RunState.WAITING,
    RunState.CANCEL_REQUESTED,
]
TASK_TERMINAL = {"completed", "failed", "blocked", "cancelled"}


class NoOrchestrator(Exception):
    pass


class Runtime(ConversationMixin, InterventionMixin):
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        broker: EventBroker,
        store: ContentStore,
    ):
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.broker = broker
        self.store = store
        self._adapters: dict[str, AgentAdapter] = {}
        self._running: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        # run_id -> (adapter, handle_id, task_id) while an invocation is in flight
        self._live: dict[str, tuple[AgentAdapter, str, str | None]] = {}
        self._conversations: dict[str, asyncio.Task] = {}  # meeting/dm task_id -> coroutine
        self._stopping: set[str] = set()  # conversation task_ids cancelled by an intervention

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._loop_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        # Runs interrupted here stay non-terminal in the DB; recover() handles them.
        tasks = [t for t in (self._loop_task, *self._running) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._running.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # State stays in the DB as-is; recover() resolves it on next start.
            log.error("runtime task crashed", exc_info=task.exception())

    async def _commit(self, s: AsyncSession) -> None:
        await commit_and_publish(s, self.broker)

    def adapter_for(self, agent: AgentDefinition) -> AgentAdapter:
        if agent.id not in self._adapters:
            self._adapters[agent.id] = create_adapter(agent.adapter_type, agent.config)
        return self._adapters[agent.id]

    # --- recovery ---------------------------------------------------------

    async def recover(self) -> None:
        turns_to_spawn: list[str] = []
        async with self.sessionmaker() as s:
            runs = (
                await s.scalars(select(AgentRun).where(AgentRun.status.in_(ACTIVE_RUN_STATES)))
            ).all()
            for run in runs:
                error = AgentError(
                    code="interrupted", message="runtime restarted during run", retryable=True
                )
                if run.purpose == "orchestrator_turn":
                    if (retry := await self._fail_turn(s, run, error)) is not None:
                        turns_to_spawn.append(retry)
                elif run.purpose == "conversation_turn":
                    # The whole conversation restarts (its task is reset below).
                    run.status, run.finished_at = RunState.FAILED, utcnow()
                    run.error = error.model_dump()
                else:
                    task = await s.get(Task, run.task_id)
                    await self._fail_task_run(s, run, task, error)
            # Defensive: an active task must have an active run.
            orphans = await s.scalars(
                select(Task).where(
                    Task.status == "active",
                    ~select(AgentRun.id)
                    .where(AgentRun.task_id == Task.id, AgentRun.status.in_(ACTIVE_RUN_STATES))
                    .exists(),
                )
            )
            for task in orphans:
                task.status, task.assignee_id = "pending", None
            for conv in await s.scalars(
                select(Conversation).where(
                    Conversation.type.in_(["group_meeting", "dm"]), Conversation.status == "open"
                )
            ):
                conv.status = "abandoned"
            # Interventions waiting for a run that no longer exists can apply now.
            await s.flush()
            requested = await s.scalars(
                select(Intervention).where(
                    Intervention.status == "requested", Intervention.task_id.is_not(None)
                )
            )
            for iv in requested:
                task = await s.get(Task, iv.task_id)
                if task.status != "active":
                    turns_to_spawn += await self._apply_stop(s, task, iv)
            await self._commit(s)
        if runs:
            log.warning("recovered %d interrupted runs", len(runs))
        for run_id in turns_to_spawn:
            self._spawn(self._execute_turn(run_id))

    # --- dispatcher -------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.settings.dispatch_interval_s)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.dispatch()
            except Exception:
                log.exception("dispatch failed")

    async def dispatch(self) -> None:
        """Assigns ready tasks to free, capable workers (BR-03, BR-08)."""
        started: list[str] = []
        turns: list[str] = []
        async with self.sessionmaker() as s:
            ready = (
                await s.scalars(
                    select(Task)
                    .join(Project, Project.id == Task.project_id)
                    .where(
                        Task.kind.in_(["work", "meeting", "dm"]),
                        Task.status == "pending",
                        Project.status == "active",
                        (Task.retry_at.is_(None)) | (Task.retry_at <= utcnow()),
                    )
                    .order_by(Task.priority.desc(), Task.created_at)
                )
            ).all()
            if not ready:
                return
            active = (
                await s.execute(
                    select(AgentRun.project_id, AgentRun.agent_id).where(
                        AgentRun.status.in_(ACTIVE_RUN_STATES), AgentRun.purpose == "task"
                    )
                )
            ).all()
            per_project = Counter(p for p, _ in active)
            for conv_task_id in list(self._conversations):  # running meetings / dms count too
                per_project[(await s.get(Task, conv_task_id)).project_id] += 1
            busy = {a for _, a in active}
            workers_by_project: dict[str, list[AgentDefinition]] = {}
            conversations: list[str] = []

            for task in ready:
                if per_project[task.project_id] >= self.settings.max_parallel_runs:
                    continue
                if task.project_id not in workers_by_project:
                    workers_by_project[task.project_id] = await self._workers(s, task.project_id)
                if task.kind in ("meeting", "dm"):
                    present = {w.id for w in workers_by_project[task.project_id]}
                    if not set(task.participants) <= present:
                        turns += await self._block(s, task, "participant_unavailable")
                        continue
                    await self._start_conversation(s, task)
                    per_project[task.project_id] += 1
                    conversations.append(task.id)
                    continue
                needed = set(task.required_capabilities)
                capable = [
                    w
                    for w in workers_by_project[task.project_id]
                    if needed <= set(w.capability_manifest.get("capabilities", []))
                ]
                if not capable:
                    turns += await self._block(s, task, "no_capable_worker")
                    continue
                free = [w for w in capable if w.id not in busy]
                if not free:
                    continue
                agent = next((w for w in free if w.id == task.assignee_hint), free[0])
                run = await self._start_task_run(s, task, agent)
                busy.add(agent.id)
                per_project[task.project_id] += 1
                started.append(run.id)
            await self._commit(s)
        for run_id in started:
            self._spawn(self._execute_task_run(run_id))
        for task_id in conversations:
            self._spawn_conversation(task_id)
        for run_id in turns:
            self._spawn(self._execute_turn(run_id))

    async def _workers(self, s: AsyncSession, project_id: str) -> list[AgentDefinition]:
        return list(
            await s.scalars(
                select(AgentDefinition)
                .join(ProjectAgent, ProjectAgent.agent_id == AgentDefinition.id)
                .where(
                    ProjectAgent.project_id == project_id,
                    ProjectAgent.role == "worker",
                    ProjectAgent.enabled.is_(True),
                )
                .order_by(ProjectAgent.created_at)
            )
        )

    async def _start_task_run(
        self, s: AsyncSession, task: Task, agent: AgentDefinition
    ) -> AgentRun:
        task.attempts += 1
        task.status, task.assignee_id, task.retry_at = "active", agent.id, None
        run = AgentRun(
            id=new_id("run"),
            project_id=task.project_id,
            task_id=task.id,
            purpose="task",
            agent_id=agent.id,
            adapter=agent.adapter_type,
            status=RunState.QUEUED,
            attempt=task.attempts,
            idempotency_key=f"{task.id}:{task.attempts}",
            trace_id=task.trace_id or new_id("trace"),
        )
        s.add(run)
        await s.flush()
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.TASK_STARTED,
            actor_id=agent.id,
            task_id=task.id,
            trace_id=run.trace_id,
            payload={"run_id": run.id, "agent_id": agent.id, "attempt": run.attempt},
        )
        return run

    # --- worker runs ------------------------------------------------------

    async def _execute_task_run(self, run_id: str) -> None:
        async with self.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            task = await s.get(Task, run.task_id)
            if (iv := await self.pending_intervention(s, task.id)) is not None:
                # Stopped before it reached the adapter.
                run.status, run.finished_at = RunState.CANCELLED, utcnow()
                turns = await self._apply_stop(s, task, iv)
                await self._commit(s)
                self._after(turns)
                return
            agent = await s.get(AgentDefinition, run.agent_id)
            dependencies = await self._dependency_results(s, task)
            thread_id = await self._task_thread(s, task, agent.id)
            await self._commit(s)

        root = workspace_root(self.settings.data_dir, task.project_id)
        workdir = root / task.id
        workdir.mkdir(parents=True, exist_ok=True)
        manifest = agent.capability_manifest
        invocation = AgentInvocation(
            invocation_id=run.id,
            idempotency_key=run.idempotency_key,
            project_id=task.project_id,
            task_id=task.id,
            agent_id=agent.id,
            trace_id=run.trace_id,
            mode=manifest.get("invocation_modes", ["async_job"])[0],
            instructions=orch.render_task_prompt(
                title=task.title,
                instructions=task.description,
                workdir=str(workdir),
                dependencies=dependencies,
            ),
            workdir=str(workdir),
            read_dirs=[str(root)],
            limits=InvocationLimits(timeout_s=int(self.settings.run_timeout_s)),
        )

        async def on_message(text: str) -> None:
            await self._record_message(
                task.project_id, thread_id, agent.id, text, task_id=task.id, trace_id=run.trace_id
            )

        status, artifacts = await self._invoke(agent, run.id, invocation, on_message)
        async with self.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            task = await s.get(Task, run.task_id)
            turns = []
            iv = await self.pending_intervention(s, task.id)
            if iv is not None and status.state != RunState.SUCCEEDED:
                run.status, run.finished_at = RunState.CANCELLED, utcnow()
                turns = await self._apply_stop(s, task, iv)
            elif status.state == RunState.SUCCEEDED:
                turns = await self._complete_task_run(s, run, task, status, artifacts)
                if iv is not None:
                    await self._resolve(
                        s, iv, "rejected", "run finished before it could be stopped"
                    )
            else:
                error = status.error or AgentError(
                    code=str(status.state), message="run did not succeed", retryable=False
                )
                turns = await self._fail_task_run(s, run, task, error)
            await self._commit(s)
        for turn_id in turns:
            self._spawn(self._execute_turn(turn_id))
        self.wake()

    async def _invoke(
        self, agent: AgentDefinition, run_id: str, invocation: AgentInvocation, on_message=None
    ) -> tuple[AgentStatus, list[ArtifactRef]]:
        """Runs one invocation to a terminal status (with timeout)."""
        adapter = self.adapter_for(agent)
        handle = None
        try:
            handle = await adapter.invoke(invocation)
            self._live[run_id] = (adapter, handle.handle_id, invocation.task_id)
            await self._mark_running(run_id, handle.provider_ref)
            status = await asyncio.wait_for(
                self._follow(adapter, handle.handle_id, on_message), self.settings.run_timeout_s
            )
            artifacts = []
            if status.state == RunState.SUCCEEDED and hasattr(adapter, "collect_artifacts"):
                artifacts = await adapter.collect_artifacts(handle.handle_id)
            return status, artifacts
        except TimeoutError:
            if handle is not None and hasattr(adapter, "cancel"):
                await adapter.cancel(handle.handle_id)
            return _failed(run_id, "timeout", "run exceeded timeout", retryable=True), []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("invocation %s failed", run_id)
            return _failed(run_id, "adapter_error", repr(exc), retryable=True), []
        finally:
            self._live.pop(run_id, None)
            if handle is not None and hasattr(adapter, "forget"):
                try:
                    if (await adapter.get_status(handle.handle_id)).state in TERMINAL:
                        adapter.forget(handle.handle_id)
                except Exception:
                    pass

    async def _follow(self, adapter: AgentAdapter, handle_id: str, on_message) -> AgentStatus:
        if isinstance(adapter, SupportsEvents):
            async for event in adapter.subscribe_events(handle_id):
                if event.kind == AgentEventKind.MESSAGE and on_message is not None:
                    text = str(event.data.get("text", "")).strip()
                    if text:
                        await on_message(text)
        while (status := await adapter.get_status(handle_id)).state not in TERMINAL:
            await asyncio.sleep(0.5)
        return status

    async def _mark_running(self, run_id: str, provider_ref: str | None) -> None:
        async with self.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            run.status, run.provider_ref, run.started_at = RunState.RUNNING, provider_ref, utcnow()
            await s.commit()

    async def _complete_task_run(
        self,
        s: AsyncSession,
        run: AgentRun,
        task: Task,
        status: AgentStatus,
        artifacts: list[ArtifactRef],
    ) -> list[str]:
        text = status.result.text if status.result else ""
        run.status, run.finished_at = RunState.SUCCEEDED, utcnow()
        run.cost = status.result.cost_usd if status.result else None
        run.output_ref = self.store.put_text(text)
        task.status, task.result_ref = "completed", run.output_ref
        for ref in artifacts:
            artifact = Artifact(
                id=new_id("art"),
                project_id=task.project_id,
                task_id=task.id,
                run_id=run.id,
                type=ref.type,
                uri=ref.uri,
                checksum=ref.checksum,
            )
            s.add(artifact)
            await s.flush()
            await append_event(
                s,
                project_id=task.project_id,
                type=EventType.ARTIFACT_CREATED,
                actor_id=run.agent_id,
                task_id=task.id,
                trace_id=run.trace_id,
                payload={"artifact_id": artifact.id, "type": ref.type, "uri": ref.uri},
            )
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.TASK_COMPLETED,
            actor_id=run.agent_id,
            task_id=task.id,
            trace_id=run.trace_id,
            payload={"run_id": run.id, "result_ref": run.output_ref, "preview": preview(text)},
        )
        await self._release_dependents(s, task)
        return await self._check_plan(s, task)

    async def _fail_task_run(
        self, s: AsyncSession, run: AgentRun, task: Task, error: AgentError
    ) -> list[str]:
        """Records the failure; retries with backoff or fails the task (AC-07)."""
        run.status, run.finished_at = RunState.FAILED, utcnow()
        run.error = error.model_dump()
        retry = error.retryable and task.attempts < task.max_attempts
        if retry:
            delay = self.settings.retry_backoff_s * 2 ** (task.attempts - 1)
            task.status, task.assignee_id = "pending", None
            task.retry_at = utcnow() + timedelta(seconds=delay)
        else:
            task.status = "failed"
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.RUN_FAILED,
            actor_id=run.agent_id,
            task_id=task.id,
            trace_id=run.trace_id,
            payload={
                "run_id": run.id,
                "attempt": run.attempt,
                "error": error.model_dump(),
                "will_retry": retry,
                "retry_at": task.retry_at.isoformat() if retry else None,
                "task_status": task.status,
            },
        )
        if retry:
            return []
        await self._block_dependents(s, task)
        return await self._check_plan(s, task)

    async def _block(self, s: AsyncSession, task: Task, reason: str) -> list[str]:
        task.status, task.blocked_reason = "blocked", reason
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.TASK_BLOCKED,
            actor_id="system",
            task_id=task.id,
            trace_id=task.trace_id,
            payload={"reason": reason},
        )
        await self._block_dependents(s, task)
        return await self._check_plan(s, task)

    async def _dependents(self, s: AsyncSession, task: Task) -> list[Task]:
        return list(
            await s.scalars(
                select(Task)
                .join(TaskDependency, TaskDependency.task_id == Task.id)
                .where(
                    TaskDependency.project_id == task.project_id,
                    TaskDependency.depends_on_id == task.id,
                )
            )
        )

    async def _release_dependents(self, s: AsyncSession, task: Task) -> None:
        await s.flush()
        for dep in await self._dependents(s, task):
            if dep.status != "waiting":
                continue
            unmet = await s.scalar(
                select(func.count())
                .select_from(TaskDependency)
                .join(Task, Task.id == TaskDependency.depends_on_id)
                .where(TaskDependency.task_id == dep.id, Task.status != "completed")
            )
            if unmet == 0:
                dep.status = "pending"

    async def _block_dependents(self, s: AsyncSession, task: Task) -> None:
        for dep in await self._dependents(s, task):
            if dep.status in {"waiting", "pending", "paused"}:
                dep.status, dep.blocked_reason = "blocked", f"dependency_failed:{task.id}"
                await append_event(
                    s,
                    project_id=dep.project_id,
                    type=EventType.TASK_BLOCKED,
                    actor_id="system",
                    task_id=dep.id,
                    trace_id=dep.trace_id,
                    payload={"reason": "dependency_failed", "dependency_id": task.id},
                )
                await self._block_dependents(s, dep)

    async def _check_plan(self, s: AsyncSession, task: Task) -> list[str]:
        """Finishes the plan when all its tasks are terminal; returns turn runs to start."""
        if task.parent_id is None:
            return []
        await s.flush()
        plan = await s.get(Task, task.parent_id)
        if plan.status in TASK_TERMINAL:
            return []
        children = (await s.scalars(select(Task).where(Task.parent_id == plan.id))).all()
        if any(c.status not in TASK_TERMINAL for c in children):
            return []
        plan.status = "completed" if all(c.status == "completed" for c in children) else "failed"
        await append_event(
            s,
            project_id=plan.project_id,
            type=EventType.TASK_COMPLETED,
            actor_id="system",
            task_id=plan.id,
            trace_id=plan.trace_id,
            payload={"kind": "plan", "status": plan.status},
        )
        try:
            run = await self.create_turn(
                s, plan.project_id, {"kind": "plan_finished", "plan_id": plan.id}, plan.trace_id
            )
        except NoOrchestrator:
            return []
        return [run.id]

    async def _dependency_results(self, s: AsyncSession, task: Task) -> list[dict[str, Any]]:
        deps = await s.scalars(
            select(Task)
            .join(TaskDependency, TaskDependency.depends_on_id == Task.id)
            .where(TaskDependency.task_id == task.id)
        )
        results = []
        for dep in deps:
            artifacts = await s.scalars(select(Artifact.uri).where(Artifact.task_id == dep.id))
            results.append(
                {
                    "title": dep.title,
                    "result": preview(self.store.get_text(dep.result_ref), 4000),
                    "files": list(artifacts),
                }
            )
        return results

    # --- conversations ----------------------------------------------------

    async def _task_thread(self, s: AsyncSession, task: Task, agent_id: str) -> str:
        conv_id = await s.scalar(
            select(Conversation.id).where(
                Conversation.task_id == task.id, Conversation.type == "task_thread"
            )
        )
        if conv_id is None:
            conv = Conversation(
                id=new_id("conv"),
                project_id=task.project_id,
                type="task_thread",
                visibility="project",
                task_id=task.id,
                created_by="system",
            )
            s.add(conv)
            await s.flush()
            conv_id = conv.id
        if await s.get(ConversationParticipant, (conv_id, agent_id)) is None:
            s.add(
                ConversationParticipant(conversation_id=conv_id, agent_id=agent_id, role="member")
            )
        return conv_id

    async def _record_message(
        self,
        project_id: str,
        conversation_id: str,
        sender_id: str,
        text: str,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        recipient_ids: list[str] | None = None,
    ) -> None:
        async with self.sessionmaker() as s:
            await add_message(
                s,
                self.store,
                project_id=project_id,
                conversation_id=conversation_id,
                sender_id=sender_id,
                text=text,
                task_id=task_id,
                trace_id=trace_id,
                recipient_ids=recipient_ids or [],
            )
            await self._commit(s)

    # --- orchestrator turns -----------------------------------------------

    async def create_turn(
        self,
        s: AsyncSession,
        project_id: str,
        trigger: dict[str, Any],
        trace_id: str | None = None,
        attempt: int = 1,
    ) -> AgentRun:
        agent_id = await active_orchestrator_id(s, project_id)
        if agent_id is None:
            raise NoOrchestrator(project_id)
        agent = await s.get(AgentDefinition, agent_id)
        run = AgentRun(
            id=new_id("run"),
            project_id=project_id,
            purpose="orchestrator_turn",
            trigger=trigger,
            agent_id=agent.id,
            adapter=agent.adapter_type,
            status=RunState.QUEUED,
            attempt=attempt,
            idempotency_key=new_id("turn"),
            trace_id=trace_id or new_id("trace"),
        )
        s.add(run)
        await s.flush()
        return run

    def start_turn(self, run_id: str) -> None:
        self._spawn(self._execute_turn(run_id))

    async def _execute_turn(self, run_id: str) -> None:
        async with self.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            agent = await s.get(AgentDefinition, run.agent_id)
            prompt = await self._turn_prompt(s, run)
            workers = {w.id for w in await self._workers(s, run.project_id)}

        invocation = AgentInvocation(
            invocation_id=run.id,
            idempotency_key=run.idempotency_key,
            project_id=run.project_id,
            agent_id=agent.id,
            trace_id=run.trace_id,
            mode=agent.capability_manifest.get("invocation_modes", ["sync"])[0],
            instructions=prompt,
            output_schema=orch.OUTPUT_SCHEMA,
            limits=InvocationLimits(timeout_s=int(self.settings.run_timeout_s)),
        )
        status, _ = await self._invoke(agent, run.id, invocation)

        output = None
        error = status.error
        if status.state == RunState.SUCCEEDED:
            try:
                output = orch.OrchestratorOutput.model_validate(status.result.structured or {})
                planned = orch.validate_plan(output, workers)
            except (ValidationError, orch.PlanError) as exc:
                error = AgentError(code="invalid_plan", message=str(exc)[:1000], retryable=True)
                output = None
        elif error is None:
            error = AgentError(code=str(status.state), message="turn did not succeed")

        retry_turn = None
        requested: list[tuple[Intervention, list[str]]] = []
        async with self.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            if output is None:
                retry_turn = await self._fail_turn(s, run, error)
            else:
                run.status, run.finished_at = RunState.SUCCEEDED, utcnow()
                run.cost = status.result.cost_usd
                run.output_ref = self.store.put_text(status.result.text or output.reply)
                conv_id = await owner_chat(s, run.project_id, agent.id)
                owners = await s.scalars(
                    select(ProjectMember.principal_id).where(
                        ProjectMember.project_id == run.project_id, ProjectMember.role == "owner"
                    )
                )
                await add_message(
                    s,
                    self.store,
                    project_id=run.project_id,
                    conversation_id=conv_id,
                    sender_id=agent.id,
                    text=output.reply,
                    trace_id=run.trace_id,
                    recipient_ids=list(owners),
                )
                await self.create_plan(s, run, output, planned)
                for item in output.interventions:
                    try:
                        requested.append(
                            await self.request_intervention(
                                s,
                                project_id=run.project_id,
                                action=item.action,
                                task_id=item.task_id,
                                instructions=item.instructions,
                                requested_by=agent.id,
                            )
                        )
                    except InterventionError as exc:
                        log.warning("orchestrator intervention ignored: %s", exc.message)
            await self._commit(s)
        if retry_turn is not None:
            self._spawn(self._execute_turn(retry_turn))
        for iv, turns in requested:
            await self.after_intervention_commit(iv, turns)
        self.wake()

    async def _fail_turn(self, s: AsyncSession, run: AgentRun, error: AgentError) -> str | None:
        run.status, run.finished_at, run.error = RunState.FAILED, utcnow(), error.model_dump()
        retry = error.retryable and run.attempt < self.settings.max_turn_attempts
        await append_event(
            s,
            project_id=run.project_id,
            type=EventType.RUN_FAILED,
            actor_id=run.agent_id,
            trace_id=run.trace_id,
            payload={
                "run_id": run.id,
                "purpose": run.purpose,
                "attempt": run.attempt,
                "error": error.model_dump(),
                "will_retry": retry,
            },
        )
        if not retry:
            return None
        try:
            new = await self.create_turn(
                s, run.project_id, run.trigger or {}, run.trace_id, attempt=run.attempt + 1
            )
        except NoOrchestrator:
            return None
        return new.id

    async def _turn_prompt(self, s: AsyncSession, run: AgentRun) -> str:
        project = await s.get(Project, run.project_id)
        roster = [
            {
                "id": w.id,
                "name": w.name,
                "capabilities": w.capability_manifest.get("capabilities", []),
            }
            for w in await self._workers(s, run.project_id)
        ]
        open_tasks = [
            {"id": t.id, "title": f"[{t.kind}] {t.title}", "status": t.status}
            for t in await s.scalars(
                select(Task)
                .where(
                    Task.project_id == run.project_id,
                    Task.kind != "plan",
                    Task.status.not_in(TASK_TERMINAL),
                )
                .order_by(Task.created_at)
            )
        ]
        history: list[tuple[str, str]] = []
        conv_id = await s.scalar(
            select(Conversation.id).where(
                Conversation.project_id == run.project_id, Conversation.type == "owner_chat"
            )
        )
        if conv_id is not None:
            msgs = (
                await s.scalars(
                    select(Message)
                    .where(Message.conversation_id == conv_id)
                    .order_by(Message.created_at.desc())
                    .limit(20)
                )
            ).all()
            for m in reversed(msgs):
                who = "Orchestrator (you)" if m.sender_id == run.agent_id else "Owner"
                history.append((who, self.store.get_text(m.content_ref) or ""))

        trigger = run.trigger or {}
        if trigger.get("kind") == "plan_finished":
            plan = await s.get(Task, trigger["plan_id"])
            results = []
            for t in await s.scalars(
                select(Task).where(Task.parent_id == plan.id).order_by(Task.created_at)
            ):
                files = await s.scalars(select(Artifact.uri).where(Artifact.task_id == t.id))
                last_error = await s.scalar(
                    select(AgentRun.error)
                    .where(AgentRun.task_id == t.id, AgentRun.error.is_not(None))
                    .order_by(AgentRun.created_at.desc())
                    .limit(1)
                )
                results.append(
                    {
                        "title": t.title,
                        "status": t.status,
                        "blocked_reason": t.blocked_reason,
                        "result": preview(self.store.get_text(t.result_ref), 2000),
                        "files": list(files),
                        "last_error": last_error,
                    }
                )
            trigger_text = orch.PLAN_FINISHED_TRIGGER.format(
                title=plan.title, results=orch.render_plan_results(results)
            )
        else:
            trigger_text = orch.OWNER_MESSAGE_TRIGGER
        return orch.render_turn_prompt(
            project_name=project.name,
            roster=roster,
            open_tasks=open_tasks,
            history=history,
            trigger=trigger_text,
        )

    async def create_plan(
        self,
        s: AsyncSession,
        run: AgentRun,
        output: orch.OrchestratorOutput,
        planned: list[orch.PlannedTask],
    ) -> Task | None:
        if not planned:
            return None
        plan = Task(
            id=new_id("task"),
            project_id=run.project_id,
            kind="plan",
            title=output.plan_title or preview(output.reply, 100),
            status="active",
            assignee_id=run.agent_id,
            trace_id=run.trace_id,
        )
        s.add(plan)
        await s.flush()
        await self._task_created_event(s, plan, run.agent_id, [])
        ids: dict[str, str] = {}
        for p in planned:
            task = Task(
                id=new_id("task"),
                project_id=run.project_id,
                parent_id=plan.id,
                kind=p.type,
                title=p.title,
                description=p.instructions,
                required_capabilities=[c.value for c in p.capabilities] if p.type == "work" else [],
                assignee_hint=p.assignee_id if p.type == "work" else None,
                participants=p.participants or [],
                turn_limit=p.turn_limit,
                status="waiting" if p.depends_on else "pending",
                trace_id=run.trace_id,
            )
            ids[p.key] = task.id
            s.add(task)
            await s.flush()
            for dep_key in p.depends_on:
                s.add(
                    TaskDependency(
                        project_id=run.project_id, task_id=task.id, depends_on_id=ids[dep_key]
                    )
                )
            await self._task_created_event(s, task, run.agent_id, [ids[k] for k in p.depends_on])
        return plan

    async def _task_created_event(
        self, s: AsyncSession, task: Task, actor_id: str, depends_on: list[str]
    ) -> None:
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.TASK_CREATED,
            actor_id=actor_id,
            task_id=task.id,
            trace_id=task.trace_id,
            payload={
                "kind": task.kind,
                "title": task.title,
                "parent_id": task.parent_id,
                "depends_on": depends_on,
                "required_capabilities": task.required_capabilities,
                "participants": task.participants,
                "status": task.status,
            },
        )


# --- helpers shared with routes ----------------------------------------------


def _failed(run_id: str, code: str, message: str, retryable: bool) -> AgentStatus:
    return AgentStatus(
        handle_id=run_id,
        state=RunState.FAILED,
        error=AgentError(code=code, message=message, retryable=retryable),
        observed_at=utcnow(),
    )


async def active_orchestrator_id(s: AsyncSession, project_id: str) -> str | None:
    return await s.scalar(
        select(OrchestratorAssignment.agent_id).where(
            OrchestratorAssignment.project_id == project_id,
            OrchestratorAssignment.active_to.is_(None),
        )
    )


async def owner_chat(s: AsyncSession, project_id: str, orchestrator_id: str) -> str:
    """The project's single Owner ↔ Orchestrator conversation (BR-02)."""
    conv_id = await s.scalar(
        select(Conversation.id).where(
            Conversation.project_id == project_id, Conversation.type == "owner_chat"
        )
    )
    if conv_id is None:
        conv = Conversation(
            id=new_id("conv"),
            project_id=project_id,
            type="owner_chat",
            visibility="project",
            created_by="system",
        )
        s.add(conv)
        await s.flush()
        conv_id = conv.id
        owners = await s.scalars(
            select(ProjectMember.principal_id).where(
                ProjectMember.project_id == project_id, ProjectMember.role == "owner"
            )
        )
        for owner in owners:
            s.add(ConversationParticipant(conversation_id=conv_id, agent_id=owner, role="member"))
    if await s.get(ConversationParticipant, (conv_id, orchestrator_id)) is None:
        s.add(
            ConversationParticipant(
                conversation_id=conv_id, agent_id=orchestrator_id, role="moderator"
            )
        )
    return conv_id


async def add_message(
    s: AsyncSession,
    store: ContentStore,
    *,
    project_id: str,
    conversation_id: str,
    sender_id: str,
    text: str,
    task_id: str | None = None,
    trace_id: str | None = None,
    recipient_ids: list[str] | None = None,
) -> Message:
    message = Message(
        id=new_id("msg"),
        project_id=project_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content_ref=store.put_text(text),
    )
    s.add(message)
    await s.flush()
    await append_event(
        s,
        project_id=project_id,
        type=EventType.MESSAGE_SENT,
        actor_id=sender_id,
        conversation_id=conversation_id,
        task_id=task_id,
        trace_id=trace_id,
        payload={
            "message_id": message.id,
            "recipient_ids": recipient_ids or [],
            "content_ref": message.content_ref,
            "preview": preview(text),
        },
    )
    return message
