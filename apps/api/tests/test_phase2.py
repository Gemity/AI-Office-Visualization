"""Phase 2: meetings, DMs, interventions, timeline filters (mock adapters)."""

import pytest
from test_phase1 import DONE, Office, key, plan, task, wait_until

OUTCOME = {
    "structured": {
        "minutes": "Đã thống nhất kiến trúc.",
        "decisions": ["Dùng Python"],
        "action_items": [
            {
                "title": "Viết skeleton",
                "instructions": "tạo main.py",
                "capabilities": ["code"],
                "assignee_id": None,
            }
        ],
    }
}


@pytest.fixture
def office(client, make_owner):
    return Office(client, make_owner())


def intervene(office, action, task_id=None, instructions=None):
    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": action, "task_id": task_id, "instructions": instructions},
        headers={**office.h, **key()},
    )
    assert r.status_code == 202, r.json()
    return r.json()["data"]


def intervention(office, iv_id):
    return next(i for i in office.get("/interventions") if i["id"] == iv_id)


def task_by_title(office, title):
    return next(t for t in office.get("/tasks") if t["title"] == title)


def test_ac04_dm_and_meeting_are_recorded_and_replayable(office):
    w1 = office.add(
        "An", "worker", {"capabilities": ["chat", "code"], "responses": [{"text": "ý của An"}]}
    )
    w2 = office.add(
        "Bình", "worker", {"capabilities": ["chat", "code"], "responses": [{"text": "ý của Bình"}]}
    )
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "Họp trước rồi làm",
                    [
                        task("d", "Hỏi nhanh", [], type_="dm", participants=[w1, w2], turns=2),
                        task(
                            "m",
                            "Chốt kiến trúc",
                            [],
                            ["d"],
                            type_="meeting",
                            participants=[w1, w2],
                            turns=2,
                        ),
                        task("w", "Triển khai", ["code"], ["m"]),
                    ],
                ),
                OUTCOME,
                DONE,
            ]
        },
    )
    office.say("bắt đầu")
    office.wait_replies(3)

    tasks = {
        t["title"]: t
        for t in office.work_tasks() + [t for t in office.get("/tasks") if t["kind"] != "work"]
    }
    assert {
        tasks[k]["status"] for k in ("Hỏi nhanh", "Chốt kiến trúc", "Triển khai", "Viết skeleton")
    } == {"completed"}

    convs = {c["type"]: c for c in office.get("/conversations")}
    dm, room = convs["dm"], convs["group_meeting"]
    assert dm["visibility"] == "participants" and sorted(dm["participants"]) == sorted([w1, w2])
    dm_msgs = office.get(f"/conversations/{dm['id']}/messages")
    assert [m["sender_id"] for m in dm_msgs] == [w1, w2]

    room_msgs = office.get(f"/conversations/{room['id']}/messages")
    assert [m["sender_id"] for m in room_msgs][:2] == [w1, w2]
    assert "Dùng Python" in room_msgs[-1]["content"]  # minutes posted by the moderator

    [meeting] = office.get("/meetings")
    assert meeting["status"] == "closed" and meeting["outcome"] == "completed"
    assert meeting["decisions"] == ["Dùng Python"] and not meeting["live"]
    assert sorted(meeting["participants"]) == sorted([w1, w2])

    types = [e["type"] for e in office.events()]
    assert (
        types.index("meeting.started")
        < types.index("decision.recorded")
        < types.index("meeting.ended")
    )

    # Replay of the meeting from the event log (AC-04).
    replay = office.get(f"/events?conversation_id={room['id']}&limit=1000")["events"]
    assert [e["type"] for e in replay][0] == "meeting.started"
    assert [e["payload"]["preview"] for e in replay if e["type"] == "message.sent"][:2] == [
        "ý của An",
        "ý của Bình",
    ]


def test_invalid_dm_participants_rejected_as_invalid_plan(office):
    w1 = office.add("A", "worker", {"capabilities": ["chat"]})
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("x", [task("d", "dm", [], type_="dm", participants=[w1], turns=2)]),
                plan("ok"),
            ]
        },
    )
    office.say("hi")
    office.wait_replies(2)
    assert office.events("run.failed")[0]["payload"]["error"]["code"] == "invalid_plan"


def test_ac06_pause_running_task_is_requested_then_confirmed_and_resumable(office):
    office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("t", "Chậm", ["code"])]), DONE]}
    )
    office.add("W", "worker", {"capabilities": ["code"], "delay_s": 1.5})
    office.say("go")
    t = wait_until(lambda: next((x for x in office.work_tasks() if x["status"] == "active"), None))

    iv = intervene(office, "pause", t["id"])
    assert iv["status"] == "requested"  # not claimed before the run actually stops
    applied = wait_until(
        lambda: (
            intervention(office, iv["id"])["status"] == "applied" and intervention(office, iv["id"])
        )
    )
    assert applied["outcome"] == "task paused"
    t = task_by_title(office, "Chậm")
    assert t["status"] == "paused" and t["runs"][0]["status"] == "cancelled"
    types = [e["type"] for e in office.events()]
    assert types.index("intervention.requested") < types.index("intervention.applied")

    resumed = intervene(office, "resume", t["id"])
    assert resumed["status"] == "applied"
    office.wait_replies(3)
    t = task_by_title(office, "Chậm")
    assert t["status"] == "completed" and [r["status"] for r in t["runs"]] == [
        "cancelled",
        "succeeded",
    ]


