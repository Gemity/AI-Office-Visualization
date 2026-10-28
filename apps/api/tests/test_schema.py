import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_project, run
from office_api.db import new_id
from office_api.models import (
    AgentDefinition,
    Conversation,
    Message,
    OrchestratorAssignment,
    ProjectAgent,
)
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).parents[3]


def _insert(client, *rows):
    async def _do():
        async with client.app.state.sessionmaker() as session:
            for row in rows:
                session.add(row)
                await session.flush()
            await session.commit()

    run(client, _do)


def _agent(project_id: str) -> tuple[AgentDefinition, ProjectAgent]:
    agent = AgentDefinition(
        id=new_id("agt"),
        name="orch",
        provider="mock",
        adapter_type="mock",
        capability_manifest={},
    )
    return agent, ProjectAgent(project_id=project_id, agent_id=agent.id, role="orchestrator")


def test_br01_single_active_orchestrator(client, make_owner):
    pid = create_project(client, make_owner()).json()["data"]["id"]
    a1, pa1 = _agent(pid)
    a2, pa2 = _agent(pid)
    _insert(client, a1, a2, pa1, pa2)
    _insert(
        client, OrchestratorAssignment(id=new_id("orc"), project_id=pid, agent_id=a1.id, version=1)
    )
    with pytest.raises(IntegrityError):
        _insert(
            client,
            OrchestratorAssignment(id=new_id("orc"), project_id=pid, agent_id=a2.id, version=2),
        )


def test_br04_rows_cannot_reference_other_project(client, make_owner):
    owner = make_owner()
    a = create_project(client, owner, "A").json()["data"]["id"]
    b = create_project(client, owner, "B").json()["data"]["id"]
    conv = Conversation(
        id=new_id("conv"), project_id=a, type="dm", visibility="participants", created_by="x"
    )
    _insert(client, conv)
    with pytest.raises(IntegrityError):
        _insert(
            client,
            Message(
                id=new_id("msg"),
                project_id=b,
                conversation_id=conv.id,
                sender_id="x",
                content_ref="obj://m",
            ),
        )


def test_generated_contracts_are_up_to_date():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/gen_contracts.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"run scripts/gen_contracts.py\n{r.stdout}{r.stderr}"
