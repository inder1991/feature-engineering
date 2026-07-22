"""Task 6 — the REPORT / METRIC (threshold sweep + Wilson CI + grounding split). Seeding mirrors
``test_shadow_store.py``: plain ``ObservationV1``/``ShadowRunV1`` rows over the bare ``conn`` fixture
(no ``build_graph`` needed — the report's DB surface is only the three ``attestation_*`` tables plus
a read of ``field_evidence`` for the triage split, and ``field_evidence`` carries no FK to
``graph_node``)."""
from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.attest import shadow_store as ss
from featuregen.overlay.upload.attest.report import (
    SPLIT_ALL,
    SPLIT_GROUNDING_COVERED,
    SPLIT_GROUNDING_THIN,
    shadow_report,
    wilson_ci,
)
from featuregen.overlay.upload.attest.shadow_store import ObservationV1, ShadowRunV1

_NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _run(run_id: str, catalog_source: str, sampled_keys: tuple[tuple[str, str], ...]) -> ShadowRunV1:
    return ShadowRunV1(
        shadow_run_id=run_id, catalog_source=catalog_source, gold_version_hash="gv1",
        model_ids={"proposer": "m", "reclassifier": "m"}, signal_versions={"grounding": "1.0.0"},
        started_at=_NOW, sampled_keys=sampled_keys)


def _obs(run_id: str, logical_ref: str, field_name: str = "concept", *, confidence: float = 0.9,
        risk_tier: str = "low", grounding_coverage: float = 1.0,
        proposer_value: str | None = "monetary_flow") -> ObservationV1:
    return ObservationV1(
        shadow_run_id=run_id, logical_ref=logical_ref, field_name=field_name,
        proposer_value=proposer_value, proposer_producer="llm", reclassify_value=proposer_value,
        reclassify_agrees=(proposer_value is not None), grounding_checks={"type_consistency": "pass"},
        grounding_coverage=grounding_coverage, grounding_conflict=False, confidence=confidence,
        risk_tier=risk_tier, created_at=_NOW)


def _gold(conn, catalog_source: str, logical_ref: str, field_name: str, gold_value: str) -> None:
    ss.write_gold_label(conn, catalog_source=catalog_source, logical_ref=logical_ref,
                        field_name=field_name, gold_value=gold_value, labeller_ids=["l1"],
                        adjudicated_by="reviewer_1")


def _hand_wilson_95(k: int, n: int) -> tuple[float, float]:
    """Independent hand-computation of the Wilson score 95% CI (z=1.96) — written out literally
    (NOT calling ``report.wilson_ci``) so the test proves the implementation against the textbook
    formula, not against itself."""
    z = 1.96
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - margin, center + margin


# ── wilson_ci (pure function) ──────────────────────────────────────────────────────────────────────
def test_wilson_ci_matches_hand_computed_bounds_for_n10_k1() -> None:
    lower, upper = _hand_wilson_95(1, 10)
    ci = wilson_ci(1, 10)
    assert ci.lower == pytest.approx(lower)
    assert ci.upper == pytest.approx(upper)


def test_wilson_ci_zero_n_is_maximally_uncertain() -> None:
    ci = wilson_ci(0, 0)
    assert ci.lower == 0.0 and ci.upper == 1.0


def test_wilson_ci_all_success_upper_bound_clamped_to_one() -> None:
    ci = wilson_ci(5, 5)
    assert ci.upper == 1.0
    assert 0.0 < ci.lower < 1.0   # a Wilson interval never collapses to a point even at k==n


# ── false_attest_rate: 10 auto-attested, 1 wrong ─────────────────────────────────────────────────
def test_false_attest_rate_ten_auto_attested_one_wrong(conn) -> None:
    run_id, source = "srun_fa", "src_fa"
    keys = tuple((f"{source}::t.c{i}", "concept") for i in range(10))
    ss.write_shadow_run(conn, _run(run_id, source, keys))
    for i, (logical_ref, field_name) in enumerate(keys):
        gold_value = "customer_id" if i == 9 else "monetary_flow"   # the 10th is the one wrong label
        _gold(conn, source, logical_ref, field_name, gold_value)
        ss.write_observation(conn, _obs(run_id, logical_ref, field_name, confidence=0.9))

    report = shadow_report(conn, run_id)
    cell = report.cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")

    assert cell is not None
    assert cell.n == 10
    assert cell.auto_attested_n == 10
    assert cell.false_attest_n == 1
    assert cell.false_attest_rate == pytest.approx(0.1)
    assert cell.auto_attestable_fraction == pytest.approx(1.0)

    lower, upper = _hand_wilson_95(1, 10)
    assert cell.false_attest_ci.lower == pytest.approx(lower)
    assert cell.false_attest_ci.upper == pytest.approx(upper)


