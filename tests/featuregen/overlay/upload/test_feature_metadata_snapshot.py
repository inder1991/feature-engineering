"""Delivery C0 Task 3 — the immutable feature-generation metadata snapshot BUILDER.

Asserts the C0 contract: under REPEATABLE READ the builder reads catalog facts through the authority
adapter, persists them write-once (migration 1006), and returns a :class:`SnapshotContext` whose
``facts(...)`` are served FROM the snapshot (never a live re-query). Key properties exercised here:
(1) persisted header + N items; (2) DETERMINISM (same committed state ⇒ same ``content_hash``);
(3) IMMUTABILITY (write-once rows); (4) facts served from memory match the live adapter read;
(5) a non-REPEATABLE-READ connection is a hard error, never a silent degrade.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.feature_metadata_snapshot import (
    SnapshotContext,
    SnapshotIsolationError,
    build_metadata_snapshot,
)
from featuregen.overlay.upload.object_ref import normalize_ref

_SRC = "bank"
_TABLE = "accounts"
_BAL_OBJ = "public.accounts.balance"
_ASOF_OBJ = "public.accounts.as_of_date"
# The balance column is seeded with schema_name="finance" (see _seed_graph /
# test_snapshot_drift_seals_non_governed), so its REAL logical_ref is schema-preserving, not
# public — this is what logical_ref_of now resolves it to (was hardcoded "public"; the fix this
# suite exercises), and it is the key every decision/evidence read below must match.
_BAL_REF = normalize_ref(_SRC, "finance", _TABLE, "balance")
_ASOF_REF = normalize_ref(_SRC, "public", _TABLE, "as_of_date")
_REFS = [(_SRC, _BAL_OBJ), (_SRC, _ASOF_OBJ)]


def _rr(conn) -> None:
    """Pin the connection to REPEATABLE READ BEFORE its first query (mirrors the C0-T2 feature-gen
    connection). Must run before any SQL on ``conn`` — psycopg refuses a mid-transaction change."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _col(conn, object_ref: str, column_name: str, **cols) -> None:
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [_SRC, object_ref, "column", _TABLE, column_name]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    conn.execute(f"INSERT INTO graph_node ({', '.join(keys)}) VALUES ({placeholders})", vals)


def _govern_additivity(conn, logical_ref: str, value: str) -> None:
    """Record a load-bearing RESOLVED decision so additivity reads as ``governed``."""
    record_field_decision(
        conn, logical_ref=logical_ref, field_name="additivity",
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None,
        supersedes_event_id=None)


def _seed_graph(conn) -> None:
    """A small graph: a governed-additivity balance column + a governed is_as_of date column."""
    _col(conn, _BAL_OBJ, "balance", additivity="non_additive",
         additivity_decision_id="fde_add_1", schema_name="finance")
    _govern_additivity(conn, _BAL_REF, "non_additive")
    _col(conn, _ASOF_OBJ, "as_of_date", is_as_of=True,
         availability_fact_event_id="evt_av_1")


