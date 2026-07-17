from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _txn_template
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.overlay.upload.planner import shadow_store as ss
from featuregen.overlay.upload.planner.contract_eval import (
    CaseResult,
    EvalReport,
    SampleUnit,
    StabilityResult,
)
from featuregen.overlay.upload.planner.shadow import run_shadow_planner
from featuregen.overlay.upload.planner.shadow_report import (
    GateInputs,
    GatePolicy,
    PopulationReportV1,
    _is_truncated,
    authority_sign_gate,
    build_gate_artifact,
    build_population_report,
    clopper_pearson_upper,
    evaluate_gate,
    report_input_digest,
    required_shapes_for_bound,
    statistical_bound,
    write_gate_artifact,
)
from featuregen.overlay.upload.planner.signing import (
    GateKeyNotConfigured,
    generate_keypair,
    sign_report,
    verify_cli,
    verify_report,
    verify_report_file,
    write_signature_sidecar,
)


# ── the statistical bound (Clopper-Pearson, no FPC) ──
def test_clopper_pearson_matches_known_references():
    assert round(clopper_pearson_upper(0, 300), 5) == 0.00994     # ≈ 3/n, ≤ 1%
    assert round(clopper_pearson_upper(1, 10), 4) == 0.3942       # textbook one-sided 95%
    assert round(clopper_pearson_upper(2, 20), 4) == 0.2826
    assert clopper_pearson_upper(5, 5) == 1.0 and clopper_pearson_upper(0, 0) == 1.0


def test_required_shapes_for_one_percent_bound_is_about_300():
    assert required_shapes_for_bound(0.01) == 299
    assert clopper_pearson_upper(0, required_shapes_for_bound(0.01)) <= 0.01


# ── ed25519 detached signing ──
def test_sign_verify_roundtrip_and_tamper_and_wrong_key():
    priv, pub = generate_keypair()
    sig = sign_report(b"artifact", priv)
    assert verify_report(b"artifact", sig, pub)
    assert not verify_report(b"artifact-TAMPERED", sig, pub)      # tampered payload
    _, other = generate_keypair()
    assert not verify_report(b"artifact", sig, other)            # wrong trust key


def test_verify_is_fail_closed_when_public_key_unset(monkeypatch):
    monkeypatch.delenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", raising=False)
    priv, _ = generate_keypair()
    with pytest.raises(GateKeyNotConfigured):
        verify_report(b"x", sign_report(b"x", priv))              # no trusted key → refuse


