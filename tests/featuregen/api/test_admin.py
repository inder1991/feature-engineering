"""User/group/role administration API — admin-gated via a REAL authenticated Bearer session (an
unauthenticated stub principal, even with X-Roles: admin, is rejected)."""
from ._helpers import AUTH  # data_owner stub — not an admin, and not authenticated


def _admin(client):
    """Bootstrap the first admin + log in → a real authenticated admin Bearer session."""
    assert client.post("/admin/bootstrap",
                       json={"username": "root", "password": "rootpass1"}).status_code == 200
    token = client.post("/auth/login",
                        json={"username": "root", "password": "rootpass1"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _root_id(client, hdr):
    return next(u["user_id"] for u in client.get("/admin/users", headers=hdr).json()
               if u["username"] == "root")


def test_bootstrap_then_admin_session_works_then_second_bootstrap_conflicts(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.get("/admin/users", headers=hdr).status_code == 200
    assert client.post("/admin/bootstrap",
                       json={"username": "x", "password": "yyyyyyyy"}).status_code == 409


def test_admin_endpoints_require_an_authenticated_admin(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    # an unauthenticated stub principal claiming admin is REJECTED (defense-in-depth vs stub misconfig)
    assert client.get("/admin/users", headers={"X-User": "m", "X-Roles": "admin"}).status_code == 403
    assert client.get("/admin/users", headers=AUTH).status_code == 403   # data_owner, not admin
    assert client.get("/admin/users", headers=hdr).status_code == 200    # real admin session


def test_create_user_group_role_and_resolve_on_login(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    uid = client.post("/admin/users", json={"username": "ana", "password": "anapass1"},
                      headers=hdr).json()["user_id"]
    gid = client.post("/admin/groups",
                      json={"name": "readers", "roles": ["catalog_viewer", "pii_reader"]},
                      headers=hdr).json()["group_id"]
    assert client.post(f"/admin/groups/{gid}/members", json={"user_id": uid},
                       headers=hdr).status_code == 200
    token = client.post("/auth/login", json={"username": "ana", "password": "anapass1"}).json()["token"]
    assert client.get("/search", params={"q": "x"},
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert any(u["username"] == "ana" for u in client.get("/admin/users", headers=hdr).json())


def test_duplicate_username_409(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    client.post("/admin/users", json={"username": "dup", "password": "duppass1"}, headers=hdr)
    assert client.post("/admin/users", json={"username": "dup", "password": "duppass1"},
                       headers=hdr).status_code == 409


def test_short_password_is_rejected(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.post("/admin/users", json={"username": "u", "password": "short"},
                       headers=hdr).status_code == 422


def test_cannot_disable_or_delete_the_last_admin(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    root_id = _root_id(client, hdr)
    assert client.post(f"/admin/users/{root_id}/disable", headers=hdr).status_code == 409
    assert client.delete(f"/admin/users/{root_id}", headers=hdr).status_code == 409


def test_disable_user_blocks_login(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    uid = client.post("/admin/users", json={"username": "bob", "password": "bobpass1"},
                      headers=hdr).json()["user_id"]
    assert client.post("/auth/login", json={"username": "bob", "password": "bobpass1"}).status_code == 200
    client.post(f"/admin/users/{uid}/disable", headers=hdr)
    assert client.post("/auth/login", json={"username": "bob", "password": "bobpass1"}).status_code == 401


def test_bad_ids_return_404(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.post("/admin/groups/nope/members", json={"user_id": "x"},
                       headers=hdr).status_code == 404
    assert client.post("/admin/groups/nope/roles", json={"role": "admin"},
                       headers=hdr).status_code == 404
    assert client.delete("/admin/users/nope", headers=hdr).status_code == 404


def _admins_gid(client, hdr):
    return next(g["group_id"] for g in client.get("/admin/groups", headers=hdr).json()
               if g["name"] == "admins")


# One op per test: each gets a fresh rolled-back conn where root is the SOLE admin. (In prod the request
# tx rolls the refused mutation back; a shared test conn doesn't, so all three can't share one test.)
def test_cannot_remove_the_last_admin_from_its_group(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.delete(f"/admin/groups/{_admins_gid(client, hdr)}/members/{_root_id(client, hdr)}",
                         headers=hdr).status_code == 409


def test_cannot_revoke_the_last_admin_role(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.delete(f"/admin/groups/{_admins_gid(client, hdr)}/roles/platform_admin",
                         headers=hdr).status_code == 409


def test_cannot_delete_the_last_admin_group(make_client, conn):
    client = make_client()
    hdr = _admin(client)
    assert client.delete(f"/admin/groups/{_admins_gid(client, hdr)}", headers=hdr).status_code == 409
