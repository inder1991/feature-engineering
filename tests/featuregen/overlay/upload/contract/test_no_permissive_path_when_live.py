"""Phase-3C.2a Task 7 (§9 item 8) — the STRUCTURAL no-permissive-path-when-live guarantee.

``find_cross_catalog_path`` — BOTH live bindings: the ``entity`` origin and the ``contract.author``
import — is replaced with a function that RECORDS the call and RAISES. Live activation is genuinely
enabled (flag + deployment id + a persisted PASS evaluation + an APPROVE decision), a cross-catalog
catalog set is seeded (ops + rev + a VERIFIED bridge), and the FULL flag-on cross-catalog flow is
driven over HTTP: considered-set (entity-scoped) → draft (the governed feature) → confirm. Every path
either SUCCEEDS or FAILS CLOSED (drift → 409 regenerate) with the recorder provably never invoked —
the structural proof that no flag-on cross-catalog path can touch the permissive implementation (its
outright removal is 3C.2b). The recorder list matters: a savepoint-swallowed invocation would not
surface as a 500, but it WOULD land in the list.

Doubles as the §9 items 2 + 5 HTTP surface: the governed option rides the response JSON with
``origin='governed_planner'`` / ``path_authority='governed_cross_catalog'`` + its plan envelope, and
the drafted join path reconstructs the PERSISTED envelope's ``ordered_path`` exactly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_contract_live_cross_catalog import (
    DEP,
    FLAG,
    _approve,
    _flow_llm,
    _fresh_now,
    _governed_scoped_body,
    _inject_fixture_template,
)
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.api.app import create_app
from featuregen.api.deps import get_conn, get_feature_gen_conn
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient on the suite's rolled-back connection (mirrors tests/featuregen/api/conftest.py's
    make_client — that fixture is directory-scoped, so this file builds its own)."""
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    app = create_app(llm_client=_flow_llm())

    def _test_conn():
        yield db

    app.dependency_overrides[get_conn] = _test_conn
    app.dependency_overrides[get_feature_gen_conn] = _test_conn   # feature-gen routes (C0) → same conn
    with TestClient(app) as c:
        yield c


@pytest.fixture
def permissive_calls(monkeypatch) -> list:
    """Replace BOTH live ``find_cross_catalog_path`` bindings with a recorder that raises. Returns the
    call list — empty at the end of a test IS the structural guarantee."""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError(
            "find_cross_catalog_path must never run while live cross-catalog is on")

    monkeypatch.setattr("featuregen.overlay.upload.entity.find_cross_catalog_path", _boom)
    monkeypatch.setattr("featuregen.overlay.upload.contract.author.find_cross_catalog_path", _boom)
    return calls


def _enable_live(db, monkeypatch) -> None:
    """Flag on + a configured deployment + a persisted PASS evaluation + an APPROVE decision — the
    REAL activation interlock enables (nothing about readiness is stubbed)."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(db)


# ── the full flag-on cross-catalog flow SUCCEEDS without ever touching the permissive path ────────────
def test_full_flag_on_cross_catalog_flow_never_invokes_permissive_path(
        client, db, monkeypatch, permissive_calls):
    """§9 item 8 (+ items 2/5 HTTP surface) — considered-set → draft → confirm, all flag-on-approved,
    all genuinely cross-catalog, all succeeding with ``find_cross_catalog_path`` provably not invoked."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)                   # ops + rev + a VERIFIED bridge → a resolvable cross-catalog plan
    _fresh_now(db, "ops", "rev")      # fresh as of the route's real wall clock
    _inject_fixture_template(monkeypatch)

    # 1. considered-set (entity-scoped): the governed option surfaces with structured authority + the
    #    compiled plan envelope riding the response JSON (§9 item 2).
    res = client.post("/contract/considered-set", json=_governed_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    governed = [f for s in body["alternatives"] for f in s["features"] if f["name"] == "t_roll"]
    assert len(governed) == 1
    assert governed[0]["origin"] == "governed_planner"
    assert governed[0]["path_authority"] == "governed_cross_catalog"
    envelope = governed[0]["plan_envelope"]
    assert envelope is not None and envelope["physical_plan_id"] and envelope["ordered_path"]
    # the option genuinely spans >1 catalog — the whole point of a governed cross-catalog plan
    assert len({cs for cs, _ref in (tuple(p) for p in governed[0]["derives_pairs"])}) > 1
    assert permissive_calls == []

    # 2. draft: the governed feature drafts EXACTLY the PERSISTED envelope's ordered_path (§9 item 5) —
    #    reconstructed server-side from the recorded considered set, never recomputed permissively.
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "t_roll", "why": "governed cross-catalog"}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    assert [s["segment"] for s in draft["join_path"]] == list(envelope["ordered_path"])
    assert permissive_calls == []

    # 3. confirm: the governing write completes — freshness rechecked against the SERVER-side envelope.
    draft["intent_id"] = body["intent_id"]
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1
    assert permissive_calls == []     # the structural guarantee, end to end


# ── drift FAILS CLOSED (409 regenerate) without a permissive fallback ─────────────────────────────────
def test_drifted_governed_plan_fails_closed_409_without_permissive_fallback(
        client, db, monkeypatch, permissive_calls):
    """§9 items 6 + 8 — the plan drifts BETWEEN considered-set and draft (the FK column's concept
    changes, so the recomputed compiler-input fingerprint no longer matches the pinned stamp): the
    draft is refused 409 (regenerate) and the permissive path is provably NOT the fallback."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)
    _fresh_now(db, "ops", "rev")
    _inject_fixture_template(monkeypatch)
    res = client.post("/contract/considered-set", json=_governed_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    intent_id = res.json()["intent_id"]

    # drift ops (mirror test_draft_rebinding): the FK column's concept flips account_id → customer_id
    rows = [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "customer_id"),
    ]
    build_graph(db, "ops", [r for r, _ in rows], concepts={content_hash(r): c for r, c in rows})

    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "alternative",
        "chosen_option_id": "t_roll", "why": ""}, headers=AUTH)
    assert dr.status_code == 409, dr.text
    assert "regenerate" in dr.json()["detail"]
    assert permissive_calls == []     # fail-closed, never a permissive substitute path
