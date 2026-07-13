"""Confirmation-surface Task 5 — the `governance` API router (list / confirm / reject).

Three routes over the Task 3/4 domain functions: `GET /sources/{source}/governance/joins` (the
open-proposal queue), `POST /governance/joins/{fact_key}/confirm` and `.../reject` (dispatching
the REAL overlay `confirm_fact`/`reject_fact` commands). All three are gated by
`require_confirmer` (raw `platform-admin` role claim).

THE HAPPY PATH RUNS UNDER A SEALED OVERLAY CONFIG (`register_overlay_config`) — the deployed
posture (api/app.py seals one at startup), which arms the SP-1.5 referent gate at the dual-join
SECOND confirm. That gate validates the join's endpoints against `graph_node` under the sentinel
`UploadContextAdapter` (Task 0, d863a3e), so the test seeds the graph rows for BOTH endpoints —
without them admin2 is denied; without the seal the test would not exercise the production gate
(and would miss a Task-0 regression).

Seeding reuses the Task 3/4 helpers (`_seed_join_with_evidence`, `_seed_grain_fact`) on the
suite's rolled-back `conn` — the same connection the TestClient's `get_conn` override serves, so
the API reads exactly what the seed wrote. The event registry is reset per test by the root
harness and the API conftest does not re-register overlay schemas (the app lifespan does, but
only once the client exists), so the seeding fixture registers them itself.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload.passc.test_sealed_join_confirm_referents import (
    _seed_graph_nodes,
)
from tests.featuregen.overlay.upload.test_join_governance import (
    _seed_grain_fact,
    _seed_join_with_evidence,
)

from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter


def _h(user: str, roles: str = "platform-admin") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The routes self-register the upload-context adapter and the app lifespan seals an overlay
    config — both PROCESS globals. Clear them after every test in this module so nothing leaks
    into a suite that expects the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def overlay_env(conn):
    """The seeding preconditions the overlay conftests normally provide (this module lives under
    tests/featuregen/api/, outside their scope): the OVERLAY_FACT_* event schemas (the root
    harness resets the registry per test) and the upload-context catalog adapter
    (`propose_fact` -> `resolve_authority` needs one)."""
    register_overlay_event_types(event_registry())
    ensure_upload_catalog_adapter()
    return conn


@pytest.fixture
def seeded_join(overlay_env):
    """One open dual-join proposal (transactions.cif_id -> customers.cif_id under source 'src'),
    seeded through the REAL propose path with its pre-minted Pass C evidence row."""
    ref, key = _seed_join_with_evidence(overlay_env)
    return ref, key


@pytest.fixture
def sealed_config(client):
    """Seal the process-wide OverlayConfig from an EMPTY env — the production gate, immune to
    ambient OVERLAY_* vars. MUST depend on `client`: the app lifespan seals its own env-based
    config at TestClient startup and `register_overlay_config` is last-writer-wins, so sealing
    before the client exists would be silently overwritten."""
    register_overlay_config(overlay_config_from_env({}))
    yield
    _clear_overlay_config()


# ── (1) GET lists the open proposal ──────────────────────────────────────────────────────────────


def test_get_lists_open_join_with_evidence(client, seeded_join):
    _ref, key = seeded_join
    r = client.get("/sources/src/governance/joins", headers=_h("priya"))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "src"
    assert body["next_cursor"] is None
    assert len(body["proposals"]) == 1
    p = body["proposals"][0]
    assert p["fact_key"] == key
    assert p["status"] == "PROPOSED"
    assert p["from"] == {"table": "transactions", "column": "cif_id"}
    assert p["to"] == {"table": "customers", "column": "cif_id"}
    assert p["evidence_parse_status"] == "parsed"
    assert isinstance(p["evidence"]["score"], int)


def test_get_excludes_other_sources(client, seeded_join):
    r = client.get("/sources/some-other-source/governance/joins", headers=_h("priya"))
    assert r.status_code == 200
    assert r.json()["proposals"] == []


# ── (2) Dual-admin happy path — SEALED config + graph_node rows (Task 0 posture) ────────────────


def test_dual_admin_confirm_reaches_verified_under_sealed_config(
        client, sealed_config, seeded_join, conn):
    _ref, key = seeded_join
    _seed_graph_nodes(conn)   # BOTH endpoints must exist in graph_node for the sealed referent gate

    # admin1 confirms with a note -> PARTIALLY_CONFIRMED, no projection yet
    r = client.post(f"/governance/joins/{key}/confirm", json={"note": "cif ok"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "PARTIALLY_CONFIRMED"
    assert body["operational_projection"] == "not_applicable"
    assert [a["note"] for a in body["approvals"]] == ["cif ok"]

    # admin1 repeats -> 409 with the friendly different-admin message
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 409
    assert "different admin" in r.json()["detail"]

    # the note is visible to the second approver in the queue
    r = client.get("/sources/src/governance/joins", headers=_h("rahman"))
    assert r.status_code == 200
    (p,) = r.json()["proposals"]
    assert p["status"] == "PARTIALLY_CONFIRMED"
    assert p["approvals"][0]["note"] == "cif ok"

    # a DISTINCT admin2 confirms -> VERIFIED + a projection outcome
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("rahman"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] in ("projected", "pending")
    assert len(body["approvals"]) == 2
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"


# ── (3) fact_type gate: a non-join fact_key 404s on BOTH mutation routes, no event written ──────


def test_non_join_fact_404s_without_writing_events(client, overlay_env):
    grain_key = _seed_grain_fact(overlay_env)
    before = len(load_fact(overlay_env, grain_key))

    r = client.post(f"/governance/joins/{grain_key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 404
    r = client.post(f"/governance/joins/{grain_key}/reject",
                    json={"category": "not_a_real_key"}, headers=_h("priya"))
    assert r.status_code == 404
    # the 404 fires BEFORE any command dispatch — the grain stream is untouched
    assert len(load_fact(overlay_env, grain_key)) == before


# ── (4) reject: {category, note} -> REJECTED; partial-then-reject is terminal ───────────────────


def test_reject_records_category(client, seeded_join, conn):
    _ref, key = seeded_join
    r = client.post(f"/governance/joins/{key}/reject",
                    json={"category": "wrong_direction", "note": "customers own the key"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "wrong_direction"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    # the event's schema-typed string reason keeps the category machine-extractable as the prefix
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"]
    assert rejected[-1].payload["reason"] == "wrong_direction: customers own the key"
    # a rejected join is no longer an open proposal
    r = client.get("/sources/src/governance/joins", headers=_h("priya"))
    assert r.json()["proposals"] == []


def test_partial_confirm_then_reject_is_terminal(client, seeded_join, conn):
    _ref, key = seeded_join
    r = client.post(f"/governance/joins/{key}/confirm", json={"note": "side one"},
                    headers=_h("priya"))
    assert r.status_code == 200 and r.json()["governance_status"] == "PARTIALLY_CONFIRMED"

    r = client.post(f"/governance/joins/{key}/reject",
                    json={"category": "needs_data_check"}, headers=_h("rahman"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "needs_data_check"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"


# ── (5) authz: a non-admin is 403'd on all three routes ─────────────────────────────────────────


def test_non_admin_403_on_all_three_routes(client):
    viewer = _h("priya", roles="catalog_viewer")
    assert client.get("/sources/src/governance/joins", headers=viewer).status_code == 403
    assert client.post("/governance/joins/any-key/confirm", json={},
                       headers=viewer).status_code == 403
    assert client.post("/governance/joins/any-key/reject",
                       json={"category": "wrong_direction"}, headers=viewer).status_code == 403


# ── (6) request validation: bad category / over-length note -> 422 ──────────────────────────────


def test_bad_category_422(client):
    r = client.post("/governance/joins/any-key/reject",
                    json={"category": "just_dont_like_it"}, headers=_h("priya"))
    assert r.status_code == 422


def test_over_length_note_422(client):
    long_note = "x" * 1001
    r = client.post("/governance/joins/any-key/confirm", json={"note": long_note},
                    headers=_h("priya"))
    assert r.status_code == 422
    r = client.post("/governance/joins/any-key/reject",
                    json={"category": "wrong_direction", "note": long_note}, headers=_h("priya"))
    assert r.status_code == 422


def test_list_limit_bounds_422(client):
    assert client.get("/sources/src/governance/joins?limit=0",
                      headers=_h("priya")).status_code == 422
    assert client.get("/sources/src/governance/joins?limit=501",
                      headers=_h("priya")).status_code == 422


# ── (7) unknown fact_key -> 404 ─────────────────────────────────────────────────────────────────


def test_unknown_fact_key_404(client):
    r = client.post("/governance/joins/no-such-fact-key/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 404
    r = client.post("/governance/joins/no-such-fact-key/reject",
                    json={"category": "wrong_direction"}, headers=_h("priya"))
    assert r.status_code == 404
