"""Phase 1: agents, Owner ↔ Orchestrator chat, task runtime (mock adapters)."""

import itertools
import json
import sqlite3
import time
from datetime import datetime

import pytest
from conftest import create_project, run
from fastapi.testclient import TestClient
from office_api.main import create_app
from office_api.models import ProjectMember

_keys = itertools.count()


def key() -> dict[str, str]:
    return {"Idempotency-Key": f"p1-{next(_keys)}"}


def wait_until(fn, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(interval)


def task(key_, title, caps, deps=(), assignee=None, type_="work", participants=None, turns=None):
    return {
        "key": key_,
        "type": type_,
        "title": title,
        "instructions": f"do {title}",
        "capabilities": caps,
        "depends_on": list(deps),
        "assignee_id": assignee,
        "participants": participants,
        "turn_limit": turns,
    }


def plan(reply, tasks=(), title="Plan", interventions=()):
    return {
        "structured": {
            "reply": reply,
            "plan_title": title if tasks else None,
            "tasks": list(tasks),
            "interventions": list(interventions),
        }
    }


DONE = plan("Đã xong")


class Office:
    def __init__(self, client, headers):
        self.c, self.h = client, headers
        self.pid = create_project(client, headers).json()["data"]["id"]

    def add(self, name, role, config, adapter="mock"):
        r = self.c.post(
            f"/projects/{self.pid}/agents",
            json={"name": name, "adapter_type": adapter, "role": role, "config": config},
            headers={**self.h, **key()},
        )
        assert r.status_code == 201, r.json()
        return r.json()["data"]["id"]

    def say(self, text):
        return self.c.post(
            f"/projects/{self.pid}/orchestrator/messages",
            json={"content": text},
            headers={**self.h, **key()},
        )

    def get(self, path):
        r = self.c.get(f"/projects/{self.pid}{path}", headers=self.h)
        assert r.status_code == 200, r.json()
        return r.json()

    def work_tasks(self):
        return [t for t in self.get("/tasks") if t["kind"] == "work"]

    def events(self, type_=None):
        events = self.get("/events?limit=1000")["events"]
        return [e for e in events if type_ is None or e["type"] == type_]

    def owner_chat(self):
        conv = next(c for c in self.get("/conversations") if c["type"] == "owner_chat")
        return self.get(f"/conversations/{conv['id']}/messages")

    def wait_replies(self, n):
        return wait_until(lambda: len(self.owner_chat()) >= n and self.owner_chat())


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_ac02_ac03_plan_runs_in_parallel_and_orchestrator_reports(office):
    orch = office.add(
        "Orchestrator",
        "orchestrator",
        {
            "responses": [
                plan(
                    "Tôi chia làm 3 việc",
                    [
                        task("t1", "Viết code", ["code"]),
                        task("t2", "Soạn nhạc", ["audio"]),
                        task("t3", "Ghép lại", ["code"], deps=["t1", "t2"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    coder = office.add(
        "Coder",
        "worker",
        {
            "capabilities": ["code"],
            "delay_s": 0.4,
            "responses": [{"text": "ok", "files": {"main.py": "x"}}],
        },
    )
    composer = office.add("Composer", "worker", {"capabilities": ["audio", "chat"], "delay_s": 0.4})

    r = office.say("Làm game nhỏ có nhạc")
    assert r.status_code == 202 and r.json()["status"] == "accepted"

    chat = office.wait_replies(3)
    assert [m["sender_id"] for m in chat[1:]] == [orch, orch]  # AC-02: only the orchestrator
    assert chat[1]["content"] == "Tôi chia làm 3 việc"
    assert chat[2]["content"] == "Đã xong"

    tasks = {t["title"]: t for t in office.work_tasks()}
    assert {t["status"] for t in tasks.values()} == {"completed"}
    t1, t2, t3 = tasks["Viết code"], tasks["Soạn nhạc"], tasks["Ghép lại"]
    assert (t1["assignee_id"], t2["assignee_id"], t3["assignee_id"]) == (coder, composer, coder)
    assert sorted(t3["depends_on"]) == sorted([t1["id"], t2["id"]])

    # AC-03: t1 and t2 overlapped in time; t3 started after both finished.
    r1, r2, r3 = t1["runs"][0], t2["runs"][0], t3["runs"][0]
    assert _ts(r1["started_at"]) < _ts(r2["finished_at"])
    assert _ts(r2["started_at"]) < _ts(r1["finished_at"])
    assert _ts(r3["started_at"]) >= max(_ts(r1["finished_at"]), _ts(r2["finished_at"]))

    # Workers' messages go to task threads, never to the owner chat.
    threads = [c for c in office.get("/conversations") if c["type"] == "task_thread"]
    assert len(threads) == 3
    artifacts = office.get("/artifacts")
    assert {a["task_id"] for a in artifacts} == {t1["id"], t3["id"]}
    assert all(a["uri"].endswith("main.py") for a in artifacts)

    types = [e["type"] for e in office.events()]
    for t in ("task.created", "task.started", "task.completed", "artifact.created", "message.sent"):
        assert t in types
    seqs = [e["sequence"] for e in office.events()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_ac07_failed_run_is_retried(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("t1", "A", ["code"])]), DONE]})
    office.add("W", "worker", {"capabilities": ["code"], "fail_first": 1})
    office.say("go")
    office.wait_replies(3)
    [t] = office.work_tasks()
    assert t["status"] == "completed" and t["attempts"] == 2
    assert [r["status"] for r in t["runs"]] == ["failed", "succeeded"]
    assert t["runs"][0]["error"]["code"] == "mock_failure"
    [failed] = office.events("run.failed")
    assert failed["payload"]["will_retry"] is True


def test_exhausted_retries_fail_task_and_block_dependents(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("ok", [task("a", "A", ["code"]), task("b", "B", ["code"], ["a"])]),
                DONE,
            ]
        },
    )
    office.add("W", "worker", {"capabilities": ["code"], "fail_first": 99})
    office.say("go")
    office.wait_replies(3)
    tasks = {t["title"]: t for t in office.work_tasks()}
    assert tasks["A"]["status"] == "failed" and tasks["A"]["attempts"] == 3
    assert tasks["B"]["status"] == "blocked"
    assert tasks["B"]["blocked_reason"].startswith("dependency_failed")
    assert [e["payload"]["will_retry"] for e in office.events("run.failed")] == [True, True, False]
    plan_task = next(t for t in office.get("/tasks") if t["kind"] == "plan")
    assert plan_task["status"] == "failed"


def test_br03_no_capable_worker_blocks_task(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("m", "Model", ["3d"])]), DONE]})
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("make a 3d model")
    office.wait_replies(3)
    [t] = office.work_tasks()
    assert (t["status"], t["blocked_reason"]) == ("blocked", "no_capable_worker")
    assert office.events("task.blocked")[0]["payload"]["reason"] == "no_capable_worker"


