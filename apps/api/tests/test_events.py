import json
from pathlib import Path

import jsonschema
import pytest
from conftest import create_project, run
from office_api.events import append_event, commit_and_publish
from office_contracts import EventType
from starlette.websockets import WebSocketDisconnect

SCHEMA = json.loads(
    (Path(__file__).parents[3] / "packages/contracts/schemas/event.schema.json").read_text()
)


def emit(client, project_id: str, n: int = 1) -> None:
    async def _emit():
        async with client.app.state.sessionmaker() as session:
            for i in range(n):
                await append_event(
                    session,
                    project_id=project_id,
                    type=EventType.TASK_CREATED,
                    actor_id="agent_test",
                    task_id=f"task_{i}",
                    payload={"i": i},
                )
            await commit_and_publish(session, client.app.state.broker)

    run(client, _emit)


@pytest.fixture
def project(client, make_owner):
    owner = make_owner()
    pid = create_project(client, owner).json()["data"]["id"]
    token = owner["Authorization"].removeprefix("Bearer ")
    return pid, owner, token


def test_project_created_event_matches_contract(client, project):
    pid, owner, _ = project
    page = client.get(f"/projects/{pid}/events", headers=owner).json()
    assert page["next_after_seq"] == 1
    [event] = page["events"]
    assert event["type"] == "project.created"
    assert event["sequence"] == 1
    assert event["project_id"] == pid
    jsonschema.validate(event, SCHEMA, format_checker=jsonschema.FormatChecker())


def test_replay_after_seq_is_ordered_and_gap_free(client, project):
    pid, owner, _ = project
    emit(client, pid, 5)
    page = client.get(f"/projects/{pid}/events?after_seq=2&limit=2", headers=owner).json()
    assert [e["sequence"] for e in page["events"]] == [3, 4]
    page = client.get(
        f"/projects/{pid}/events?after_seq={page['next_after_seq']}", headers=owner
    ).json()
    assert [e["sequence"] for e in page["events"]] == [5, 6]
    assert client.get(f"/projects/{pid}/events?after_seq=6", headers=owner).json() == {
        "events": [],
        "next_after_seq": 6,
    }


def test_sequences_are_per_project(client, project, make_owner):
    pid, owner, _ = project
    other = create_project(client, owner, "Other").json()["data"]["id"]
    emit(client, pid, 2)
    page = client.get(f"/projects/{other}/events", headers=owner).json()
    assert [(e["sequence"], e["project_id"]) for e in page["events"]] == [(1, other)]


def test_stream_replays_then_pushes_live(client, project):
    pid, _, token = project
    with client.websocket_connect(f"/projects/{pid}/stream?token={token}&after_seq=0") as ws:
        assert ws.receive_json()["type"] == "project.created"
        emit(client, pid, 2)
        live = [ws.receive_json(), ws.receive_json()]
        assert [e["sequence"] for e in live] == [2, 3]
        assert all(e["project_id"] == pid for e in live)


def test_stream_resumes_from_after_seq(client, project):
    pid, _, token = project
    emit(client, pid, 3)
    with client.websocket_connect(f"/projects/{pid}/stream?token={token}&after_seq=2") as ws:
        assert [ws.receive_json()["sequence"] for _ in range(2)] == [3, 4]


def test_stream_rejects_bad_token_and_foreign_project(client, project, make_owner):
    pid, _, _ = project
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/projects/{pid}/stream?token=po_bad") as ws:
            ws.receive_json()
    assert exc.value.code == 4401

    intruder = make_owner("intruder")["Authorization"].removeprefix("Bearer ")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/projects/{pid}/stream?token={intruder}") as ws:
            ws.receive_json()
    assert exc.value.code == 4404


def test_stream_does_not_leak_other_projects(client, project):
    pid, owner, token = project
    other = create_project(client, owner, "Other").json()["data"]["id"]
    with client.websocket_connect(f"/projects/{pid}/stream?token={token}") as ws:
        assert ws.receive_json()["sequence"] == 1
        emit(client, other, 1)
        emit(client, pid, 1)
        nxt = ws.receive_json()
        assert (nxt["project_id"], nxt["sequence"]) == (pid, 2)
