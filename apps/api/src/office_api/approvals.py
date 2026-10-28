"""Approval gate for high-risk actions (BR-07, AC-09, spec §13).

A work task needs approval when the Orchestrator marked it with
``risk_actions`` or when the worker chosen for it declares a high-risk
permission in its manifest (``required_permissions``) — so the gate holds even
if the plan forgot to flag the task. The dispatcher calls ``_approval_gate``
after picking a worker and before creating the run:

- no risk → run starts;
- an approved, unexpired, unused approval whose scope matches (same task,
  actions, worker and instructions) → run starts and consumes it (single use;
  a retry needs a new approval);
- otherwise → an approval is requested and the task waits in
  ``awaiting_approval``; nothing reaches the adapter.

Approve → the task becomes ``pending`` again; reject → ``blocked``
(``approval_rejected``); not decided before ``expires_at`` → ``expired`` and the
task is ``blocked`` (``approval_expired``). Stopping or redirecting the task
withdraws open approvals (``cancelled``).
"""

import hashlib
from datetime import timedelta

from office_contracts import ApprovalScope, EventType, RiskAction
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.db import new_id, utcnow
from office_api.events import append_event
from office_api.models import AgentDefinition, Approval, Project, Task
from office_api.storage import preview

HIGH_RISK = {a.value for a in RiskAction}
WAIT = "wait"


class ApprovalError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def risk_actions(task: Task, agent: AgentDefinition) -> list[str]:
    declared = set(task.risk_actions or [])
    permissions = set(agent.capability_manifest.get("required_permissions", [])) & HIGH_RISK
    return sorted(declared | permissions)


class ApprovalMixin:
    """Mixed into Runtime."""

    async def _open_approvals(self, s: AsyncSession, task_id: str) -> list[Approval]:
        return list(
            await s.scalars(
                select(Approval).where(
                    Approval.task_id == task_id,
                    Approval.status.in_(["pending", "approved"]),
                    Approval.run_id.is_(None),
                )
            )
        )

    async def approved_agent(self, s: AsyncSession, task: Task) -> str | None:
        """Worker an unused approval was granted for, so dispatch can wait for it."""
        for a in await self._open_approvals(s, task.id):
            if a.status == "approved" and a.expires_at > utcnow():
                return a.scope["agent_id"]
        return None

    async def _approval_gate(
        self, s: AsyncSession, task: Task, agent: AgentDefinition
    ) -> Approval | str | None:
        """None: no approval needed. Approval: use it for the run. WAIT: requested."""
        actions = risk_actions(task, agent)
        if not actions:
            return None
        wanted = {
            "task_id": task.id,
            "actions": actions,
            "agent_id": agent.id,
            "instructions_sha256": _sha(task.description),
        }
        for a in await self._open_approvals(s, task.id):
            same = {k: a.scope.get(k) for k in wanted} == wanted
            if same and a.status == "approved" and a.expires_at > utcnow():
                return a
            await self._settle(s, a, "cancelled", None, "scope changed or approval expired")

        project = await s.get(Project, task.project_id)
        ttl = float((project.config or {}).get("approval_ttl_s", self.settings.approval_ttl_s))
        scope = ApprovalScope(
            **wanted,
            summary=f"{agent.name} sẽ thực hiện {', '.join(actions)} cho task “{task.title}”: "
            + preview(task.description, 500),
        )
        approval = Approval(
            id=new_id("apr"),
            project_id=task.project_id,
            task_id=task.id,
            action=",".join(actions),
            scope=scope.model_dump(mode="json"),
            requester_id="system",
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
        s.add(approval)
        task.status, task.assignee_hint = "awaiting_approval", agent.id
        await s.flush()
        await append_event(
            s,
            project_id=task.project_id,
            type=EventType.APPROVAL_REQUESTED,
            actor_id="system",
            task_id=task.id,
            trace_id=task.trace_id,
            payload={
                "approval_id": approval.id,
                "actions": actions,
                "agent_id": agent.id,
                "summary": scope.summary,
                "expires_at": approval.expires_at.isoformat(),
            },
        )
        return WAIT

    async def decide_approval(
        self,
        s: AsyncSession,
        *,
        project_id: str,
        approval_id: str,
        decision: str,
        approver_id: str,
        comment: str | None,
    ) -> tuple[Approval, list[str]]:
        approval = await s.get(Approval, approval_id)
        if approval is None or approval.project_id != project_id:
            raise ApprovalError(404, "approval_not_found", "Approval not found")
        if approval.status != "pending":
            raise ApprovalError(409, "approval_not_pending", f"Approval is {approval.status}")
        if approval.expires_at <= utcnow():
            raise ApprovalError(409, "approval_expired", "Approval has expired")
        approval.comment = comment
        task = await s.get(Task, approval.task_id) if approval.task_id else None
        turns: list[str] = []
        if decision == "approve":
            await self._settle(s, approval, "approved", approver_id, None)
            if task is not None and task.status == "awaiting_approval":
                task.status = "pending"
        else:
            await self._settle(s, approval, "rejected", approver_id, None)
            if task is not None and task.status == "awaiting_approval":
                turns = await self._block(s, task, "approval_rejected")
        return approval, turns

    async def expire_approvals(self, s: AsyncSession) -> list[str]:
        """Called by the dispatcher; returns orchestrator turns to start."""
        turns: list[str] = []
        due = await s.scalars(
            select(Approval).where(
                Approval.status.in_(["pending", "approved"]),
                Approval.run_id.is_(None),
                Approval.expires_at <= utcnow(),
            )
        )
        for approval in due:
            await self._settle(s, approval, "expired", None, None)
            task = await s.get(Task, approval.task_id) if approval.task_id else None
            if task is not None and task.status == "awaiting_approval":
                turns += await self._block(s, task, "approval_expired")
        return turns

    async def withdraw_approvals(self, s: AsyncSession, task: Task, reason: str) -> None:
        for approval in await self._open_approvals(s, task.id):
            await self._settle(s, approval, "cancelled", None, reason)

    async def _settle(
        self,
        s: AsyncSession,
        approval: Approval,
        status: str,
        approver_id: str | None,
        reason: str | None,
    ) -> None:
        approval.status, approval.resolved_at = status, utcnow()
        if approver_id:
            approval.approver_id = approver_id
        await append_event(
            s,
            project_id=approval.project_id,
            type=EventType.APPROVAL_RESOLVED,
            actor_id=approver_id or "system",
            task_id=approval.task_id,
            payload={
                "approval_id": approval.id,
                "status": status,
                "actions": approval.action.split(","),
                "reason": reason,
                "comment": approval.comment,
            },
        )
