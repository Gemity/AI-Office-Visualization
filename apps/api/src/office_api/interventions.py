"""Owner interventions: pause / resume / cancel / redirect (spec §8.3, BR-06, AC-06).

An intervention is ``requested`` until the runtime has observed its effect:
- on a task that is not running it applies immediately;
- on a running task the runtime asks the adapter to stop the run and marks the
  intervention ``applied`` only once the run has actually stopped;
- if the run finishes before it could be stopped, it is ``rejected``.
Providers have no real pause: pausing a running task stops the run, and resume
starts it again from scratch.
"""

from office_contracts import EventType, RunState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.db import new_id, utcnow
from office_api.events import append_event
from office_api.models import AgentRun, Intervention, Project, Task, TaskDependency

TASK_FINAL = {"completed", "failed", "cancelled", "blocked"}


class InterventionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class InterventionMixin:
    """Mixed into Runtime; uses its session/event/plan helpers."""

    async def request_intervention(
        self,
        s: AsyncSession,
        *,
        project_id: str,
        action: str,
        task_id: str | None,
        instructions: str | None,
        requested_by: str,
    ) -> tuple[Intervention, list[str]]:
        """Stages the intervention; returns it plus orchestrator turns to start after commit.
        Call ``after_intervention_commit`` once committed."""
        task = None
        if task_id is not None:
            task = await s.get(Task, task_id)
            if task is None or task.project_id != project_id or task.kind == "plan":
                raise InterventionError(404, "task_not_found", "Task not found")
            open_request = await s.scalar(
                select(func.count())
                .select_from(Intervention)
                .where(Intervention.task_id == task_id, Intervention.status == "requested")
            )
            if open_request:
                raise InterventionError(
                    409, "intervention_in_progress", "Another intervention on this task is pending"
                )
        elif action not in {"pause", "resume"}:
            raise InterventionError(422, "task_required", f"{action} needs a task_id")
        if action == "redirect" and not (instructions or "").strip():
            raise InterventionError(422, "instructions_required", "redirect needs instructions")

        iv = Intervention(
            id=new_id("int"),
            project_id=project_id,
            task_id=task_id,
            action=action,
            instructions=instructions,
            requested_by=requested_by,
        )
        s.add(iv)
        await s.flush()
        await append_event(
            s,
            project_id=project_id,
            type=EventType.INTERVENTION_REQUESTED,
            actor_id=requested_by,
            task_id=task_id,
            payload={"intervention_id": iv.id, "action": action, "instructions": instructions},
        )

        turns: list[str] = []
        if task is None:
            project = await s.get(Project, project_id)
            if action == "pause":
                project.status = "paused"
                running = await s.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(AgentRun.project_id == project_id, AgentRun.status == RunState.RUNNING)
                )
                await self._resolve(
                    s, iv, "applied", f"dispatch stopped; {running} running run(s) will finish"
                )
            else:
                project.status = "active"
                await self._resolve(s, iv, "applied", "dispatch resumed")
            return iv, turns

        if action == "redirect":
            task.description = (
                f"{task.description}\n\nOwner redirect ({utcnow():%Y-%m-%d %H:%M} UTC):\n"
                f"{instructions.strip()}"
            )
        if task.status in TASK_FINAL:
            await self._resolve(s, iv, "rejected", f"task already {task.status}")
        elif action == "resume":
            if task.status == "paused":
                task.status = await self._ready_status(s, task)
                await self._resolve(s, iv, "applied", f"task is {task.status}")
            else:
                await self._resolve(s, iv, "rejected", f"task is {task.status}, not paused")
        elif task.status == "active":
            pass  # applied once the run is observed to stop (after_intervention_commit)
        else:  # pending / waiting / paused: nothing is running
            turns = await self._apply_stop(s, task, iv)
        return iv, turns

    async def after_intervention_commit(self, iv: Intervention, turns: list[str]) -> None:
        if iv.status == "requested" and iv.task_id:
            await self._stop_running(iv.task_id)
        for run_id in turns:
            self.start_turn(run_id)
        self.wake()

    async def _stop_running(self, task_id: str) -> None:
        if (conv := self._conversations.get(task_id)) is not None:
            self._stopping.add(task_id)
            conv.cancel()
            return
        for adapter, handle_id, live_task_id in list(self._live.values()):
            if live_task_id == task_id and hasattr(adapter, "cancel"):
                await adapter.cancel(handle_id)
        # A run that has not reached the adapter yet checks for pending
        # interventions before invoking (see _execute_task_run).

    async def pending_intervention(self, s: AsyncSession, task_id: str) -> Intervention | None:
        return await s.scalar(
            select(Intervention)
            .where(Intervention.task_id == task_id, Intervention.status == "requested")
            .order_by(Intervention.created_at)
            .limit(1)
        )

    async def _apply_stop(self, s: AsyncSession, task: Task, iv: Intervention) -> list[str]:
        """The task is not running (any more): apply pause/cancel/redirect to it."""
        turns: list[str] = []
        task.assignee_id, task.retry_at = None, None
        # Approvals were for the task as it was; a stopped or redirected task asks again.
        await self.withdraw_approvals(s, task, f"intervention {iv.action}")
        if iv.action == "cancel":
            task.status = "cancelled"
            await self._block_dependents(s, task)
            turns = await self._check_plan(s, task)
            outcome = "task cancelled"
        elif iv.action == "pause":
            task.status = "paused"
            outcome = "task paused"
        else:  # redirect: run again with the updated instructions
            if task.status != "paused":
                task.status = await self._ready_status(s, task)
            outcome = f"instructions updated; task is {task.status}"
        await self._resolve(s, iv, "applied", outcome)
        return turns

    async def _resolve(self, s: AsyncSession, iv: Intervention, status: str, outcome: str) -> None:
        iv.status, iv.outcome, iv.resolved_at = status, outcome, utcnow()
        await append_event(
            s,
            project_id=iv.project_id,
            type=EventType.INTERVENTION_APPLIED,
            actor_id="system",
            task_id=iv.task_id,
            payload={
                "intervention_id": iv.id,
                "action": iv.action,
                "status": status,
                "outcome": outcome,
            },
        )

    async def _ready_status(self, s: AsyncSession, task: Task) -> str:
        await s.flush()
        unmet = await s.scalar(
            select(func.count())
            .select_from(TaskDependency)
            .join(Task, Task.id == TaskDependency.depends_on_id)
            .where(TaskDependency.task_id == task.id, Task.status != "completed")
        )
        return "waiting" if unmet else "pending"
