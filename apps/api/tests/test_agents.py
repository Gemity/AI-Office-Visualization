"""Editing and removing workers; adapter config documentation (ADR-0008)."""

import pytest
from test_phase1 import DONE, Office, key, plan, task, wait_until


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def _agent(office, agent_id):
    return next(a for a in office.get("/agents") if a["id"] == agent_id)


def test_adapters_document_their_config_and_capabilities_are_listed(office):
    adapters = {a["adapter_type"]: a for a in office.c.get("/adapters", headers=office.h).json()}
    assert "claude-cli" in adapters and "claude-agent-sdk" not in adapters
    keys = {f["key"] for f in adapters["claude-cli"]["config_fields"]}
    assert {"model", "effort", "system_prompt", "capabilities"} <= keys
    effort = next(f for f in adapters["claude-cli"]["config_fields"] if f["key"] == "effort")
    assert effort["choices"] == ["low", "medium", "high", "xhigh", "max"]
    # Job adapters have fixed capabilities: no 'capabilities' key to edit.
    assert "capabilities" not in {f["key"] for f in adapters["mock-suno"]["config_fields"]}
    caps = office.c.get("/capabilities", headers=office.h).json()
    assert {"chat", "code", "audio", "3d"} <= set(caps)


def test_edit_worker_changes_name_config_and_capabilities_for_next_runs(office):
    office.add(
        "O",
        "orchestrator",
        {"responses": [plan("ok", [task("t", "Vẽ", ["image"])]), DONE]},
    )
    w = office.add("W", "worker", {"capabilities": ["code"], "responses": [{"text": "cũ"}]})
    r = office.c.patch(
        f"/projects/{office.pid}/agents/{w}",
        json={
            "name": "Hoạ sĩ",
            "config": {"capabilities": ["image"], "responses": [{"text": "mới"}]},
        },
        headers={**office.h, **key()},
    )
    assert r.status_code == 200, r.json()
    a = _agent(office, w)
    assert a["name"] == "Hoạ sĩ" and a["capabilities"] == ["image"]
    assert a["config"]["responses"] == [{"text": "mới"}]
    [ev] = office.events("agent.updated")
    assert ev["payload"]["changed"] == ["name", "config"]

    office.say("vẽ đi")
    office.wait_replies(3)
    [t] = office.work_tasks()
    assert t["status"] == "completed" and t["assignee_id"] == w
    assert "mới" in t["result_preview"]  # the runtime rebuilt the adapter from the new config

    bad = office.c.patch(
        f"/projects/{office.pid}/agents/{w}",
        json={"config": {"capabilities": ["not-a-capability"]}},
        headers={**office.h, **key()},
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_agent_config"


def test_remove_worker_keeps_history_and_stops_new_work(office):
    office.add(
        "O",
        "orchestrator",
        {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]},
    )
    w = office.add("W", "worker", {"capabilities": ["code"]})
    r = office.c.delete(f"/projects/{office.pid}/agents/{w}", headers={**office.h, **key()})
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False
    assert _agent(office, w)["enabled"] is False  # still listed: history needs the name
    assert office.events("agent.removed")[0]["payload"]["agent_id"] == w

    office.say("làm đi")
    wait_until(lambda: office.work_tasks() and office.work_tasks()[0]["status"] == "blocked")
    assert office.work_tasks()[0]["blocked_reason"] == "no_capable_worker"

    again = office.c.delete(f"/projects/{office.pid}/agents/{w}", headers={**office.h, **key()})
    assert again.status_code == 404


def test_orchestrator_and_busy_workers_cannot_be_edited_or_removed(office):
    script = {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]}
    o = office.add("O", "orchestrator", script)
    w = office.add("W", "worker", {"capabilities": ["code"], "delay_s": 1.5})
    r = office.c.delete(f"/projects/{office.pid}/agents/{o}", headers={**office.h, **key()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "orchestrator_managed"

    office.say("go")
    wait_until(lambda: _agent(office, w)["current_runs"])
    r = office.c.patch(
        f"/projects/{office.pid}/agents/{w}", json={"name": "X"}, headers={**office.h, **key()}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "agent_busy"
    r = office.c.delete(f"/projects/{office.pid}/agents/{w}", headers={**office.h, **key()})
    assert r.status_code == 409
    office.wait_replies(3)
    assert _agent(office, w)["name"] == "W"
