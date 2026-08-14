"""Phase-3C.2a Task 7 (§9 item 8) — the STRUCTURAL no-permissive-path-when-live guarantee.

``find_cross_catalog_path`` — BOTH live bindings: the ``entity`` origin and the ``contract.author``
import — is replaced with a function that RECORDS the call and RAISES. Live activation is genuinely
enabled (flag + deployment id + a persisted PASS evaluation + an APPROVE decision), a cross-catalog
catalog set is seeded (ops + rev + a VERIFIED bridge), and the flag-on cross-catalog request is
driven over HTTP with the recorder provably never invoked. The recorder list matters: a
savepoint-swallowed invocation would not surface as a 500, but it WOULD land in the list.

What closes the permissive path CHANGED with the E4 cutover (2026-08-14), and to something
stronger. These tests used to drive the whole entity-scoped flow — considered-set → draft → confirm
— and prove each stage either succeeded or failed closed without reaching the permissive
implementation. Now ``/contract/considered-set`` REFUSES the entity-only request outright: a
confirmed scope with no ``catalog_source`` gets a typed 422 ``SEMANTIC_REQUIRES_CATALOG_SOURCE``,
because the semantic engine plans over ONE frozen catalog context and the free-form generator that
used to fill an entity-only page is deleted — an honest refusal replacing a silently empty page. So
no flag-on cross-catalog generation runs AT ALL, which is a stronger closure than "it ran and did
not take the permissive branch": there is no branch left to take.

(The governed cross-catalog machinery itself is unchanged and still covered where it now lives —
``test_h3_typed_governed_deps`` confirms over the server's real envelope→ordered_path rewrite, and
``test_gate1_governed_lens`` covers the builder's lens.)
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
)
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.api.app import create_app
from featuregen.api.deps import get_conn, get_feature_gen_conn
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

HYPOTHESIS = "customers churn when their balance drops"
CHURN = "customer.relationship_attrition.churn"

#: The entity-only cross-catalog request: a confirmed scope naming a target ENTITY and no
#: ``catalog_source``. Owned here rather than imported, because the refusal it now provokes is this
#: suite's subject and must not drift with another suite's fixture.
_ENTITY_ONLY_BODY = {
    "hypothesis": HYPOTHESIS, "objective": "predict churn",
    "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                        "target_entity": "account"},
}
_REFUSAL = "SEMANTIC_REQUIRES_CATALOG_SOURCE"


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient on the suite's rolled-back connection (mirrors tests/featuregen/api/conftest.py's
    make_client — that fixture is directory-scoped, so this file builds its own)."""
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    # This 3C.2a suite predates Delivery 0 and tests planner activation, not recognition lineage.
    # Production defaults to confirmation_required; the dedicated Delivery 0 suite covers it.
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "legacy_unscoped")
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


# ── the flag-on cross-catalog request is REFUSED before any generation runs ───────────────────────────
def test_flag_on_cross_catalog_request_is_refused_and_never_reaches_the_permissive_path(
        client, db, monkeypatch, permissive_calls):
    """§9 item 8 — the closure at its strongest form. Live activation is genuinely on and a
    RESOLVABLE cross-catalog plan is seeded, so nothing about the environment is what stops the
    request: the route itself refuses an entity-only scope with a typed 422
    ``SEMANTIC_REQUIRES_CATALOG_SOURCE`` (E4 cutover, 2026-08-14). No generation dispatches, no
    considered set is minted, and ``find_cross_catalog_path`` is provably never invoked — not
    because a flag-on path avoided it, but because no flag-on cross-catalog path exists to run."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)                   # ops + rev + a VERIFIED bridge → a resolvable cross-catalog plan
    _fresh_now(db, "ops", "rev")      # fresh as of the route's real wall clock

    res = client.post("/contract/considered-set", json=_ENTITY_ONLY_BODY, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == _REFUSAL
    # The refusal precedes every DURABLE write — asserted across the whole write set, not just the
    # considered set. (The E4 cutover placed the refusal after the run mint and scope persist, so
    # this request used to leave a generation run and a confirmed scope behind; the E4 follow-up
    # moved it above them.)
    assert db.execute("SELECT count(*) FROM contract_considered").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM contract_intent").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0
    assert permissive_calls == []     # the structural guarantee


# ── the refusal is unconditional — drift cannot open a permissive fallback either ─────────────────────
def test_a_drifted_cross_catalog_catalog_is_refused_the_same_way(
        client, db, monkeypatch, permissive_calls):
    """§9 items 6 + 8 — the drift scenario, restated after the E4 cutover (2026-08-14).

    This used to stage a plan that drifted BETWEEN considered-set and draft (the FK column's concept
    flips, so the recomputed compiler-input fingerprint no longer matches the pinned stamp) and prove
    the draft was refused 409 rather than falling back to the permissive path. That sequence needs an
    entity-only considered set to drift FROM, and the route no longer mints one. The invariant it
    protected survives in a stronger form: the refusal is a property of the REQUEST SHAPE, not of
    catalog health, so a drifted catalog is refused identically — there is no state of the world in
    which a flag-on cross-catalog request reaches generation and needs a fallback."""
    _enable_live(db, monkeypatch)
    _cross_seed(db)
    _fresh_now(db, "ops", "rev")

    # drift ops (mirror test_draft_rebinding): the FK column's concept flips account_id → customer_id
    rows = [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "customer_id"),
    ]
    build_graph(db, "ops", [r for r, _ in rows], concepts={content_hash(r): c for r, c in rows})

    res = client.post("/contract/considered-set", json=_ENTITY_ONLY_BODY, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == _REFUSAL
    assert permissive_calls == []     # fail-closed, never a permissive substitute path
