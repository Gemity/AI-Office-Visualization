"""Phase 4: budgets / circuit breaker, sandbox policy (mock adapters)."""

import pytest
from office_api.storage import sandbox_policy
from test_phase1 import DONE, Office, key, plan, task, wait_until


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def by_title(office, title):
    return next((t for t in office.get("/tasks") if t["title"] == title), None)


def set_budget(office, body, headers=None):
    return office.c.put(
        f"/projects/{office.pid}/budget", json=body, headers={**(headers or office.h), **key()}
    )


def intervene(office, action, task_id=None):
    return office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": action, "task_id": task_id},
        headers={**office.h, **key()},
    )


def test_project_budget_trips_circuit_breaker_until_raised(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [
        task("a", "A", ["code"]),
        task("b", "B", ["code"], ["a"]),
        task("c", "C", ["code"], ["b"]),
    ]), DONE]})  # fmt: skip
    office.add("W", "worker", {"capabilities": ["code"], "cost_usd": 0.02})
    assert set_budget(office, {"max_cost_usd": 0.03}).status_code == 200
    office.say("go")

    wait_until(lambda: office.get("")["status"] == "paused")
    assert [by_title(office, t)["status"] for t in "ABC"] == ["completed", "completed", "pending"]
    [iv] = office.get("/interventions")
    assert (iv["requested_by"], iv["action"], iv["status"]) == ("system", "pause", "applied")
    assert iv["outcome"].startswith("budget_exceeded: spent $0.0400 of $0.0300")
    m = office.get("/metrics")
    assert m["over_budget"] and m["budget"]["max_cost_usd"] == 0.03

    r = intervene(office, "resume")
    assert r.status_code == 409 and r.json()["error"]["code"] == "budget_exceeded"
    assert set_budget(office, {"max_cost_usd": 1.0}).status_code == 200
    assert intervene(office, "resume").json()["data"]["status"] == "applied"
    office.wait_replies(3)
    assert by_title(office, "C")["status"] == "completed"


def test_agent_budget_excludes_agent_and_blocks_when_no_one_left(office):
    cheap = office.add("Cheap", "worker", {"capabilities": ["code"]})
    pricey = office.add("Pricey", "worker", {"capabilities": ["code", "audio"]})
    office.add("O", "orchestrator", {"responses": [plan("ok", [
        task("c", "Code", ["code"], assignee=pricey),
        task("m", "Music", ["audio"]),
    ]), DONE]})  # fmt: skip
    set_budget(office, {"agents": {pricey: 0}})
    office.say("go")
    office.wait_replies(3)
    code, music = by_title(office, "Code"), by_title(office, "Music")
    assert code["status"] == "completed" and code["runs"][0]["agent_id"] == cheap
    assert (music["status"], music["blocked_reason"]) == ("blocked", "budget_exceeded")


def test_budget_is_validated_and_owner_only(client, office, make_owner):
    assert set_budget(office, {"max_cost_usd": -1}).status_code == 422
    assert set_budget(office, {"max_cost_usd": 1, "bogus": 2}).status_code == 422
    stranger = make_owner("stranger")
    assert set_budget(office, {"max_cost_usd": 1}, stranger).status_code == 404
    r = client.post(
        "/projects",
        json={"name": "B", "budget": {"max_cost_usd": "lots"}},
        headers={**office.h, **key()},
    )
    assert r.status_code == 422


def test_sandbox_policy_hides_office_data_but_exposes_own_workspace(settings, tmp_path):
    policy = sandbox_policy(settings, "prj_a", tmp_path / "w")
    assert policy.mode == "bwrap"
    data = str((tmp_path / "data").resolve())
    db = str((tmp_path / "test.db").resolve())
    assert data in policy.hidden and db in policy.hidden and f"{db}-wal" in policy.hidden
    assert "~/.ssh" in policy.hidden
    assert policy.readable == [f"{data}/workspaces/prj_a"]
    assert policy.writable == [str(tmp_path / "w")]


def _sync_url(settings):
    return settings.database_url.replace("+aiosqlite", "")


