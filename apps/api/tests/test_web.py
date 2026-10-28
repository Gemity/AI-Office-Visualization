from fastapi.testclient import TestClient
from office_api.config import Settings
from office_api.main import create_app


def test_serves_frontend_without_shadowing_api(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert client.get("/office.js").status_code == 200

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/projects").json()["error"]["code"] == "unauthenticated"
    assert client.get("/no-such-file.js").json()["error"]["code"] == "not_found"


def test_frontend_can_be_disabled(tmp_path):
    app = create_app(
        Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
            web_dir="",
            data_dir=str(tmp_path / "data"),
        )
    )
    with TestClient(app) as c:
        assert c.get("/").status_code == 404
