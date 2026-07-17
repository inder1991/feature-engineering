"""Phase-3B.4 D8 — the durable POPULATION REPORT (§9) + the conjunctive 3C ENABLEMENT GATE (§10) +
the signed gate ARTIFACT (§10.7).

The report fixes the population ONCE (so implementation and gate policy cannot pick different
denominators): the numerator/denominator are over SELECTED, COMPLETE, path-resolved observations,
one per (run, recipe). The gate is CONJUNCTIVE — seven sub-gates, EVERY one must pass, NO averaging;
a human supplies labels/approval for the signed sub-gates (2b/3/4/7) but CANNOT override a FAILED
MACHINE sub-gate (1/2a/5/6). The artifact records exactly what was gated (commit, gold-set hash,
policy hash, versions, window, sample ids, signer) and is ed25519 DETACHED-signed so the evaluator
cannot sign its own PASS. No silent exclusion, no signed-exclusion escape hatch (F19).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from featuregen.overlay.upload.planner.cause import (
    CATEGORY_MAP_VERSION,
    assert_map_exhaustive,
    category_of,
)
from featuregen.overlay.upload.planner.contract_eval import (
    EvalReport,
    SampleUnit,
    StabilityResult,
    StratifiedSample,
    stratified_sample,
)
from featuregen.overlay.upload.planner.contract_gold import GOLD_SET_HASH, GOLD_SET_VERSION
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.planner.shadow_store import reconcile
from featuregen.overlay.upload.planner.strata import STRATA_VERSION
from featuregen.overlay.upload.templates import ALL_TEMPLATES

EVALUATOR_VERSION = "1.0.0"
_RESOLVED = "resolved"
_COMPLETE = "complete"
_SOURCE_TO_TARGET = "source_to_target_resolved"


# ─── §9 population report ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PopulationReportV1:
    run_ids: tuple[str, ...]
    denominator: int                              # selected + complete + source_to_target_resolved
    numerator: int                                # ... AND contract_resolution_status != resolved
    headline_by_primary: dict[str, int]           # numerator counted ONCE by primary reason
    breakdown_by_category: dict[str, int]         # numerator counted once per distinct Layer-A category
    recipe_outcome_matrix: dict[str, int]         # "planner_outcome|compile_status" -> count
    replay_freshness: dict[str, int]              # current/drifted/incompatible/unverifiable (injected)
    operationally_unmeasured_count: int           # population primary reasons with NO Layer-A map entry
    incomplete_count: int
    compile_disabled_count: int
    internal_error_count: int
    preloop_failure_count: int
    persistence_partial_count: int
    truncated_count: int                          # any BoundingMetrics *_truncated flag (F8)
    reconcile_complete: bool
    persistence_loss: int                         # total manifest recipes with no run-result row
    sample_units: tuple[SampleUnit, ...]          # selected+complete frame for the stratified audit

    @property
    def unresolved_ratio(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0


def _family_map() -> dict[str, str]:
    return {t.id: t.family for t in ALL_TEMPLATES}


def _is_truncated(bounding: Any) -> bool:
    return isinstance(bounding, dict) and any(
        k.endswith("_truncated") and bool(v) for k, v in bounding.items())


def build_population_report(conn, run_ids: Sequence[str], *,
                            family_of: Callable[[str], str] | None = None,
                            replay_freshness: dict[str, int] | None = None) -> PopulationReportV1:
    """Compute the §9 report over the given runs. One observation per (run, recipe) — the SELECTED
    plan — enters the numerator/denominator; incomplete/partial compiles are excluded from the
    denominator (and gated separately by Gate 1). Reads the store directly (the report has specific
    column needs beyond the generic readers)."""
    run_ids = tuple(run_ids)
    fam = family_of or _family_map().__getitem__
    runs = conn.execute(
        "SELECT generation_run_id, recipe_id, planner_outcome, compile_status,"
        " selected_contract_physical_plan_id, capture_status, bounding"
        " FROM planner_shadow_run_result WHERE generation_run_id = ANY(%s)", (list(run_ids),)).fetchall()
    obs = conn.execute(
        "SELECT generation_run_id, recipe_id, physical_plan_id, path_resolution_status,"
        " contract_resolution_status, contract_input_hash, contract_primary_reason_code,"
        " contract_reason_codes, tier"
        " FROM planner_shadow_plan_observation WHERE generation_run_id = ANY(%s)", (list(run_ids),)).fetchall()
    obs_by_key = {(o[0], o[1], o[2]): o for o in obs}   # (run, recipe, physical_plan_id) -> row

    matrix: dict[str, int] = {}
    incomplete = disabled = internal = preloop = partial = truncated = 0
    denominator = numerator = op_unmeasured = 0
    headline: dict[str, int] = {}
    breakdown: dict[str, int] = {}
    units: list[SampleUnit] = []

    for run_id, recipe_id, outcome, compile_status, selected_pid, capture, bounding in runs:
        matrix[f"{outcome}|{compile_status}"] = matrix.get(f"{outcome}|{compile_status}", 0) + 1
        incomplete += compile_status == "incomplete"
        disabled += compile_status == "compile_disabled"
        internal += outcome == "internal_error"
        preloop += outcome == "preloop_failure"
        partial += capture == "persistence_partial"
        truncated += _is_truncated(bounding)
        selected = obs_by_key.get((run_id, recipe_id, selected_pid)) if selected_pid is not None else None
        if selected is None or selected[3] != _SOURCE_TO_TARGET or compile_status != _COMPLETE:
            continue                                      # outside the §9 frame
        crs, cih, primary, reason_codes, tier = selected[4], selected[5], selected[6], selected[7], selected[8]
        denominator += 1
        units.append(SampleUnit(
            tier=tier, family=fam(recipe_id), contract_resolution_status=crs or "",
            primary_reason_code=primary, contract_input_hash=cih or "",
            is_selected=True, is_complete=True))
        if crs != _RESOLVED:
            numerator += 1
            key = primary or "unclassified"
            headline[key] = headline.get(key, 0) + 1
            for cat in _distinct_categories(reason_codes):
                breakdown[cat] = breakdown.get(cat, 0) + 1
            if primary is not None and _category_or_none(primary) is None:
                op_unmeasured += 1

    reconciles = [reconcile(conn, rid) for rid in run_ids]
    loss = sum(len(r.missing_recipe_ids) for r in reconciles)
    return PopulationReportV1(
        run_ids=run_ids, denominator=denominator, numerator=numerator,
        headline_by_primary=headline, breakdown_by_category=breakdown,
        recipe_outcome_matrix=matrix, replay_freshness=dict(replay_freshness or {}),
        operationally_unmeasured_count=op_unmeasured, incomplete_count=incomplete,
        compile_disabled_count=disabled, internal_error_count=internal, preloop_failure_count=preloop,
        persistence_partial_count=partial, truncated_count=truncated,
        reconcile_complete=all(r.complete for r in reconciles), persistence_loss=loss,
        sample_units=tuple(units))


def _category_or_none(reason: str) -> str | None:
    try:
        cat = category_of(ReasonCode(reason))
    except ValueError:
        return None
    return str(cat) if cat is not None else None


def _distinct_categories(reason_codes: Iterable[str]) -> list[str]:
    """The distinct Layer-A categories a plan's reason codes touch — for the multi-reason breakdown
    (each plan counts once PER category). An unmapped reason surfaces as ``operationally_unmeasured``."""
    cats = {_category_or_none(r) or "operationally_unmeasured" for r in reason_codes}
    return sorted(cats)


# ─── §10.4 the statistical bound (binomial Clopper-Pearson, NO finite-population correction) ─────
def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-30
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1.0)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((a + m2 - 1.0) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        h *= d * c
        aa = -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1.0))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b + lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def clopper_pearson_upper(failures: int, n: int, alpha: float = 0.05) -> float:
    """The one-sided upper confidence bound (confidence 1-alpha) for a binomial rate — the exact
    Clopper-Pearson interval, NO finite-population correction (the estimand is FUTURE traffic, an
    unbounded population — F9). ``failures==0`` uses the exact closed form ``1 - alpha**(1/n)``
    (≈ 3/n at 95%); ``failures>0`` inverts the incomplete beta by bisection."""
    if n <= 0:
        return 1.0
    if failures >= n:
        return 1.0
    if failures == 0:
        return 1.0 - alpha ** (1.0 / n)
    a, b, target = failures + 1.0, float(n - failures), 1.0 - alpha
    lo, hi = 0.0, 1.0
    for _ in range(100):                     # bisection on I_x(a,b) = 1-alpha
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def required_shapes_for_bound(max_bound: float, alpha: float = 0.05) -> int:
    """The smallest ZERO-failure distinct-shape count whose upper bound is ≤ ``max_bound`` — the
    per-stratum sampling target (≈300 for a 1% bound at 95%)."""
    if not 0.0 < max_bound < 1.0:
        raise ValueError("max_bound must be in (0, 1)")
    return max(1, math.ceil(math.log(alpha) / math.log(1.0 - max_bound)))


# ─── §10 the conjunctive gate ────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GatePolicy:
    """The SIGNED policy parameters: the max acceptable per-stratum false-resolve bound + the alpha.
    ``policy_hash`` pins them into the artifact."""

    max_false_resolve_bound: float = 0.01
    alpha: float = 0.05

    @property
    def required_shapes(self) -> int:
        return required_shapes_for_bound(self.max_false_resolve_bound, self.alpha)

    @property
    def policy_hash(self) -> str:
        payload = {"max_false_resolve_bound": self.max_false_resolve_bound, "alpha": self.alpha}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class GateResult:
    gate1_capture: bool
    gate2a_map: bool
    gate2b_review: bool
    gate3_no_false_resolves: bool
    gate4_statistical_bound: bool
    gate5_replay_stability: bool
    gate6_drift: bool
    gate7_artifact: bool
    reasons: tuple[str, ...] = ()

    @property
    def machine_gates(self) -> tuple[bool, ...]:
        return (self.gate1_capture, self.gate2a_map, self.gate5_replay_stability, self.gate6_drift)

    @property
    def passed(self) -> bool:
        """CONJUNCTIVE — every sub-gate must pass; NO averaging. A human cannot override this: the
        signed sub-gates are AND-ed with the machine gates, so a False machine gate forces False."""
        return all((self.gate1_capture, self.gate2a_map, self.gate2b_review,
                    self.gate3_no_false_resolves, self.gate4_statistical_bound,
                    self.gate5_replay_stability, self.gate6_drift, self.gate7_artifact))


@dataclass(frozen=True, slots=True)
class GateInputs:
    """The already-computed sub-verdicts the gate ANDs together. The report drives the machine gates
    1/2a; the human/derived verdicts (2b/3/4/5/6/7) arrive pre-computed."""

    report: PopulationReportV1
    review_clean: bool                    # Gate 2b: shadow_review.review_gate_clean over observed shapes
    gold_report: EvalReport               # Gate 3: the curated gold set
    audit_false_resolves: int             # Gate 3: false resolves in the stratified real-population audit
    stability: StabilityResult            # Gate 5: D7 double-compile
    drift_detected_ratio: float           # Gate 6: fraction of controlled mutations detected (need 1.0)
    signature_valid: bool                 # Gate 7: verify_report over the artifact
    policy: GatePolicy = field(default_factory=GatePolicy)


def _gate1(report: PopulationReportV1) -> tuple[bool, list[str]]:
    reasons = []
    checks = {
        "reconcile incomplete (missing run-result rows)": not report.reconcile_complete,
        "persistence loss": report.persistence_loss > 0,
        "persistence_partial rows": report.persistence_partial_count > 0,
        "incomplete compiles": report.incomplete_count > 0,
        "compile_disabled eligible recipes": report.compile_disabled_count > 0,
        "planner internal_error": report.internal_error_count > 0,
        "preloop_failure": report.preloop_failure_count > 0,
        "planner truncation/bounding (F8)": report.truncated_count > 0,
    }
    reasons = [f"Gate 1: {name}" for name, failed in checks.items() if failed]
    return not reasons, reasons


def statistical_bound(units: Iterable[SampleUnit], policy: GatePolicy
                      ) -> tuple[bool, StratifiedSample, list[str]]:
    """Gate 4: per FIXED stratum, the zero-failure Clopper-Pearson upper bound must be ≤ the policy
    max. A rare stratum (fewer distinct shapes than required) FAILS for that stratum (no
    signed-exclusion, F19). An EMPTY frame (no strata) FAILS (no evidence)."""
    sample = stratified_sample(units, seed=0, per_stratum=policy.required_shapes)
    reasons: list[str] = []
    if not sample.strata:
        reasons.append("Gate 4: empty sampling frame — no evidence for any bound")
    for s in sample.strata:
        if s.distinct_shapes == 0:
            reasons.append(f"Gate 4: stratum {s.stratum.key} has zero in-frame shapes")
        elif clopper_pearson_upper(0, s.distinct_shapes, policy.alpha) > policy.max_false_resolve_bound:
            reasons.append(f"Gate 4: stratum {s.stratum.key} bound exceeds policy "
                           f"({s.distinct_shapes} < {policy.required_shapes} shapes)")
    return not reasons, sample, reasons


def evaluate_gate(inputs: GateInputs) -> GateResult:
    """The conjunctive §10 gate. Each sub-gate is computed independently and AND-ed; NO averaging, and
    NO signed-exclusion escape hatch (F19). Machine gates (1/2a/5/6) cannot be overridden by a human."""
    reasons: list[str] = []

    gate1, r1 = _gate1(inputs.report)
    reasons += r1

    try:
        assert_map_exhaustive()
        map_exhaustive = True
    except AssertionError as exc:
        map_exhaustive = False
        reasons.append(f"Gate 2a: {exc}")
    gate2a = map_exhaustive and inputs.report.operationally_unmeasured_count == 0
    if inputs.report.operationally_unmeasured_count:
        reasons.append(f"Gate 2a: {inputs.report.operationally_unmeasured_count} operationally_unmeasured")

    gate2b = inputs.review_clean
    if not gate2b:
        reasons.append("Gate 2b: population review not clean (unlabelled / defect / unknown shape)")

    gate3 = inputs.gold_report.passed and inputs.audit_false_resolves == 0
    if not inputs.gold_report.passed:
        reasons.append(f"Gate 3: gold-set failures {inputs.gold_report.false_resolves}")
    if inputs.audit_false_resolves:
        reasons.append(f"Gate 3: {inputs.audit_false_resolves} audit false resolves")

    gate4, _, r4 = statistical_bound(inputs.report.sample_units, inputs.policy)
    reasons += r4

    gate5 = inputs.stability.stable
    if not gate5:
        reasons.append(f"Gate 5: replay unstable (compared={inputs.stability.compared}, "
                       f"mismatched={inputs.stability.mismatched_keys})")

    gate6 = inputs.drift_detected_ratio >= 1.0
    if not gate6:
        reasons.append(f"Gate 6: drift detection {inputs.drift_detected_ratio:.3f} < 1.0")

    gate7 = inputs.signature_valid
    if not gate7:
        reasons.append("Gate 7: artifact signature invalid")

    return GateResult(gate1_capture=gate1, gate2a_map=gate2a, gate2b_review=gate2b,
                      gate3_no_false_resolves=gate3, gate4_statistical_bound=gate4,
                      gate5_replay_stability=gate5, gate6_drift=gate6, gate7_artifact=gate7,
                      reasons=tuple(reasons))


# ─── §10.7 the signed artifact ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GateArtifactV1:
    """The machine-readable record of exactly what was gated — everything a verifier needs to
    reproduce the population and the policy, plus the PASS/FAIL. The DETACHED signature is NOT stored
    here (it lives in the sidecar); ``canonical_bytes`` is what the signer signs and the verifier
    checks. The trusted PUBLIC key is NEVER embedded (it is a verifier config input)."""

    code_commit: str
    producer_cohort: str
    gold_set_hash: str
    gold_set_version: str
    evaluator_version: str
    category_map_version: str
    strata_version: str
    policy_hash: str
    observation_window: tuple[str, ...]         # the run ids gated
    sample_ids: tuple[str, ...]                 # the immutable distinct contract_input_hashes audited
    review_content_hash: str                    # the signed Gate-2b review artifact identity
    signer_key_id: str                          # WHICH signing authority (not the key material)
    report_input_digest: str                    # sha256 over the population report
    gate_passed: bool
    gate_reasons: tuple[str, ...]

    def _material(self) -> dict[str, Any]:
        return {
            "code_commit": self.code_commit, "producer_cohort": self.producer_cohort,
            "gold_set_hash": self.gold_set_hash, "gold_set_version": self.gold_set_version,
            "evaluator_version": self.evaluator_version, "category_map_version": self.category_map_version,
            "strata_version": self.strata_version, "policy_hash": self.policy_hash,
            "observation_window": list(self.observation_window), "sample_ids": sorted(self.sample_ids),
            "review_content_hash": self.review_content_hash, "signer_key_id": self.signer_key_id,
            "report_input_digest": self.report_input_digest, "gate_passed": self.gate_passed,
            "gate_reasons": list(self.gate_reasons),
        }

    def canonical_bytes(self) -> bytes:
        """The exact bytes the signer signs and the verifier checks — stable, sorted, separatorless."""
        return json.dumps(self._material(), sort_keys=True, separators=(",", ":")).encode()


def report_input_digest(report: PopulationReportV1) -> str:
    material = {
        "run_ids": sorted(report.run_ids), "denominator": report.denominator,
        "numerator": report.numerator, "headline": report.headline_by_primary,
        "breakdown": report.breakdown_by_category, "recipe_outcome_matrix": report.recipe_outcome_matrix,
        "replay_freshness": report.replay_freshness,
        "operationally_unmeasured": report.operationally_unmeasured_count,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_gate_artifact(*, report: PopulationReportV1, result: GateResult, sample: StratifiedSample,
                        review_content_hash: str, policy: GatePolicy, code_commit: str,
                        producer_cohort: str, signer_key_id: str) -> GateArtifactV1:
    sample_ids = tuple(sorted({h for s in sample.strata for h in s.sampled}))
    return GateArtifactV1(
        code_commit=code_commit, producer_cohort=producer_cohort, gold_set_hash=GOLD_SET_HASH,
        gold_set_version=GOLD_SET_VERSION, evaluator_version=EVALUATOR_VERSION,
        category_map_version=CATEGORY_MAP_VERSION, strata_version=STRATA_VERSION,
        policy_hash=policy.policy_hash, observation_window=report.run_ids, sample_ids=sample_ids,
        review_content_hash=review_content_hash, signer_key_id=signer_key_id,
        report_input_digest=report_input_digest(report), gate_passed=result.passed,
        gate_reasons=result.reasons)


__all__ = [
    "EVALUATOR_VERSION", "GateArtifactV1", "GateInputs", "GatePolicy", "GateResult",
    "PopulationReportV1", "build_gate_artifact", "build_population_report", "clopper_pearson_upper",
    "evaluate_gate", "report_input_digest", "required_shapes_for_bound", "statistical_bound",
]
