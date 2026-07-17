"""Phase-3B.4 D7 — the deterministic evaluators that turn the durable population + the curated gold
set into the machine inputs the 3C gate consumes. Three responsibilities, all PURE (no DB, no clock):

  1. ``evaluate`` — exact-match the gold cases' ACTUAL classifier verdict against the immutable
     expected verdict, PLUS the strict false-resolve check: a case the classifier reports RESOLVED
     that the expert asserts is NOT a valid resolution is a hard FAILURE (a false resolve is the one
     error the 3C gate exists to forbid — it can NEVER be traded off against coverage).
  2. ``stratified_sample`` — the seeded, shape-weighted, per-stratum sampler over the real population.
     The sampling unit is a DISTINCT ``contract_input_hash`` (clustered/repeated traffic is deduped —
     repeated runs of one shape are not independent evidence); a stratum with fewer distinct shapes
     than required is flagged ``rare`` (§10: rare strata FAIL the gate, no signed-exclusion in v1).
  3. ``double_compile_stable`` — the determinism procedure: compile the SAME frozen fixture twice and
     compare verdicts, over ONLY identity-comparable (``complete``) runs (D6/F17 — a budget-truncated
     run's compiled set depends on wall-time, so it is excluded). An EMPTY comparison FAILS: stability
     cannot be claimed from zero evidence.
"""
from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.planner.shadow_capture import is_identity_comparable
from featuregen.overlay.upload.planner.shadow_store import CompileStatus
from featuregen.overlay.upload.planner.strata import StratumId, stratum_of


# ─── 1. gold-set exact match + false-resolve ──────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ExpectedVerdict:
    """The immutable expert-authored expectation for one gold case."""

    declaration_status: str
    contract_resolution_status: str
    primary_reason_code: str | None
    cause: str                    # the Layer-B ResolutionCause value
    resolved_is_valid: bool       # expert assertion: IF this resolves, is that resolution correct?


@dataclass(frozen=True, slots=True)
class ActualVerdict:
    """What the real classifier produced for the case's seeded fixture."""

    declaration_status: str
    contract_resolution_status: str
    primary_reason_code: str | None
    cause: str


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    passed: bool
    false_resolve: bool           # the classifier resolved a case the expert says must NOT resolve
    mismatches: tuple[str, ...]   # human-readable field diffs


@dataclass(frozen=True, slots=True)
class EvalReport:
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        """The gold set passes ONLY with zero mismatches AND zero false resolves."""
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def false_resolves(self) -> tuple[str, ...]:
        return tuple(r.case_id for r in self.results if r.false_resolve)


_RESOLVED_DECL = "resolved"


def _evaluate_case(case_id: str, expected: ExpectedVerdict, actual: ActualVerdict) -> CaseResult:
    mismatches: list[str] = []
    for field in ("declaration_status", "contract_resolution_status", "primary_reason_code", "cause"):
        exp, act = getattr(expected, field), getattr(actual, field)
        if exp != act:
            mismatches.append(f"{field}: expected {exp!r}, got {act!r}")
    # the strict false-resolve check is INDEPENDENT of exact-match: the classifier resolved the
    # declaration, but the expert asserts this shape must not be treated as a valid resolution.
    false_resolve = (actual.declaration_status == _RESOLVED_DECL and not expected.resolved_is_valid)
    if false_resolve:
        mismatches.append("FALSE RESOLVE: classifier resolved a case the expert marks invalid")
    return CaseResult(case_id=case_id, passed=not mismatches, false_resolve=false_resolve,
                      mismatches=tuple(mismatches))


def evaluate(pairs: Iterable[tuple[str, ExpectedVerdict, ActualVerdict]]) -> EvalReport:
    """Exact-match + false-resolve over ``(case_id, expected, actual)`` triples."""
    return EvalReport(results=tuple(_evaluate_case(cid, e, a) for cid, e, a in pairs))


# ─── 2. seeded, shape-weighted stratified sampler ─────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SampleUnit:
    """One population observation as the sampler sees it. The frame is ``is_selected`` + ``is_complete``
    plans; the sampling unit is the DISTINCT ``contract_input_hash``."""

    tier: str
    family: str
    contract_resolution_status: str
    primary_reason_code: str | None
    contract_input_hash: str
    is_selected: bool
    is_complete: bool

    @property
    def stratum(self) -> StratumId:
        return stratum_of(tier=self.tier, family=self.family,
                          contract_resolution_status=self.contract_resolution_status,
                          primary_reason_code=self.primary_reason_code)


