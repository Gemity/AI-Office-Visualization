"""Acceptance criteria AC-01 … AC-10 (spec §15) against a running backend.

The in-process tests in apps/api/tests cover each rule in detail; these drive
the deployed shape — separate server process, HTTP, WebSocket, real restarts.
"""

import json
import signal
from datetime import datetime

import httpx
import pytest
from harness import DONE, plan, server, task, wait_until  # noqa: F401 (fixture)
from websockets.exceptions import ConnectionClosedError
from websockets.sync.client import connect

JOB = {"duration_s": 0.5, "poll_s": 0.05}
MINUTES = {
    "structured": {
        "minutes": "Chốt dùng Python.",
        "decisions": ["Dùng Python"],
        "action_items": [],
    }
}


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stream(server, project, after_seq=0, token=None):
    token = token or project.c.token
    return connect(
        f"{server.ws_url}/projects/{project.pid}/stream?token={token}&after_seq={after_seq}"
    )


def test_ac01_projects_do_not_see_each_other(server):
    owner = server.owner()
    a, b = owner.project("A"), owner.project("B")
    b.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "Bí mật B", ["code"])]), DONE]})
    b.add(
        "W",
        "worker",
        {"capabilities": ["code"], "responses": [{"text": "x", "files": {"b.txt": "B"}}]},
    )
    b.say("làm việc của B")
    b.wait_replies(3)
    [art] = b.get("/artifacts")

    assert a.tasks(None) == [] and a.get("/artifacts") == []
    assert a.get("/conversations") == []
    assert {e["project_id"] for e in a.events()} == {a.pid}
    # B's ids cannot be reached through A.
    assert a.c.request("GET", f"/projects/{a.pid}/artifacts/{art['id']}/content").status_code == 404
    b_conv = b.get("/conversations")[0]["id"]
    r = a.c.request("GET", f"/projects/{a.pid}/conversations/{b_conv}/messages")
    assert r.status_code == 404

    intruder = server.owner("intruder")
    assert intruder.request("GET", f"/projects/{b.pid}/tasks").status_code == 404
    assert [p["id"] for p in intruder.get("/projects")] == []


def test_ac02_to_ac05_ac07_to_ac09_one_project_end_to_end(server):
    owner = server.owner()
    p = owner.project("Game nhỏ")
    coder = p.add(
        "Coder",
        "worker",
        {
            "capabilities": ["code", "chat"],
            "delay_s": 0.6,
            "fail_first": 1,  # AC-07
            "responses": [{"text": "ý của Coder", "files": {"main.py": "print(1)"}}],
        },
    )
    reviewer = p.add(
        "Reviewer",
        "worker",
        {
            "capabilities": ["image", "chat"],
            "delay_s": 0.6,
            "responses": [{"text": "ý của Reviewer"}],
        },
    )
    suno = p.add("Suno", "worker", JOB, adapter="mock-suno")
    tripo = p.add("Tripo", "worker", JOB, adapter="mock-tripo")
    orch = p.add(
        "Orchestrator",
        "orchestrator",
        {
            "responses": [
                plan(
                    "Tôi chia việc",
                    [
                        task("c", "Viết code", ["code"]),
                        task("r", "Review", ["image"]),
                        task("a", "Nhạc nền", ["audio"]),
                        task("m", "Nhân vật 3D", ["3d"]),
                        task(
                            "dm",
                            "Hỏi nhanh",
                            [],
                            ["c", "r"],
                            type_="dm",
                            participants=[coder, reviewer],
                            turns=2,
                        ),
                        task(
                            "g",
                            "Họp chốt",
                            [],
                            ["dm"],
                            type_="meeting",
                            participants=[coder, reviewer],
                            turns=2,
                        ),
                        task("d", "Deploy production", ["code"], ["g"], risk=["deploy_production"]),
                    ],
                ),
                MINUTES,
                DONE,
            ]
        },
    )
    with stream(server, p) as ws:
        p.say("Làm game nhỏ")
        live = []
        approval = wait_until(
            lambda: [a for a in p.get("/approvals") if a["status"] == "pending"], 40
        )[0]
        # AC-09: nothing runs until the Owner approves.
        deploy = p.task("Deploy production")
        assert deploy["status"] == "awaiting_approval" and deploy["runs"] == []
        r = p.post(f"/approvals/{approval['id']}/decision", {"decision": "approve"})
        assert r.status_code == 200
        chat = p.wait_replies(3)
        try:
            while True:  # drain what the stream delivered so far
                live.append(json.loads(ws.recv(timeout=1)))
        except TimeoutError:
            pass

    # AC-02: the Owner only hears from the Orchestrator.
    assert {m["sender_id"] for m in chat[1:]} == {orch}
    # AC-03: ≥3 tasks, ≥2 workers overlapping in time.
    t = {x["title"]: x for x in p.tasks(None)}
    assert all(x["status"] == "completed" for x in t.values()), {
        k: v["status"] for k, v in t.items()
    }
    first = [t[k]["runs"][0] for k in ("Viết code", "Review", "Nhạc nền", "Nhân vật 3D")]
    assert len({r["agent_id"] for r in first}) == 4
    assert max(ts(r["started_at"]) for r in first) < min(ts(r["finished_at"]) for r in first)
    # AC-07: the failed run was retried, with cause and history kept.
    code = t["Viết code"]
    assert [r["status"] for r in code["runs"]] == ["failed", "succeeded"]
    assert code["runs"][0]["error"]["code"] == "mock_failure"
    assert [e["payload"]["will_retry"] for e in p.events("&type=run.failed")] == [True]
    # AC-08: async jobs report provider job status and produce artifacts.
    for title in ("Nhạc nền", "Nhân vật 3D"):
        [run] = t[title]["runs"]
        assert run["provider_ref"].startswith("job_") and run["progress"] == 1.0
    arts = {a["name"]: a for a in p.get("/artifacts")}
    assert {"main.py", "track.wav", "model.obj"} <= set(arts)
    assert (arts["track.wav"]["agent_id"], arts["model.obj"]["agent_id"]) == (suno, tripo)
    wav = p.c.request("GET", f"/projects/{p.pid}/artifacts/{arts['track.wav']['id']}/content")
    assert wav.content[:4] == b"RIFF" and len(wav.content) == arts["track.wav"]["size_bytes"]
    # AC-09: the approval was used by exactly the deploy run.
    used = next(a for a in p.get("/approvals") if a["id"] == approval["id"])
    assert (
        used["status"] == "approved" and used["run_id"] == t["Deploy production"]["runs"][0]["id"]
    )
    # AC-04: DM and meeting stored, and replayable from the event log.
    convs = {c["type"]: c for c in p.get("/conversations")}
    dm = p.get(f"/conversations/{convs['dm']['id']}/messages")
    assert [m["sender_id"] for m in dm] == [coder, reviewer]
    [meeting] = p.get("/meetings")
    assert meeting["decisions"] == ["Dùng Python"] and not meeting["live"]
    replay = p.events(f"&conversation_id={convs['group_meeting']['id']}")
    assert replay[0]["type"] == "meeting.started" and replay[-1]["type"] == "meeting.ended"
    # The live stream carried the same events, in order, without gaps.
    seqs = [e["sequence"] for e in live]
    assert seqs == list(range(1, len(seqs) + 1))
    assert [e["event_id"] for e in live] == [e["event_id"] for e in p.events()][: len(live)]
    # AC-05: what the Office shows for an agent is backed by real records.
    agent_events = p.events(f"&actor_id={reviewer}")
    assert agent_events and {e["actor_id"] for e in agent_events} == {reviewer}
    reviewer_tasks = {x["id"] for x in t.values() if reviewer in [r["agent_id"] for r in x["runs"]]}
    assert {e["task_id"] for e in agent_events if e["task_id"]} <= reviewer_tasks
    thread = next(c for c in p.get("/conversations") if c["task_id"] == t["Review"]["id"])
    assert [m["content"] for m in p.get(f"/conversations/{thread['id']}/messages")][
        -1
    ] == "ý của Reviewer"
    agent = next(a for a in p.get("/agents") if a["id"] == reviewer)
    assert agent["current_runs"] == []  # idle: nothing invented while no run is active


