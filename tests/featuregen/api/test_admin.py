"""User/group/role administration API (admin-gated) + first-run bootstrap."""
from ._helpers import AUTH   # data_owner — NOT an admin

ADMIN = {"X-User": "root", "X-Roles": "admin"}


def test_bootstrap_creates_first_admin_then_conflicts(make_client, conn):
    client = make_client()
    r = client.post("/admin/bootstrap", json={"username": "root", "password": "pw"})
    assert r.status_code == 200 and r.json()["user_id"]
    # the bootstrapped admin can log in and carries the admin role
    token = client.post("/auth/login", json={"username": "root", "password": "pw"}).json()["token"]
    assert client.get("/admin/users", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # second bootstrap is refused once any user exists
    assert client.post("/admin/bootstrap", json={"username": "x", "password": "y"}).status_code == 409


def test_admin_endpoints_require_admin_role(make_client, conn):
    client = make_client()
    assert client.get("/admin/users", headers=AUTH).status_code == 403     # data_owner is not admin
    assert client.get("/admin/users", headers=ADMIN).status_code == 200


def test_create_user_group_role_and_resolve_on_login(make_client, conn):
    client = make_client()
    uid = client.post("/admin/users", json={"username": "ana", "password": "pw"},
                      headers=ADMIN).json()["user_id"]
    gid = client.post("/admin/groups", json={"name": "readers", "roles": ["pii_reader"]},
                      headers=ADMIN).json()["group_id"]
    assert client.post(f"/admin/groups/{gid}/members", json={"user_id": uid},
                       headers=ADMIN).status_code == 200
    # ana logs in -> a real Bearer session that authenticates a protected endpoint
    token = client.post("/auth/login", json={"username": "ana", "password": "pw"}).json()["token"]
    assert client.get("/search", params={"q": "x"},
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert any(u["username"] == "ana" for u in client.get("/admin/users", headers=ADMIN).json())
    groups = client.get("/admin/groups", headers=ADMIN).json()
    assert any(g["name"] == "readers" and "pii_reader" in g["roles"] for g in groups)


def test_duplicate_username_409(make_client, conn):
    client = make_client()
    client.post("/admin/users", json={"username": "dup", "password": "pw"}, headers=ADMIN)
    assert client.post("/admin/users", json={"username": "dup", "password": "pw"},
                       headers=ADMIN).status_code == 409


def test_disable_user_blocks_login(make_client, conn):
    client = make_client()
    uid = client.post("/admin/users", json={"username": "bob", "password": "pw"},
                      headers=ADMIN).json()["user_id"]
    assert client.post("/auth/login", json={"username": "bob", "password": "pw"}).status_code == 200
    client.post(f"/admin/users/{uid}/disable", headers=ADMIN)
    assert client.post("/auth/login", json={"username": "bob", "password": "pw"}).status_code == 401


def test_bad_ids_return_404(make_client, conn):
    client = make_client()
    assert client.post("/admin/groups/nope/members", json={"user_id": "x"},
                       headers=ADMIN).status_code == 404
    assert client.post("/admin/groups/nope/roles", json={"role": "admin"},
                       headers=ADMIN).status_code == 404
    assert client.delete("/admin/users/nope", headers=ADMIN).status_code == 404
