"""Phase 3: async job adapters, artifacts, approvals, metrics (mock adapters)."""

import dataclasses
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from conftest import run
from fastapi.testclient import TestClient
from office_api.main import create_app
from office_api.models import ProjectMember
from test_phase1 import DONE, Office, key, plan, task, wait_until

JOB = {"duration_s": 0.3, "poll_s": 0.05}


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def by_title(office, title):
    return next((t for t in office.get("/tasks") if t["title"] == title), None)


def job_ref(office, title):
    t = by_title(office, title)
    return t and t["runs"] and t["runs"][0]["provider_ref"]


def decide(office, approval_id, decision, headers=None):
    return office.c.post(
        f"/projects/{office.pid}/approvals/{approval_id}/decision",
        json={"decision": decision},
        headers={**(headers or office.h), **key()},
    )


def pending_approval(office):
    return wait_until(lambda: [a for a in office.get("/approvals") if a["status"] == "pending"])[0]


def test_ac08_async_jobs_return_status_and_artifacts(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "ok",
                    [
                        task("m", "Nhạc nền", ["audio"]),
                        task("d", "Mô hình nhân vật", ["3d"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    music = office.add("Suno", "worker", {**JOB, "price_usd": 0.02}, adapter="mock-suno")
    model = office.add("Tripo", "worker", JOB, adapter="mock-tripo")
    office.say("làm asset")
    office.wait_replies(3)

    for title, agent in (("Nhạc nền", music), ("Mô hình nhân vật", model)):
        t = by_title(office, title)
        assert t["status"] == "completed"
        [r] = t["runs"]
        assert r["agent_id"] == agent and r["provider_ref"].startswith("job_")
        assert r["progress"] == 1.0
    assert by_title(office, "Nhạc nền")["runs"][0]["cost"] == 0.02
    assert "track.wav" in by_title(office, "Nhạc nền")["result_preview"]

    arts = {a["name"]: a for a in office.get("/artifacts")}
    wav, obj = arts["track.wav"], arts["model.obj"]
    assert (wav["type"], wav["mime_type"], wav["agent_id"]) == ("audio", "audio/x-wav", music)
    assert (obj["type"], obj["agent_id"], obj["version"]) == ("3d", model, 1)
    assert wav["size_bytes"] > 10_000

    r = office.c.get(f"/projects/{office.pid}/artifacts/{wav['id']}/content", headers=office.h)
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert len(r.content) == wav["size_bytes"]
    assert r.headers["content-type"] == "audio/x-wav"
    assert r.headers["content-security-policy"].startswith("sandbox")
    r = office.c.get(
        f"/projects/{office.pid}/artifacts/{obj['id']}/content?download=true", headers=office.h
    )
    assert r.text.startswith("# mock 3D model") and "attachment" in r.headers["content-disposition"]
    names = {e["payload"]["name"] for e in office.events("artifact.created")}
    assert names == {"track.wav", "model.obj"}


def test_async_job_progress_is_reported_and_cancel_reaches_provider(office):
    office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("m", "Nhạc", ["audio"])]), DONE]}
    )
    music = office.add("Suno", "worker", {"duration_s": 5, "poll_s": 0.05}, adapter="mock-suno")
    office.say("go")

    def running_job():
        agent = next(a for a in office.get("/agents") if a["id"] == music)
        runs = [r for r in agent["current_runs"] if r["provider_ref"] and r["progress"]]
        return runs and runs[0]

    current = wait_until(running_job)
    assert 0 < current["progress"] < 1

    t = by_title(office, "Nhạc")
    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": "pause", "task_id": t["id"]},
        headers={**office.h, **key()},
    )
    assert r.json()["data"]["status"] == "requested"
    wait_until(lambda: by_title(office, "Nhạc")["status"] == "paused")
    adapter = office.c.app.state.runtime._adapters[music]
    assert adapter.service.jobs[current["provider_ref"]].state == "cancelled"
    assert by_title(office, "Nhạc")["runs"][0]["status"] == "cancelled"


def test_async_job_failure_is_retried(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("d", "3D", ["3d"])]), DONE]})
    office.add("Tripo", "worker", {**JOB, "fail_first": 1}, adapter="mock-tripo")
    office.say("go")
    office.wait_replies(3)
    t = by_title(office, "3D")
    assert t["status"] == "completed"
    assert [r["status"] for r in t["runs"]] == ["failed", "succeeded"]
    assert office.events("run.failed")[0]["payload"]["error"]["code"] == "provider_error"


