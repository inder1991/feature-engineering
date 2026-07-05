"""Local password login → Bearer session → authenticated requests (roles from the user's groups)."""
from featuregen.identity.local_session import add_user_to_group, create_group, create_user

_BEARER = "Authorization"


def _provision(conn, username="alice", password="pw", roles=("data_owner",)):
    uid = create_user(conn, username, password)
    gid = create_group(conn, "grp", roles=roles)
    add_user_to_group(conn, uid, gid)


def test_login_returns_token_and_authenticates_a_request(make_client, conn):
    _provision(conn)
    client = make_client()
    res = client.post("/auth/login", json={"username": "alice", "password": "pw"})
    assert res.status_code == 200
    body = res.json()
    assert body["token"] and body["token_type"] == "bearer"
    r = client.get("/search", params={"q": "x"}, headers={_BEARER: f"Bearer {body['token']}"})
    assert r.status_code == 200                       # the Bearer session authenticates the request


def test_bad_password_and_unknown_user_401(make_client, conn):
    _provision(conn)
    client = make_client()
    assert client.post("/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
    assert client.post("/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401


def test_invalid_bearer_token_401(make_client, conn):
    client = make_client()
    r = client.get("/search", params={"q": "x"}, headers={_BEARER: "Bearer bogus"})
    assert r.status_code == 401


def test_no_stub_and_no_token_rejects(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "0")   # production posture: header stub disabled
    client = make_client()
    assert client.get("/search", params={"q": "x"}).status_code == 401   # no Bearer, no stub


def test_logout_invalidates_the_token(make_client, conn):
    _provision(conn)
    client = make_client()
    token = client.post("/auth/login", json={"username": "alice", "password": "pw"}).json()["token"]
    hdr = {_BEARER: f"Bearer {token}"}
    assert client.get("/search", params={"q": "x"}, headers=hdr).status_code == 200
    client.post("/auth/logout", json={"token": token})
    assert client.get("/search", params={"q": "x"}, headers=hdr).status_code == 401
