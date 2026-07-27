from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.recipe_formula_gate import (
    FormulaShadowPopulationReport,
    ProviderCaseOutcome,
    ProviderGateEvidence,
    build_population_report,
    build_provider_evidence,
    evaluate_gate,
)


def _report() -> FormulaShadowPopulationReport:
    return FormulaShadowPopulationReport(
        expected_runs=2,
        manifests=2,
        complete_manifests=2,
        expected_observations=20,
        actual_observations=20,
        dispatched=20,
        resolved=19,
        technical_failures=0,
        unreconciled_dispatches=0,
        malformed_observations=0,
        positives_by_recipe=(
            ("merchant_mcc_diversity", 10),
            ("obligor_facility_count", 9),
        ),
    )


def _provider() -> ProviderGateEvidence:
    return ProviderGateEvidence(
        distinct_clean_cases=20,
        exact_matches=18,
        technical_failures=0,
        false_resolves=0,
        accepted_results=18,
        preservation_successes=18,
        adversarial_cases=10,
        blocking_adversarial_detections=10,
        dispatches=30,
        reconciled_dispatches=30,
        strict_audited_dispatches=30,
    )


def _evaluate(report=None, provider=None, **overrides):
    return evaluate_gate(
        report or _report(),
        provider or _provider(),
        deterministic_false_resolves=overrides.get("false_resolves", 0),
        deterministic_adversarial_cases=10,
        deterministic_adversarial_detections=overrides.get("detections", 10),
        real_authority_population=overrides.get("authority"),
    )


def test_gate_is_conjunctive_and_shadow_ready_is_not_live_ready() -> None:
    result = _evaluate()
    assert result.passed
    assert result.activation_status == "SHADOW_READY_AUTHORITY_PROVISIONING_REQUIRED"
    live = _evaluate(authority={
        "merchant_mcc_diversity": 2,
        "obligor_facility_count": 1,
    })
    assert live.activation_status == "SHADOW_READY"


def test_empty_or_reject_everything_population_cannot_pass() -> None:
    empty = replace(
        _report(),
        expected_runs=0,
        manifests=0,
        complete_manifests=0,
        expected_observations=0,
        actual_observations=0,
        resolved=0,
        positives_by_recipe=(
            ("merchant_mcc_diversity", 0),
            ("obligor_facility_count", 0),
        ),
    )
    result = _evaluate(report=empty)
    assert not result.passed
    assert not result.population_reconciled
    assert not result.non_vacuous_positives


def test_any_population_or_provider_failure_fails_the_whole_gate() -> None:
    assert not _evaluate(
        report=replace(_report(), malformed_observations=1)).passed
    assert not _evaluate(
        report=replace(_report(), unreconciled_dispatches=1)).passed
    assert not _evaluate(
        provider=replace(_provider(), technical_failures=1)).passed
    assert not _evaluate(
        provider=replace(_provider(), preservation_successes=17)).passed
    assert not _evaluate(
        report=replace(_report(), technical_failures=1)).passed
    assert not _evaluate(false_resolves=1).passed
    assert not _evaluate(detections=9).passed


def test_population_report_is_empty_not_vacuously_complete(db) -> None:
    report = build_population_report(db)
    assert report.expected_runs == 0
    assert report.actual_observations == 0
    assert not _evaluate(report=report).passed


def test_provider_evidence_is_derived_from_unique_case_outcomes() -> None:
    clean = tuple(
        ProviderCaseOutcome(
            case_id=f"clean-{index}",
            case_kind="clean",
            technical_failure=False,
            exact_match=index < 18,
            false_resolve=False,
            accepted=index < 18,
            preservation_ok=index < 18,
            blocking_detected=False,
            dispatch_count=2,
            reconciled_dispatch_count=2,
            strict_audited_dispatch_count=2,
        )
        for index in range(20)
    )
    adversarial = tuple(
        ProviderCaseOutcome(
            case_id=f"adversarial-{index}",
            case_kind="adversarial",
            technical_failure=False,
            exact_match=False,
            false_resolve=False,
            accepted=False,
            preservation_ok=False,
            blocking_detected=True,
            dispatch_count=2,
            reconciled_dispatch_count=2,
            strict_audited_dispatch_count=2,
        )
        for index in range(10)
    )
    evidence = build_provider_evidence(clean + adversarial)
    assert evidence == replace(_provider(), dispatches=60,
                               reconciled_dispatches=60,
                               strict_audited_dispatches=60)

    import pytest

    with pytest.raises(ValueError, match="unique"):
        build_provider_evidence((clean[0], clean[0]))