def test_ac06_pause_is_requested_then_confirmed_and_resumable(server):
    p = server.owner().project()
    p.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "Chậm", ["code"])]), DONE]})
    p.add("W", "worker", {"capabilities": ["code"], "delay_s": 2})
    p.say("go")
    running = wait_until(lambda: (x := p.task("Chậm")) and x["status"] == "active" and x)

    r = p.post("/interventions", {"action": "pause", "task_id": running["id"]})
    assert r.status_code == 202 and r.json()["data"]["status"] == "requested"
    iv_id = r.json()["data"]["id"]
    wait_until(
        lambda: next(i for i in p.get("/interventions") if i["id"] == iv_id)["status"] == "applied"
    )
    assert p.task("Chậm")["status"] == "paused"
    types = [e["type"] for e in p.events()]
    assert types.index("intervention.requested") < types.index("intervention.applied")

    r = p.post("/interventions", {"action": "resume", "task_id": running["id"]})
    assert r.json()["data"]["status"] == "applied"
    p.wait_replies(3)
    assert [r["status"] for r in p.task("Chậm")["runs"]] == ["cancelled", "succeeded"]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["stop", "crash"])
def test_ac10_restart_replays_events_and_restores_tasks(server, sig):
    owner = server.owner()
    p = owner.project()
    orch = p.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("ok", [task("m", "Nhạc", ["audio"]), task("w", "Ghép", ["code"], ["m"])])
            ]
        },
    )
    p.add("Suno", "worker", {"duration_s": 2, "poll_s": 0.05}, adapter="mock-suno")
    p.add("W", "worker", {"capabilities": ["code"]})
    p.say("go")
    job = wait_until(lambda: (x := p.task("Nhạc")) and x["runs"] and x["runs"][0]["provider_ref"])
    before = p.events()

    server.stop(sig)
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{server.url}/healthz", timeout=1)
    server.set_agent_config(orch, {"responses": [DONE]})
    server.start()

    # Committed events survive and replay identically, over REST and WebSocket.
    after = p.events()
    assert after[: len(before)] == before
    with stream(server, p) as ws:
        replayed = [json.loads(ws.recv(timeout=5)) for _ in before]
    assert [e["event_id"] for e in replayed] == [e["event_id"] for e in before]

    p.wait_replies(3)
    music, merge = p.task("Nhạc"), p.task("Ghép")
    assert (music["status"], merge["status"]) == ("completed", "completed")
    [run] = music["runs"]  # same provider job re-attached, nothing resubmitted
    assert (run["status"], run["provider_ref"]) == ("succeeded", job)
    assert [a["name"] for a in p.get("/artifacts")] == ["track.wav"]
    seqs = [e["sequence"] for e in p.events()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_stream_rejects_unauthorised_clients(server):
    p = server.owner().project()
    stranger = server.owner("stranger")
    for token, code in [("po_bogus", 4401), (stranger.token, 4404)]:
        with pytest.raises(ConnectionClosedError) as exc:
            with stream(server, p, token=token) as ws:
                ws.recv(timeout=5)
        assert exc.value.rcvd.code == code
