from conftest import create_project


def test_requires_bearer_token(client):
    r = client.get("/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert r.headers["X-Request-ID"] == r.json()["request_id"]

    r = client.get("/projects", headers={"Authorization": "Bearer po_wrong"})
    assert r.status_code == 401


def test_create_project_returns_envelope(client, make_owner):
    owner = make_owner()
    r = create_project(client, owner, "Alpha")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed"
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert body["data"]["name"] == "Alpha"
    assert body["data"]["role"] == "owner"

    pid = body["data"]["id"]
    assert client.get(f"/projects/{pid}", headers=owner).json()["id"] == pid


def test_ac01_projects_are_isolated(client, make_owner):
    alice, bob = make_owner("alice"), make_owner("bob")
    a = create_project(client, alice, "A").json()["data"]["id"]
    b = create_project(client, bob, "B").json()["data"]["id"]

    assert [p["id"] for p in client.get("/projects", headers=alice).json()] == [a]
    for path in (f"/projects/{b}", f"/projects/{b}/events"):
        r = client.get(path, headers=alice)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "project_not_found"
    # Non-existent and inaccessible projects look the same.
    assert client.get("/projects/prj_missing", headers=alice).json()["error"]["code"] == (
        "project_not_found"
    )


def test_idempotency_key_replays_same_request(client, make_owner):
    owner = make_owner()
    first = create_project(client, owner, "Alpha", key="k1")
    again = create_project(client, owner, "Alpha", key="k1")
    assert again.status_code == 201
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert len(client.get("/projects", headers=owner).json()) == 1


def test_idempotency_key_reused_for_different_request(client, make_owner):
    owner = make_owner()
    create_project(client, owner, "Alpha", key="k1")
    r = create_project(client, owner, "Beta", key="k1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_reused"


def test_idempotency_keys_are_per_principal(client, make_owner):
    alice, bob = make_owner("alice"), make_owner("bob")
    a = create_project(client, alice, "Same", key="k1").json()["data"]["id"]
    b = create_project(client, bob, "Same", key="k1").json()["data"]["id"]
    assert a != b


def test_write_requires_idempotency_key(client, make_owner):
    r = client.post("/projects", json={"name": "x"}, headers=make_owner())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
