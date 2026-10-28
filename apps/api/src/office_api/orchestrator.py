"""Orchestrator protocol: what the Orchestrator agent is asked and must return.

Each Orchestrator turn is one adapter invocation with structured output
(``OrchestratorOutput``). The Orchestrator never touches state directly; the
runtime validates its plan and turns it into tasks (spec §5: the Orchestrator
decides, the runtime owns state).
"""

import json
from typing import Any

from office_contracts import Capability
from pydantic import BaseModel, ConfigDict, Field


class PlannedTask(BaseModel):
    # Every field is required (nullable where optional) so the schema also works
    # with strict structured-output providers.
    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Short unique id within this plan, e.g. 't1'.")
    title: str
    instructions: str = Field(description="Complete, self-contained instructions for the worker.")
    capabilities: list[Capability] = Field(
        min_length=1, description="Capabilities a worker needs for this task."
    )
    depends_on: list[str] = Field(description="Keys of tasks that must complete first.")
    assignee_id: str | None = Field(
        description="Preferred worker id from the roster, or null to let the runtime choose."
    )


class OrchestratorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(description="Message to the Owner.")
    plan_title: str | None = Field(description="Short title when tasks are created, else null.")
    tasks: list[PlannedTask] = Field(description="New tasks to create; empty for no new work.")


OUTPUT_SCHEMA = OrchestratorOutput.model_json_schema()


class PlanError(ValueError):
    pass


def validate_plan(output: OrchestratorOutput, worker_ids: set[str]) -> list[PlannedTask]:
    """Returns tasks in dependency order; raises PlanError for an invalid DAG."""
    tasks = {t.key: t for t in output.tasks}
    if len(tasks) != len(output.tasks):
        raise PlanError("duplicate task keys")
    for t in output.tasks:
        unknown = set(t.depends_on) - tasks.keys()
        if unknown:
            raise PlanError(f"task {t.key} depends on unknown {sorted(unknown)}")
        if t.assignee_id is not None and t.assignee_id not in worker_ids:
            t.assignee_id = None  # hint only; never trust ids the model invented
    ordered, done = [], set()
    while len(ordered) < len(tasks):
        ready = [t for k, t in tasks.items() if k not in done and set(t.depends_on) <= done]
        if not ready:
            raise PlanError("dependency cycle")
        for t in ready:
            ordered.append(t)
            done.add(t.key)
    return ordered


ORCHESTRATOR_PROMPT = """\
You are the Project Orchestrator of an AI project office. You are the only agent
that talks to the Owner. You plan work and delegate it to worker agents; you do
not do the work yourself and you have no tools.

Rules:
- Reply in the same language the Owner uses.
- Create tasks only when the Owner asks for work. Each task must be
  self-contained: the worker sees only its own instructions plus the results of
  the tasks it depends on.
- Choose task capabilities from what workers in the roster actually have. If no
  worker has a needed capability, say so in your reply instead of creating it.
- Independent tasks should not depend on each other, so they can run in parallel.
- Never claim work is done unless the results below show it completed.

Project: {project_name}

Worker roster (id, name, capabilities):
{roster}

Open tasks:
{open_tasks}

Conversation with the Owner (oldest first):
{history}

{trigger}

Respond with JSON matching the required schema."""

OWNER_MESSAGE_TRIGGER = "Respond to the Owner's latest message."

PLAN_FINISHED_TRIGGER = """\
The plan "{title}" has finished. Results:
{results}

Report the outcome to the Owner: what was produced (with file paths), what
failed or was blocked and why. Create follow-up tasks only if clearly needed to
finish what the Owner asked for."""


def render_turn_prompt(
    *,
    project_name: str,
    roster: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    history: list[tuple[str, str]],
    trigger: str,
) -> str:
    return ORCHESTRATOR_PROMPT.format(
        project_name=project_name,
        roster="\n".join(
            f"- {w['id']} | {w['name']} | {', '.join(w['capabilities'])}" for w in roster
        )
        or "(no workers assigned)",
        open_tasks="\n".join(f"- [{t['status']}] {t['title']}" for t in open_tasks) or "(none)",
        history="\n".join(f"{who}: {text}" for who, text in history) or "(empty)",
        trigger=trigger,
    )


def render_plan_results(results: list[dict[str, Any]]) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)


TASK_PROMPT = """\
You are a worker agent in an AI project office. Complete the task below.

Task: {title}

Instructions:
{instructions}

{dependencies}
Write all deliverable files inside the current working directory ({workdir}).
Finish with a concise summary of what you did and which files you produced."""


def render_task_prompt(
    *, title: str, instructions: str, workdir: str, dependencies: list[dict[str, Any]]
) -> str:
    deps = ""
    if dependencies:
        deps = (
            "Results of tasks this one depends on:\n"
            + json.dumps(dependencies, ensure_ascii=False, indent=2)
            + "\n"
        )
    return TASK_PROMPT.format(
        title=title, instructions=instructions, workdir=workdir, dependencies=deps
    )
