"""Delivery C0 Task 5 — the immutable metadata snapshot referenced at considered-set time.

When the considered set is built on the feature-generation connection (REPEATABLE READ, C0-T2) the
builder mints a generation run, snapshots the in-scope catalog state (C0-T3, migration 1006), and
records the lineage on the contract_considered row (migration 1007). draft/confirm then reload THAT
server snapshot (never a client-supplied id). This suite exercises: (1) an RR build persists the
lineage + a real snapshot with items for the candidate refs; (2) a READ COMMITTED caller takes NO
snapshot (additive — the columns stay NULL); (3) draft/confirm reload the SERVER lineage; (4) a
projection-lagged catalog ABORTS the whole considered set atomically (no considered-set row written).
"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    build_considered_set,
    considered_snapshot_lineage,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.projections.runner import _checkpoint_seq, _head_seq

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def _rr(db) -> None:
    """Pin REPEATABLE READ BEFORE the first query — mirrors the C0-T2 feature-generation connection the
    route uses. The snapshot builder asserts this level; the considered-set builder snapshots ONLY here."""
    db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _bank(db) -> None:
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _client() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _append_event(db, expected_version: int):
    """Append one real event so the event head advances (aggregate 'run' → the OverlayProjection is a
    no-op for it, so NOT running the projection leaves the overlay checkpoint behind head = LAGGED)."""
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    return append_event(
        db,
        NewEvent(
            aggregate="run", aggregate_id="r", type="E", schema_version=1,
            payload={"i": expected_version},
            actor=IdentityEnvelope(subject="u", actor_kind="human", authenticated=True,
                                   auth_method="oidc", role_claims=()),
            provenance=ProvenanceEnvelope(artifact_type="DRAFT_CONTRACT", schema_version=1,
                                          producing_component="t@1"),
            run_id="r"),
        expected_version=expected_version, table_version=1)


# ── 1. an RR build persists the lineage + a real snapshot with items for the candidate refs ────────────
def test_considered_set_persists_snapshot_lineage_on_repeatable_read(db):
    _rr(db)
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per account", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW,
                              generation_run_id="fgr_test_1")

    row = db.execute(
        "SELECT generation_run_id, snapshot_id, snapshot_content_hash "
        "FROM contract_considered WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row[0] == "fgr_test_1"        # the caller-supplied run is anchored
    assert row[1] and row[2]             # snapshot_id + content_hash are recorded

    # a real snapshot header exists for that run, and the run manifest was created (FK parent)
    header = db.execute(
        "SELECT snapshot_id, content_hash FROM catalog_metadata_snapshot "
        "WHERE generation_run_id = %s", ("fgr_test_1",)).fetchone()
    assert header == (row[1], row[2])
    assert db.execute("SELECT 1 FROM feature_generation_run WHERE generation_run_id = %s",
                      ("fgr_test_1",)).fetchone() is not None

    # the snapshot captured the candidate ref the set derives from (public.accounts.balance)
    (n_bal,) = db.execute(
        "SELECT count(*) FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND catalog_source = 'bank' AND graph_ref = %s",
        (row[1], "public.accounts.balance")).fetchone()
    assert n_bal > 0
    # the considered set itself is unchanged by the snapshot (additive)
    assert cs.anchor is not None and cs.anchor.name == "avg_balance_90d"


def test_considered_set_mints_fgr_run_when_none_supplied(db):
    _rr(db)
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    build_considered_set(db, intent, _client(), catalog_source="bank",
                         target_ref="public.accounts.churned", now=NOW)   # no generation_run_id
    run_id = db.execute("SELECT generation_run_id FROM contract_considered WHERE intent_id = %s",
                        (intent.intent_id,)).fetchone()[0]
    assert run_id and run_id.startswith("fgr_")   # a fresh fgr run was minted for the snapshot


# ── 2. a READ COMMITTED caller takes NO snapshot — additive, the lineage columns stay NULL ─────────────
def test_read_committed_build_takes_no_snapshot(db):
    # the db fixture is READ COMMITTED by default; the direct-call flow is byte-identical to pre-C0.
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    build_considered_set(db, intent, _client(), catalog_source="bank",
                         target_ref="public.accounts.churned", now=NOW,
                         generation_run_id="fgr_ignored")
    row = db.execute(
        "SELECT generation_run_id, snapshot_id, snapshot_content_hash "
        "FROM contract_considered WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row == (None, None, None)   # no snapshot under READ COMMITTED
    assert db.execute("SELECT count(*) FROM catalog_metadata_snapshot").fetchone()[0] == 0
    assert considered_snapshot_lineage(db, intent.intent_id) is None


# ── 3. draft/confirm reload the SERVER lineage (the reload helper reads only the server row) ───────────
def test_considered_snapshot_lineage_reloads_server_value(db):
    _rr(db)
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition="90-day average balance per account", actor="ds1")
    build_considered_set(db, intent, _client(), catalog_source="bank",
                         target_ref="public.accounts.churned", now=NOW,
                         generation_run_id="fgr_reload")
    lineage = considered_snapshot_lineage(db, intent.intent_id)
    assert lineage is not None
    assert lineage["generation_run_id"] == "fgr_reload"
    # exactly the persisted server snapshot id/hash — draft/confirm carry THIS forward, never a client id
    persisted = db.execute(
        "SELECT snapshot_id, snapshot_content_hash FROM contract_considered WHERE intent_id = %s",
        (intent.intent_id,)).fetchone()
    assert (lineage["snapshot_id"], lineage["content_hash"]) == persisted


# ── 4. a projection-lagged catalog ABORTS the whole considered set atomically (no row written) ─────────
def test_projection_lagged_aborts_considered_set_and_writes_no_row(db):
    _rr(db)
    _bank(db)
    _append_event(db, 0)                       # head advances; the overlay projection is NOT run → lagged
    assert _checkpoint_seq(db, "overlay") < _head_seq(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")

    with pytest.raises(CatalogProjectionUnavailable) as exc:
        build_considered_set(db, intent, _client(), catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW,
                             generation_run_id="fgr_lag")
    assert exc.value.code == CATALOG_PROJECTION_UNAVAILABLE

    # ATOMIC: the abort raised BEFORE the considered-set INSERT — no row, no snapshot for this run
    assert db.execute("SELECT count(*) FROM contract_considered WHERE intent_id = %s",
                      (intent.intent_id,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM catalog_metadata_snapshot WHERE generation_run_id = %s",
                      ("fgr_lag",)).fetchone()[0] == 0