@dataclass(frozen=True, slots=True)
class StratumSample:
    stratum: StratumId
    distinct_shapes: int
    sampled: tuple[str, ...]       # the chosen distinct contract_input_hashes (deterministic)
    rare: bool                     # distinct_shapes < per_stratum → the stratum FAILS the gate


@dataclass(frozen=True, slots=True)
class StratifiedSample:
    seed: int
    per_stratum: int
    strata: tuple[StratumSample, ...]

    @property
    def rare_strata(self) -> tuple[StratumId, ...]:
        return tuple(s.stratum for s in self.strata if s.rare)


def stratified_sample(units: Iterable[SampleUnit], *, seed: int, per_stratum: int) -> StratifiedSample:
    """Partition the FRAME (selected + complete plans) into strata, DEDUP by distinct
    ``contract_input_hash`` (a repeated shape is one unit of evidence), then draw up to ``per_stratum``
    shapes from each stratum with a preserved seed. A stratum with fewer distinct shapes than
    ``per_stratum`` is ``rare`` (insufficient evidence for its bound).

    Fail-closed / observable (no silent exclusion): the strata are enumerated over the PRE-FRAME
    universe — EVERY stratum that has any population at all — so a stratum whose whole population is
    out-of-frame (e.g. every run of that shape was budget-truncated) surfaces as an explicit
    zero-coverage ``rare`` sample, never a silent absence that the gate would read as covered."""
    units = list(units)
    universe = {u.stratum for u in units}             # every OBSERVED stratum (in or out of frame)
    by_stratum: dict[StratumId, set[str]] = {}
    for u in units:
        if not (u.is_selected and u.is_complete):
            continue                                  # outside the sampling frame
        by_stratum.setdefault(u.stratum, set()).add(u.contract_input_hash)
    strata: list[StratumSample] = []
    for stratum in sorted(universe, key=lambda s: s.key):
        hashes = sorted(by_stratum.get(stratum, set()))   # deterministic base order ((), if 0 in-frame)
        rng = random.Random(f"{seed}:{stratum.key}")      # seed folds the stratum so draws differ per stratum
        rng.shuffle(hashes)
        sampled = tuple(sorted(hashes[:per_stratum]))
        strata.append(StratumSample(stratum=stratum, distinct_shapes=len(hashes),
                                    sampled=sampled, rare=len(hashes) < per_stratum))
    return StratifiedSample(seed=seed, per_stratum=per_stratum, strata=tuple(strata))


# ─── 3. double-compile determinism procedure ──────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CompileVerdict:
    """One plan's verdict from a single compile of the frozen fixture, keyed for pairing."""

    key: str
    compile_status: CompileStatus
    contract_id: str | None
    declaration_status: str


@dataclass(frozen=True, slots=True)
class StabilityResult:
    stable: bool
    compared: int                       # identity-comparable pairs actually checked
    mismatched_keys: tuple[str, ...]


def double_compile_stable(first: Iterable[CompileVerdict],
                          second: Iterable[CompileVerdict]) -> StabilityResult:
    """Compare two compiles of the SAME frozen fixture. A verdict is comparable only when the run is
    identity-comparable (not budget-truncated — D6/F17; the caller feeds compiled fixtures so the
    comparison is non-vacuous). Stable iff there is at least one comparable pair AND every comparable
    pair agrees on ``(contract_id, declaration_status)``.

    Fail-closed / observable (no silent exclusion): a plan that is comparable in ONE compile but not
    the other — dropped, added, or its status flipped to/from budget-truncated between runs — is a
    plan-set DIVERGENCE and is flagged, never silently tolerated. An empty comparison is NOT stable
    (no evidence)."""
    first_by = {v.key: v for v in first}
    second_by = {v.key: v for v in second}
    cmp_first = {k for k, v in first_by.items() if is_identity_comparable(v.compile_status)}
    cmp_second = {k for k, v in second_by.items() if is_identity_comparable(v.compile_status)}
    mismatched: set[str] = cmp_first ^ cmp_second      # a comparable plan present on only one side
    compared = 0
    for k in cmp_first & cmp_second:
        compared += 1
        a, b = first_by[k], second_by[k]
        if (a.contract_id, a.declaration_status) != (b.contract_id, b.declaration_status):
            mismatched.add(k)
    return StabilityResult(stable=compared > 0 and not mismatched, compared=compared,
                           mismatched_keys=tuple(sorted(mismatched)))
