"""Delivery C0 Task 4 — the projection-readiness GATE + watermark capture.

C0-T3's snapshot reads PROJECTED catalog truth (the overlay read model + field decisions the
overlay projection materializes). If a load-bearing projection is LAGGED (its checkpoint sits behind
the event head) or DEGRADED (poisoned), the snapshot would seal STALE truth — so feature generation
must ABORT with :data:`CATALOG_PROJECTION_UNAVAILABLE`, never silently snapshot a lagged view.

These tests exercise: (1) READY — a caught-up overlay projection lets the build succeed AND records
the overlay checkpoint seq into the header's ``projection_watermarks``; (2) LAGGED → abort + NO
snapshot row; (3) DEGRADED → abort; (4) FAIL-CLOSED — an unknown/absent load-bearing projection is
treated as unavailable, never silently "ready". Reuses the SAME projection-health primitives the gate
does (``projections.runner``); it does NOT re-implement projection health.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
    build_metadata_snapshot,
    check_projection_readiness,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.projections.runner import _checkpoint_seq, _head_seq, _mark_degraded, run_projection

_SRC = "bank"
_TABLE = "accounts"
_BAL_OBJ = "public.accounts.balance"
_ASOF_OBJ = "public.accounts.as_of_date"
_BAL_REF = normalize_ref(_SRC, "public", _TABLE, "balance")
_REFS = [(_SRC, _BAL_OBJ), (_SRC, _ASOF_OBJ)]


def _rr(conn) -> None:
    """Pin REPEATABLE READ BEFORE the first query (mirrors the C0-T2 feature-gen connection). The
    build asserts this level, so every build call in these tests needs it."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _col(conn, object_ref: str, column_name: str, **cols) -> None:
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [_SRC, object_ref, "column", _TABLE, column_name]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    conn.execute(f"INSERT INTO graph_node ({', '.join(keys)}) VALUES ({placeholders})", vals)


def _seed_graph(conn) -> None:
    """A minimal in-scope graph so the build has real catalog items to snapshot."""
    _col(conn, _BAL_OBJ, "balance", additivity="non_additive",
         additivity_decision_id="fde_add_1", schema_name="finance")
    record_field_decision(
        conn, logical_ref=_BAL_REF, field_name="additivity",
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash("non_additive"),
        load_bearing_value_hash=canonical_hash("non_additive"), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)
    _col(conn, _ASOF_OBJ, "as_of_date", is_as_of=True, availability_fact_event_id="evt_av_1")


def _append_event(conn, expected_version: int):
    """Append one real event so the event head (``max(global_seq)``) advances. The aggregate is
    'run' (NOT 'overlay_fact'), so ``OverlayProjection.apply`` is a no-op for it — running the
    projection then just advances the overlay checkpoint to head WITHOUT any read-model change,
    which is exactly what a caught-up (lag-0) projection looks like."""
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    return append_event(
        conn,
        NewEvent(
            aggregate="run", aggregate_id="r", type="E", schema_version=1, payload={"i": expected_version},
            actor=IdentityEnvelope(subject="u", actor_kind="human", authenticated=True,
                                   auth_method="oidc", role_claims=()),
            provenance=ProvenanceEnvelope(artifact_type="DRAFT_CONTRACT", schema_version=1,
                                          producing_component="t@1"),
            run_id="r"),
        expected_version=expected_version, table_version=1)


# ── 1. READY: caught-up overlay projection → build succeeds + records the overlay watermark ──────────
def test_ready_path_records_overlay_watermark_in_header(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    _append_event(conn, 0)                      # advances the event head
    run_projection(conn, OverlayProjection())   # overlay checkpoint → head (no-op apply), lag 0
    caught_up = _checkpoint_seq(conn, "overlay")
    # genuinely caught up at a non-trivial seq (global_seq_seq is process-shared, so not asserting ==1)
    assert caught_up == _head_seq(conn) > 0

    # the gate itself returns the watermark for the caught-up projection
    assert check_projection_readiness(conn) == {"overlay": caught_up}

    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_ready", refs=_REFS, read_scope_hash="sha256:scope")

    (wm,) = conn.execute(
        "SELECT projection_watermarks FROM catalog_metadata_snapshot WHERE snapshot_id = %s",
        (ctx.snapshot_id,)).fetchone()
    assert wm == {"overlay": caught_up}         # the exact overlay checkpoint seq is pinned


# ── 2. LAGGED: overlay checkpoint behind the event head → abort, NO snapshot written ─────────────────
def test_lagged_projection_aborts_and_writes_no_snapshot(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    _append_event(conn, 0)                      # head → 1, but the overlay projection is NOT run
    assert _checkpoint_seq(conn, "overlay") < _head_seq(conn)   # checkpoint 0 < head 1 → lagged

    with pytest.raises(CatalogProjectionUnavailable) as exc:
        build_metadata_snapshot(
            conn, generation_run_id="genrun_lag", refs=_REFS, read_scope_hash="sha256:scope")
    assert exc.value.code == CATALOG_PROJECTION_UNAVAILABLE
    assert "LAGGED" in exc.value.detail

    # abort writes NOTHING — no snapshot header for this run (and no run manifest either)
    assert conn.execute(
        "SELECT count(*) FROM catalog_metadata_snapshot WHERE generation_run_id = %s",
        ("genrun_lag",)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM feature_generation_run WHERE generation_run_id = %s",
        ("genrun_lag",)).fetchone()[0] == 0


# ── 3. DEGRADED: a poison marker on the overlay projection → abort even when NOT lagged ──────────────
def test_degraded_projection_aborts(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ev = _append_event(conn, 0)                 # head → 1
    run_projection(conn, OverlayProjection())   # overlay checkpoint → 1 (lag 0 — isolate degraded)
    assert _checkpoint_seq(conn, "overlay") == _head_seq(conn)   # NOT lagged
    _mark_degraded(conn, "overlay", aggregate="overlay_fact", aggregate_id="poisoned",
                   reason="poison", event=ev)

    with pytest.raises(CatalogProjectionUnavailable) as exc:
        build_metadata_snapshot(
            conn, generation_run_id="genrun_deg", refs=_REFS, read_scope_hash="sha256:scope")
    assert exc.value.code == CATALOG_PROJECTION_UNAVAILABLE
    assert "DEGRADED" in exc.value.detail
    assert conn.execute(
        "SELECT count(*) FROM catalog_metadata_snapshot WHERE generation_run_id = %s",
        ("genrun_deg",)).fetchone()[0] == 0


# ── 4. FAIL-CLOSED: an unknown/absent load-bearing projection is unavailable, never "ready" ──────────
def test_unknown_load_bearing_projection_fails_closed(conn) -> None:
    # 'overlay' is caught up (default state), but an unknown load-bearing name has no checkpoint row:
    # the gate must NOT treat a missing checkpoint as lag-0 "ready".
    with pytest.raises(CatalogProjectionUnavailable) as exc:
        check_projection_readiness(conn, projections=("overlay", "no_such_projection"))
    assert exc.value.code == CATALOG_PROJECTION_UNAVAILABLE
    assert "untracked" in exc.value.detail or "no_such_projection" in exc.value.detail


def test_ready_gate_returns_overlay_watermark_on_default_state(conn) -> None:
    # the migrated default state: overlay checkpoint seeded at 0, no events → caught up at head 0.
    assert check_projection_readiness(conn) == {"overlay": 0}
