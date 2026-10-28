import itertools

import pytest
from fastapi.testclient import TestClient
from office_api.auth import create_principal_with_token
from office_api.config import Settings
from office_api.main import create_app

_keys = itertools.count()


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db"))
    with TestClient(app) as c:
        yield c


def run(client: TestClient, fn, *args):
    """Run an async function on the app's event loop."""
    return client.portal.call(fn, *args)


@pytest.fixture
def make_owner(client):
    def _make(name: str = "owner") -> dict[str, str]:
        async def _create():
            async with client.app.state.sessionmaker() as session:
                return await create_principal_with_token(session, name)

        _principal, token = run(client, _create)
        return {"Authorization": f"Bearer {token}"}

    return _make


def create_project(client, headers, name="Alpha", key=None):
    key = key or f"key-{next(_keys)}"
    return client.post(
        "/projects", json={"name": name}, headers={**headers, "Idempotency-Key": key}
    )