def test_threshold_above_confidence_excludes_from_auto_attested(conn) -> None:
    """Raising T past an observation's confidence removes it from the auto-attested set — the
    denominator (n) is unchanged (it is still gold-joined), only auto_attested_n/fraction move."""
    run_id, source = "srun_thresh", "src_thresh"
    logical_ref = f"{source}::t.c1"
    ss.write_shadow_run(conn, _run(run_id, source, ((logical_ref, "concept"),)))
    _gold(conn, source, logical_ref, "concept", "monetary_flow")
    ss.write_observation(conn, _obs(run_id, logical_ref, confidence=0.6))

    report = shadow_report(conn, run_id)
    below = report.cell(threshold=0.55, split=SPLIT_ALL, field_name="concept")
    above = report.cell(threshold=0.65, split=SPLIT_ALL, field_name="concept")

    assert below.n == 1 and below.auto_attested_n == 1 and below.auto_attestable_fraction == 1.0
    assert above.n == 1 and above.auto_attested_n == 0 and above.auto_attestable_fraction == 0.0
    assert above.false_attest_n == 0 and above.false_attest_rate == 0.0   # no auto-attested -> 0, not NaN


# ── grounding_covered vs grounding_thin split ────────────────────────────────────────────────────
def test_grounding_covered_vs_thin_partitions_correctly(conn) -> None:
    run_id, source = "srun_split", "src_split"
    covered_ref, thin_ref = f"{source}::t.covered", f"{source}::t.thin"
    keys = ((covered_ref, "concept"), (thin_ref, "concept"))
    ss.write_shadow_run(conn, _run(run_id, source, keys))
    _gold(conn, source, covered_ref, "concept", "monetary_flow")
    _gold(conn, source, thin_ref, "concept", "monetary_flow")
    ss.write_observation(conn, _obs(run_id, covered_ref, grounding_coverage=1.0))
    ss.write_observation(conn, _obs(run_id, thin_ref, grounding_coverage=0.0))

    report = shadow_report(conn, run_id)
    all_cell = report.cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")
    covered_cell = report.cell(threshold=0.9, split=SPLIT_GROUNDING_COVERED, field_name="concept")
    thin_cell = report.cell(threshold=0.9, split=SPLIT_GROUNDING_THIN, field_name="concept")

    assert all_cell.n == 2
    assert covered_cell.n == 1 and covered_cell.grounding_coverage_distribution == {1.0: 1}
    assert thin_cell.n == 1 and thin_cell.grounding_coverage_distribution == {0.0: 1}
    # partition: every row in exactly one of covered/thin, and covered+thin == all
    assert covered_cell.n + thin_cell.n == all_cell.n


def test_grounding_partial_coverage_counts_as_covered_not_thin(conn) -> None:
    """coverage > 0 (even partial, e.g. 1/3) is 'covered' — only coverage == 0 is 'thin'."""
    run_id, source = "srun_partial", "src_partial"
    logical_ref = f"{source}::t.c1"
    ss.write_shadow_run(conn, _run(run_id, source, ((logical_ref, "concept"),)))
    _gold(conn, source, logical_ref, "concept", "monetary_flow")
    ss.write_observation(conn, _obs(run_id, logical_ref, grounding_coverage=1 / 3))

    report = shadow_report(conn, run_id)
    covered = report.cell(threshold=0.9, split=SPLIT_GROUNDING_COVERED, field_name="concept")
    thin = report.cell(threshold=0.9, split=SPLIT_GROUNDING_THIN, field_name="concept")
    assert covered.n == 1 and thin.n == 0


