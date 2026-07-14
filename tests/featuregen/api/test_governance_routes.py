"""Confirmation-surface Task 5 — the `governance` API router (list / confirm / reject).

Three routes over the Task 3/4 domain functions: `GET /sources/{source}/governance/joins` (the
open-proposal queue), `POST /governance/joins/{fact_key}/confirm` and `.../reject` (dispatching
the REAL overlay `confirm_fact`/`reject_fact` commands). All three are gated by
`require_confirmer` (raw `platform-admin` role claim).

Pass B confirm surface (Task 2) adds the table-fact siblings on the SAME router:
`GET /sources/{source}/governance/table-facts` + `POST /governance/table-facts/{fact_key}/confirm`
and `.../reject` — SINGLE-confirmer (one platform-admin -> VERIFIED directly; four-eyes holds
because the proposer is the service enrichment actor), with the synchronous drain-then-project
`project_verified_table_fact` reporting `operational_projection` honestly ("projected" only when
the graph_node flag actually landed; a stale drift watermark's correct refusal is "pending").

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

from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR
from tests.featuregen.overlay.upload.passc.test_sealed_join_confirm_referents import (
    _seed_graph_nodes,
)
from tests.featuregen.overlay.upload.test_join_governance import (
    _seed_grain_fact,
    _seed_join_with_evidence,
)
from tests.featuregen.overlay.upload.test_table_fact_governance import (
    _seed_availability,
    _seed_grain,
)
from tests.featuregen.overlay.upload.test_table_fact_governance import (
    _seed_graph_nodes as _seed_table_graph_nodes,
)

from featuregen.api.deps import get_conn
from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.catalog_changes import _write_watermark, detect_catalog_changes
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.upload_catalog import (
    UploadCatalog,
    ensure_upload_catalog_adapter,
)


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
    # Sealed config also arms resolve_fact's read-time DRIFT-FRESHNESS guard, which the synchronous
    # post-VERIFIED projection resolves through: seed the drift watermark the way production does —
    # the upload that produced this proposal ran detect_catalog_changes (ingest.py) before Pass C.
    detect_catalog_changes(
        conn,
        UploadCatalog("src", [CanonicalRow("src", "transactions", "cif_id", "text"),
                              CanonicalRow("src", "customers", "cif_id", "text", is_grain=True)]),
        actor=SERVICE_ACTOR, open_reverify=False)

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

    # a DISTINCT admin2 confirms -> VERIFIED, and the join is made operational IN THIS REQUEST
    # (whole-branch review, FIX 2): the confirm appended OVERLAY_FACT_CONFIRMED on the SAME
    # uncommitted connection, so project_verified_join must drain the overlay projection on that
    # conn before projecting — pre-fix projection_lag was always >= 1 here, the helper returned
    # "pending" unconditionally, and no edge existed until a future re-ingest.
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("rahman"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "projected"
    assert len(body["approvals"]) == 2
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    # the operational graph_edge exists NOW and the planner traverses it — no re-ingest
    rows = conn.execute(
        "SELECT authority, approved_join_fact_key, approved_join_status FROM graph_edge"
        " WHERE kind = 'joins' AND catalog_source = 'src'").fetchall()
    assert rows == [("operational", key, "VERIFIED")]
    assert find_join_path(conn, "src", "transactions", "customers") == \
        [JoinStep("public.transactions.cif_id", "public.customers.cif_id", "N:1")]


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
                    json={"category": "different_entity", "note": "watchlist CIF"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "different_entity"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    # `category` is a first-class payload field (reliable analytics key); `reason` carries
    # ONLY the free-text note — NOT the old "category: note" serialization.
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"]
    assert rejected[-1].payload["category"] == "different_entity"
    assert rejected[-1].payload["reason"] == "watchlist CIF"
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
    # no note -> reason is None (NOT the category string); category stands alone
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"]
    assert rejected[-1].payload["category"] == "needs_data_check"
    assert rejected[-1].payload["reason"] is None


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


# ═════════════════════════════ Table-fact routes (Pass B confirm surface, Task 2) ════════════════


@pytest.fixture
def seeded_grain(overlay_env):
    """One open grain proposal (table 't', columns ['cif_id'], source 'src') seeded through the
    REAL Pass B propose path (`propose_fact` from the service enrichment actor — opens the
    platform-admin gate task)."""
    ref, key = _seed_grain(overlay_env)
    return ref, key


def _grain_flags(conn, source="src", table="t"):
    return {c: (g, e) for c, g, e in conn.execute(
        "SELECT column_name, is_grain, grain_fact_event_id FROM graph_node "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (source, table)).fetchall()}


# ── (1) GET lists the open table-fact proposal ───────────────────────────────────────────────────


def test_table_fact_get_lists_open_grain_proposal(client, seeded_grain):
    _ref, key = seeded_grain
    r = client.get("/sources/src/governance/table-facts", headers=_h("priya"))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "src"
    assert body["next_cursor"] is None
    (p,) = body["proposals"]
    assert p["fact_key"] == key
    assert p["status"] == "PROPOSED"
    assert p["fact_type"] == "grain"
    assert p["proposed_value"] == {"columns": ["cif_id"], "is_unique": True}
    assert p["origin"] == "llm_proposed_not_profiled"


def test_table_fact_get_excludes_other_sources(client, seeded_grain):
    r = client.get("/sources/some-other-source/governance/table-facts", headers=_h("priya"))
    assert r.status_code == 200
    assert r.json()["proposals"] == []


# ── (2) SINGLE-admin happy path — sealed config + graph_node rows + FRESH watermark ─────────────


def test_table_fact_single_admin_confirm_verifies_and_projects(
        client, sealed_config, seeded_grain, conn):
    _ref, key = seeded_grain
    _seed_table_graph_nodes(conn)   # column nodes for table 't' — the projection's target rows
    # Sealed config arms resolve_fact's read-time drift-freshness guard: a FRESH watermark
    # (within the default 60m SLA) lets the synchronous post-VERIFIED projection actually land.
    _write_watermark(conn, "src", datetime.now(UTC))

    r = client.post(f"/governance/table-facts/{key}/confirm", json={"note": "grain looks right"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    # ONE platform-admin confirm -> VERIFIED directly (single-confirmer path: proposer is the
    # service actor, so four-eyes holds) — no PARTIALLY_CONFIRMED, no approvals array.
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "projected"
    assert "approvals" not in body
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    # the grain flag landed on graph_node IN THIS REQUEST, with fact-event provenance
    flags = _grain_flags(conn)
    assert flags["cif_id"][0] is True and flags["cif_id"][1] is not None
    assert flags["amt"][0] is False
    # a verified fact leaves the open queue
    r = client.get("/sources/src/governance/table-facts", headers=_h("priya"))
    assert r.json()["proposals"] == []


def test_table_fact_availability_confirm_verifies_and_projects(
        client, sealed_config, overlay_env, conn):
    """The availability_time sibling of the grain happy path (whole-branch review FIX 3): one
    platform-admin confirm on a Pass B as-of proposal -> VERIFIED + synchronously projected —
    graph_node.is_as_of lands on the proposed column with fact-event provenance."""
    _ref, key = _seed_availability(overlay_env, table="t", column="tran_date")
    _seed_table_graph_nodes(conn, cols=("cif_id", "tran_date"))
    _write_watermark(conn, "src", datetime.now(UTC))   # fresh: within the default 60m SLA

    r = client.post(f"/governance/table-facts/{key}/confirm", json={"note": "as-of looks right"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "projected"
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    # is_as_of landed on tran_date IN THIS REQUEST, with fact-event provenance — and only there
    flags = {c: (a, e) for c, a, e in conn.execute(
        "SELECT column_name, is_as_of, availability_fact_event_id FROM graph_node "
        "WHERE catalog_source = 'src' AND table_name = 't' AND kind = 'column'").fetchall()}
    assert flags["tran_date"][0] is True and flags["tran_date"][1] is not None
    assert flags["cif_id"][0] is False
    # a verified fact leaves the open queue
    r = client.get("/sources/src/governance/table-facts", headers=_h("priya"))
    assert r.json()["proposals"] == []


# ── (3) stale watermark: VERIFIED but the projection honestly defers ─────────────────────────────


def test_table_fact_confirm_stale_watermark_verified_but_pending(
        client, sealed_config, seeded_grain, conn):
    _ref, key = seeded_grain
    _seed_table_graph_nodes(conn)
    _write_watermark(conn, "src", datetime.now(UTC) - timedelta(hours=2))  # stale vs the 60m SLA

    r = client.post(f"/governance/table-facts/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "pending"
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    # resolve_fact correctly refused to serve under a stale watermark — NO flag landed
    assert not any(g for g, _e in _grain_flags(conn).values())


# ── (4) fact_type gate: a join fact_key 404s on BOTH mutation routes, no event written ───────────


def test_table_fact_routes_404_on_join_fact_without_writing_events(client, seeded_join, conn):
    _ref, join_key = seeded_join
    before = len(load_fact(conn, join_key))

    r = client.post(f"/governance/table-facts/{join_key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 404
    r = client.post(f"/governance/table-facts/{join_key}/reject",
                    json={"category": "wrong_grain_columns"}, headers=_h("priya"))
    assert r.status_code == 404
    # the 404 fires BEFORE any command dispatch — the join stream is untouched
    assert len(load_fact(conn, join_key)) == before


# ── (5) reject: {category, note} -> REJECTED with category on the event payload ─────────────────


def test_table_fact_reject_records_category(client, seeded_grain, conn):
    _ref, key = seeded_grain
    r = client.post(f"/governance/table-facts/{key}/reject",
                    json={"category": "wrong_grain_columns", "note": "grain is (cif_id, dt)"},
                    headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json() == {"governance_status": "REJECTED", "category": "wrong_grain_columns"}
    assert fold_overlay_state(load_fact(conn, key)).status == "REJECTED"
    # `category` is a first-class payload field; `reason` carries ONLY the free-text note
    rejected = [e for e in load_fact(conn, key) if e.type == "OVERLAY_FACT_REJECTED"]
    assert rejected[-1].payload["category"] == "wrong_grain_columns"
    assert rejected[-1].payload["reason"] == "grain is (cif_id, dt)"
    # a rejected fact is no longer an open proposal
    r = client.get("/sources/src/governance/table-facts", headers=_h("priya"))
    assert r.json()["proposals"] == []


# ── (6) authz: a non-admin is 403'd on all three routes ──────────────────────────────────────────


def test_table_fact_non_admin_403_on_all_three_routes(client):
    viewer = _h("priya", roles="catalog_viewer")
    assert client.get("/sources/src/governance/table-facts", headers=viewer).status_code == 403
    assert client.post("/governance/table-facts/any-key/confirm", json={},
                       headers=viewer).status_code == 403
    assert client.post("/governance/table-facts/any-key/reject",
                       json={"category": "wrong_grain_columns"},
                       headers=viewer).status_code == 403


# ── (7) request validation + unknown fact_key ────────────────────────────────────────────────────


def test_table_fact_bad_category_422(client):
    # "wrong_direction" is a valid JOIN category — the table-fact enum must not accept it
    r = client.post("/governance/table-facts/any-key/reject",
                    json={"category": "wrong_direction"}, headers=_h("priya"))
    assert r.status_code == 422


def test_table_fact_over_length_note_422(client):
    long_note = "x" * 1001
    r = client.post("/governance/table-facts/any-key/confirm", json={"note": long_note},
                    headers=_h("priya"))
    assert r.status_code == 422
    r = client.post("/governance/table-facts/any-key/reject",
                    json={"category": "not_unique", "note": long_note}, headers=_h("priya"))
    assert r.status_code == 422


def test_table_fact_list_limit_bounds_422(client):
    assert client.get("/sources/src/governance/table-facts?limit=0",
                      headers=_h("priya")).status_code == 422
    assert client.get("/sources/src/governance/table-facts?limit=501",
                      headers=_h("priya")).status_code == 422


def test_table_fact_unknown_fact_key_404(client):
    r = client.post("/governance/table-facts/no-such-key/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 404
    r = client.post("/governance/table-facts/no-such-key/reject",
                    json={"category": "needs_data_check"}, headers=_h("priya"))
    assert r.status_code == 404


# ═══════════ Denial-audit durability — audit I-3 redo (commit the deny-path tx) ══════════════════


def _denial_rows(conn) -> list[str]:
    """The raw denied_reason of every COMMAND_DENIED row on the tamper-evident chain."""
    return [r[0] for r in conn.execute(
        "SELECT reason FROM security_audit WHERE event_type = 'COMMAND_DENIED' ORDER BY seq"
    ).fetchall()]


@pytest.fixture
def prod_tx_client(client, conn):
    """`client` with get_conn re-overridden to PRODUCTION transaction semantics at savepoint
    granularity: a route that RETURNS releases the savepoint (deps.get_conn: commit), a route
    that RAISES rolls back to it (deps.get_conn: rollback). The plain `client` override does
    neither, so under it a COMMAND_DENIED row written on the request conn stays visible after a
    409 whether or not production get_conn would have rolled it back — THIS override is what
    makes denial-audit DURABILITY (audit I-3) observable inside the suite's rolled-back outer
    transaction."""
    conn.execute("SELECT 1")   # open the outer tx so conn.transaction() nests as a SAVEPOINT

    def _prod_like():
        with conn.transaction():
            yield conn

    client.app.dependency_overrides[get_conn] = _prod_like
    return client


def test_denied_confirm_commits_a_durable_command_denied_row(prod_tx_client, seeded_join, conn):
    """A four-eyes/SoD denial must leave a DURABLE trace: the overlay's `_deny_audited` writes
    COMMAND_DENIED on the REQUEST connection, so the route must COMMIT the deny-path tx (return
    the 409, not raise it) — raising makes get_conn roll the row back and an insider probe
    leaves zero evidence (audit I-3)."""
    _ref, key = seeded_join
    r = prod_tx_client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 200, r.text

    # same-admin repeat -> the overlay denies (SoD), the 409 body is the HTTPException-identical
    # contract, AND the COMMAND_DENIED row SURVIVES the request (the deny-path tx committed).
    r = prod_tx_client.post(f"/governance/joins/{key}/confirm", json={}, headers=_h("priya"))
    assert r.status_code == 409
    assert r.json() == {"detail": "You already approved this — a different admin must confirm."}
    assert _denial_rows(conn) == ["this owner already confirmed; awaiting the other owner"]


def test_successful_confirm_writes_no_denial_row(prod_tx_client, seeded_join, conn):
    _ref, key = seeded_join
    r = prod_tx_client.post(f"/governance/joins/{key}/confirm", json={"note": "cif ok"},
                            headers=_h("priya"))
    assert r.status_code == 200, r.text
    assert r.json()["governance_status"] == "PARTIALLY_CONFIRMED"
    assert _denial_rows(conn) == []


def test_denied_409_is_returned_not_raised_on_all_four_routes(
        prod_tx_client, seeded_join, seeded_grain, conn):
    """Every governed deny path RETURNS its 409 (committing the request tx) with the exact
    `{"detail": ...}` body HTTPException produced — the frontend contract is unchanged. The
    not-awaiting denials here are benign plain-CommandResult denials (no audit row), proving
    the deny-path commit is a harmless no-op when there is nothing to persist."""
    _jref, join_key = seeded_join
    _gref, grain_key = seeded_grain
    assert prod_tx_client.post(f"/governance/joins/{join_key}/reject",
                               json={"category": "needs_data_check"},
                               headers=_h("priya")).status_code == 200
    assert prod_tx_client.post(f"/governance/table-facts/{grain_key}/reject",
                               json={"category": "not_unique"},
                               headers=_h("priya")).status_code == 200

    body = {"detail": "fact not awaiting confirmation (status=REJECTED)"}
    r = prod_tx_client.post(f"/governance/joins/{join_key}/confirm", json={}, headers=_h("priya"))
    assert (r.status_code, r.json()) == (409, body)
    r = prod_tx_client.post(f"/governance/joins/{join_key}/reject",
                            json={"category": "wrong_direction"}, headers=_h("priya"))
    assert (r.status_code, r.json()) == (409, body)
    r = prod_tx_client.post(f"/governance/table-facts/{grain_key}/confirm", json={},
                            headers=_h("priya"))
    assert (r.status_code, r.json()) == (409, body)
    r = prod_tx_client.post(f"/governance/table-facts/{grain_key}/reject",
                            json={"category": "needs_data_check"}, headers=_h("priya"))
    assert (r.status_code, r.json()) == (409, body)
    assert _denial_rows(conn) == []   # benign denials are unaudited; the commit persisted nothing
