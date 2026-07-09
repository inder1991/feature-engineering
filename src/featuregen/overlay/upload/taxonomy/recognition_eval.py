"""Phase-1A Task 5 — the evaluation harness + false-narrowing metrics.

This harness runs the shadow recognizer over the gold set and scores the TWO metrics that gate
Phase 1B (see the plan's "Global Constraints"):

* **Recognition accuracy** — did the LLM pick the right objective (``primary_accuracy``,
  ``top3_recall``), abstain when it should (``abstention_precision``), and answer stably
  (``stability``).
* **Applicability recall / false-narrowing** — after a recognised scope is mapped to concrete
  in-scope recipe ids (Task 3), are the recipes an expert wanted still retained? A gold
  ``expected_relevant_recipe`` the recognised scope drops is a **false narrowing** — the failure
  mode Phase-1B filtering must not introduce. ``applicability_recall`` is the Phase-1B gate;
  ``false_narrowing_regulated`` isolates the regulated (fair-lending / AML) cases where a dropped
  recipe is most costly.

``evaluate`` is deterministic given a deterministic client, so CI drives it with scripted stubs
(see the test). ``main`` resolves the process-wide ``LLMClient`` and prints the report: it is the
runnable REAL-LLM shadow run whose results (with the gold set's expert review) gate Phase 1B —
``python -m featuregen.overlay.upload.taxonomy.recognition_eval``.

Behaviour-neutral: read-only over the recognizer, the applicability evaluator and the gold set —
nothing here filters grounding or touches ``templates.py``. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 5.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.intake.llm import LLMClient, current_llm_client
from featuregen.overlay.upload.taxonomy.applicability import (
    ConfirmedScope,
    in_scope_recipes,
    scope_from_recognition,
)

# The gold set is an EVAL artifact (Task 4) that lives in the tests tree; this harness — and its
# runnable real-LLM shadow run — is the one place production code reads it, as the default corpus.
from featuregen.overlay.upload.taxonomy.gold_recognition import GOLD, GoldCase
from featuregen.overlay.upload.taxonomy.recognition import RecognitionStatus
from featuregen.overlay.upload.taxonomy.recognizer import recognize

# Statuses the recognizer emits when it declines to narrow: a genuine abstention (``UNSCOPED``) or a
# fail-open technical failure (``TECHNICAL_FAILURE``). Both fold to full grounding; abstention
# precision measures whether those declines land on the truly-unscoped cases.
_ABSTAINED: frozenset[RecognitionStatus] = frozenset(
    {RecognitionStatus.UNSCOPED, RecognitionStatus.TECHNICAL_FAILURE})


@dataclass(frozen=True)
class CaseResult:
    """One gold case's outcome. ``false_narrowed`` are the expected-relevant recipes that fell OUT of
    the recognised scope (an expert wanted them; the scope dropped them)."""

    case_id: str
    category: str
    recognized_primary: str | None
    primary_correct: bool
    false_narrowed: tuple[str, ...]


@dataclass(frozen=True)
class EvalReport:
    """The scored run over the gold set. ``applicability_recall`` is the Phase-1B gate;
    ``false_narrowing_count`` / ``false_narrowing_regulated`` count cases (not recipes) that lost at
    least one expert-relevant recipe."""

    primary_accuracy: float
    top3_recall: float
    applicability_recall: float
    false_narrowing_count: int
    false_narrowing_regulated: int
    abstention_precision: float
    stability: float
    per_case: tuple[CaseResult, ...]


def _scope_signature(scope: ConfirmedScope) -> tuple[str | None, tuple[str, ...], bool]:
    """A comparable identity for a scope: primary + SORTED secondary + the unscoped flag. Sorting the
    secondaries makes the stability check order-insensitive (a re-run that returns the same objectives
    in a different order is still stable)."""
    return (scope.primary, tuple(sorted(scope.secondary)), scope.unscoped)


def evaluate(client: LLMClient, gold: tuple[GoldCase, ...] = GOLD) -> EvalReport:
    """Recognise every gold case, map each recognised scope to in-scope recipe ids, and score the run.

    For each case: ``recognize`` -> ``scope_from_recognition`` -> ``in_scope_recipes``; ``retained``
    is the union of the primary-scoped and supporting-scoped recipe ids. A gold
    ``expected_relevant_recipe`` that is not ``retained`` is a **false narrowing**. Metrics:

    * ``primary_accuracy`` — fraction of cases whose recognised primary == ``expected_primary`` (both
      ``None`` counts as correct — a correctly-unscoped case).
    * ``top3_recall`` — of cases with a real ``expected_primary``, the fraction where it appears among
      the recognised candidates' ids (the recognizer returns at most three).
    * ``applicability_recall`` — total expected-relevant recipes retained / total expected-relevant
      across all cases (the Phase-1B gate). ``1.0`` when there are none.
    * ``false_narrowing_count`` / ``false_narrowing_regulated`` — cases with any false-narrowing, and
      that count restricted to ``category == "regulated"``.
    * ``abstention_precision`` — of the cases the recognizer abstained on (``UNSCOPED`` /
      ``TECHNICAL_FAILURE``), the fraction that were truly ``category == "unscoped"``. ``1.0`` when it
      never abstained.
    * ``stability`` — fraction of cases whose recognised scope is identical on a SECOND ``recognize``
      call (primary + sorted secondary + unscoped flag).
    """
    per_case: list[CaseResult] = []
    primary_correct_count = 0
    top3_hits = 0
    top3_total = 0
    retained_relevant = 0
    total_relevant = 0
    false_narrowing_count = 0
    false_narrowing_regulated = 0
    abstained_total = 0
    abstained_on_unscoped = 0
    stable_count = 0

    for case in gold:
        result = recognize(
            client, redacted_hypothesis=case.hypothesis, redacted_goal=case.prediction_goal)
        scope = scope_from_recognition(result)
        primary_scoped, supporting_scoped = in_scope_recipes(scope)
        retained = primary_scoped | supporting_scoped

        # Recognition accuracy: the recognised primary objective (None when abstained / no primary).
        primary_correct = scope.primary == case.expected_primary
        if primary_correct:
            primary_correct_count += 1
        if case.expected_primary is not None:
            top3_total += 1
            if case.expected_primary in {c.use_case_id for c in result.candidates}:
                top3_hits += 1

        # Applicability recall / false-narrowing: expert-relevant recipes the scope dropped.
        false_narrowed = tuple(r for r in case.expected_relevant_recipes if r not in retained)
        retained_relevant += len(case.expected_relevant_recipes) - len(false_narrowed)
        total_relevant += len(case.expected_relevant_recipes)
        if false_narrowed:
            false_narrowing_count += 1
            if case.category == "regulated":
                false_narrowing_regulated += 1

        # Abstention precision: did an abstention land on a truly-unscoped case?
        if result.status in _ABSTAINED:
            abstained_total += 1
            if case.category == "unscoped":
                abstained_on_unscoped += 1

        # Stability: an identical scope on an independent second recognition of the same input.
        scope_again = scope_from_recognition(
            recognize(
                client, redacted_hypothesis=case.hypothesis, redacted_goal=case.prediction_goal))
        if _scope_signature(scope) == _scope_signature(scope_again):
            stable_count += 1

        per_case.append(
            CaseResult(
                case_id=case.id,
                category=case.category,
                recognized_primary=scope.primary,
                primary_correct=primary_correct,
                false_narrowed=false_narrowed,
            ))

    n = len(gold)
    return EvalReport(
        primary_accuracy=primary_correct_count / n if n else 1.0,
        top3_recall=top3_hits / top3_total if top3_total else 1.0,
        applicability_recall=retained_relevant / total_relevant if total_relevant else 1.0,
        false_narrowing_count=false_narrowing_count,
        false_narrowing_regulated=false_narrowing_regulated,
        abstention_precision=abstained_on_unscoped / abstained_total if abstained_total else 1.0,
        stability=stable_count / n if n else 1.0,
        per_case=tuple(per_case),
    )


def format_report(report: EvalReport) -> str:
    """Render an ``EvalReport`` as a human-readable block: the headline metrics, then the per-case
    false-narrowing detail (the expert-relevant recipes each narrowed case dropped)."""
    lines = [
        "use-case recognizer — shadow evaluation",
        "=" * 46,
        f"cases                 : {len(report.per_case)}",
        f"primary_accuracy      : {report.primary_accuracy:.3f}",
        f"top3_recall           : {report.top3_recall:.3f}",
        f"applicability_recall  : {report.applicability_recall:.3f}   <- Phase-1B gate",
        f"false_narrowing_count : {report.false_narrowing_count}",
        f"  of which regulated  : {report.false_narrowing_regulated}",
        f"abstention_precision  : {report.abstention_precision:.3f}",
        f"stability             : {report.stability:.3f}",
        "",
        "false-narrowing detail (expert-relevant recipes the recognised scope dropped):",
    ]
    narrowed = [cr for cr in report.per_case if cr.false_narrowed]
    if narrowed:
        for cr in narrowed:
            lines.append(
                f"  {cr.case_id} [{cr.category}] primary={cr.recognized_primary!r} "
                f"dropped={list(cr.false_narrowed)}")
    else:
        lines.append("  (none — every case retained all its expert-relevant recipes)")
    return "\n".join(lines)


def main() -> None:
    """Run the recognizer over the gold set with the process-wide ``LLMClient`` and print the report.

    This is the runnable REAL-LLM shadow run — its applicability-recall / false-narrowing results
    (together with expert review of the gold set) gate Phase 1B. Requires a registered client
    (``register_llm_client``); ``current_llm_client`` fails closed with a clear error otherwise."""
    print(format_report(evaluate(current_llm_client())))


if __name__ == "__main__":
    main()
