"""Conjunctive readiness gate for the durable recipe-formula shadow population."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

AUTHORABLE_RECIPE_IDS = frozenset({
    "merchant_mcc_diversity",
    "obligor_facility_count",
})

_TERMINAL_AUTHORIZATIONS = frozenset({
    "NOT_EVALUATED",
    "AUTHORIZED_CURRENT",
    "AUTHORIZATION_REVOKED",
    "AUTHORIZATION_UNVERIFIABLE",
    "AUTHORIZATION_SCOPE_CHANGED",
})
_TERMINAL_DELIVERY = frozenset({
    "DISPATCHED_AUDITED",
    "NOT_DISPATCHED",
    "EGRESS_REJECTED",
    "PRIOR_DISPATCH_UNRECONCILED",
    "NOT_ENQUEUED",
})
_TERMINAL_AUTHORING = frozenset({
    "RESOLVED",
    "NEEDS_REVIEW",
    "UNSUPPORTED",
    "REJECTED",
    "TECHNICAL_FAILURE",
    "NOT_RUN",
})


@dataclass(frozen=True, slots=True)
class FormulaShadowPopulationReport:
    expected_runs: int
    manifests: int
    complete_manifests: int
    expected_observations: int
    actual_observations: int
    dispatched: int
    resolved: int
    technical_failures: int
    unreconciled_dispatches: int
    malformed_observations: int
    positives_by_recipe: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ProviderGateEvidence:
    distinct_clean_cases: int
    exact_matches: int
    technical_failures: int
    false_resolves: int
    accepted_results: int
    preservation_successes: int
    adversarial_cases: int
    blocking_adversarial_detections: int
    dispatches: int
    reconciled_dispatches: int
    strict_audited_dispatches: int


@dataclass(frozen=True, slots=True)
class ProviderCaseOutcome:
    case_id: str
    case_kind: str
    technical_failure: bool
    exact_match: bool
    false_resolve: bool
    accepted: bool
    preservation_ok: bool
    blocking_detected: bool
    dispatch_count: int
    reconciled_dispatch_count: int
    strict_audited_dispatch_count: int


@dataclass(frozen=True, slots=True)
class FormulaShadowGateResult:
    population_reconciled: bool
    non_vacuous_positives: bool
    deterministic_correctness: bool
    provider_quality: bool
    passed: bool
    activation_status: str
    reasons: tuple[str, ...]
    report: FormulaShadowPopulationReport


def build_provider_evidence(
    outcomes: tuple[ProviderCaseOutcome, ...],
) -> ProviderGateEvidence:
    ids = [outcome.case_id for outcome in outcomes]
    if len(ids) != len(set(ids)):
        raise ValueError("provider gate case ids must be unique")
    if any(
        outcome.case_kind not in {"clean", "adversarial"}
        or outcome.dispatch_count < 0
        or outcome.reconciled_dispatch_count < 0
        or outcome.strict_audited_dispatch_count < 0
        or outcome.reconciled_dispatch_count > outcome.dispatch_count
        or outcome.strict_audited_dispatch_count > outcome.dispatch_count
        for outcome in outcomes
    ):
        raise ValueError("provider gate outcome has an invalid shape")
    clean = [outcome for outcome in outcomes if outcome.case_kind == "clean"]
    adversarial = [
        outcome for outcome in outcomes if outcome.case_kind == "adversarial"]
    accepted = [outcome for outcome in clean if outcome.accepted]
    return ProviderGateEvidence(
        distinct_clean_cases=len(clean),
        exact_matches=sum(1 for outcome in clean if outcome.exact_match),
        technical_failures=sum(1 for outcome in outcomes if outcome.technical_failure),
        false_resolves=sum(1 for outcome in outcomes if outcome.false_resolve),
        accepted_results=len(accepted),
        preservation_successes=sum(
            1 for outcome in accepted if outcome.preservation_ok),
        adversarial_cases=len(adversarial),
        blocking_adversarial_detections=sum(
            1 for outcome in adversarial if outcome.blocking_detected),
        dispatches=sum(outcome.dispatch_count for outcome in outcomes),
        reconciled_dispatches=sum(
            outcome.reconciled_dispatch_count for outcome in outcomes),
        strict_audited_dispatches=sum(
            outcome.strict_audited_dispatch_count for outcome in outcomes),
    )


def _result_json(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def build_population_report(
    conn,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> FormulaShadowPopulationReport:
    predicates = []
    parameters: list[Any] = []
    if since is not None:
        predicates.append("e.declared_at >= %s")
        parameters.append(since)
    if until is not None:
        predicates.append("e.declared_at < %s")
        parameters.append(until)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    expected = conn.execute(
        "SELECT e.generation_run_id,m.status,m.expected_observation_count,"
        "m.actual_observation_count "
        "FROM recipe_formula_shadow_expected_run e "
        "LEFT JOIN recipe_formula_shadow_run_manifest m "
        "ON m.generation_run_id=e.generation_run_id" + where,
        tuple(parameters),
    ).fetchall()
    run_ids = [row[0] for row in expected]
    observations = []
    if run_ids:
        observations = conn.execute(
            "SELECT recipe_id,authorization_axis,delivery_axis,authoring_axis,"
            "technical_axis,authoring_result_json "
            "FROM recipe_formula_shadow_observation "
            "WHERE generation_run_id = ANY(%s)",
            (run_ids,),
        ).fetchall()
    malformed = 0
    dispatched = 0
    resolved = 0
    technical = 0
    unreconciled = 0
    positives = {recipe_id: 0 for recipe_id in AUTHORABLE_RECIPE_IDS}
    for recipe_id, authorization, delivery, authoring, technical_axis, result in observations:
        if (
            authorization not in _TERMINAL_AUTHORIZATIONS
            or delivery not in _TERMINAL_DELIVERY
            or authoring not in _TERMINAL_AUTHORING
        ):
            malformed += 1
        if delivery == "DISPATCHED_AUDITED":
            dispatched += 1
        if delivery == "PRIOR_DISPATCH_UNRECONCILED":
            unreconciled += 1
        result_json = _result_json(result)
        technical_failed = (
            technical_axis != "OK"
            or authoring == "TECHNICAL_FAILURE"
            or result_json.get("technical_status") == "technical_failure"
        )
        if technical_failed:
            technical += 1
        exact_resolve = (
            authoring == "RESOLVED"
            and result_json.get("structural_status") == "ok"
            and result_json.get("capability_status") == "ok"
            and result_json.get("output_status") == "resolved"
            and result_json.get("expectation_status") == "match"
            and result_json.get("critic_status") == "clean"
            and result_json.get("technical_status") == "ok"
            and isinstance(result_json.get("candidate_formula_hash"), str)
            and bool(result_json["candidate_formula_hash"])
        )
        if exact_resolve:
            resolved += 1
            if recipe_id in positives:
                positives[recipe_id] += 1
    return FormulaShadowPopulationReport(
        expected_runs=len(expected),
        manifests=sum(1 for row in expected if row[1] is not None),
        complete_manifests=sum(1 for row in expected if row[1] == "COMPLETE"),
        expected_observations=sum(int(row[2] or 0) for row in expected),
        actual_observations=len(observations),
        dispatched=dispatched,
        resolved=resolved,
        technical_failures=technical,
        unreconciled_dispatches=unreconciled,
        malformed_observations=malformed,
        positives_by_recipe=tuple(sorted(positives.items())),
    )


def evaluate_gate(
    report: FormulaShadowPopulationReport,
    provider: ProviderGateEvidence,
    *,
    deterministic_false_resolves: int,
    deterministic_adversarial_cases: int,
    deterministic_adversarial_detections: int,
    real_authority_population: dict[str, int] | None = None,
) -> FormulaShadowGateResult:
    reasons: list[str] = []
    population = (
        report.expected_runs > 0
        and report.manifests == report.expected_runs
        and report.complete_manifests == report.expected_runs
        and report.expected_observations == report.actual_observations
        and report.malformed_observations == 0
        and report.unreconciled_dispatches == 0
        and report.technical_failures == 0
    )
    if report.expected_runs == 0:
        reasons.append("population: no independently declared shadow runs")
    if report.manifests != report.expected_runs:
        reasons.append("population: expected-run declaration is missing a manifest")
    if report.complete_manifests != report.expected_runs:
        reasons.append("population: one or more manifests are not COMPLETE")
    if report.expected_observations != report.actual_observations:
        reasons.append("population: expected and actual observation counts differ")
    if report.malformed_observations:
        reasons.append("population: unknown or non-terminal observation axes")
    if report.unreconciled_dispatches:
        reasons.append("population: ambiguous provider dispatch outcomes remain")
    if report.technical_failures:
        reasons.append("population: technical failures are present")

    positive_counts = dict(report.positives_by_recipe)
    non_vacuous = all(positive_counts.get(recipe_id, 0) > 0
                       for recipe_id in AUTHORABLE_RECIPE_IDS)
    if not non_vacuous:
        reasons.append("positives: every authorable recipe requires an exact resolved case")

    deterministic = (
        deterministic_false_resolves == 0
        and deterministic_adversarial_cases > 0
        and deterministic_adversarial_detections == deterministic_adversarial_cases
    )
    if deterministic_false_resolves:
        reasons.append("deterministic: false formula resolves detected")
    if deterministic_adversarial_cases == 0:
        reasons.append("deterministic: adversarial corpus is empty")
    elif deterministic_adversarial_detections != deterministic_adversarial_cases:
        reasons.append("deterministic: blocking-adversarial detection is incomplete")

    provider_quality = (
        provider.distinct_clean_cases >= 20
        and provider.technical_failures == 0
        and provider.false_resolves == 0
        and provider.exact_matches / max(provider.distinct_clean_cases, 1) >= 0.90
        and provider.accepted_results > 0
        and provider.preservation_successes == provider.accepted_results
        and provider.adversarial_cases > 0
        and provider.blocking_adversarial_detections == provider.adversarial_cases
        and provider.dispatches > 0
        and provider.reconciled_dispatches == provider.dispatches
        and provider.strict_audited_dispatches == provider.dispatches
    )
    if not provider_quality:
        reasons.append("provider: pre-registered quality/reconciliation thresholds not met")

    shadow_passed = population and non_vacuous and deterministic and provider_quality
    live_authority = real_authority_population or {}
    activation_ready = all(
        live_authority.get(recipe_id, 0) > 0 for recipe_id in AUTHORABLE_RECIPE_IDS)
    if shadow_passed and not activation_ready:
        activation_status = "SHADOW_READY_AUTHORITY_PROVISIONING_REQUIRED"
    elif shadow_passed:
        activation_status = "SHADOW_READY"
    else:
        activation_status = "NOT_READY"
    return FormulaShadowGateResult(
        population_reconciled=population,
        non_vacuous_positives=non_vacuous,
        deterministic_correctness=deterministic,
        provider_quality=provider_quality,
        passed=shadow_passed,
        activation_status=activation_status,
        reasons=tuple(reasons),
        report=report,
    )