def test_migrations_match_models_exactly(settings):
    """Schema comes from Alembic (head) and must equal models.py: any model change
    needs `office-api makemigration` (ADR-0006)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from office_api import migrations
    from office_api.db import Base
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(settings))
    with engine.begin() as conn:
        migrations.upgrade(conn)
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0001"
    engine.dispose()


def test_pre_migration_database_is_refused(settings):
    from office_api import migrations
    from office_api.db import Base
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(settings))
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        with pytest.raises(RuntimeError, match="before migrations"):
            migrations.upgrade(conn)
    engine.dispose()


def test_backup_contains_consistent_db_and_data(client, office, settings):
    import sqlite3
    import tarfile

    from office_api.cli import backup

    office.add("W", "worker", {"capabilities": ["code"]})
    archive = backup(settings, keep=2)
    backup(settings, keep=2)
    backup(settings, keep=2)
    assert len(list(archive.parent.glob("office-*.tar.gz"))) <= 2
    latest = sorted(archive.parent.glob("office-*.tar.gz"))[-1]
    assert oct(latest.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(latest) as tar:
        names = tar.getnames()
        assert "office.db" in names and not any(n.startswith("data/backups") for n in names)
        tar.extract("office.db", settings.data_dir + "/restore", filter="data")
    db = sqlite3.connect(settings.data_dir + "/restore/office.db")
    assert db.execute("SELECT count(*) FROM agent_definitions").fetchone()[0] == 1
    db.close()


def test_tokens_are_redacted_from_logs():
    import logging

    from office_api.logs import RedactingFilter, redact

    token = "po_Xd8ez3hWLwQ7664IBW28qBUv3gH9vYAK-lLiQyiV2go"
    assert token not in redact(f"GET /projects/p/stream?token={token}&after_seq=3")
    assert "after_seq=3" in redact(f"/x?token={token}&after_seq=3")
    assert token not in redact(f"Authorization: Bearer {token}")
    assert token not in redact(f"leaked {token} here")
    record = logging.LogRecord(
        "uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/p?token={token}", "1.1", 200), None,
    )  # fmt: skip
    RedactingFilter().filter(record)
    assert token not in record.getMessage() and len(record.args) == 5


def test_security_headers_and_readiness(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/readyz").json() == {"status": "ready"}
    page = client.get("/")
    assert page.status_code == 200 and "<script>" not in page.text  # CSP: no inline scripts


def _project_routes(client):
    """Every ``/projects/{project_id}/...`` operation in the OpenAPI document."""
    for path, ops in client.app.openapi()["paths"].items():
        if path.startswith("/projects/{project_id}"):
            for method in ops:
                yield method, path


def _add_observer(client, office, make_owner):
    from conftest import run
    from office_api.auth import authenticate
    from office_api.models import ProjectMember

    observer = make_owner("observer")

    async def _add():
        async with client.app.state.sessionmaker() as s:
            principal = await authenticate(s, observer["Authorization"].split()[1])
            s.add(ProjectMember(project_id=office.pid, principal_id=principal.id, role="observer"))
            await s.commit()

    run(client, _add)
    return observer


def test_every_project_route_enforces_membership_and_roles(client, office, make_owner):
    """BR-04 sweep: no route under /projects/{id} is reachable cross-project, and
    observers can read but never write (spec §13)."""
    stranger = make_owner("stranger")
    observer = _add_observer(client, office, make_owner)
    routes = list(_project_routes(client))
    assert len(routes) >= 17  # sweep must not silently shrink

    for method, path in routes:
        url = path.replace("{project_id}", office.pid)
        url = url.replace("{", "").replace("}", "")  # other ids: placeholders that do not exist

        def call(h, method=method, url=url):
            return client.request(method, url, headers={**h, **key()}, json={})

        assert call(stranger).status_code == 404, (method, path)
        assert call({}).status_code == 401, (method, path)
        r = call(observer)
        if method == "get":
            assert r.status_code not in (401, 403), (method, path)
            if path.count("{") == 1:
                assert r.status_code == 200, (method, path, r.text)
        else:
            assert r.status_code == 403, (method, path)


def test_stream_requires_member_token(client, office, make_owner):
    from starlette.websockets import WebSocketDisconnect

    stranger = make_owner("stranger")["Authorization"].split()[1]
    for token, code in [("po_bogus", 4401), (stranger, 4404)]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/projects/{office.pid}/stream?token={token}") as ws:
                ws.receive_json()
        assert exc.value.code == code