def test_sidecar_file_verify_and_cli_exit(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    monkeypatch.setenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", pub)
    report = tmp_path / "gate.json"
    report.write_bytes(b'{"gate_passed":true}')
    write_signature_sidecar(report, sign_report(report.read_bytes(), priv))
    assert verify_report_file(report) and verify_cli(report) == 0
    report.write_bytes(b'{"gate_passed":true,"tampered":1}')      # edit the report, keep the old sig
    assert not verify_report_file(report) and verify_cli(report) == 1   # nonzero exit


def test_verify_cli_is_fail_closed_on_missing_file_and_unset_key(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    monkeypatch.setenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", pub)
    report = tmp_path / "g.json"
    report.write_bytes(b"{}")
    write_signature_sidecar(report, sign_report(report.read_bytes(), priv))
    assert verify_cli(tmp_path / "missing.json") == 1              # no report/sidecar → nonzero, no raise
    monkeypatch.delenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", raising=False)
    assert verify_cli(report) == 1                                 # unset trusted key → fail-closed, no raise


# ── the conjunctive gate ──
def _report(**over) -> PopulationReportV1:
    base = dict(
        run_ids=("r",), denominator=10, numerator=2,
        headline_by_primary={"grain_incompatible": 2}, breakdown_by_category={"topology_or_model": 2},
        recipe_outcome_matrix={"compiled|complete": 10}, replay_freshness={"current": 10},
        operationally_unmeasured_count=0, incomplete_count=0, compile_disabled_count=0,
        internal_error_count=0, preloop_failure_count=0, template_not_found_count=0,
        persistence_partial_count=0, truncated_count=0, reconcile_complete=True, persistence_loss=0,
        sample_units=tuple(SampleUnit("tier_1_single_catalog", "balance_stock", "resolved", None,
                                      f"h{i}", True, True) for i in range(6)))
    base.update(over)
    return PopulationReportV1(**base)


_LENIENT = GatePolicy(max_false_resolve_bound=0.5)   # required_shapes = 5 → the 6-shape frame passes


def _inputs(**over) -> GateInputs:
    gold = EvalReport(results=(CaseResult("c1", True, False, ()),))
    base = dict(report=_report(), review_clean=True, gold_report=gold, audit_false_resolves=0,
                stability=StabilityResult(stable=True, compared=3, mismatched_keys=()),
                drift_detected_ratio=1.0, signature_valid=True, policy=_LENIENT)
    base.update(over)
    return GateInputs(**base)


def test_all_sub_gates_pass_gate_passes():
    assert evaluate_gate(_inputs()).passed


@pytest.mark.parametrize("override", [
    {"review_clean": False},
    {"gold_report": EvalReport(results=(CaseResult("c1", False, True, ("FALSE RESOLVE",)),))},
    {"audit_false_resolves": 1},
    {"stability": StabilityResult(stable=False, compared=0, mismatched_keys=())},
    {"drift_detected_ratio": 0.99},
    {"signature_valid": False},
])
def test_any_single_sub_gate_failure_fails_the_whole_gate(override):
    # conjunctive, NO averaging: one failing sub-gate fails the gate even with all others passing
    assert not evaluate_gate(_inputs(**override)).passed


@pytest.mark.parametrize("flag", [
    "incomplete_count", "compile_disabled_count", "internal_error_count", "preloop_failure_count",
    "template_not_found_count", "persistence_partial_count", "truncated_count", "persistence_loss",
])
def test_gate1_fails_on_each_integrity_breach(flag):
    res = evaluate_gate(_inputs(report=_report(**{flag: 1})))
    assert not res.gate1_capture and not res.passed


def test_gate1_fails_on_reconcile_incomplete():
    assert not evaluate_gate(_inputs(report=_report(reconcile_complete=False))).gate1_capture


def test_gate2a_fails_on_operationally_unmeasured():
    res = evaluate_gate(_inputs(report=_report(operationally_unmeasured_count=1)))
    assert not res.gate2a_map and not res.passed


def test_a_human_cannot_override_a_failed_machine_gate():
    # every SIGNED/human sub-gate passes, but a machine gate (capture) fails → the gate stays FAILED
    res = evaluate_gate(_inputs(report=_report(incomplete_count=3), review_clean=True,
                                audit_false_resolves=0, signature_valid=True))
    assert res.gate2b_review and res.gate3_no_false_resolves and res.gate7_artifact   # humans "approve"
    assert not res.gate1_capture and not res.passed                                   # machine still blocks


# ── Gate 4 statistical bound ──
def test_statistical_bound_rare_stratum_fails():
    units = tuple(SampleUnit("tier_1_single_catalog", "fam", "resolved", None, f"h{i}", True, True)
                  for i in range(2))                          # only 2 distinct shapes
    ok, sample, reasons = statistical_bound(units, GatePolicy(max_false_resolve_bound=0.5))
    assert not ok and reasons and sample.rare_strata


def test_statistical_bound_empty_frame_fails():
    ok, _, reasons = statistical_bound((), GatePolicy(max_false_resolve_bound=0.5))
    assert not ok and any("empty" in r for r in reasons)


def test_statistical_bound_sufficient_shapes_pass():
    units = tuple(SampleUnit("tier_1_single_catalog", "fam", "resolved", None, f"h{i}", True, True)
                  for i in range(6))
    ok, _, reasons = statistical_bound(units, GatePolicy(max_false_resolve_bound=0.5))
    assert ok and not reasons


# ── the signed artifact ──
def test_artifact_records_provenance_and_signature_covers_it():
    priv, pub = generate_keypair()
    report = _report()
    result = evaluate_gate(_inputs(report=report))    # the sample Gate 4 used is carried on the result
    art = build_gate_artifact(report=report, result=result,
                              review_content_hash="rev123", policy=_LENIENT, code_commit="abc123",
                              producer_cohort="cohort-A", signer_key_id="authority-1")
    assert art.code_commit == "abc123" and art.gold_set_hash and art.policy_hash
    assert art.observation_window == ("r",) and art.report_input_digest == report_input_digest(report)
    # the artifact pins the IMMUTABLE audited sample (per_stratum draw from the frame), a subset here
    assert len(art.sample_ids) == _LENIENT.required_shapes
    assert set(art.sample_ids) <= {f"h{i}" for i in range(6)} and art.gate_passed
    # the signature covers the canonical artifact bytes; changing ANY field breaks verification
    sig = sign_report(art.canonical_bytes(), priv)
    assert verify_report(art.canonical_bytes(), sig, pub)
    tampered = dataclasses.replace(art, gate_passed=not art.gate_passed)
    assert not verify_report(tampered.canonical_bytes(), sig, pub)


def test_write_gate_artifact_bytes_verify_via_sidecar(tmp_path, monkeypatch):
    priv, pub = generate_keypair()
    monkeypatch.setenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", pub)
    report = _report()
    art = build_gate_artifact(report=report, result=evaluate_gate(_inputs(report=report)),
                              review_content_hash="r", policy=_LENIENT, code_commit="c",
                              producer_cohort="p", signer_key_id="a")
    path = tmp_path / "gate.json"
    write_gate_artifact(str(path), art)                           # exact canonical bytes on disk
    write_signature_sidecar(path, sign_report(art.canonical_bytes(), priv))
    assert verify_report_file(path)                               # round-trips through the file/sidecar


def test_authority_signing_refuses_to_bless_a_forged_pass():
    # the authority re-derives the gate from inputs; a FAILING gate yields gate_passed=False in the
    # signed artifact — an evaluator cannot get a hand-asserted PASS signed.
    priv, pub = generate_keypair()
    failing = _inputs(report=_report(incomplete_count=9))         # Gate 1 fails
    art, sig = authority_sign_gate(failing, private_key_pem=priv, code_commit="c",
                                   producer_cohort="p", signer_key_id="a", review_content_hash="r")
    assert art.gate_passed is False                              # the authority signed a FAIL, not a forged PASS
    assert verify_report(art.canonical_bytes(), sig, pub)        # the signature is valid over the honest FAIL


def test_is_truncated_covers_every_bounding_bound_flag():
    # Gate 1's truncation detection must catch every real BoundingMetrics bound flag (F8) and must NOT
    # trip on the deterministic best-tier prune (deeper_tiers_not_explored).
    from dataclasses import fields

    from featuregen.overlay.upload.planner.contracts import BoundingMetricsV1
    bound_flags = [f.name for f in fields(BoundingMetricsV1) if f.name.endswith("_truncated")]
    assert bound_flags   # there ARE bound flags to catch
    for flag in bound_flags:
        assert _is_truncated({flag: True}), f"{flag} not detected as truncation"
    assert not _is_truncated({"deeper_tiers_not_explored": True})   # deterministic prune, not a bound
    assert not _is_truncated({}) and not _is_truncated(None)


# ── §9 report over the real store + PG e2e (route→manifest→run→plan→report→gate) ──
def test_population_report_numerator_denominator(db):
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="rep1", now=_NOW, templates=(_txn_template(),),
                       compile_contracts=True, persist=True)
    report = build_population_report(db, ["rep1"], family_of=lambda rid: "roll_family")
    # the selected plan compiled complete and resolved → in the denominator, not the numerator
    assert report.denominator == 1 and report.numerator == 0
    assert report.recipe_outcome_matrix and report.sample_units
    assert report.reconcile_complete and report.persistence_loss == 0


def test_template_not_found_trips_gate1_even_though_reconcile_is_complete(db):
    # Minor-1: an eligible recipe with no template is RECORDED (reconcile stays complete), so without
    # this driver a taxonomy drift would silently drop it from the denominator. Gate 1 must catch it.
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll", "ghost_recipe"}),
                       target_entity="account", roles=(), run_id="tnf", now=_NOW,
                       templates=(_txn_template(),), compile_contracts=True, persist=True)
    report = build_population_report(db, ["tnf"], family_of=lambda rid: "roll_family")
    assert report.reconcile_complete and report.template_not_found_count == 1   # recorded, not lost
    assert not evaluate_gate(_inputs(report=report)).gate1_capture              # but Gate 1 blocks