def test_ac09_high_risk_task_waits_for_approval(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "Cần bạn duyệt deploy",
                    [
                        task("b", "Build", ["code"]),
                        task("d", "Deploy production", ["code"], ["b"], risk=["deploy_production"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    worker = office.add("W", "worker", {"capabilities": ["code"]})
    office.say("deploy đi")

    approval = pending_approval(office)
    t = by_title(office, "Deploy production")
    assert t["status"] == "awaiting_approval" and t["runs"] == []
    assert approval["actions"] == ["deploy_production"] and approval["agent_id"] == worker
    assert approval["task_id"] == t["id"] and "Deploy production" in approval["summary"]
    [requested] = office.events("approval.requested")
    assert requested["payload"]["approval_id"] == approval["id"]

    time.sleep(0.3)  # the dispatcher keeps it blocked (AC-09)
    assert by_title(office, "Deploy production")["runs"] == []

    r = decide(office, approval["id"], "approve")
    assert r.status_code == 200 and r.json()["data"]["status"] == "approved"
    office.wait_replies(3)
    t = by_title(office, "Deploy production")
    assert t["status"] == "completed"
    used = next(a for a in office.get("/approvals") if a["id"] == approval["id"])
    assert used["run_id"] == t["runs"][0]["id"] and used["approver_id"].startswith("usr_")
    started = [e for e in office.events("task.started") if e["task_id"] == t["id"]]
    assert started[0]["payload"]["approval_id"] == approval["id"]
    assert decide(office, approval["id"], "reject").json()["error"]["code"] == (
        "approval_not_pending"
    )


def test_rejected_approval_blocks_task_and_dependents(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "ok",
                    [
                        task("x", "Xóa dữ liệu cũ", ["code"], risk=["delete_data"]),
                        task("y", "Báo cáo", ["code"], ["x"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("dọn dẹp")
    approval = pending_approval(office)
    assert decide(office, approval["id"], "reject").status_code == 200
    office.wait_replies(3)  # plan finished → orchestrator reports
    x, y = by_title(office, "Xóa dữ liệu cũ"), by_title(office, "Báo cáo")
    assert (x["status"], x["blocked_reason"]) == ("blocked", "approval_rejected")
    assert x["runs"] == [] and y["status"] == "blocked"
    resolved = office.events("approval.resolved")[0]["payload"]
    assert resolved["status"] == "rejected"


def test_worker_permission_requires_approval_even_if_plan_omits_it(office):
    office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("p", "Mua domain", ["code"])]), DONE]}
    )
    office.add("Payer", "worker", {"capabilities": ["code"], "permissions": ["payment"]})
    office.say("mua domain")
    approval = pending_approval(office)
    assert approval["actions"] == ["payment"]
    assert by_title(office, "Mua domain")["status"] == "awaiting_approval"


def test_approval_expires_and_blocks_task(settings):
    with TestClient(create_app(dataclasses.replace(settings, approval_ttl_s=0.3))) as client:
        # Own app (short TTL); the default client fixture would start a second runtime.
        from office_api.auth import create_principal_with_token

        async def _owner():
            async with client.app.state.sessionmaker() as s:
                return await create_principal_with_token(s, "o")

        _, token = run(client, _owner)
        office = Office(client, {"Authorization": f"Bearer {token}"})
        office.add(
            "O",
            "orchestrator",
            {
                "responses": [
                    plan(
                        "ok",
                        [
                            task(
                                "e",
                                "Xuất dữ liệu khách hàng",
                                ["code"],
                                risk=["export_sensitive_data"],
                            ),
                        ],
                    ),
                    DONE,
                ]
            },
        )
        office.add("W", "worker", {"capabilities": ["code"]})
        office.say("xuất dữ liệu")
        office.wait_replies(3)  # the approval expires quickly; the plan then finishes
        [approval] = office.get("/approvals")
        t = by_title(office, "Xuất dữ liệu khách hàng")
        assert (t["status"], t["blocked_reason"]) == ("blocked", "approval_expired")
        assert next(a for a in office.get("/approvals"))["status"] == "expired"
        r = decide(office, approval["id"], "approve")
        assert r.status_code == 409


def test_only_owner_of_the_project_can_decide(client, office, make_owner):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("ok", [task("d", "Deploy", ["code"], risk=["deploy_production"])]),
                DONE,
            ]
        },
    )
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("deploy")
    approval = pending_approval(office)

    observer = make_owner("observer")

    async def _join():
        from office_api.auth import authenticate

        async with client.app.state.sessionmaker() as s:
            principal = await authenticate(s, observer["Authorization"].split()[1])
            s.add(ProjectMember(project_id=office.pid, principal_id=principal.id, role="observer"))
            await s.commit()

    run(client, _join)
    assert decide(office, approval["id"], "approve", observer).status_code == 403

    other = Office(client, make_owner("stranger"))
    r = client.post(
        f"/projects/{other.pid}/approvals/{approval['id']}/decision",
        json={"decision": "approve"},
        headers={**other.h, **key()},
    )
    assert r.status_code == 404  # BR-04: an approval is only reachable through its project
    assert decide(other, approval["id"], "approve").status_code == 404