# ── low-risk TRIAGED vs DEFAULTED visibility (the T5 note) ──────────────────────────────────────
def test_low_risk_triaged_vs_defaulted_is_visible(conn) -> None:
    run_id, source = "srun_triage", "src_triage"
    triaged_ref, defaulted_ref = f"{source}::t.triaged", f"{source}::t.defaulted"
    keys = ((triaged_ref, "concept"), (defaulted_ref, "concept"))
    ss.write_shadow_run(conn, _run(run_id, source, keys))
    _gold(conn, source, triaged_ref, "concept", "monetary_flow")
    _gold(conn, source, defaulted_ref, "concept", "monetary_flow")

    # triaged_ref: a taxonomy signal exists (and cleared it as low risk) — TRIAGED.
    record_field_evidence(
        conn, logical_ref=triaged_ref, field_name="sensitivity_floor", proposed_value="public",
        producer="taxonomy", strength="proposed", producer_ref="test", source_snapshot_id="snap",
        input_hash="h1")
    # defaulted_ref: NO taxonomy evidence at all — reads 'low' only because runner._risk_tier
    # treats "no signal" as "no risk". This must not silently hide inside the headline number.
    ss.write_observation(conn, _obs(run_id, triaged_ref, risk_tier="low"))
    ss.write_observation(conn, _obs(run_id, defaulted_ref, risk_tier="low"))

    report = shadow_report(conn, run_id)
    cell = report.cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")

    assert cell.auto_attested_n == 2
    assert cell.triaged_low_n == 1
    assert cell.defaulted_low_n == 1
    assert cell.triaged_low_n + cell.defaulted_low_n == cell.auto_attested_n


# ── the READ-TIME-JOIN property: correcting gold re-scores without re-running any signal ────────
def test_gold_correction_rescores_without_new_observations(conn) -> None:
    run_id, source = "srun_join", "src_join"
    logical_ref = f"{source}::t.c1"
    ss.write_shadow_run(conn, _run(run_id, source, ((logical_ref, "concept"),)))
    _gold(conn, source, logical_ref, "concept", "WRONG_LABEL")   # a mislabelled gold value
    ss.write_observation(conn, _obs(run_id, logical_ref, proposer_value="monetary_flow"))

    n_obs_before = conn.execute(
        "SELECT count(*) FROM attestation_shadow_observation WHERE shadow_run_id = %s",
        (run_id,)).fetchone()[0]
    before = shadow_report(conn, run_id).cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")
    assert before.false_attest_n == 1 and before.false_attest_rate == 1.0

    # Simulate an out-of-band gold-label correction: attestation_gold_label is append-only WORM
    # (migration 1018 — the writer's own ON CONFLICT DO NOTHING never overwrites an existing key,
    # and a normal UPDATE is blocked by the row trigger), so a genuine correction is a
    # superuser-level replica-scoped UPDATE — the same technique
    # test_pointer_model.py/test_drift_invalidation.py use elsewhere in this codebase for
    # out-of-band WORM corrections.
    conn.execute("SET session_replication_role = replica")
    conn.execute(
        "UPDATE attestation_gold_label SET gold_value = %s WHERE logical_ref = %s AND field_name = %s",
        ("monetary_flow", logical_ref, "concept"))
    conn.execute("SET session_replication_role = origin")

    n_obs_after = conn.execute(
        "SELECT count(*) FROM attestation_shadow_observation WHERE shadow_run_id = %s",
        (run_id,)).fetchone()[0]
    assert n_obs_after == n_obs_before   # no new observation written — no signal was re-run

    after = shadow_report(conn, run_id).cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")
    assert after.false_attest_n == 0 and after.false_attest_rate == 0.0   # the rate CHANGED
    assert after.n == before.n == 1   # same population, only the read-time join outcome moved


# ── unlabelled observations are excluded, not mismatched ────────────────────────────────────────
def test_observation_with_no_gold_label_is_excluded_not_mismatched(conn) -> None:
    run_id, source = "srun_unlabelled", "src_unlabelled"
    labelled_ref, unlabelled_ref = f"{source}::t.labelled", f"{source}::t.unlabelled"
    keys = ((labelled_ref, "concept"), (unlabelled_ref, "concept"))
    ss.write_shadow_run(conn, _run(run_id, source, keys))
    _gold(conn, source, labelled_ref, "concept", "monetary_flow")
    ss.write_observation(conn, _obs(run_id, labelled_ref))
    ss.write_observation(conn, _obs(run_id, unlabelled_ref))   # no gold label written for this one

    report = shadow_report(conn, run_id)
    cell = report.cell(threshold=0.9, split=SPLIT_ALL, field_name="concept")
    assert cell.n == 1   # only the gold-joined row is scored