def test_cancel_waiting_task_applies_immediately_and_blocks_dependents(office):
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan(
                    "ok",
                    [
                        task("a", "A", ["code"]),
                        task("b", "B", ["code"], ["a"]),
                        task("c", "C", ["code"], ["b"]),
                    ],
                ),
                DONE,
            ]
        },
    )
    office.add("W", "worker", {"capabilities": ["code"], "delay_s": 1.0})
    office.say("go")
    b = wait_until(lambda: next((x for x in office.work_tasks() if x["title"] == "B"), None))
    iv = intervene(office, "cancel", b["id"])
    assert iv["status"] == "applied"
    office.wait_replies(3)
    assert task_by_title(office, "A")["status"] == "completed"
    assert task_by_title(office, "B")["status"] == "cancelled"
    assert task_by_title(office, "C")["status"] == "blocked"


def test_redirect_running_task_reruns_with_new_instructions(office):
    office.add(
        "O", "orchestrator", {"responses": [plan("ok", [task("t", "Viết", ["code"])]), DONE]}
    )
    office.add("W", "worker", {"capabilities": ["code"], "delay_s": 1.0})
    office.say("go")
    t = wait_until(lambda: next((x for x in office.work_tasks() if x["status"] == "active"), None))
    iv = intervene(office, "redirect", t["id"], "Viết bằng Go thay vì Python")
    assert iv["status"] == "requested"
    office.wait_replies(3)
    t = task_by_title(office, "Viết")
    assert "Viết bằng Go" in t["description"]
    assert [r["status"] for r in t["runs"]] == ["cancelled", "succeeded"]
    assert intervention(office, iv["id"])["status"] == "applied"


def test_intervention_on_finished_task_is_rejected_and_needs_valid_target(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]})
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("go")
    office.wait_replies(3)
    t = task_by_title(office, "T")
    iv = intervene(office, "cancel", t["id"])
    assert (iv["status"], iv["outcome"]) == ("rejected", "task already completed")

    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": "cancel", "task_id": "task_nope"},
        headers={**office.h, **key()},
    )
    assert r.status_code == 404
    r = office.c.post(
        f"/projects/{office.pid}/interventions",
        json={"action": "cancel"},
        headers={**office.h, **key()},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "task_required"


def test_project_pause_stops_dispatch_until_resume(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]})
    office.add("W", "worker", {"capabilities": ["code"]})
    assert intervene(office, "pause")["status"] == "applied"
    office.say("go")
    wait_until(lambda: office.work_tasks())
    import time

    time.sleep(0.4)
    assert task_by_title(office, "T")["status"] == "pending"
    assert intervene(office, "resume")["status"] == "applied"
    office.wait_replies(3)
    assert task_by_title(office, "T")["status"] == "completed"


def test_orchestrator_can_request_intervention_for_owner(office):
    office.add("O1", "orchestrator", {"responses": [plan("ok", [task("t", "Dài", ["code"])])]})
    office.add("W", "worker", {"capabilities": ["code"], "delay_s": 30})
    office.say("làm đi")
    t = wait_until(lambda: next((x for x in office.work_tasks() if x["status"] == "active"), None))
    orch2 = office.add(
        "O2",
        "orchestrator",
        {
            "responses": [
                plan(
                    "Đã gửi yêu cầu hủy",
                    interventions=[{"action": "cancel", "task_id": t["id"], "instructions": None}],
                ),
                DONE,
            ]
        },
    )
    office.say("hủy task đó")
    iv = wait_until(lambda: [i for i in office.get("/interventions") if i["status"] == "applied"])[
        0
    ]
    assert iv["requested_by"] == orch2 and iv["action"] == "cancel"
    assert task_by_title(office, "Dài")["status"] == "cancelled"


def test_cancel_running_meeting(office):
    w1 = office.add("A", "worker", {"capabilities": ["chat"], "delay_s": 1.0})
    w2 = office.add("B", "worker", {"capabilities": ["chat"], "delay_s": 1.0})
    office.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("ok", [task("m", "Họp", [], type_="meeting", participants=[w1, w2], turns=6)]),
                DONE,
            ]
        },
    )
    office.say("họp")
    wait_until(lambda: [m for m in office.get("/meetings") if m["live"]])
    m = task_by_title(office, "Họp")
    iv = intervene(office, "cancel", m["id"])
    assert iv["status"] == "requested"
    wait_until(lambda: intervention(office, iv["id"])["status"] == "applied")
    [meeting] = office.get("/meetings")
    assert meeting["outcome"] == "cancelled" and not meeting["live"]
    assert task_by_title(office, "Họp")["status"] == "cancelled"
    assert "meeting.ended" in [e["type"] for e in office.events()]


def test_timeline_filters(office):
    office.add("O", "orchestrator", {"responses": [plan("ok", [task("t", "T", ["code"])]), DONE]})
    office.add("W", "worker", {"capabilities": ["code"]})
    office.say("go")
    office.wait_replies(3)
    t = task_by_title(office, "T")
    page = office.get(f"/events?task_id={t['id']}&type=task.started&type=task.completed")
    assert [e["type"] for e in page["events"]] == ["task.started", "task.completed"]
    assert (
        office.c.get(f"/projects/{office.pid}/events?type=bogus", headers=office.h).status_code
        == 422
    )