def test_stopping_or_redirecting_withdraws_approval(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "ok",
                    [
                        task("a", "Deploy A", ["code"], risk=["deploy_production"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("deploy")
    first = pending_approval(office)
    t = by_title(office, "Deploy A")

    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": "redirect", "task_id": t["id"], "instructions": "chỉ deploy staging"},
        headers={**office.h, **key()},
    )
    assert r.json()["data"]["status"] == "applied"
    second = wait_until(
        lambda: [
            a
            for a in office.get("/approvals")
            if a["status"] == "pending" and a["id"] != first["id"]
        ]
    )[0]
    statuses = {a["id"]: a["status"] for a in office.get("/approvals")}
    assert statuses[first["id"]] == "cancelled"
    assert "staging" in second["summary"]

    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": "cancel", "task_id": t["id"]},
        headers={**office.h, **key()},
    )
    assert r.json()["data"]["status"] == "applied"
    assert {a["status"] for a in office.get("/approvals")} == {"cancelled"}
    assert by_title(office, "Deploy A")["status"] == "cancelled"


def test_metrics_report_cost_runs_and_governance(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "ok",
                    [
                        task("m", "Nhạc", ["audio"]),
                        task("c", "Code", ["code"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    music = office.add("Suno", "worker", {**JOB, "price_usd": 0.05}, adapter="mock-suno")
    coder = office.add("W", "worker", {"capabilities": ["code"], "fail_first": 1, "cost_usd": 0.01})
    office.say("go")
    office.wait_replies(3)

    m = office.get("/metrics")
    assert m["cost_usd"] == pytest.approx(0.06)
    assert m["tasks_by_status"] == {"completed": 2}
    assert m["run_failures"] == 1 and m["retries"] == 1
    assert m["artifacts_by_type"] == {"audio": 1}
    agents = {a["agent_id"]: a for a in m["agents"]}
    assert agents[music]["cost_usd"] == 0.05 and agents[music]["runs"] == 1
    assert agents[coder]["runs_by_status"] == {"failed": 1, "succeeded": 1}
    assert agents[coder]["cost_usd"] == 0.01
    orch = next(a for a in m["agents"] if a["runs_by_purpose"].get("orchestrator_turn"))
    assert orch["runs_without_cost"] == 2  # mock orchestrator reports no cost: not shown as $0


def test_artifact_content_is_project_scoped_and_checksummed(client, office, make_owner):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("d", "3D", ["3d"])]), DONE]})
    office.add("Tripo", "worker", JOB, adapter="mock-tripo")
    office.say("go")
    office.wait_replies(3)
    [art] = office.get("/artifacts")

    other = Office(client, office.h)  # same owner, another project
    r = client.get(f"/projects/{other.pid}/artifacts/{art['id']}/content", headers=office.h)
    assert r.status_code == 404
    stranger = make_owner("stranger")
    r = client.get(f"/projects/{office.pid}/artifacts/{art['id']}/content", headers=stranger)
    assert r.status_code == 404

    Path(unquote(urlparse(art["uri"]).path)).write_text("tampered")
    r = client.get(f"/projects/{office.pid}/artifacts/{art['id']}/content", headers=office.h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "artifact_changed"


def _set_config(settings, agent_id, config):
    db = sqlite3.connect(settings.database_url.split("///")[1])
    db.execute(
        "UPDATE agent_definitions SET config = ? WHERE id = ?", (json.dumps(config), agent_id)
    )
    db.commit()
    db.close()


def test_restart_reattaches_running_provider_job(settings, make_owner, client):
    """AC-10 for async jobs: the provider job keeps running while the backend is
    down; the next process re-attaches by provider_ref instead of resubmitting."""
    office = Office(client, make_owner())
    orch = office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("m", "Nhạc", ["audio"])])]}
    )
    music = office.add("Suno", "worker", {"duration_s": 1.5, "poll_s": 0.05}, adapter="mock-suno")
    office.say("go")
    job = wait_until(lambda: job_ref(office, "Nhạc"))
    client.__exit__(None, None, None)  # crash while the job runs
    _set_config(settings, orch, {"responses": [DONE]})

    with TestClient(create_app(settings)) as restarted:
        office.c = restarted
        office.wait_replies(3)
        t = by_title(office, "Nhạc")
        assert t["status"] == "completed"
        [r] = t["runs"]  # same run, same provider job: nothing resubmitted
        assert (r["status"], r["provider_ref"]) == ("succeeded", job)
        assert [a["name"] for a in office.get("/artifacts")] == ["track.wav"]
        assert not office.events("run.failed")
        adapter = restarted.app.state.runtime._adapters[music]
        assert list(adapter.service.jobs) == [job]


def test_restart_with_unknown_job_falls_back_to_retry(settings, make_owner, client):
    office = Office(client, make_owner())
    orch = office.add("O", "orchestrator", {"responses": [plan("ok", [task("d", "3D", ["3d"])])]})
    office.add("Tripo", "worker", {"duration_s": 1.5, "poll_s": 0.05}, adapter="mock-tripo")
    office.say("go")
    wait_until(lambda: job_ref(office, "3D"))
    client.__exit__(None, None, None)
    _set_config(settings, orch, {"responses": [DONE]})
    for record in Path(settings.data_dir).rglob(".mockjob-*.json"):
        record.unlink()  # the provider lost the job

    with TestClient(create_app(settings)) as restarted:
        office.c = restarted
        office.wait_replies(3)
        t = by_title(office, "3D")
        assert t["status"] == "completed"
        assert [r["status"] for r in t["runs"]] == ["failed", "succeeded"]
        assert t["runs"][0]["error"]["code"] == "job_not_found"
