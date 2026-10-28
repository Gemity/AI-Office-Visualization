"""Orchestrator protocol: what the Orchestrator agent is asked and must return.

Each Orchestrator turn is one adapter invocation with structured output
(``OrchestratorOutput``). The Orchestrator never touches state directly; the
runtime validates its plan and turns it into tasks (spec §5: the Orchestrator
decides, the runtime owns state).
"""

import json
from typing import Any, Literal

from office_contracts import Capability, RiskAction
from pydantic import BaseModel, ConfigDict, Field


class PlannedTask(BaseModel):
    # Every field is required (nullable where optional) so the schema also works
    # with strict structured-output providers.
    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Short unique id within this plan, e.g. 't1'.")
    type: Literal["work", "meeting", "dm"] = Field(
        description="work: one worker does it. meeting: workers discuss an agenda you moderate, "
        "producing decisions and action items. dm: two workers talk directly."
    )
    title: str
    instructions: str = Field(
        description="work: complete, self-contained instructions. meeting/dm: the agenda/question."
    )
    capabilities: list[Capability] = Field(
        description="work: capabilities a worker needs (at least one). meeting/dm: []."
    )
    depends_on: list[str] = Field(description="Keys of tasks that must complete first.")
    assignee_id: str | None = Field(
        description="work: preferred worker id from the roster, or null to let the runtime "
        "choose. meeting/dm: null."
    )
    participants: list[str] | None = Field(
        description="meeting: 2+ worker ids; dm: exactly 2 worker ids; work: null."
    )
    turn_limit: int | None = Field(
        description="meeting/dm: total speaking turns (2-12), or null for the default; work: null."
    )
    risk_actions: list[RiskAction] = Field(
        description="work: high-risk actions this task performs (needs Owner approval before it "
        "runs); [] if none. meeting/dm: []."
    )


class PlannedIntervention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "cancel", "redirect"]
    task_id: str = Field(description="Id of an open task listed above.")
    instructions: str | None = Field(description="redirect: the new instructions; else null.")


class OrchestratorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(description="Message to the Owner.")
    plan_title: str | None = Field(description="Short title when tasks are created, else null.")
    tasks: list[PlannedTask] = Field(description="New tasks to create; empty for no new work.")
    interventions: list[PlannedIntervention] = Field(
        description="Pause/resume/cancel/redirect open tasks when the Owner asks; else []."
    )


OUTPUT_SCHEMA = OrchestratorOutput.model_json_schema()


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    instructions: str = Field(description="Complete, self-contained instructions for a worker.")
    capabilities: list[Capability] = Field(min_length=1)
    assignee_id: str | None = Field(description="Preferred participant id, or null.")
    risk_actions: list[RiskAction] = Field(
        description="High-risk actions this item performs; [] if none."
    )


class MeetingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: str = Field(description="Concise minutes of the discussion.")
    decisions: list[str] = Field(description="Decisions reached; [] if none.")
    action_items: list[ActionItem] = Field(description="Follow-up work to create; [] if none.")


MEETING_OUTCOME_SCHEMA = MeetingOutcome.model_json_schema()

DEFAULT_TURNS = {"meeting": 6, "dm": 4}
MAX_TURNS = 12


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
        if t.type == "work":
            if not t.capabilities:
                raise PlanError(f"work task {t.key} needs at least one capability")
            if t.assignee_id is not None and t.assignee_id not in worker_ids:
                t.assignee_id = None  # hint only; never trust ids the model invented
        else:
            people = list(dict.fromkeys(t.participants or []))
            if not set(people) <= worker_ids:
                raise PlanError(f"{t.type} {t.key} has participants that are not workers")
            if t.type == "dm" and len(people) != 2:
                raise PlanError(f"dm {t.key} needs exactly 2 participants")
            if t.type == "meeting" and len(people) < 2:
                raise PlanError(f"meeting {t.key} needs at least 2 participants")
            t.participants = people
            t.risk_actions = []
            t.turn_limit = max(2, min(t.turn_limit or DEFAULT_TURNS[t.type], MAX_TURNS))
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
- Use a meeting when several workers must agree on something (you moderate it
  and record decisions/action items); use a dm when one worker needs input from
  another. Their result feeds the tasks that depend on them.
- When the Owner asks to pause, resume, cancel or change an open task, add an
  intervention with that task's id. Do not claim it took effect: it is applied
  asynchronously and confirmed separately.
- Mark a work task's risk_actions when it would deploy to production, make a
  payment, delete data or export sensitive data. The runtime then holds it until
  the Owner approves; tell the Owner that approval is needed.
- Never claim work is done unless the results below show it completed.

Project: {project_name}

Worker roster (id, name, capabilities):
{roster}

Open tasks (id, status, title):
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
        open_tasks="\n".join(f"- {t['id']} | {t['status']} | {t['title']}" for t in open_tasks)
        or "(none)",
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


CONVERSATION_TURN_PROMPT = """\
You are {name}, a worker agent in an AI project office, taking part in a {kind}.
Participants: {participants}.

{kind_title}: {title}
{agenda}

Transcript so far (oldest first):
{transcript}

Write your next contribution: concise, concrete, building on what others said.
Do not repeat yourself. Reply with your message only."""


MEETING_SUMMARY_PROMPT = """\
You moderated a meeting in an AI project office.

Meeting: {title}
Agenda: {agenda}
Participants (id, name): {participants}

Transcript (oldest first):
{transcript}

Summarize it: concise minutes, the decisions actually reached, and action items
for follow-up work (assign to a participant when one clearly owns it). Use the
language of the transcript. Respond with JSON matching the required schema."""


def render_transcript(lines: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{who}: {text}" for who, text in lines) or "(nobody has spoken yet)"
