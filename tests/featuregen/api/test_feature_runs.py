"""Run-list/detail routes. NEVER conn.commit() here — the shared test conn rolls back.

The seeds spell their subjects the way the REQUEST does: `api/deps.py` builds the stub envelope as
``subject=f"user:{x_user}"``, so a caller sending ``X-User: priya`` carries ``user:priya``. The read
policy compares that envelope subject against the stored owner, so the SEED is what has to match it
— aligning the policy to the seed instead would be a visibility hole dressed up as a fixture.
"""
from tests.featuregen.runs._chain import seed_run_chain


def _hdr(user, roles):
    return {"X-User": user, "X-Roles": roles}


def _ids(body):
    return [r["generation_run_id"] for g in body["groups"] for r in g["runs"]]


def test_list_scopes_to_the_caller(client, conn):
    seed_run_chain(conn, run_id="api-a", subject="user:priya")
    seed_run_chain(conn, run_id="api-b", subject="user:other")
    body = client.get("/feature-runs", headers=_hdr("priya", "feature_engineer")).json()
    ids = _ids(body)
    # Both directions: the caller's own run is PRESENT (a policy that hid everything would pass a
    # one-sided assertion) and the other subject's is ABSENT.
    assert "api-a" in ids and "api-b" not in ids
    other = client.get("/feature-runs", headers=_hdr("other", "feature_engineer")).json()
    assert "api-b" in _ids(other) and "api-a" not in _ids(other)


def test_admin_lists_all(client, conn):
    seed_run_chain(conn, run_id="api-c", subject="user:other")
    body = client.get("/feature-runs", headers=_hdr("a", "platform_admin")).json()
    assert "api-c" in _ids(body)


def test_limit_zero_is_clamped_rather_than_crashing(client, conn):
    """`limit=0` reaches the projection as a page of ZERO with a next page — `page[-1]` on an
    empty page is an IndexError, i.e. a 500 from a query string. The route owns the clamp."""
    seed_run_chain(conn, run_id="lim-a", subject="user:priya")
    seed_run_chain(conn, run_id="lim-b", subject="user:priya")
    resp = client.get("/feature-runs", params={"limit": 0},
                      headers=_hdr("priya", "feature_engineer"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(_ids(body)) == 1 and body["next_cursor"] is not None


def test_malformed_cursor_is_a_422_not_a_500(client, conn):
    """A cursor is opaque to the caller but is spliced into a `::timestamptz` cast, so garbage in it
    raises out of the driver. A caller-supplied value must never be a server error."""
    seed_run_chain(conn, run_id="cur-a", subject="user:priya")
    resp = client.get("/feature-runs", params={"cursor": "garbage"},
                      headers=_hdr("priya", "feature_engineer"))
    assert resp.status_code == 422
    # A plain string, not FastAPI's list-of-errors shape: this denial is the route's, not the
    # request validator's.
    assert isinstance(resp.json()["detail"], str)


def test_detail_404_hides_denial_from_absence(client, conn):
    seed_run_chain(conn, run_id="api-d", subject="user:other")
    r1 = client.get("/feature-runs/api-d", headers=_hdr("priya", "feature_engineer"))
    r2 = client.get("/feature-runs/does-not-exist", headers=_hdr("priya", "feature_engineer"))
    assert r1.status_code == 404 and r2.status_code == 404
    assert r1.json() == r2.json()          # indistinguishable: no shape leak


def test_detail_shape(client, conn):
    seed_run_chain(conn, run_id="api-e", subject="user:priya")
    body = client.get("/feature-runs/api-e", headers=_hdr("priya", "feature_engineer")).json()
    assert body["pre_spine"] is True       # chain seeded but no identity row
    assert {s["stage"] for s in body["rail"]} >= {"GENERATE_PREVIEW", "TRAIN_MODEL"}


def test_read_permission_is_required(client, conn):
    """The router carries `feature:read`; `data_owner` is a REAL role that genuinely lacks it, so
    the refusal is the permission bundle's and not an unknown-role accident."""
    seed_run_chain(conn, run_id="api-f", subject="user:priya")
    assert client.get("/feature-runs", headers=_hdr("priya", "data_owner")).status_code == 403
    assert client.get("/feature-runs/api-f", headers=_hdr("priya", "data_owner")).status_code == 403
