"""Communication hub: group meetings and agent-to-agent DMs (spec §8.2, BR-05).

A ``meeting`` / ``dm`` task runs a turn-taking conversation between its
participants; every turn is one adapter invocation (no tools, no workdir)
recorded as an ``agent_runs`` row (purpose ``conversation_turn``) and a
message. A meeting is moderated by the active Orchestrator, who closes it with
structured minutes, decisions (``decision.recorded``) and action items that
become new tasks in the same plan. The conversation's result feeds the tasks
that depend on it.

Lifecycle: meeting.started → turns → decision_recorded → action_items_created
→ closed (meeting.ended). DMs have visibility ``participants``; the Owner can
still read them (audit).
"""

import asyncio
import json

from agent_adapters import TERMINAL
from office_contracts import AgentError, AgentInvocation, EventType, InvocationLimits, RunState
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api import orchestrator as orch
from office_api.db import new_id, utcnow
from office_api.events import append_event
from office_api.models import (
    AgentDefinition,
    AgentRun,
    Conversation,
    ConversationParticipant,
    Meeting,
    Task,
)
from office_api.storage import preview

TURN_ATTEMPTS = 2


class ConversationFailed(Exception):
    def __init__(self, error: AgentError):
        super().__init__(error.message)
        self.error = error


class ConversationMixin:
    """Mixed into Runtime."""

    async def _start_conversation(self, s: AsyncSession, task: Task) -> None:
        task.attempts += 1
        task.status, task.retry_at = "active", None
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.TASK_STARTED,
            actor_id="system",
            task_id=task.id,
            trace_id=task.trace_id,
            payload={"kind": task.kind, "participants": task.participants},
        )

    def _spawn_conversation(self, task_id: str) -> None:
        conv = asyncio.create_task(self._execute_conversation(task_id))
        self._conversations[task_id] = conv
        self._running.add(conv)

        def _done(t: asyncio.Task) -> None:
            self._conversations.pop(task_id, None)
            self._on_done(t)

        conv.add_done_callback(_done)

    async def _execute_conversation(self, task_id: str) -> None:
        async with self.sessionmaker() as s:
            task = await s.get(Task, task_id)
            if (iv := await self.pending_intervention(s, task_id)) is not None:
                turns = await self._apply_stop(s, task, iv)
                await self._commit(s)
                self._after(turns)
                return
            ids = list(task.participants)
            agents = {
                a.id: a
                for a in await s.scalars(select(AgentDefinition).where(AgentDefinition.id.in_(ids)))
            }
            from office_api.runtime import active_orchestrator_id

            moderator_id = await active_orchestrator_id(s, task.project_id)
            moderator = await s.get(AgentDefinition, moderator_id) if moderator_id else None
            conv, meeting = await self._open_conversation(s, task, ids, agents, moderator)
            await self._commit(s)

        transcript: list[tuple[str, str]] = []
        try:
            async with asyncio.timeout(self.settings.conversation_time_limit_s):
                for i in range(task.turn_limit or orch.DEFAULT_TURNS[task.kind]):
                    speaker = agents[ids[i % len(ids)]]
                    prompt = orch.CONVERSATION_TURN_PROMPT.format(
                        name=speaker.name,
                        kind="group meeting" if task.kind == "meeting" else "direct conversation",
                        participants=", ".join(agents[a].name for a in ids),
                        kind_title="Meeting" if task.kind == "meeting" else "Topic",
                        title=task.title,
                        agenda=task.description,
                        transcript=orch.render_transcript(transcript),
                    )
                    status = await self._conversation_turn(task, speaker, prompt)
                    text = (status.result.text if status.result else "").strip() or "…"
                    transcript.append((speaker.name, text))
                    await self._record_message(
                        task.project_id,
                        conv.id,
                        speaker.id,
                        text,
                        task_id=task.id,
                        trace_id=task.trace_id,
                        recipient_ids=[a for a in ids if a != speaker.id],
                    )
                outcome = None
                if meeting is not None and moderator is not None:
                    outcome = await self._summarize_meeting(
                        task, moderator, agents, ids, transcript
                    )
        except asyncio.CancelledError:
            if task_id not in self._stopping:
                raise  # runtime shutdown: recover() resolves the task on next start
            self._stopping.discard(task_id)
            await self._finish_conversation(task_id, conv.id, meeting, "cancelled")
            return
        except TimeoutError:
            error = AgentError(code="timeout", message="conversation exceeded its time limit")
            await self._finish_conversation(task_id, conv.id, meeting, "failed", error=error)
            return
        except ConversationFailed as exc:
            await self._finish_conversation(task_id, conv.id, meeting, "failed", error=exc.error)
            return
        await self._finish_conversation(
            task_id, conv.id, meeting, "completed", transcript=transcript, outcome=outcome
        )

    async def _open_conversation(self, s, task, ids, agents, moderator):
        is_meeting = task.kind == "meeting"
        conv = Conversation(
            id=new_id("conv"),
            project_id=task.project_id,
            type="group_meeting" if is_meeting else "dm",
            visibility="project" if is_meeting else "participants",
            task_id=task.id,
            created_by=moderator.id if moderator else "system",
        )
        s.add(conv)
        await s.flush()
        for agent_id in ids:
            s.add(
                ConversationParticipant(conversation_id=conv.id, agent_id=agent_id, role="member")
            )
        meeting = None
        if is_meeting and moderator is not None:
            s.add(
                ConversationParticipant(
                    conversation_id=conv.id, agent_id=moderator.id, role="moderator"
                )
            )
            meeting = Meeting(
                id=new_id("mtg"),
                project_id=task.project_id,
                conversation_id=conv.id,
                task_id=task.id,
                agenda=task.description,
                moderator_id=moderator.id,
                status="active",
                turn_limit=task.turn_limit,
                time_limit_s=int(self.settings.conversation_time_limit_s),
            )
            s.add(meeting)
            await s.flush()
            await append_event(
                s,
                project_id=task.project_id,
                type=EventType.MEETING_STARTED,
                actor_id=moderator.id,
                conversation_id=conv.id,
                task_id=task.id,
                trace_id=task.trace_id,
                payload={
                    "meeting_id": meeting.id,
                    "title": task.title,
                    "participants": ids,
                    "moderator_id": moderator.id,
                    "turn_limit": task.turn_limit,
                },
            )
        return conv, meeting

    async def _conversation_turn(
        self, task: Task, agent: AgentDefinition, prompt: str, schema=None
    ):
        """One speaking turn (retried once if retryable); raises ConversationFailed."""
        for attempt in range(1, TURN_ATTEMPTS + 1):
            async with self.sessionmaker() as s:
                run = AgentRun(
                    id=new_id("run"),
                    project_id=task.project_id,
                    task_id=task.id,
                    purpose="conversation_turn",
                    agent_id=agent.id,
                    adapter=agent.adapter_type,
                    status=RunState.QUEUED,
                    attempt=attempt,
                    idempotency_key=new_id("turn"),
                    trace_id=task.trace_id or new_id("trace"),
                )
                s.add(run)
                await s.commit()
            invocation = AgentInvocation(
                invocation_id=run.id,
                idempotency_key=run.idempotency_key,
                project_id=task.project_id,
                task_id=task.id,
                agent_id=agent.id,
                trace_id=run.trace_id,
                mode=agent.capability_manifest.get("invocation_modes", ["sync"])[0],
                instructions=prompt,
                output_schema=schema,
                limits=InvocationLimits(timeout_s=int(self.settings.run_timeout_s)),
            )
            try:
                status, _ = await self._invoke(agent, run.id, invocation)
            except asyncio.CancelledError:
                await self._close_run(run.id, RunState.CANCELLED)
                raise
            async with self.sessionmaker() as s:
                row = await s.get(AgentRun, run.id)
                row.finished_at = utcnow()
                if status.state == RunState.SUCCEEDED:
                    row.status = RunState.SUCCEEDED
                    row.cost = status.result.cost_usd if status.result else None
                    await s.commit()
                    return status
                error = status.error or AgentError(code=str(status.state), message="turn failed")
                row.status, row.error = RunState.FAILED, error.model_dump()
                will_retry = error.retryable and attempt < TURN_ATTEMPTS
                await append_event(
                    s,
                    project_id=task.project_id,
                    type=EventType.RUN_FAILED,
                    actor_id=agent.id,
                    task_id=task.id,
                    trace_id=row.trace_id,
                    payload={
                        "run_id": row.id,
                        "purpose": "conversation_turn",
                        "attempt": attempt,
                        "error": error.model_dump(),
                        "will_retry": will_retry,
                    },
                )
                await self._commit(s)
            if not will_retry:
                raise ConversationFailed(error)
        raise AssertionError("unreachable")

    async def _close_run(self, run_id: str, state: RunState) -> None:
        async with self.sessionmaker() as s:
            row = await s.get(AgentRun, run_id)
            if row is not None and row.status not in TERMINAL:
                row.status, row.finished_at = state, utcnow()
                await s.commit()

    async def _summarize_meeting(self, task, moderator, agents, ids, transcript):
        prompt = orch.MEETING_SUMMARY_PROMPT.format(
            title=task.title,
            agenda=task.description,
            participants=", ".join(f"{a} ({agents[a].name})" for a in ids),
            transcript=orch.render_transcript(transcript),
        )
        status = await self._conversation_turn(
            task, moderator, prompt, schema=orch.MEETING_OUTCOME_SCHEMA
        )
        try:
            return orch.MeetingOutcome.model_validate(status.result.structured or {})
        except ValidationError as exc:
            raise ConversationFailed(
                AgentError(code="invalid_minutes", message=str(exc)[:1000])
            ) from None

    async def _finish_conversation(
        self,
        task_id: str,
        conv_id: str,
        meeting: Meeting | None,
        result: str,
        *,
        transcript: list[tuple[str, str]] | None = None,
        outcome: "orch.MeetingOutcome | None" = None,
        error: AgentError | None = None,
    ) -> None:
        turns: list[str] = []
        async with self.sessionmaker() as s:
            task = await s.get(Task, task_id)
            conv = await s.get(Conversation, conv_id)
            conv.status = "closed"
            text = orch.render_transcript(transcript or [])
            if meeting is not None:
                meeting = await s.get(Meeting, meeting.id)
                if outcome is not None:
                    await self._record_outcome(s, task, meeting, outcome)
                    text = _outcome_text(outcome)
                meeting.status, meeting.outcome, meeting.closed_at = "closed", result, utcnow()
                await append_event(
                    s,
                    project_id=task.project_id,
                    type=EventType.MEETING_ENDED,
                    actor_id=meeting.moderator_id,
                    conversation_id=conv_id,
                    task_id=task.id,
                    trace_id=task.trace_id,
                    payload={"meeting_id": meeting.id, "outcome": result},
                )
            if result == "completed":
                task.status, task.result_ref = "completed", self.store.put_text(text)
                await append_event(
                    s,
                    project_id=task.project_id,
                    type=EventType.TASK_COMPLETED,
                    actor_id="system",
                    task_id=task.id,
                    trace_id=task.trace_id,
                    payload={
                        "kind": task.kind,
                        "result_ref": task.result_ref,
                        "preview": preview(text),
                    },
                )
                await self._release_dependents(s, task)
                turns = await self._check_plan(s, task)
            elif result == "cancelled":
                iv = await self.pending_intervention(s, task_id)
                if iv is not None:
                    turns = await self._apply_stop(s, task, iv)
                else:
                    task.status = await self._ready_status(s, task)
            else:
                task.status = "failed"
                task.blocked_reason = error.code if error else None
                await self._block_dependents(s, task)
                turns = await self._check_plan(s, task)
            await self._commit(s)
        self._after(turns)

    async def _record_outcome(
        self, s: AsyncSession, task: Task, meeting: Meeting, outcome: "orch.MeetingOutcome"
    ) -> None:
        from office_api.runtime import add_message

        meeting.minutes_ref = self.store.put_text(outcome.minutes)
        meeting.decisions_ref = self.store.put_text(
            json.dumps(outcome.decisions, ensure_ascii=False)
        )
        meeting.status = "decision_recorded"
        await add_message(
            s,
            self.store,
            project_id=task.project_id,
            conversation_id=meeting.conversation_id,
            sender_id=meeting.moderator_id,
            text=_outcome_text(outcome),
            task_id=task.id,
            trace_id=task.trace_id,
        )
        for decision in outcome.decisions:
            await append_event(
                s,
                project_id=task.project_id,
                type=EventType.DECISION_RECORDED,
                actor_id=meeting.moderator_id,
                conversation_id=meeting.conversation_id,
                task_id=task.id,
                trace_id=task.trace_id,
                payload={"meeting_id": meeting.id, "decision": decision},
            )
        participants = set(task.participants)
        for item in outcome.action_items:
            follow_up = Task(
                id=new_id("task"),
                project_id=task.project_id,
                parent_id=task.parent_id,
                kind="work",
                title=item.title,
                description=item.instructions,
                required_capabilities=[c.value for c in item.capabilities],
                assignee_hint=item.assignee_id if item.assignee_id in participants else None,
                status="pending",
                trace_id=task.trace_id,
            )
            s.add(follow_up)
            await s.flush()
            await self._task_created_event(s, follow_up, meeting.moderator_id, [])
        if outcome.action_items:
            meeting.status = "action_items_created"

    def _after(self, turns: list[str]) -> None:
        for run_id in turns:
            self.start_turn(run_id)
        self.wake()


def _outcome_text(outcome: "orch.MeetingOutcome") -> str:
    lines = ["Biên bản / Minutes:", outcome.minutes]
    if outcome.decisions:
        lines += ["", "Quyết định / Decisions:", *[f"- {d}" for d in outcome.decisions]]
    if outcome.action_items:
        lines += [
            "",
            "Việc tiếp theo / Action items:",
            *[f"- {a.title}" for a in outcome.action_items],
        ]
    return "\n".join(lines)
