"""Child-1 §J — the gold gate: what a curated authoring run must achieve, and its pinned v1 bar.

Pure and hermetic: no DB, no provider, no I/O. It scores a sequence of
``(curated case, observed AuthoringResult)`` observations, so the SAME scorer serves all three
halves of the three-way gate — the curated-fixture suite (``test_gold_gate.py``) and the key-gated
real-provider evaluation (``test_provider_eval.py``) both produce :class:`GoldOutcome`\\ s and both
are measured by :data:`GOLD_GATE_V1`.

WHAT THE METRICS ARE FOR. Every one of them is asymmetric on purpose — the expensive failure in a
governed authoring system is not "we did not produce a feature", it is "we produced one that looks
authoritative and is not":

* ``false_resolve_rate`` — the ONE that matters most: a case curated as unsupported / rejected /
  needing review that the system nevertheless auto-RESOLVED. Gated at **0**. There is no acceptable
  rate of laundering an unproven formula into authority.
* ``operand_preservation_rate`` — did the operands the intent names survive into the authored
  artifact? A formula that quietly aggregates a DIFFERENT column is the failure mode no structural
  gate can see. Gated at **1.0**.
* ``exact_match_rate`` — the curated positives reproduce the hand-authored ``formula_content_hash``
  EXACTLY. Identity, not similarity. Gated at **1.0**, and ``required_operations`` additionally
  demands at least one exact match per §J operation, so the bar cannot be met by a subset.
* ``classification_rate`` — the §J unsupported/reject cases land on the right disposition AND the
  right axis (``unsupported != invalid`` is an axis-level distinction, not a disposition-level one).
  Gated at **1.0**.
* ``blocking_critic_recall`` — the curated adversarial cases (a valid, resolvable formula that does
  not answer the question) were CAUGHT by the independent critic. Gated at **1.0**: a critic that
  misses these is not adding a second opinion.
* ``technical_failure_rate_clean`` — technical failures in the CLEAN population. Gated at **0**: a
  technical failure is honest, but on a clean case it means the loop itself is broken.

Repeated runs simply contribute more outcomes: the metrics are over OBSERVATIONS, not cases, so a
model that resolves a case correctly four times out of five fails ``exact_match_rate`` — which is
the point of running the provider evaluation repeatedly.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from featuregen.formula.critic import proposal_column_refs
from featuregen.formula.result import AuthoringResult
from featuregen.formula.turns import AuthoringIntent
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

__all__ = [
    "GOLD_GATE_V1",
    "GoldCase",
    "GoldGate",
    "GoldOutcome",
    "GoldScore",
    "REQUIRED_OPERATIONS",
    "gate_failures",
    "load_gold_cases",
    "score_gold",
]

#: The §J operations a v1 gold set must each prove with at least one EXACT positive match.
REQUIRED_OPERATIONS: frozenset[str] = frozenset(
    {"sum", "count_rows", "count_non_null", "count_distinct", "ratio", "difference"})

#: The dispositions §J's "unsupported/reject classifications" metric ranges over.
_CLASSIFICATION_DISPOSITIONS: frozenset[str] = frozenset({"UNSUPPORTED", "REJECTED"})


@dataclass(frozen=True, slots=True)
class GoldCase:
    """ONE curated ``(intent, expectation)`` pair — the immutable half of a gold observation.

    ``expected_formula_hash`` is present only for a curated POSITIVE (a case whose correct authoring
    has one exact identity); ``expected_axes`` pins whichever §F axes the case is actually about (a
    partial pin, because a curated case asserts what it curates, not everything); ``expected_operands``
    are the column refs the intent NAMES and that must therefore survive into whatever artifact the
    run carries — declare them only on cases that expect an artifact at all. ``adversarial`` marks a
    case built to be got WRONG (so it is excluded from the clean technical-failure population)."""

    case_id: str
    operation: str
    intent: AuthoringIntent
    expected_disposition: str
    expected_axes: Mapping[str, str] = field(default_factory=dict)
    expected_operands: tuple[str, ...] = ()
    expected_formula_hash: str | None = None
    expects_blocking_critic: bool = False
    adversarial: bool = False


@dataclass(frozen=True, slots=True)
class GoldOutcome:
    """One observation: what a curated case EXPECTED, and what a run actually produced."""

    case: GoldCase
    result: AuthoringResult


@dataclass(frozen=True, slots=True)
class GoldScore:
    """The measured §J metrics over a set of observations, plus the denominators they were computed
    against — a rate of ``1.0`` over an empty denominator proves nothing, so the counts travel with
    it and :func:`gate_failures` checks them."""

    observations: int
    false_resolve_rate: float
    false_resolves: int
    operand_preservation_rate: float
    operand_cases: int
    exact_match_rate: float
    exact_match_cases: int
    classification_rate: float
    classification_cases: int
    blocking_critic_recall: float
    blocking_critic_cases: int
    technical_failure_rate_clean: float
    clean_cases: int
    disposition_accuracy_rate: float
    operations_exactly_matched: frozenset[str]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoldGate:
    """A named, versioned threshold set. Every field is a HARD bar — §J says "all must hold"."""

    name: str
    max_false_resolve_rate: float
    min_operand_preservation_rate: float
    min_exact_match_rate: float
    min_classification_rate: float
    min_blocking_critic_recall: float
    max_technical_failure_rate_clean: float
    min_disposition_accuracy_rate: float
    required_operations: frozenset[str]
    min_blocking_critic_cases: int


#: §J's PINNED v1 thresholds. false-resolve 0 · operand-preservation 1.0 · blocking-critic recall
#: 1.0 · technical-failures-in-clean 0, plus §J's exact-match and classification bars and the
#: per-operation coverage requirement. Changing any of these is a policy decision, not a fix.
GOLD_GATE_V1 = GoldGate(
    name="gold_gate_v1",
    max_false_resolve_rate=0.0,
    min_operand_preservation_rate=1.0,
    min_exact_match_rate=1.0,
    min_classification_rate=1.0,
    min_blocking_critic_recall=1.0,
    max_technical_failure_rate_clean=0.0,
    min_disposition_accuracy_rate=1.0,
    required_operations=REQUIRED_OPERATIONS,
    min_blocking_critic_cases=1,
)


def score_gold(outcomes: Iterable[GoldOutcome]) -> GoldScore:
    """Measure the §J metrics over ``outcomes`` (see the module docstring for what each is FOR).

    Every rate is reported with its denominator, and ``failures`` names each observation that missed
    an expectation, so a red gate says WHICH case and WHY rather than only that a number moved."""
    observed = list(outcomes)
    failures: list[str] = []

    false_resolve_pool = [o for o in observed if o.case.expected_disposition != "RESOLVED"]
    false_resolves = [o for o in false_resolve_pool
                      if o.result.authoring_disposition == "RESOLVED"]
    for outcome in false_resolves:
        failures.append(
            f"{outcome.case.case_id}: FALSE RESOLVE — expected "
            f"{outcome.case.expected_disposition}, auto-RESOLVED instead")

    operand_pool = [o for o in observed if o.case.expected_operands]
    operand_preserved = []
    for outcome in operand_pool:
        missing = _missing_operands(outcome)
        if missing:
            failures.append(
                f"{outcome.case.case_id}: operands dropped from the authored artifact: "
                + ", ".join(sorted(missing)))
        else:
            operand_preserved.append(outcome)

    exact_pool = [o for o in observed if o.case.expected_formula_hash]
    exact_matched = []
    for outcome in exact_pool:
        if outcome.result.candidate_formula_hash == outcome.case.expected_formula_hash:
            exact_matched.append(outcome)
        else:
            failures.append(
                f"{outcome.case.case_id}: formula_content_hash "
                f"{outcome.result.candidate_formula_hash} != curated "
                f"{outcome.case.expected_formula_hash}")

    classification_pool = [o for o in observed
                           if o.case.expected_disposition in _CLASSIFICATION_DISPOSITIONS]
    classification_correct = [o for o in classification_pool if not _disposition_failures(o)]

    blocking_pool = [o for o in observed if o.case.expects_blocking_critic]
    blocking_caught = [o for o in blocking_pool if o.result.critic_status == "blocking"]
    for outcome in blocking_pool:
        if outcome.result.critic_status != "blocking":
            failures.append(
                f"{outcome.case.case_id}: the independent critic did NOT block "
                f"(critic_status={outcome.result.critic_status})")

    clean_pool = [o for o in observed if not o.case.adversarial]
    clean_technical = [o for o in clean_pool
                       if o.result.authoring_disposition == "TECHNICAL_FAILURE"]
    for outcome in clean_technical:
        failures.append(f"{outcome.case.case_id}: TECHNICAL_FAILURE on a clean gold case")

    accurate = []
    for outcome in observed:
        problems = _disposition_failures(outcome)
        if problems:
            failures.extend(f"{outcome.case.case_id}: {p}" for p in problems)
        else:
            accurate.append(outcome)

    return GoldScore(
        observations=len(observed),
        false_resolve_rate=_rate(len(false_resolves), len(false_resolve_pool)),
        false_resolves=len(false_resolves),
        operand_preservation_rate=_rate(len(operand_preserved), len(operand_pool), empty=1.0),
        operand_cases=len(operand_pool),
        exact_match_rate=_rate(len(exact_matched), len(exact_pool), empty=1.0),
        exact_match_cases=len(exact_pool),
        classification_rate=_rate(len(classification_correct), len(classification_pool), empty=1.0),
        classification_cases=len(classification_pool),
        blocking_critic_recall=_rate(len(blocking_caught), len(blocking_pool), empty=1.0),
        blocking_critic_cases=len(blocking_pool),
        technical_failure_rate_clean=_rate(len(clean_technical), len(clean_pool)),
        clean_cases=len(clean_pool),
        disposition_accuracy_rate=_rate(len(accurate), len(observed), empty=1.0),
        operations_exactly_matched=frozenset(o.case.operation for o in exact_matched),
        failures=tuple(dict.fromkeys(failures)),   # de-duplicated, order preserved
    )


def gate_failures(score: GoldScore, gate: GoldGate = GOLD_GATE_V1) -> tuple[str, ...]:
    """Every threshold ``score`` misses against ``gate`` — an EMPTY tuple is the pass.

    Returned rather than raised so a caller can report all of them at once; the coverage checks
    (``required_operations``, ``min_blocking_critic_cases``) are here too, because a gold set that
    simply omits an operation or has no adversarial case would otherwise sail through on vacuous
    rates."""
    misses: list[str] = []
    if score.false_resolve_rate > gate.max_false_resolve_rate:
        misses.append(f"false_resolve_rate {score.false_resolve_rate} > "
                      f"{gate.max_false_resolve_rate} ({score.false_resolves} false resolves)")
    if score.operand_preservation_rate < gate.min_operand_preservation_rate:
        misses.append(f"operand_preservation_rate {score.operand_preservation_rate} < "
                      f"{gate.min_operand_preservation_rate}")
    if score.exact_match_rate < gate.min_exact_match_rate:
        misses.append(f"exact_match_rate {score.exact_match_rate} < {gate.min_exact_match_rate}")
    if score.classification_rate < gate.min_classification_rate:
        misses.append(f"classification_rate {score.classification_rate} < "
                      f"{gate.min_classification_rate}")
    if score.blocking_critic_recall < gate.min_blocking_critic_recall:
        misses.append(f"blocking_critic_recall {score.blocking_critic_recall} < "
                      f"{gate.min_blocking_critic_recall}")
    if score.technical_failure_rate_clean > gate.max_technical_failure_rate_clean:
        misses.append(f"technical_failure_rate_clean {score.technical_failure_rate_clean} > "
                      f"{gate.max_technical_failure_rate_clean}")
    if score.disposition_accuracy_rate < gate.min_disposition_accuracy_rate:
        misses.append(f"disposition_accuracy_rate {score.disposition_accuracy_rate} < "
                      f"{gate.min_disposition_accuracy_rate}")
    uncovered = gate.required_operations - score.operations_exactly_matched
    if uncovered:
        misses.append("no exact positive match for: " + ", ".join(sorted(uncovered)))
    if score.blocking_critic_cases < gate.min_blocking_critic_cases:
        misses.append(f"only {score.blocking_critic_cases} adversarial blocking-critic case(s); "
                      f"{gate.min_blocking_critic_cases} required")
    return tuple(misses)


# ── internals ────────────────────────────────────────────────────────────────────────────────────

def _rate(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    """``numerator/denominator``, or ``empty`` when nothing was measured.

    The empty default differs per metric on purpose: an unmeasured FAILURE rate is 0 (nothing went
    wrong) while an unmeasured SUCCESS rate is 1.0 (nothing was missed) — and in BOTH cases the
    denominator travels in the score so :func:`gate_failures` can refuse a vacuous pass."""
    return empty if denominator == 0 else numerator / denominator


def _disposition_failures(outcome: GoldOutcome) -> tuple[str, ...]:
    """The disposition/axis expectations this observation missed (empty == it landed correctly).

    Axes are checked as well as the disposition because §F distinctions live there:
    ``unsupported_operation`` and ``unsupported_capability`` BOTH fold to UNSUPPORTED, and an
    external requirement and a blocking critic BOTH fold to NEEDS_REVIEW — a curated case that means
    one of them must not be satisfied by the other."""
    problems: list[str] = []
    if outcome.result.authoring_disposition != outcome.case.expected_disposition:
        problems.append(
            f"disposition {outcome.result.authoring_disposition} != expected "
            f"{outcome.case.expected_disposition}")
    for axis, expected in outcome.case.expected_axes.items():
        actual = getattr(outcome.result, axis, None)
        if actual != expected:
            problems.append(f"{axis} {actual!r} != expected {expected!r}")
    return tuple(problems)


def _missing_operands(outcome: GoldOutcome) -> frozenset[str]:
    """Which of the case's named operands did NOT survive into the authored artifact.

    The artifact is the ``TypedFormulaV1`` when one exists, else the reviewable proposal; a case that
    names operands but produced NEITHER has lost all of them (which is the honest reading — there is
    nothing carrying them)."""
    artifact = outcome.result.candidate_formula or outcome.result.candidate_proposal
    expected = {_norm_ref(ref) for ref in outcome.case.expected_operands}
    if artifact is None:
        return frozenset(expected)
    # proposal_column_refs walks grain keys + every body expression's operand, window event-time ref
    # and filter lefts. It reads only ``.grain``/``.body``, which TypedFormulaV1 and
    # TypedFormulaProposalV1 both carry, so it serves either artifact.
    present = {_norm_ref(ref) for ref in proposal_column_refs(artifact)}
    return frozenset(expected - present)


def _norm_ref(ref: str) -> str:
    """A ``logical_ref`` folded to its canonical form for comparison (the §E normalization), so a
    casing/whitespace difference between a curated ref and an authored one is not a dropped operand.
    An unparseable ref compares as its raw text — it is certainly not equal to a real one."""
    try:
        source, schema, table, column = parse_ref(ref)
    except ValueError:
        return ref
    return normalize_ref(source, schema, table, column)


def load_gold_cases(fixtures: Sequence[Mapping]) -> tuple[GoldCase, ...]:
    """Build :class:`GoldCase`\\ s from already-parsed curated fixture documents.

    Kept here (not in the test) so the real-provider evaluation and the curated-fixture suite read
    the SAME immutable fixtures through the SAME loader — the curated expectations cannot drift
    between the hermetic gate and the key-gated one."""
    return tuple(
        GoldCase(
            case_id=doc["case_id"],
            operation=doc["operation"],
            intent=AuthoringIntent(
                name=doc["intent"]["name"],
                hypothesis=doc["intent"]["hypothesis"],
                target_entity=doc["intent"]["target_entity"],
                target_grain_keys=tuple(doc["intent"]["target_grain_keys"]),
            ),
            expected_disposition=doc["expected_disposition"],
            expected_axes=dict(doc.get("expected_axes") or {}),
            expected_operands=tuple(doc.get("expected_operands") or ()),
            expected_formula_hash=doc.get("expected_formula_hash"),
            expects_blocking_critic=bool(doc.get("expects_blocking_critic")),
            adversarial=bool(doc.get("adversarial")),
        )
        for doc in fixtures
    )
