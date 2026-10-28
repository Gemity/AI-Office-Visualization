"""Per-project and per-agent cost budgets with a circuit breaker (spec §13, ADR-0006).

No budget is set by default (Owner decision 2026-10-28); limits apply only when
configured in ``projects.budget``:

    {"max_cost_usd": 5.0, "agents": {"agt_…": 1.0}}

Spend is the sum of costs providers reported on ``agent_runs`` (runs without a
reported cost count as 0 — they are visible separately in /metrics). A run's
cost is only known once it finishes, so in-flight runs can overshoot a limit;
the remaining budget is passed to adapters as ``limits.max_cost`` so those that
support it (Claude Agent SDK) stop on their own.

- Project over budget → the runtime pauses the project through a regular
  intervention requested by ``system`` (events + audit); running runs finish,
  nothing new is dispatched. Resuming is refused until the budget is raised.
- Agent over budget → the agent is not given new work; a task whose capable
  workers are all over budget is blocked (``budget_exceeded``).
"""

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.models import AgentRun, Project


class ProjectBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float | None = Field(default=None, ge=0, description="null = no limit")
    agents: dict[str, float] = Field(
        default_factory=dict, description="Per-agent limits in USD, by agent id."
    )


class Spend:
    def __init__(self, project: float, agents: dict[str, float]):
        self.project, self.agents = project, agents


async def spend(s: AsyncSession, project_id: str) -> Spend:
    rows = (
        await s.execute(
            select(AgentRun.agent_id, func.coalesce(func.sum(AgentRun.cost), 0))
            .where(AgentRun.project_id == project_id)
            .group_by(AgentRun.agent_id)
        )
    ).all()
    agents = defaultdict(float, {a: float(c) for a, c in rows})
    return Spend(sum(agents.values()), agents)


def limits(project: Project) -> ProjectBudget:
    return ProjectBudget.model_validate(project.budget or {})


def project_over(project: Project, used: Spend) -> bool:
    cap = limits(project).max_cost_usd
    return cap is not None and used.project >= cap


def agent_over(project: Project, used: Spend, agent_id: str) -> bool:
    cap = limits(project).agents.get(agent_id)
    return cap is not None and used.agents[agent_id] >= cap


def remaining(project: Project, used: Spend, agent_id: str) -> float | None:
    """Budget left for one run of ``agent_id`` (None = unlimited)."""
    budget = limits(project)
    left = []
    if budget.max_cost_usd is not None:
        left.append(budget.max_cost_usd - used.project)
    if agent_id in budget.agents:
        left.append(budget.agents[agent_id] - used.agents[agent_id])
    return max(0.0, min(left)) if left else None
