"""NFR §14: ≥5 concurrent workers per project, p95 event→client latency < 2 s."""

import json
import statistics
from datetime import UTC, datetime

from harness import DONE, plan, server, task, wait_until  # noqa: F401 (fixture)
from websockets.sync.client import connect

WORKERS, TASKS = 6, 12


_work_ids: set[str] = set()


def work_ids(p):
    """Ids of the plan's tasks (fetched until all exist), not orchestrator turns."""
    if len(_work_ids) < TASKS:
        _work_ids.update(t["id"] for t in p.tasks())
    return _work_ids


def test_parallel_workers_and_event_latency(server):
    p = server.owner().project("Tải")
    p.add(
        "O",
        "orchestrator",
        {
            "responses": [
                plan("ok", [task(f"t{i}", f"Việc {i}", ["code"]) for i in range(TASKS)]),
                DONE,
            ]
        },
    )
    for i in range(WORKERS):
        p.add(f"W{i}", "worker", {"capabilities": ["code"], "delay_s": 1.0})

    latencies = []
    url = f"{server.ws_url}/projects/{p.pid}/stream?token={p.c.token}&after_seq=0"
    with connect(url) as ws:
        p.say("chạy tải")
        completed = 0
        while completed < TASKS:
            event = json.loads(ws.recv(timeout=60))
            received = datetime.now(UTC)
            latencies.append(
                (received - datetime.fromisoformat(event["occurred_at"])).total_seconds()
            )
            completed += event["type"] == "task.completed" and event["task_id"] in work_ids(p)

    tasks = wait_until(
        lambda: (ts := p.tasks()) and all(t["status"] == "completed" for t in ts) and ts
    )
    runs = [t["runs"][0] for t in tasks]
    edges = sorted(
        [(datetime.fromisoformat(r["started_at"]), 1) for r in runs]
        + [(datetime.fromisoformat(r["finished_at"]), -1) for r in runs]
    )
    peak = level = 0
    for _, step in edges:
        level += step
        peak = max(peak, level)
    assert peak >= 5, f"only {peak} runs overlapped"

    p95 = statistics.quantiles(latencies, n=20)[-1]
    p50 = statistics.median(latencies)
    print(f"\nevents={len(latencies)} parallel={peak} p50={p50:.3f}s p95={p95:.3f}s")
    assert p95 < 2.0
