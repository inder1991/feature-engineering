from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from featuregen.overlay.upload.attest import shadow_store as ss
from featuregen.overlay.upload.attest.shadow_store import ObservationV1, ShadowRunV1

_NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _run(run_id: str = "srun_1", column_count: int = 2) -> ShadowRunV1:
    return ShadowRunV1(
        shadow_run_id=run_id, catalog_source="ftr_export", gold_version_hash="gvh_1",
        model_ids={"proposer": "claude-sonnet-5", "reclassifier": "claude-sonnet-5"},
        signal_versions={"grounding": "1.0.0", "fusion": "1.0.0"}, started_at=_NOW,
        column_count=column_count)


def _observation(logical_ref: str = "ftr_export::t.c1", field_name: str = "concept",
                  run_id: str = "srun_1") -> ObservationV1:
    return ObservationV1(
        shadow_run_id=run_id, logical_ref=logical_ref, field_name=field_name,
        proposer_value="customer_id", proposer_producer="ai", reclassify_value="customer_id",
        reclassify_agrees=True, grounding_checks={"type_consistency": "pass", "path_agreement": "absent"},
        grounding_coverage=0.5, grounding_conflict=False, confidence=0.82, risk_tier="low",
        created_at=_NOW)


# ── attestation_gold_label ──
def test_write_gold_label_idempotent(conn) -> None:
    ss.write_gold_label(conn, catalog_source="ftr_export", logical_ref="ftr_export::t.c1",
                        field_name="concept", gold_value="customer_id", labeller_ids=["l1", "l2"],
                        adjudicated_by="reviewer_1")
    ss.write_gold_label(conn, catalog_source="ftr_export", logical_ref="ftr_export::t.c1",
                        field_name="concept", gold_value="customer_id", labeller_ids=["l1", "l2"],
                        adjudicated_by="reviewer_1")   # re-write same key: no error, single row
    n = conn.execute(
        "SELECT count(*) FROM attestation_gold_label WHERE logical_ref = %s AND field_name = %s",
        ("ftr_export::t.c1", "concept")).fetchone()[0]
    assert n == 1


# ── attestation_shadow_run + attestation_shadow_observation + reconcile ──
def test_run_and_observations_roundtrip(conn) -> None:
    ss.write_shadow_run(conn, _run())
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c1"))
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c2"))
    rec = ss.reconcile(conn, "srun_1")
    assert rec.expected == 2 and rec.present == 2
    assert rec.complete is True


def test_reconcile_incomplete_when_observation_missing(conn) -> None:
    ss.write_shadow_run(conn, _run(run_id="srun_2", column_count=2))
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c1", run_id="srun_2"))
    rec = ss.reconcile(conn, "srun_2")
    assert rec.expected == 2 and rec.present == 1
    assert rec.complete is False


# ── WORM ──
def test_observation_update_is_rejected(conn) -> None:
    ss.write_shadow_run(conn, _run(run_id="srun_3", column_count=1))
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c1", run_id="srun_3"))
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        conn.execute(
            "UPDATE attestation_shadow_observation SET confidence = 0.99 "
            "WHERE shadow_run_id = %s AND logical_ref = %s AND field_name = %s",
            ("srun_3", "ftr_export::t.c1", "concept"))
