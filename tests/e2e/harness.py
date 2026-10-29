"""End-to-end harness: the real ``office-api serve`` process, spoken to over
HTTP and WebSocket like the browser does (spec §15/§14). Only mock adapters —
no provider is ever called."""

import itertools
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_keys = itertools.count()


def wait_until(fn, timeout=20.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(interval)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """One backend process at a time over a fixed database and data directory."""

    def __init__(self, root: Path):
        self.db = root / "office.db"
        self.env = {
            **os.environ,
            "OFFICE_DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
            "OFFICE_DATA_DIR": str(root / "data"),
            "OFFICE_WEB_DIR": "",
            "OFFICE_POLICY_FILE": "",  # built-in defaults
        }
        self.log = (root / "server.log").open("ab")
        self.proc: subprocess.Popen | None = None
        self.port = 0

    def cli(self, *args: str) -> str:
        return subprocess.run(
            [sys.executable, "-m", "office_api.cli", *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def start(self) -> None:
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "office_api.cli", "serve", "--port", str(self.port)],
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        wait_until(self._ready, timeout=30)

    def _ready(self) -> bool:
        assert self.proc.poll() is None, "server exited during startup (see server.log)"
        try:
            return httpx.get(f"{self.url}/readyz", timeout=1).status_code == 200
        except httpx.TransportError:
            return False

    def stop(self, sig=signal.SIGTERM) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            self.proc.wait(timeout=30)
        self.proc = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def owner(self, name="owner") -> "Client":
        out = self.cli("create-owner", "--name", name)
        token = next(line.split()[1] for line in out.splitlines() if line.startswith("token:"))
        return Client(self, token)

    def set_agent_config(self, agent_id: str, config: dict) -> None:
        """Operator edit while the backend is down (mock replies restart from the
        first one in a new process)."""
        db = sqlite3.connect(self.db)
        db.execute(
            "UPDATE agent_definitions SET config = ? WHERE id = ?", (json.dumps(config), agent_id)
        )
        db.commit()
        db.close()


class Client:
    def __init__(self, server: Server, token: str):
        self.server, self.token = server, token

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def request(self, method, path, body=None):
        headers = self.h if method == "GET" else {**self.h, "Idempotency-Key": f"e2e-{next(_keys)}"}
        return httpx.request(
            method, f"{self.server.url}{path}", json=body, headers=headers, timeout=10
        )

    def get(self, path):
        r = self.request("GET", path)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    def project(self, name="Project") -> "Project":
        r = self.request("POST", "/projects", {"name": name})
        assert r.status_code == 201, r.text
        return Project(self, r.json()["data"]["id"])


class Project:
    def __init__(self, client: Client, pid: str):
        self.c, self.pid = client, pid

    def get(self, path=""):
        return self.c.get(f"/projects/{self.pid}{path}")

    def post(self, path, body):
        return self.c.request("POST", f"/projects/{self.pid}{path}", body)

    def add(self, name, role, config, adapter="mock") -> str:
        r = self.post(
            "/agents", {"name": name, "adapter_type": adapter, "role": role, "config": config}
        )
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]

    def say(self, text):
        r = self.post("/orchestrator/messages", {"content": text})
        assert r.status_code == 202, r.text

    def tasks(self, kind="work"):
        return [t for t in self.get("/tasks") if kind is None or t["kind"] == kind]

    def task(self, title):
        return next((t for t in self.get("/tasks") if t["title"] == title), None)

    def events(self, query=""):
        return self.get(f"/events?limit=1000{query}")["events"]

    def owner_chat(self):
        conv = next(c for c in self.get("/conversations") if c["type"] == "owner_chat")
        return self.get(f"/conversations/{conv['id']}/messages")

    def wait_replies(self, n, timeout=30.0):
        return wait_until(lambda: len(chat := self.owner_chat()) >= n and chat, timeout)


def task(key, title, caps, deps=(), type_="work", participants=None, turns=None, risk=()):
    return {
        "key": key,
        "type": type_,
        "title": title,
        "instructions": f"do {title}",
        "capabilities": caps,
        "depends_on": list(deps),
        "assignee_id": None,
        "participants": participants,
        "turn_limit": turns,
        "risk_actions": list(risk),
    }


def plan(reply, tasks=()):
    return {
        "structured": {
            "reply": reply,
            "plan_title": "Plan" if tasks else None,
            "tasks": list(tasks),
            "interventions": [],
        }
    }


DONE = plan("Đã xong")


@pytest.fixture
def server(tmp_path):
    """A migrated, running backend; imported by each e2e module (no conftest:
    pytest would collide it with apps/api/tests/conftest.py)."""
    srv = Server(tmp_path)
    srv.cli("migrate")
    srv.start()
    yield srv
    srv.stop()
