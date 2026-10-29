"""Retention sweep from the ``[retention]`` policy (spec §13, ADR-0007).

Deletes, per project that is idle (no unfinished task and no run in flight):
- content-store objects (message bodies, task results, minutes, run outputs)
  older than ``content_days`` — rows and events keep their ``content_ref``,
  readers just get no content;
- task workspaces (agent files, artifact bytes) not modified for
  ``workspace_days`` — artifact rows stay, ``/content`` answers 410.

Events, audit logs and DB rows are never deleted (audit trail, §13). A project
with work still in progress is skipped entirely, so nothing a running plan may
read disappears under it. Each project pruned gets one audit-log entry.
"""

import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_adapters import TERMINAL
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.db import new_id
from office_api.interventions import TASK_FINAL
from office_api.models import AgentRun, AuditLog, Meeting, Message, Project, Task
from office_api.storage import ContentStore, workspace_root

log = logging.getLogger(__name__)


@dataclass
class ProjectPrune:
    project_id: str
    objects: list[Path] = field(default_factory=list)
    workspaces: list[Path] = field(default_factory=list)


async def _idle(s: AsyncSession, project_id: str) -> bool:
    open_tasks = await s.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.project_id == project_id, Task.status.not_in(TASK_FINAL))
    )
    open_runs = await s.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.project_id == project_id, AgentRun.status.not_in(list(TERMINAL)))
    )
    return not open_tasks and not open_runs


async def _content_refs(s: AsyncSession, project_id: str) -> set[str]:
    columns = [
        Message.content_ref,
        Task.result_ref,
        Meeting.minutes_ref,
        Meeting.decisions_ref,
        AgentRun.output_ref,
    ]
    refs: set[str] = set()
    for col in columns:
        refs.update(
            r for r in await s.scalars(select(col).where(col.class_.project_id == project_id)) if r
        )
    return refs


def _newest_mtime(path: Path) -> float:
    return max((p.stat().st_mtime for p in path.rglob("*")), default=path.stat().st_mtime)


async def plan(
    s: AsyncSession, settings, store: ContentStore, now: datetime | None = None
) -> list[ProjectPrune]:
    """What a sweep would delete; nothing is touched."""
    policy = settings.policy.retention
    now = now or datetime.now(UTC)
    result = []
    for project_id in await s.scalars(select(Project.id).order_by(Project.created_at)):
        if not await _idle(s, project_id):
            continue
        prune = ProjectPrune(project_id)
        if policy.content_days:
            cutoff = (now - timedelta(days=policy.content_days)).timestamp()
            for ref in await _content_refs(s, project_id):
                path = store.path(ref)
                if path.is_file() and path.stat().st_mtime < cutoff:
                    prune.objects.append(path)
        root = workspace_root(settings.data_dir, project_id)
        if policy.workspace_days and root.is_dir():
            cutoff = (now - timedelta(days=policy.workspace_days)).timestamp()
            for workdir in root.iterdir():
                if workdir.is_dir() and _newest_mtime(workdir) < cutoff:
                    prune.workspaces.append(workdir)
        if prune.objects or prune.workspaces:
            result.append(prune)
    return result


async def sweep(
    s: AsyncSession, settings, store: ContentStore, *, dry_run: bool = False
) -> list[ProjectPrune]:
    pruned = await plan(s, settings, store)
    if dry_run:
        return pruned
    for p in pruned:
        for path in p.objects:
            path.unlink(missing_ok=True)
        for workdir in p.workspaces:
            shutil.rmtree(workdir, ignore_errors=True)
        s.add(
            AuditLog(
                id=new_id("aud"),
                project_id=p.project_id,
                principal="system",
                action="retention.prune",
                resource=f"objects={len(p.objects)} workspaces={len(p.workspaces)}",
                outcome="success",
            )
        )
        log.info(
            "retention: project %s, %d objects, %d workspaces deleted",
            p.project_id,
            len(p.objects),
            len(p.workspaces),
        )
    await s.commit()
    return pruned
