"""Audit / cost view (spec §12): runs, retries, failures, approvals and cost per
agent and project, computed from recorded rows only. Runs whose provider
reported no cost are counted separately instead of being treated as free."""

from collections import Counter, defaultdict

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from office_api import budget
from office_api.auth import Membership, Session
from office_api.budget import ProjectBudget
from office_api.models import (
    AgentDefinition,
    AgentRun,
    Approval,
    Artifact,
    EventRow,
    Intervention,
    Project,
    Task,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["metrics"])


class AgentMetrics(BaseModel):
    agent_id: str
    name: str
    adapter_type: str
    runs: int
    runs_by_status: dict[str, int]
    runs_by_purpose: dict[str, int]
    retries: int = Field(description="Runs that were a retry (attempt > 1).")
    cost_usd: float = Field(description="Sum of costs reported by the provider.")
    runs_without_cost: int = Field(description="Finished runs with no reported cost.")
    busy_seconds: float = Field(description="Sum of finished runs' wall-clock time.")


class ProjectMetrics(BaseModel):
    cost_usd: float
    runs_without_cost: int
    runs: int
    runs_by_status: dict[str, int]
    retries: int
    run_failures: int = Field(description="run.failed events (each failed attempt).")
    tasks_by_status: dict[str, int] = Field(description="work/meeting/dm tasks.")
    approvals_by_status: dict[str, int]
    interventions_by_status: dict[str, int]
    artifacts_by_type: dict[str, int]
    events: int
    budget: ProjectBudget = Field(description="Configured limits (null/absent = unlimited).")
    over_budget: bool = Field(description="Project spend reached max_cost_usd.")
    agents: list[AgentMetrics]


FINISHED = {"succeeded", "failed", "cancelled"}


@router.get("/metrics", response_model=ProjectMetrics)
async def project_metrics(project_id: str, member: Membership, session: Session):
    runs = (await session.scalars(select(AgentRun).where(AgentRun.project_id == project_id))).all()
    agents = {
        a.id: a
        for a in await session.scalars(
            select(AgentDefinition).where(AgentDefinition.id.in_({r.agent_id for r in runs}))
        )
    }

    async def count_by(column, *where) -> dict[str, int]:
        rows = await session.execute(select(column, func.count()).where(*where).group_by(column))
        return {str(k): n for k, n in rows.all()}

    per_agent: dict[str, list[AgentRun]] = defaultdict(list)
    for r in runs:
        per_agent[r.agent_id].append(r)

    def summary(rs: list[AgentRun]) -> dict:
        finished = [r for r in rs if r.status in FINISHED]
        return {
            "runs": len(rs),
            "runs_by_status": dict(Counter(r.status for r in rs)),
            "retries": sum(1 for r in rs if r.attempt > 1),
            "cost_usd": round(sum(r.cost or 0 for r in rs), 6),
            "runs_without_cost": sum(1 for r in finished if r.cost is None),
        }

    agent_rows = []
    for agent_id, rs in per_agent.items():
        agent = agents.get(agent_id)
        busy = sum(
            (r.finished_at - r.started_at).total_seconds()
            for r in rs
            if r.started_at and r.finished_at
        )
        agent_rows.append(
            AgentMetrics(
                agent_id=agent_id,
                name=agent.name if agent else agent_id,
                adapter_type=agent.adapter_type if agent else "?",
                runs_by_purpose=dict(Counter(r.purpose for r in rs)),
                busy_seconds=round(busy, 1),
                **summary(rs),
            )
        )
    agent_rows.sort(key=lambda a: (-a.cost_usd, -a.runs))
    project = await session.get(Project, project_id)
    total = summary(list(runs))
    return ProjectMetrics(
        cost_usd=total["cost_usd"],
        runs_without_cost=total["runs_without_cost"],
        runs=total["runs"],
        runs_by_status=total["runs_by_status"],
        retries=total["retries"],
        run_failures=await session.scalar(
            select(func.count())
            .select_from(EventRow)
            .where(EventRow.project_id == project_id, EventRow.type == "run.failed")
        ),
        tasks_by_status=await count_by(
            Task.status, Task.project_id == project_id, Task.kind != "plan"
        ),
        approvals_by_status=await count_by(Approval.status, Approval.project_id == project_id),
        interventions_by_status=await count_by(
            Intervention.status, Intervention.project_id == project_id
        ),
        artifacts_by_type=await count_by(Artifact.type, Artifact.project_id == project_id),
        budget=budget.limits(project),
        over_budget=budget.project_over(project, await budget.spend(session, project_id)),
        events=await session.scalar(
            select(func.count()).select_from(EventRow).where(EventRow.project_id == project_id)
        ),
        agents=agent_rows,
    )
