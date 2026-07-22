from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import psycopg
import pytest

from featuregen.overlay.upload.attest import shadow_store as ss
from featuregen.overlay.upload.attest.shadow_store import ObservationV1, ShadowRunV1

_NOW = datetime(2026, 7, 22, tzinfo=UTC)

_DEFAULT_SAMPLED_KEYS = (
    ("ftr_export::t.c1", "concept"),
    ("ftr_export::t.c2", "concept"),
)


def _run(run_id: str = "srun_1",
        sampled_keys: tuple[tuple[str, str], ...] = _DEFAULT_SAMPLED_KEYS) -> ShadowRunV1:
    return ShadowRunV1(
        shadow_run_id=run_id, catalog_source="ftr_export", gold_version_hash="gvh_1",
        model_ids={"proposer": "claude-sonnet-5", "reclassifier": "claude-sonnet-5"},
        signal_versions={"grounding": "1.0.0", "fusion": "1.0.0"}, started_at=_NOW,
        sampled_keys=sampled_keys)


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
    assert rec.missing == ()
    assert rec.complete is True


def test_reconcile_incomplete_when_observation_missing(conn) -> None:
    ss.write_shadow_run(conn, _run(run_id="srun_2"))
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c1", run_id="srun_2"))
    rec = ss.reconcile(conn, "srun_2")
    assert rec.expected == 2 and rec.present == 1
    assert rec.missing == (("ftr_export::t.c2", "concept"),)
    assert rec.complete is False


def test_reconcile_detects_key_substitution_capture_loss(conn) -> None:
    """The count-based check this replaces would have falsely reported complete here: 2 sampled keys
    (A, B), 2 observations written — but for A and a WRONG key C, never B. A present-but-unexpected
    observation must NOT satisfy a missing expected key."""
    key_a = ("ftr_export::t.c1", "concept")
    key_b = ("ftr_export::t.c2", "concept")
    key_c = ("ftr_export::t.wrong", "concept")   # never sampled — substituted in by mistake
    ss.write_shadow_run(conn, _run(run_id="srun_sub", sampled_keys=(key_a, key_b)))
    ss.write_observation(conn, _observation(logical_ref=key_a[0], field_name=key_a[1], run_id="srun_sub"))
    ss.write_observation(conn, _observation(logical_ref=key_c[0], field_name=key_c[1], run_id="srun_sub"))
    rec = ss.reconcile(conn, "srun_sub")
    assert rec.expected == 2 and rec.present == 2   # counts coincidentally agree ...
    assert rec.missing == (key_b,)                  # ... but the set check catches the substitution
    assert rec.complete is False


# ── risk_tier CHECK (N-4) ──
def test_write_observation_rejects_unknown_risk_tier(conn) -> None:
    """N-4: ``risk_tier`` is CHECK-constrained to the two values ``runner._risk_tier`` ever emits
    ('low'/'high') so a future typo'd tier can't silently never-match 'low' and vanish from the
    auto-attested set undetected."""
    ss.write_shadow_run(conn, _run(run_id="srun_bad_tier"))
    bad = dataclasses.replace(_observation(run_id="srun_bad_tier"), risk_tier="bogus")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        ss.write_observation(conn, bad)


# ── WORM ──
def test_observation_update_is_rejected(conn) -> None:
    ss.write_shadow_run(conn, _run(run_id="srun_3", sampled_keys=(("ftr_export::t.c1", "concept"),)))
    ss.write_observation(conn, _observation(logical_ref="ftr_export::t.c1", run_id="srun_3"))
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        conn.execute(
            "UPDATE attestation_shadow_observation SET confidence = 0.99 "
            "WHERE shadow_run_id = %s AND logical_ref = %s AND field_name = %s",
            ("srun_3", "ftr_export::t.c1", "concept"))