def test_pg_e2e_run_to_report_to_signed_gate(db, monkeypatch):
    # the full chain on real Postgres: run (manifest→run→plan→persist, telemetry+compile ON) →
    # population report → conjunctive gate → build artifact → sign (authority) → verify (evaluator/CI).
    priv, pub = generate_keypair()
    monkeypatch.setenv("FEATUREGEN_INTENT_GATE_PUBLIC_KEY", pub)
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="e2e", now=_NOW, templates=(_txn_template(),),
                       compile_contracts=True, persist=True)
    assert ss.reconcile(db, "e2e").complete
    report = build_population_report(db, ["e2e"], family_of=lambda rid: "roll_family")
    inputs = GateInputs(
        report=report, review_clean=True,
        gold_report=EvalReport(results=(CaseResult("c1", True, False, ()),)),
        audit_false_resolves=0, stability=StabilityResult(True, 2, ()), drift_detected_ratio=1.0,
        signature_valid=True, policy=GatePolicy(max_false_resolve_bound=0.99))   # 1 shape suffices here
    # authority-side: the signer INDEPENDENTLY re-derives the gate and signs only its own result
    art, sig = authority_sign_gate(inputs, private_key_pem=priv, code_commit="e2e-commit",
                                   producer_cohort="e2e", signer_key_id="authority",
                                   review_content_hash="rev")
    assert verify_report(art.canonical_bytes(), sig)        # EVALUATOR side (config public key)
    assert art.report_input_digest == report_input_digest(report)
    assert evaluate_gate(inputs).gate1_capture   # capture integrity held over the real persisted run