# ── 1. persists a header + items; snapshot facts match the live authority read ─────────────────────────
def test_build_persists_header_and_items_and_facts_match_live(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_snap_1", refs=_REFS, read_scope_hash="sha256:scope")

    assert isinstance(ctx, SnapshotContext)
    header = conn.execute(
        "SELECT generation_run_id, read_scope_hash, isolation_level, content_hash "
        "FROM catalog_metadata_snapshot WHERE snapshot_id = %s", (ctx.snapshot_id,)).fetchone()
    assert header == ("genrun_snap_1", "sha256:scope", "repeatable read", ctx.content_hash)

    (n_items,) = conn.execute(
        "SELECT count(*) FROM catalog_metadata_snapshot_item WHERE snapshot_id = %s",
        (ctx.snapshot_id,)).fetchone()
    assert n_items == len(ctx.items()) > 0

    # the run manifest was created (FK parent of the header)
    assert conn.execute(
        "SELECT 1 FROM feature_generation_run WHERE generation_run_id = %s",
        ("genrun_snap_1",)).fetchone() is not None

    # snapshot facts equal what the live adapter returns for the same (source, object, field)
    for object_ref, ref, field in [
        (_BAL_OBJ, _BAL_REF, "additivity"), (_ASOF_OBJ, _ASOF_REF, "is_as_of")]:
        snap = ctx.facts(_SRC, object_ref, field)
        live = read_column_facts(conn, ref, field)
        assert snap == live, field


def test_governed_provenance_lands_in_the_right_link_column(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_prov", refs=_REFS, read_scope_hash="sha256:scope")

    add = conn.execute(
        "SELECT decision_event_id, fact_event_id FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND graph_ref = %s AND field_or_fact_type = 'additivity'",
        (ctx.snapshot_id, _BAL_OBJ)).fetchone()
    assert add == ("fde_add_1", None)   # decision-governed → decision_event_id link

    asof = conn.execute(
        "SELECT decision_event_id, fact_event_id FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND graph_ref = %s AND field_or_fact_type = 'is_as_of'",
        (ctx.snapshot_id, _ASOF_OBJ)).fetchone()
    assert asof == (None, "evt_av_1")   # fact-governed → fact_event_id link


def test_physical_ref_captures_declared_schema(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_phys", refs=_REFS, read_scope_hash="sha256:scope")
    (phys,) = conn.execute(
        "SELECT DISTINCT physical_ref FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND graph_ref = %s", (ctx.snapshot_id, _BAL_OBJ)).fetchone()
    assert phys == "finance"   # graph_node.schema_name, a light provenance read


# ── 2. DETERMINISM: same committed state ⇒ same content_hash ───────────────────────────────────────────
def test_content_hash_is_deterministic_over_the_same_state(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    a = build_metadata_snapshot(
        conn, generation_run_id="genrun_det", refs=_REFS, read_scope_hash="sha256:scope")
    b = build_metadata_snapshot(
        conn, generation_run_id="genrun_det", refs=_REFS, read_scope_hash="sha256:scope")
    assert a.snapshot_id != b.snapshot_id          # a fresh snapshot id each build
    assert a.content_hash == b.content_hash         # identical committed state ⇒ identical seal
    assert {i.item_hash for i in a.items()} == {i.item_hash for i in b.items()}


def test_content_hash_changes_when_read_scope_changes(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    a = build_metadata_snapshot(
        conn, generation_run_id="genrun_scope", refs=_REFS, read_scope_hash="sha256:scopeA")
    b = build_metadata_snapshot(
        conn, generation_run_id="genrun_scope", refs=_REFS, read_scope_hash="sha256:scopeB")
    assert a.content_hash != b.content_hash


# ── 3. IMMUTABILITY: header + items are write-once (migration-level guarantee) ──────────────────────────
def test_snapshot_rows_are_write_once(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_wo", refs=_REFS, read_scope_hash="sha256:scope")
    conn.execute("SELECT 1")   # open the outer tx before the savepoint
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE catalog_metadata_snapshot SET content_hash = 'tampered' "
                     "WHERE snapshot_id = %s", (ctx.snapshot_id,))
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE catalog_metadata_snapshot_item SET value_json = '{}'::jsonb "
                     "WHERE snapshot_id = %s", (ctx.snapshot_id,))
    # content is untouched: the persisted content_hash still equals the returned one
    (persisted,) = conn.execute(
        "SELECT content_hash FROM catalog_metadata_snapshot WHERE snapshot_id = %s",
        (ctx.snapshot_id,)).fetchone()
    assert persisted == ctx.content_hash


# ── 4. facts served FROM the snapshot, not a live column ───────────────────────────────────────────────
def test_facts_served_from_snapshot_not_live_columns(conn) -> None:
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_mem", refs=_REFS, read_scope_hash="sha256:scope")
    captured = ctx.facts(_SRC, _BAL_OBJ, "additivity")
    assert captured is not None and captured.value == "non_additive"

    # Mutate the LIVE column; the in-memory snapshot fact does not move (it is not a re-query).
    conn.execute("UPDATE graph_node SET additivity = 'additive' WHERE object_ref = %s", (_BAL_OBJ,))
    assert read_column_facts(conn, _BAL_REF, "additivity").value == "additive"   # live moved
    assert ctx.facts(_SRC, _BAL_OBJ, "additivity").value == "non_additive"       # snapshot frozen

    # a ref/field never captured yields None (no fabrication, no query)
    assert ctx.facts(_SRC, "public.accounts.unknown", "unit") is None


# ── C1: the C0 snapshot seals the OPERATIONAL status, and ONLY a hash-verified resolved head is
#    governed — a drifted graph value seals as NON-governed (never a false governed). ──────────────────
def test_snapshot_seals_c1_status_resolved_and_hint(conn) -> None:
    _rr(conn)
    _seed_graph(conn)   # governed non_additive (consistent) + hint-by-policy fields (unit/currency/…)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_status", refs=_REFS, read_scope_hash="sha256:scope")
    by_field = {i.field_or_fact_type: i for i in ctx.items() if i.graph_ref == _BAL_OBJ}
    # a GOVERNED, hash-verified additivity seals status="resolved" / authority="governed"
    assert by_field["additivity"].status == "resolved"
    assert by_field["additivity"].authority == "governed"
    # a hint-by-policy field never seals governed — it seals "not_operational"
    assert by_field["unit"].status == "not_operational"
    assert by_field["unit"].authority == "hint"
    # the sealed status rides in the persisted authority_json (and is part of the item_hash)
    (status_json,) = conn.execute(
        "SELECT authority_json->>'status' FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND graph_ref = %s AND field_or_fact_type = 'additivity'",
        (ctx.snapshot_id, _BAL_OBJ)).fetchone()
    assert status_json == "resolved"


def test_snapshot_drift_seals_non_governed(conn) -> None:
    """THE FIX at the snapshot boundary: the approved additivity decision is ``non_additive`` but the
    flat graph value DRIFTED to ``additive``. The OLD ``read_column_facts`` still sees governed; C1
    hash-verifies → the item seals ``status="hash_mismatch"`` / ``authority="hint"`` (NON-governed),
    so a drifted value can never seal (or be served downstream) as a false governed."""
    _rr(conn)
    _col(conn, _BAL_OBJ, "balance", additivity="additive",
         additivity_decision_id="fde_add_1", schema_name="finance")
    _govern_additivity(conn, _BAL_REF, "non_additive")   # approved value ≠ flat graph value
    # OLD-reader CONTROL: the permissive reader still serves GOVERNED-additive at this boundary.
    old = read_column_facts(conn, _BAL_REF, "additivity")
    assert old.authority == "governed" and old.value == "additive"
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_drift", refs=[(_SRC, _BAL_OBJ)],
        read_scope_hash="sha256:scope")
    add = next(i for i in ctx.items() if i.field_or_fact_type == "additivity")
    assert add.status == "hash_mismatch"
    assert add.authority == "hint"            # sealed NON-governed
    assert add.value is None                  # no operational value served on a drifted read
    assert add.decision_event_id is None      # provenance dropped for a non-governed seal
    (status_json,) = conn.execute(
        "SELECT authority_json->>'status' FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND field_or_fact_type = 'additivity'",
        (ctx.snapshot_id,)).fetchone()
    assert status_json == "hash_mismatch"
    # facts() served downstream (C2-C4) reflects the C1 status — a hint, not a false governed
    assert ctx.facts(_SRC, _BAL_OBJ, "additivity").authority == "hint"


def test_unknown_requested_field_raises_naming_it(conn) -> None:
    # D6 (Task 0.6): an unknown requested field used to be silently dropped — a caller asking for a
    # field the adapter does not model got a snapshot that quietly lacked it. It must RAISE.
    _rr(conn)
    _seed_graph(conn)
    with pytest.raises(ValueError, match="not_a_real_field"):
        build_metadata_snapshot(
            conn, generation_run_id="genrun_skip", refs=[(_SRC, _BAL_OBJ)],
            read_scope_hash="sha256:scope", fields=["additivity", "not_a_real_field"])
    # pure input validation — nothing was written for the doomed request
    assert conn.execute(
        "SELECT 1 FROM feature_generation_run WHERE generation_run_id = 'genrun_skip'"
    ).fetchone() is None


# ── 5. a non-REPEATABLE-READ connection is a hard isolation error ──────────────────────────────────────
def test_read_committed_connection_raises_isolation_error(conn) -> None:
    # default isolation (READ COMMITTED) — the C0-T2 guarantee is absent, so build must fail loudly
    assert conn.execute("SHOW transaction_isolation").fetchone()[0] == "read committed"
    with pytest.raises(SnapshotIsolationError, match="REPEATABLE READ"):
        build_metadata_snapshot(
            conn, generation_run_id="genrun_iso", refs=_REFS, read_scope_hash="sha256:scope")


# ── Task 0.6 Seam 4 (D6): six pin kinds, compat-safe hashing, kind-dispatched freshness ────────────────

def test_item_kind_vocabulary_is_the_pinned_list() -> None:
    from featuregen.overlay.upload.feature_metadata_snapshot import ITEM_KINDS
    assert ITEM_KINDS == frozenset({
        "column_field", "dataset_profile", "serving_policy", "source_selection",
        "physical_binding", "temporal_policy", "row_selection",
        # SE-2 (2026-08-11): the frozen Layer-A generation context's identity pin — the one
        # non-column kind with a REGISTERED comparator (rebuild-at-stored-scope).
        "generation_semantic_context"})


_GOLDEN_MATERIAL = {
    "catalog_source": "bank",
    "graph_ref": "public.accounts.balance",
    "field": "additivity",
    "value": "additive",
    "authority": "governed",
    "provenance": "fde_add_1",
    "status": "resolved",
}


def test_legacy_column_field_hash_is_byte_identical_to_the_pinned_literal() -> None:
    """Hash-compat golden (D6): `column_field` keeps the EXACT legacy computation — `item_kind`
    excluded — so every already-stored snapshot hash stays valid. The literal below was computed
    with the pre-seam code; changing it means invalidating stored snapshots."""
    from featuregen.overlay.upload.feature_metadata_snapshot import snapshot_item_hash
    assert snapshot_item_hash("column_field", _GOLDEN_MATERIAL) == \
        "6d654ba49047a4f0d57eeb3f56e315a3b3145f13aa3384c537cfc703a47b31c0"
    assert snapshot_item_hash("column_field", _GOLDEN_MATERIAL) == \
        canonical_hash(_GOLDEN_MATERIAL)


def test_identical_payloads_under_different_kinds_hash_differently() -> None:
    """Property (D6): a NEW kind hashes `item_kind` into its material, so two kinds can never
    collide on an identical payload — without touching any legacy column_field row."""
    from featuregen.overlay.upload.feature_metadata_snapshot import ITEM_KINDS, snapshot_item_hash
    hashes = {kind: snapshot_item_hash(kind, _GOLDEN_MATERIAL) for kind in sorted(ITEM_KINDS)}
    assert len(set(hashes.values())) == len(hashes)   # pairwise distinct across all six kinds
    with pytest.raises(ValueError, match="unregistered_kind"):
        snapshot_item_hash("unregistered_kind", _GOLDEN_MATERIAL)


def _restamp_item_kind(conn, snapshot_id: str, kind: str) -> None:
    """Restamp ONE stored item's kind, bypassing the 1006 write-once seal the way a future writer
    of the new kinds legitimately would (the seal guards items, not this test's simulation)."""
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")   # flush the deferred item->header FK events
    conn.execute("ALTER TABLE catalog_metadata_snapshot_item DISABLE TRIGGER USER")
    conn.execute(
        "UPDATE catalog_metadata_snapshot_item SET item_kind = %s WHERE snapshot_id = %s "
        "AND field_or_fact_type = 'additivity'", (kind, snapshot_id))
    conn.execute("ALTER TABLE catalog_metadata_snapshot_item ENABLE TRIGGER USER")


def test_stored_kind_without_a_comparator_is_a_typed_refusal_not_drift(conn) -> None:
    from featuregen.overlay.upload.feature_metadata_snapshot import compare_snapshot_to_current
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_kind", refs=[(_SRC, _BAL_OBJ)],
        read_scope_hash="sha256:scope", fields=["additivity"])
    # a registered vocabulary kind whose builder ships in a LATER task: typed refusal
    _restamp_item_kind(conn, ctx.snapshot_id, "dataset_profile")
    freshness = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert (freshness.status, freshness.reason) == ("unverifiable", "SNAPSHOT_KIND_UNSUPPORTED")
    # a kind outside the vocabulary entirely: the SAME typed refusal — never drift, never a crash
    _restamp_item_kind(conn, ctx.snapshot_id, "bogus_kind")
    freshness = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert (freshness.status, freshness.reason) == ("unverifiable", "SNAPSHOT_KIND_UNSUPPORTED")


def test_column_field_snapshot_still_verifies_current(conn) -> None:
    # dispatch regression: the one SUPPORTED kind keeps comparing exactly as before
    from featuregen.overlay.upload.feature_metadata_snapshot import compare_snapshot_to_current
    _rr(conn)
    _seed_graph(conn)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_cur", refs=[(_SRC, _BAL_OBJ)],
        read_scope_hash="sha256:scope", fields=["additivity"])
    freshness = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert (freshness.status, freshness.reason) == ("current", None)