def test_invalid_plan_is_retried(office):
    cyclic = plan("x", [task("a", "A", ["code"], ["b"]), task("b", "B", ["code"], ["a"])])
    office.add("O", "orchestrator", {"responses": [cyclic, plan("Chào anh")]})
    office.say("hi")
    chat = office.wait_replies(2)
    assert chat[1]["content"] == "Chào anh"
    [failed] = office.events("run.failed")
    assert failed["payload"]["error"]["code"] == "invalid_plan"
    assert office.work_tasks() == []


def test_br01_new_orchestrator_replaces_active_one(office):
    first = office.add("O1", "orchestrator", {})
    second = office.add("O2", "orchestrator", {})
    agents = {a["id"]: a for a in office.get("/agents")}
    assert not agents[first]["is_active_orchestrator"]
    assert agents[second]["is_active_orchestrator"]
    assigned = office.events("agent.assigned")
    assert [e["payload"].get("version") for e in assigned] == [1, 2]
    assert assigned[1]["payload"]["replaced_agent_id"] == first


def test_owner_message_requires_orchestrator_and_owner_role(client, office, make_owner):
    r = office.say("hello")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_orchestrator"

    observer = make_owner("observer")

    async def _join():
        from office_api.auth import authenticate

        async with client.app.state.sessionmaker() as s:
            principal = await authenticate(s, observer["Authorization"].split()[1])
            s.add(ProjectMember(project_id=office.pid, principal_id=principal.id, role="observer"))
            await s.commit()

    run(client, _join)
    r = client.post(
        f"/projects/{office.pid}/orchestrator/messages",
        json={"content": "hi"},
        headers={**observer, **key()},
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert client.get(f"/projects/{office.pid}/tasks", headers=observer).status_code == 200


def test_invalid_agent_config_rejected(office):
    r = office.c.post(
        f"/projects/{office.pid}/agents",
        json={
            "name": "x",
            "adapter_type": "codex-cli",
            "role": "worker",
            "config": {"sandbox": "danger-full-access"},
        },
        headers={**office.h, **key()},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_agent_config"
    r = office.c.post(
        f"/projects/{office.pid}/agents",
        json={"name": "x", "adapter_type": "nope", "role": "worker"},
        headers={**office.h, **key()},
    )
    assert r.status_code == 422


def test_ac10_restart_recovers_interrupted_run(settings, make_owner, client):
    office = Office(client, make_owner())
    orch = office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("t", "Slow", ["code"])])]}
    )
    worker = office.add("W", "worker", {"capabilities": ["code"], "delay_s": 60})
    office.say("go")
    wait_until(lambda: [t for t in office.work_tasks() if t["status"] == "active"])
    client.__exit__(None, None, None)  # "crash": the run never finishes in this process

    # Mock adapters keep their script position in memory only; make the next
    # process finish quickly and have the orchestrator just report.
    db = sqlite3.connect(settings.database_url.split("///")[1])
    for agent_id, config in (
        (worker, {"capabilities": ["code"], "delay_s": 0.05}),
        (orch, {"responses": [DONE]}),
    ):
        db.execute(
            "UPDATE agent_definitions SET config = ? WHERE id = ?", (json.dumps(config), agent_id)
        )
    db.commit()
    db.close()

    with TestClient(create_app(settings)) as restarted:
        office.c = restarted
        office.wait_replies(3)
        [t] = office.work_tasks()
        assert t["status"] == "completed"
        assert [r["status"] for r in t["runs"]] == ["failed", "succeeded"]
        assert t["runs"][0]["error"]["code"] == "interrupted"
