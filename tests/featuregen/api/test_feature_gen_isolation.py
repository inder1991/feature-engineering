"""Delivery C0 Task 2 — the feature-generation connection runs under REPEATABLE READ so the C0 metadata
snapshot reads one torn-free catalog view, the isolation boundary precedes the handler's first SQL, and a
late (mid-transaction) isolation change is a SERVER error, never a silent fallback to READ COMMITTED. The
read-only /contracts routes are unchanged (they keep get_conn / default isolation)."""
from __future__ import annotations

from typing import Annotated

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from featuregen.api.deps import get_conn, get_feature_gen_conn
from featuregen.api.routes import contract as contract_routes


def _dep_calls(dependant) -> set:
    """Every callable reachable from a route's dependency tree (route-level + parameter deps, recursive)."""
    calls: set = set()
    stack = [dependant]
    while stack:
        d = stack.pop()
        if d.call is not None:
            calls.add(d.call)
        stack.extend(d.dependencies)
    return calls


def _route(routes, path: str, method: str):
    for r in routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    raise AssertionError(f"route not found: {method} {path}")


# ── 1. A feature-gen route dependency runs the handler under REPEATABLE READ ──────────────────────────
def test_feature_gen_conn_dependency_yields_repeatable_read(monkeypatch, _dsn):
    """Resolve get_feature_gen_conn through FastAPI's DI exactly as a feature-gen route would, and assert
    the handler's FIRST query on that connection already sees REPEATABLE READ — the boundary is set before
    any SQL is issued."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    app = FastAPI()

    @app.get("/_probe")
    def _probe(conn: Annotated[psycopg.Connection,
                               Depends(get_feature_gen_conn, scope="function")]) -> dict:
        return {"iso": conn.execute("SHOW transaction_isolation").fetchone()[0]}

    with TestClient(app) as c:
        res = c.get("/_probe")
    assert res.status_code == 200, res.text
    assert res.json()["iso"] == "repeatable read"


def test_get_feature_gen_conn_sets_isolation_before_first_query(monkeypatch, _dsn):
    """Direct-generator unit test (mirrors test_get_conn_commits_and_closes): the very first statement on
    the yielded connection reports REPEATABLE READ, then the generator drives commit + close."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    gen = get_feature_gen_conn()
    conn = next(gen)
    assert conn.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
    assert next(gen, None) is None   # drives the commit + close path
    assert conn.closed


def test_get_feature_gen_conn_fails_closed_without_dsn(monkeypatch):
    """Same fail-closed 503 contract as get_conn — no DSN is a server-config error, never a silent skip."""
    from fastapi import HTTPException
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    with pytest.raises(HTTPException) as exc:
        next(get_feature_gen_conn())
    assert exc.value.status_code == 503


# ── 2. Only the snapshot-BUILDING route got the RR conn; every other contract route keeps get_conn ────
def test_only_considered_set_binds_the_repeatable_read_conn():
    """MF-2: ONLY /contract/considered-set BUILDS the C0 snapshot, so only it needs the REPEATABLE READ
    conn. draft/confirm/recognitions merely reload lineage / re-run the MCV — REPEATABLE READ gave them no
    benefit and turned designed 409 races into uncaught 40001 500s — so they must bind get_conn instead."""
    routes = contract_routes.router.routes
    calls = _dep_calls(_route(routes, "/contract/considered-set", "POST").dependant)
    assert get_feature_gen_conn in calls, "considered-set should read the feature-gen (RR) conn"


def test_non_snapshot_feature_routes_keep_default_isolation():
    """MF-2: draft/confirm/recognitions build NO snapshot, so they bind get_conn (READ COMMITTED) and must
    NOT take the REPEATABLE READ conn — a concurrent re-confirm / double-submit must surface as a designed
    409 (UniqueViolation / mapped SerializationFailure), never a 40001 SerializationFailure 500."""
    routes = contract_routes.router.routes
    for path, method in [("/contract/recognitions", "POST"), ("/contract/draft", "POST"),
                         ("/contract/confirm", "POST")]:
        calls = _dep_calls(_route(routes, path, method).dependant)
        assert get_conn in calls, f"{method} {path} should keep get_conn (default isolation)"
        assert get_feature_gen_conn not in calls, f"{method} {path} must not take the RR conn (MF-2)"


def test_read_only_contract_routes_keep_default_isolation():
    """The list/detail reads are NOT feature-generation writes: they must still bind get_conn and must NOT
    bind the REPEATABLE READ conn (a light guard that only the writes were switched)."""
    routes = contract_routes.router.routes
    for path in ["/contracts", "/contracts/{contract_id}"]:
        calls = _dep_calls(_route(routes, path, "GET").dependant)
        assert get_conn in calls, f"GET {path} should keep get_conn (default isolation)"
        assert get_feature_gen_conn not in calls, f"GET {path} must not take the REPEATABLE READ conn"


def test_the_new_conn_is_a_distinct_dependency_from_get_conn():
    """Sanity: the two connection dependencies are separate callables — auth (get_identity → get_conn)
    resolves on its OWN connection and never reads the feature-gen conn before the boundary."""
    assert get_feature_gen_conn is not get_conn
    # get_identity (used by require_feature_generate and the _Identity param) reads get_conn, NOT the
    # feature-gen conn — so nothing reads the REPEATABLE READ connection before the handler's first SQL.
    from featuregen.api.deps import get_identity
    id_calls = _dep_calls_of_callable(get_identity)
    assert get_conn in id_calls
    assert get_feature_gen_conn not in id_calls


def _dep_calls_of_callable(call) -> set:
    """The dependency callables reachable from a plain dependency function (for get_identity, which is not
    itself a route). Built by wiring it onto a throwaway route so FastAPI computes its Dependant."""
    app = FastAPI()

    @app.get("/_z")
    def _z(_ident=Depends(call)):   # noqa: B008 — FastAPI dependency default is the intended form
        return {}

    return _dep_calls(_route(app.routes, "/_z", "GET").dependant)


# ── 3. A late (mid-transaction) isolation change is a SERVER error, never a silent degrade ────────────
def test_changing_isolation_mid_transaction_raises(_dsn):
    """psycopg refuses to change isolation once a transaction is in progress — the property that makes a
    read-before-the-boundary a hard error rather than a silent fallback to READ COMMITTED."""
    conn = psycopg.connect(_dsn)
    try:
        conn.execute("SELECT 1")   # opens the transaction
        with pytest.raises(psycopg.ProgrammingError):
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    finally:
        conn.rollback()
        conn.close()
