"""Phase 3C.2b-i-B · Task 10 — Gate 1: the COMPONENT-QUALIFICATION gate for ``govern_llm_idea`` (T9).

The measurement that lets B land shadow-only. It drives the immutable, partitioned Gate-1 gold
(:mod:`b_gate1_gold`) through the REAL ``govern_llm_idea`` (all authority seeded via the real governance
commands) and evaluates the clean population against six criteria (mirrors A's ``multisource_gate``):

  1. **Seed** every clean-population case via its real-command seeder (NO direct table inserts for
     concept/grain/bridge authority).
  2. **Exact outcomes + operand/operation preservation:** each POSITIVE two-axis-governs to a
     :class:`GovernedResult` whose normalized intent carries the expected operand
     (``semantic_role=measure`` + ``path_strategy.aggregation=sum``), ``final_expression.operation=
     identity``, the expected composite ``source_grain_key_refs``, and both winning ids; each NEGATIVE
     returns its EXACT :class:`BDisposition` and NEVER a ``GovernedResult``.
  3. **Non-vacuity:** distinct positive ``shape``s that governed ``>= B_GATE1_MIN_POSITIVE_SHAPES`` (a
     reject-all / no-op ``govern_llm_idea`` cannot clear this).
  4. **Zero false resolves:** no NEGATIVE case returned a ``GovernedResult``.
  5. **Determinism:** every case run a SECOND time on the same seeded authority yields an IDENTICAL
     outcome — the same ``BDisposition``, and for positives an identical normalized ``intent`` (frozen
     equality) + equal ``selected_plan_id`` / ``selected_contract_id``.
  6. **No fault leak + fault controls classified exactly:** no clean case reads ``technical_failure`` /
     ``budget_truncated``, and the SEPARATE fault-control partition classifies each injected fault
     exactly — EXCLUDED from criteria 2–5's clean population.

Resolution rate is DESCRIPTIVE — the gate does not gate on it. The gate is not vacuous:
:func:`evaluate_b_gate1` FAILS positive coverage under a reject-all ``govern_llm_idea`` and FAILS on a
fault reading leaked into the clean population (the Task-10 test proves both).

Read-only over every reused surface (``govern_llm_idea`` + the gold); this module only drives and
measures — it edits no reused engine / T2–T9 module (behaviour-neutrality). The one write it performs is
seeding the gold's authority through the real commands.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.planner import b_service
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_gate1_gold import (
    B_GATE1_MIN_POSITIVE_SHAPES,
    CORRECTNESS_GOLD,
    FAULT_CONTROLS,
    FRESH_WITHIN,
    GOLD_NOW,
    RUN_ID,
    BFaultControl,
    BGate1Case,
    seed_correctness_gold,
)
from featuregen.overlay.upload.planner.b_service import GovernedResult, govern_llm_idea
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalOperation,
    MultiSourceReason,
    PathAggregation,
    SemanticRole,
)
from featuregen.overlay.upload.planner.multisource_shadow import _synthetic_result

# The two technical/capture dispositions that must NEVER appear in the clean population (they are the
# fault-control partition's exclusive vocabulary).
_FAULT_DISPOSITIONS: frozenset[BDisposition] = frozenset(
    {BDisposition.technical_failure, BDisposition.budget_truncated})

# One run's outcome: the returned result (GovernedResult | BDisposition) OR None with a captured error
# string (a raised precondition — e.g. a no-op seed leaving no confirmed scope). Kept as a plain tuple
# so the evaluator stays pure over already-captured outcomes.
_Outcome = tuple[object | None, str | None]
GovernFn = Callable[..., object]


@dataclass(frozen=True, slots=True)
class BGate1Report:
    """The Gate-1 verdict over the CLEAN correctness population + the fault-control partition.

    ``passed`` iff EVERY criterion held. Each boolean is one criterion; ``failures`` carries a
    human-readable reason per breach (empty iff passed). ``positive_shapes_covered`` is the set of
    positive shapes that two-axis-governed; ``resolution_rate`` is DESCRIPTIVE (negatives reject by
    design, so it never gates)."""
    passed: bool
    positive_shapes_covered: tuple[str, ...]
    positive_coverage_ok: bool
    outcomes_match_expected: bool
    operand_operation_preservation_ok: bool
    zero_false_resolves: bool
    deterministic_ok: bool
    no_fault_leak: bool
    fault_controls_ok: bool
    resolution_rate: float
    failures: tuple[str, ...] = field(default_factory=tuple)


def _feature_engineer() -> IdentityEnvelope:
    """A ``feature_engineer`` principal — authenticated + carrying ``feature:generate`` (the role
    ``govern_llm_idea``'s auth precondition clears)."""
    return IdentityEnvelope(subject="fe", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("feature_engineer",))


def _disposition_of(result: object | None) -> BDisposition | None:
    """The ``BDisposition`` a captured outcome carries (a ``GovernedResult``'s is ``governed``), or
    ``None`` for a raised/absent outcome."""
    if isinstance(result, GovernedResult):
        return result.disposition
    if isinstance(result, BDisposition):
        return result
    return None


def _identity(outcome: _Outcome) -> tuple[object, ...]:
    """A determinism-comparable identity for a captured outcome: for a governed result, plan id + the
    normalized intent + both winning ids (A keys these on stable authored fact_keys); for a disposition,
    the disposition; for a raised outcome, its error string. Byte-stable across runs on the same seeded
    authority."""
    result, error = outcome
    if isinstance(result, GovernedResult):
        return ("governed", result.disposition, result.intent,
                result.planning_result.selected_plan_id,
                result.planning_result.selected_contract_id)
    if isinstance(result, BDisposition):
        return ("disposition", result)
    return ("error", error)


def _preservation_failures(case: BGate1Case, result: object) -> list[str]:
    """The operand/operation-preservation breaches of a POSITIVE's governed result (empty iff every
    expectation holds): the single MEASURE operand summed, IDENTITY final expression, the expected
    composite source grain keys preserved verbatim, and both winning ids set."""
    out: list[str] = []
    if not isinstance(result, GovernedResult):
        return [f"{case.case_id}: positive did not two-axis-govern (got {result!r})"]
    intent = result.intent
    if len(intent.operands) != 1:
        out.append(f"{case.case_id}: expected exactly one operand, got {len(intent.operands)}")
        return out
    op = intent.operands[0]
    if op.semantic_role is not SemanticRole.measure:
        out.append(f"{case.case_id}: operand role {op.semantic_role} != measure")
    if op.path_strategy.aggregation is not PathAggregation.sum:
        out.append(f"{case.case_id}: aggregation {op.path_strategy.aggregation} != sum")
    if intent.final_expression.operation is not FinalOperation.identity:
        out.append(f"{case.case_id}: final operation {intent.final_expression.operation} != identity")
    got_keys = tuple(op.source_binding.source_grain_key_refs)
    if got_keys != case.expected_grain_key_refs:
        out.append(f"{case.case_id}: source_grain_key_refs {got_keys} != {case.expected_grain_key_refs}")
    if result.planning_result.selected_plan_id is None:
        out.append(f"{case.case_id}: selected_plan_id is None")
    if result.planning_result.selected_contract_id is None:
        out.append(f"{case.case_id}: selected_contract_id is None")
    return out


# ── the pure evaluator (reads already-captured outcomes only — no planning, no DB writes) ─────────
def evaluate_b_gate1(
        case_runs: dict[str, tuple[_Outcome, _Outcome]],
        fault_runs: Sequence[tuple[BFaultControl, _Outcome]],
        *, cases: Sequence[BGate1Case] = CORRECTNESS_GOLD) -> BGate1Report:
    """Evaluate the Gate-1 criteria over ALREADY-CAPTURED outcomes: ``case_runs`` maps each clean case's
    id to its (first-run, second-run) outcomes; ``fault_runs`` pairs each fault control with its captured
    outcome. Pure over the inputs — it drives nothing and writes nothing, so a test can call it with
    poked outcomes to prove the gate is not vacuous."""
    cases = tuple(cases)
    failures: list[str] = []

    outcomes_match = True
    preservation_ok = True
    zero_false_resolves = True
    deterministic_ok = True
    no_fault_leak = True
    resolved_positive = 0
    total = 0
    governed_shapes: set[str] = set()

    for case in cases:
        total += 1
        runs = case_runs.get(case.case_id)
        if runs is None:
            outcomes_match = False
            failures.append(f"missing outcome for case={case.case_id}")
            continue
        run1, run2 = runs
        result1 = run1[0]
        disp1 = _disposition_of(result1)

        # (5) determinism — identical identity across the two runs.
        if _identity(run1) != _identity(run2):
            deterministic_ok = False
            failures.append(f"non-deterministic case={case.case_id}: {run1} vs {run2}")

        # (6-leak) no technical/truncation reading in the clean population.
        if disp1 in _FAULT_DISPOSITIONS:
            no_fault_leak = False
            failures.append(f"fault disposition {disp1} leaked into clean pop case={case.case_id}")

        # (2) exact expected disposition.
        if disp1 is not case.expected:
            outcomes_match = False
            failures.append(
                f"outcome mismatch case={case.case_id}: got {disp1} ({run1[1]}) want {case.expected}")

        if case.is_positive:
            # (2) operand/operation/composite-grain preservation for the governed positive.
            pf = _preservation_failures(case, result1)
            if pf:
                preservation_ok = False
                failures.extend(pf)
            if isinstance(result1, GovernedResult) and result1.disposition is BDisposition.governed:
                resolved_positive += 1
                if case.shape is not None:
                    governed_shapes.add(case.shape)
        else:
            # (4) zero false resolves — a negative NEVER leaks a GovernedResult.
            if isinstance(result1, GovernedResult):
                zero_false_resolves = False
                failures.append(f"negative leaked a GovernedResult case={case.case_id}")

    # (3) non-vacuity: distinct positive shapes that governed.
    positive_coverage_ok = len(governed_shapes) >= B_GATE1_MIN_POSITIVE_SHAPES
    if not positive_coverage_ok:
        failures.append(
            f"positive coverage {len(governed_shapes)} < {B_GATE1_MIN_POSITIVE_SHAPES} "
            f"(shapes={sorted(governed_shapes)})")

    # (6) fault controls classified EXACTLY (their own partition — never in the clean population).
    fault_controls_ok = True
    clean_ids = {c.case_id for c in cases}
    for ctrl, outcome in fault_runs:
        if ctrl.control_id in clean_ids:      # structural guard: a control must not be a clean case
            fault_controls_ok = False
            failures.append(f"fault control {ctrl.control_id} collides with a clean case id")
        if _disposition_of(outcome[0]) is not ctrl.expected:
            fault_controls_ok = False
            failures.append(
                f"fault control {ctrl.control_id}: got {_disposition_of(outcome[0])} "
                f"({outcome[1]}) want {ctrl.expected}")

    passed = (positive_coverage_ok and outcomes_match and preservation_ok and zero_false_resolves
              and deterministic_ok and no_fault_leak and fault_controls_ok)
    return BGate1Report(
        passed=passed, positive_shapes_covered=tuple(sorted(governed_shapes)),
        positive_coverage_ok=positive_coverage_ok, outcomes_match_expected=outcomes_match,
        operand_operation_preservation_ok=preservation_ok, zero_false_resolves=zero_false_resolves,
        deterministic_ok=deterministic_ok, no_fault_leak=no_fault_leak,
        fault_controls_ok=fault_controls_ok,
        resolution_rate=(resolved_positive / total) if total else 0.0,
        failures=tuple(failures))


# ── outcome capture (drives govern_llm_idea; robust to a raised precondition) ─────────────────────
def _run_once(govern_fn: GovernFn, conn: DbConn, adapter: CatalogAdapter, *,
              actor: IdentityEnvelope, proposal: object, now: datetime, fresh_within: object,
              budget: CompileBudget | None = None) -> _Outcome:
    """Drive one ``govern_llm_idea`` (or an injected stand-in) and CAPTURE its outcome. A raised
    precondition (e.g. a stubbed/absent confirmed scope) is captured as ``(None, error)`` rather than
    aborting the gate run — the criteria then read it as neither governed nor the expected disposition."""
    try:
        result = govern_fn(conn, adapter, actor=actor, proposal=proposal,
                           generation_run_id=RUN_ID, now=now, fresh_within=fresh_within,
                           budget=budget)
        return (result, None)
    except Exception as exc:   # noqa: BLE001 — the harness classifies; it must not itself abort
        return (None, f"{type(exc).__name__}: {exc}")


def _boom_plan(conn: DbConn, adapter: object, **kwargs: object) -> object:
    """A GENUINE DB error inside the T9 savepoint (the injected ``db_error`` fault): a bad statement
    aborts the subtransaction, which ``conn.transaction()`` rolls back to the savepoint — exactly the
    failure ``govern_llm_idea`` must contain and classify ``technical_failure``."""
    conn.execute("SELECT * FROM __b_gate1_no_such_table__")
    raise AssertionError("unreachable")   # pragma: no cover


def _budget_truncating_plan(real_plan: Callable[..., object]) -> Callable[..., object]:
    """Wrap ``plan_multi_source`` so a SPENT compile budget yields A's OWN canonical budget-truncated
    result (:func:`_synthetic_result` — the exact object A's shadow harness produces on budget
    exhaustion). It reproduces, at the plan seam, the ``budget.remaining <= 0`` rule the A shadow harness
    applies (``multisource_shadow``) but which the DIRECT ``plan_multi_source`` call in ``govern_llm_idea``
    does NOT itself apply — so the ``budget=`` param is otherwise inert through T9 (see the module report).
    This is a fault INJECTION at the same seam as ``_boom_plan``; it fabricates no governed result, only
    the genuine spent-budget outcome the run's classification must fold to ``budget_truncated``."""
    def _plan(conn: DbConn, adapter: object, *, intent: object, budget: object = None,
              **kwargs: object) -> object:
        if isinstance(budget, CompileBudget) and budget.remaining <= 0:
            return _synthetic_result(intent, None, MultiSourceReason.budget_truncated)  # type: ignore[arg-type]
        return real_plan(conn, adapter, intent=intent, budget=budget, **kwargs)
    return _plan


def run_fault_controls(
        conn: DbConn, adapter: CatalogAdapter, *, actor: IdentityEnvelope, now: datetime,
        fresh_within: object,
        controls: Sequence[BFaultControl] = FAULT_CONTROLS,
) -> list[tuple[BFaultControl, _Outcome]]:
    """Drive the fault-control partition under its OWN handling (excluded from the clean population): a
    ``db_error`` control temporarily swaps ``b_service.plan_multi_source`` for a raising stand-in (the T9
    savepoint must catch it); a ``budget_truncation`` control passes a SPENT compile budget. Each is
    driven through the REAL ``govern_llm_idea`` — the point is the real service's fault classification."""
    out: list[tuple[BFaultControl, _Outcome]] = []
    for ctrl in controls:
        if ctrl.injection == "db_error":
            original = b_service.plan_multi_source
            b_service.plan_multi_source = _boom_plan   # type: ignore[assignment]
            try:
                outcome = _run_once(govern_llm_idea, conn, adapter, actor=actor,
                                    proposal=ctrl.proposal, now=now, fresh_within=fresh_within)
            finally:
                b_service.plan_multi_source = original
        elif ctrl.injection == "budget_truncation":
            budget = CompileBudget(remaining=0, deadline_monotonic=float("inf"),
                                   clock=time.monotonic)
            original = b_service.plan_multi_source
            b_service.plan_multi_source = _budget_truncating_plan(original)   # type: ignore[assignment]
            try:
                outcome = _run_once(govern_llm_idea, conn, adapter, actor=actor,
                                    proposal=ctrl.proposal, now=now, fresh_within=fresh_within,
                                    budget=budget)
            finally:
                b_service.plan_multi_source = original
        else:   # pragma: no cover — the closed injection vocabulary
            raise ValueError(f"unknown fault injection {ctrl.injection!r}")
        out.append((ctrl, outcome))
    return out


# ── the runner (seed → run each case twice + the fault controls → evaluate) ───────────────────────
def run_b_gate1(
        conn: DbConn, adapter: CatalogAdapter, *, service_actor: IdentityEnvelope,
        human_actor: IdentityEnvelope, now: datetime = GOLD_NOW, fresh_within: object = FRESH_WITHIN,
        cases: Sequence[BGate1Case] = CORRECTNESS_GOLD,
        controls: Sequence[BFaultControl] = FAULT_CONTROLS,
        seed_fn: Callable[..., None] = seed_correctness_gold,
        govern_fn: GovernFn = govern_llm_idea) -> BGate1Report:
    """Seed the Gate-1 gold through the REAL governance commands, drive each clean case's RAW proposal
    through ``govern_llm_idea`` TWICE (same seeded authority — ``govern_llm_idea`` is read-only over it,
    so a second run is naturally deterministic), drive the fault-control partition under its own
    handling, and evaluate the criteria.

    ``govern_fn`` is injectable so a test can pass a reject-all stand-in to prove the gate is NOT vacuous
    (positive coverage collapses); ``seed_fn`` is injectable for the same reason. The fault controls
    always use the REAL ``govern_llm_idea`` (their point is the real service's fault classification)."""
    cases = tuple(cases)
    actor = _feature_engineer()
    seed_fn(conn, service_actor=service_actor, human_actor=human_actor, now=now)

    case_runs: dict[str, tuple[_Outcome, _Outcome]] = {}
    for case in cases:
        run1 = _run_once(govern_fn, conn, adapter, actor=actor, proposal=case.proposal, now=now,
                         fresh_within=fresh_within)
        run2 = _run_once(govern_fn, conn, adapter, actor=actor, proposal=case.proposal, now=now,
                         fresh_within=fresh_within)
        case_runs[case.case_id] = (run1, run2)

    fault_runs = run_fault_controls(conn, adapter, actor=actor, now=now, fresh_within=fresh_within,
                                    controls=controls)
    return evaluate_b_gate1(case_runs, fault_runs, cases=cases)
