"""Owner-configurable default policies (office-policy.toml, ADR-0007)."""

import dataclasses
import os
import time
from pathlib import Path

import pytest
from conftest import create_project, run
from office_api import retention
from office_api.models import AuditLog
from office_api.policy import Policy, load_policy
from sqlalchemy import select
from test_phase1 import DONE, Office, key, plan, task, wait_until
from test_phase2 import intervene
from test_phase4 import _add_observer

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def policy():
    return Policy()


@pytest.fixture
def settings(settings, policy):
    return dataclasses.replace(settings, policy=policy)


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def test_policy_file_shipped_with_repo_matches_builtin_defaults():
    assert load_policy(str(ROOT / "office-policy.toml")) == Policy()


def test_policy_file_missing_means_defaults_and_bad_keys_stop_startup(tmp_path):
    assert load_policy(str(tmp_path / "nope.toml")) == load_policy("") == Policy()
    bad = tmp_path / "bad.toml"
    bad.write_text("[transcripts]\nprivate_reader = ['owner']\n")
    with pytest.raises(SystemExit, match="private_reader"):
        load_policy(str(bad))
    bad.write_text("[interventions]\nobserver_actions = ['delete']\n")
    with pytest.raises(SystemExit):
        load_policy(str(bad))


def _run_dm(office):
    w1 = office.add(
        "An", "worker", {"capabilities": ["chat"], "responses": [{"text": "bí mật An"}]}
    )
    w2 = office.add(
        "Bình", "worker", {"capabilities": ["chat"], "responses": [{"text": "bí mật B"}]}
    )
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("DM", [task("d", "Hỏi", [], type_="dm", participants=[w1, w2], turns=2)]),
                DONE,
            ]
        },
    )
    office.say("go")
    office.wait_replies(3)
    return next(c for c in office.get("/conversations") if c["type"] == "dm")


def _dm_events(events, dm):
    return [e for e in events if e["type"] == "message.sent" and e["conversation_id"] == dm["id"]]


def test_private_dm_transcript_owner_audits_observer_sees_only_metadata(client, office, make_owner):
    dm = _run_dm(office)
    observer = _add_observer(client, office, make_owner)

    assert [m["content"] for m in office.get(f"/conversations/{dm['id']}/messages")] == [
        "bí mật An",
        "bí mật B",
    ]
    owner_events = _dm_events(office.events(), dm)
    assert [e["payload"]["preview"] for e in owner_events] == ["bí mật An", "bí mật B"]
    assert all(e["payload"]["private"] for e in owner_events)

    r = client.get(f"/projects/{office.pid}/conversations/{dm['id']}/messages", headers=observer)
    assert r.status_code == 403 and r.json()["error"]["code"] == "transcript_private"
    r = client.get(f"/projects/{office.pid}/events?limit=1000", headers=observer)
    hidden = _dm_events(r.json()["events"], dm)
    assert [e["actor_id"] for e in hidden] == [e["actor_id"] for e in owner_events]
    assert all("preview" not in e["payload"] and "content_ref" not in e["payload"] for e in hidden)

    # The realtime stream applies the same rule.
    last = r.json()["next_after_seq"]
    token = observer["Authorization"].split()[1]
    with client.websocket_connect(f"/projects/{office.pid}/stream?token={token}") as ws:
        streamed = []
        while not streamed or streamed[-1]["sequence"] < last:
            streamed.append(ws.receive_json())
    assert _dm_events(streamed, dm) == hidden

    # Owner chat and other non-private conversations stay readable for observers.
    owner_chat = next(c for c in office.get("/conversations") if c["type"] == "owner_chat")
    path = f"/projects/{office.pid}/conversations/{owner_chat['id']}/messages"
    assert client.get(path, headers=observer).status_code == 200


@pytest.mark.parametrize("policy", [Policy(transcripts={"private_readers": []})])
def test_private_readers_empty_hides_dm_from_owner_too(office):
    dm = _run_dm(office)
    r = office.c.get(f"/projects/{office.pid}/conversations/{dm['id']}/messages", headers=office.h)
    assert r.status_code == 403
    assert all("preview" not in e["payload"] for e in _dm_events(office.events(), dm))


@pytest.mark.parametrize("policy", [Policy(interventions={"observer_actions": ["pause"]})])
def test_observer_may_request_only_the_configured_interventions(client, office, make_owner):
    observer = _add_observer(client, office, make_owner)
    path = f"/projects/{office.pid}/interventions"
    r = client.post(path, json={"action": "pause"}, headers={**observer, **key()})
    assert r.status_code == 202 and r.json()["data"]["status"] == "applied"
    r = client.post(path, json={"action": "resume"}, headers={**observer, **key()})
    assert r.status_code == 403
    assert intervene(office, "resume")["status"] == "applied"


@pytest.mark.parametrize("policy", [Policy(budget={"default_max_cost_usd": 2.5})])
def test_default_budget_applies_only_to_projects_created_without_one(client, make_owner):
    owner = make_owner()
    r = create_project(client, owner)
    assert r.json()["data"]["budget"]["max_cost_usd"] == 2.5
    r = client.post(
        "/projects",
        json={"name": "Free", "budget": {"max_cost_usd": None}},
        headers={**owner, **key()},
    )
    assert r.json()["data"]["budget"]["max_cost_usd"] is None


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (old, old))


def test_retention_prunes_old_content_of_idle_projects_only(client, office, make_owner):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]})
    office.add(
        "W",
        "worker",
        {"capabilities": ["code"], "responses": [{"text": "xong", "files": {"a.py": "x"}}]},
    )
    office.say("go")
    office.wait_replies(3)
    [artifact] = office.get("/artifacts")

    # A second project with work still pending must not be touched.
    busy = Office(client, office.h)
    busy.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]})
    intervene(busy, "pause")
    busy.say("go")
    wait_until(lambda: busy.work_tasks())

    runtime = client.app.state.runtime
    data = Path(runtime.settings.data_dir)
    for p in (data / "objects").iterdir():
        _age(p, 3)
    for p in (data / "workspaces").iterdir():
        _age(p, 3)
    settings = dataclasses.replace(
        runtime.settings, policy=Policy(retention={"content_days": 2, "workspace_days": 2})
    )

    async def _sweep(dry_run):
        async with client.app.state.sessionmaker() as s:
            return await retention.sweep(s, settings, runtime.store, dry_run=dry_run)

    [would] = run(client, _sweep, True)
    assert would.project_id == office.pid and would.objects and would.workspaces
    assert all(p.exists() for p in would.objects + would.workspaces)

    [done] = run(client, _sweep, False)
    assert done.project_id == office.pid
    assert not any(p.exists() for p in done.objects + done.workspaces)

    assert all(m["content"] is None for m in office.owner_chat())
    assert [m["content"] for m in busy.owner_chat()] == ["go", "ok"]
    r = client.get(f"/projects/{office.pid}/artifacts/{artifact['id']}/content", headers=office.h)
    assert r.status_code == 410
    assert office.events()  # events are never pruned

    async def _audit():
        async with client.app.state.sessionmaker() as s:
            return (
                await s.scalars(select(AuditLog).where(AuditLog.action == "retention.prune"))
            ).all()

    [entry] = run(client, _audit)
    assert entry.project_id == office.pid and entry.principal == "system"
    assert run(client, _sweep, False) == []  # nothing left to prune
