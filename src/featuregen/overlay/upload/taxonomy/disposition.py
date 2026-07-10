"""Phase-1B Task 5 — the per-stage disposition model (grounded results -> a disposition lens).

Where :mod:`applicability` decides *once* which recipes are in scope, this module folds that single
:class:`~featuregen.overlay.upload.taxonomy.applicability.ApplicabilityResult` together with the
grounding + safety-gauntlet outcomes into one auditable, per-stage :class:`RecipeEvaluation` per recipe.
It **consumes** the shared applicability result — it never recomputes applicability, and it never
rescans the template registry.

Three staged evaluations per recipe (:class:`StageEvaluation`), each carrying its own
``evaluation_version`` (the mapping/taxonomy version it ran under) and ``evaluated_at`` (server clock)
so a disposition is replayable:

* **applicability** — always ``COMPLETED``; its ``reason_codes`` are lifted straight from the shared
  ``ApplicabilityResult`` (the single source of truth for *why* a recipe was placed where it was).
* **grounding** — did the recipe bind a candidate against the catalog?
* **safety** — did the safety gauntlet pass the bound candidate?

A stage that never ran is stamped ``NOT_EVALUATED`` with an explanatory reason — **never a bare
null** — so the lens can always say *why* a downstream stage was skipped (an ``out_of_scope`` recipe
never grounds; an unbuildable recipe has nothing for safety to check).

The four :class:`FinalDisposition` outcomes are a pure function of the three stages:

* ``OUT_OF_SCOPE`` — applicability placed it out of scope; grounding + safety ``NOT_EVALUATED``.
* ``SAFETY_REJECTED`` — it bound (grounding ``COMPLETED``) but the gauntlet refused it (safety ``FAILED``).
* ``ELIGIBLE`` — it bound and the gauntlet passed it (grounding + safety ``COMPLETED``).
* ``UNBUILDABLE`` — it was in scope and grounding ran, but nothing bound (safety ``NOT_EVALUATED``).

``relevance_tier`` (``"primary"``/``"supporting"``/``None``) mirrors the applicability relationship —
it is NOT a disposition. Ranking / presentation-priority is a Phase-2 *attribute* layered on top of
this model, deliberately absent here. See
``docs/superpowers/plans/2026-07-10-phase1b-scoped-grounding.md`` Task 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.taxonomy.applicability import ApplicabilityResult

# Reason codes for stages that did not (or could not) run. An out-of-scope recipe short-circuits both
# downstream stages; an in-scope recipe that never bound a candidate leaves safety with nothing to check.
_PRIOR_STAGE_OUT_OF_SCOPE: tuple[str, ...] = ("prior_stage_out_of_scope",)
_NO_BINDING: tuple[str, ...] = ("no_binding",)
# A stage that ran cleanly with nothing more to explain carries no reason codes.
_NO_REASON: tuple[str, ...] = ()


class StageStatus(StrEnum):
    """The outcome of a single evaluation stage. ``COMPLETED`` = the stage ran and produced a verdict;
    ``FAILED`` = the stage ran and rejected the recipe; ``NOT_EVALUATED`` = the stage never ran (a
    prior stage short-circuited it, or there was nothing to evaluate). ``NOT_EVALUATED`` is an explicit
    status — the disposition lens never carries a bare null for a skipped stage."""

    COMPLETED = "completed"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"


class FinalDisposition(StrEnum):
    """The single rolled-up outcome for a recipe, derived from its three stages. These are the buckets
    the Gate-#1 disposition lens groups by: *outside confirmed scope*, *relevant but missing data*,
    *rejected by safety*, *recommended/eligible*."""

    OUT_OF_SCOPE = "out_of_scope"
    UNBUILDABLE = "unbuildable"
    SAFETY_REJECTED = "safety_rejected"
    ELIGIBLE = "eligible"


@dataclass(frozen=True, slots=True)
class StageEvaluation:
    """One recipe's outcome at one stage, stamped for replay. ``reason_codes`` explains the status (an
    empty tuple when a clean ``COMPLETED`` has nothing to add); ``evaluation_version`` is the mapping
    version the stage ran under and ``evaluated_at`` the server clock at evaluation time."""

    status: StageStatus
    reason_codes: tuple[str, ...]
    evaluation_version: str
    evaluated_at: object


@dataclass(frozen=True, slots=True)
class RecipeEvaluation:
    """The full per-recipe disposition: the three staged evaluations, the rolled-up
    ``final_disposition``, and the ``relevance_tier`` (``"primary"``/``"supporting"``/``None`` when out
    of scope) carried through from applicability. ``relevance_tier`` is an applicability attribute, not
    a disposition — Phase-2 ranking layers on top of it."""

    recipe_id: str
    applicability: StageEvaluation
    grounding: StageEvaluation
    safety: StageEvaluation
    final_disposition: FinalDisposition
    relevance_tier: str | None


def evaluate_dispositions(
    result: ApplicabilityResult,
    grounded_ids: frozenset[str],
    rejected: dict[str, tuple[str, ...]],
    *,
    evaluation_version: str,
    now: object,
) -> list[RecipeEvaluation]:
    """Fold the shared applicability decision + grounding/safety outcomes into one
    :class:`RecipeEvaluation` per recipe in ``result.by_recipe``.

    Consumes the SAME :class:`ApplicabilityResult` grounding ran on — applicability is never recomputed
    here. ``grounded_ids`` are the recipe (template) ids that bound a surviving candidate; ``rejected``
    maps a recipe id to the safety/gauntlet reason codes for recipes that bound but the gauntlet
    refused. Every stage on every recipe is stamped with ``evaluation_version`` + ``evaluated_at=now``.

    Per recipe, the applicability stage is always ``COMPLETED`` (its reason lifted from
    ``result.reason_codes``); the remaining stages and the final disposition follow from the
    applicability relationship:

    * ``out_of_scope`` -> grounding + safety ``NOT_EVALUATED`` -> ``OUT_OF_SCOPE`` (``relevance_tier`` is None).
    * ``primary``/``supporting`` (``relevance_tier`` = the relationship):
        * in ``rejected`` -> grounding ``COMPLETED``, safety ``FAILED`` -> ``SAFETY_REJECTED``.
        * else in ``grounded_ids`` -> grounding + safety ``COMPLETED`` -> ``ELIGIBLE``.
        * else -> grounding ``COMPLETED`` (ran, nothing bound), safety ``NOT_EVALUATED`` -> ``UNBUILDABLE``.
    """
    evaluations: list[RecipeEvaluation] = []
    for recipe_id, decision in result.by_recipe.items():
        applicability = StageEvaluation(
            status=StageStatus.COMPLETED,
            reason_codes=result.reason_codes[recipe_id],
            evaluation_version=evaluation_version,
            evaluated_at=now,
        )

        if decision == "out_of_scope":
            # Applicability placed it out of scope: the downstream stages never run, but we stamp them
            # NOT_EVALUATED with a reason so the lens is never left holding a bare null.
            grounding = StageEvaluation(
                StageStatus.NOT_EVALUATED, _PRIOR_STAGE_OUT_OF_SCOPE, evaluation_version, now)
            safety = StageEvaluation(
                StageStatus.NOT_EVALUATED, _PRIOR_STAGE_OUT_OF_SCOPE, evaluation_version, now)
            evaluations.append(RecipeEvaluation(
                recipe_id=recipe_id,
                applicability=applicability,
                grounding=grounding,
                safety=safety,
                final_disposition=FinalDisposition.OUT_OF_SCOPE,
                relevance_tier=None,
            ))
            continue

        # In scope: the relevance tier IS the applicability relationship (primary/supporting).
        relevance_tier = decision
        if recipe_id in rejected:
            # It bound a candidate (grounding completed) but the safety gauntlet refused it.
            grounding = StageEvaluation(
                StageStatus.COMPLETED, _NO_REASON, evaluation_version, now)
            safety = StageEvaluation(
                StageStatus.FAILED, rejected[recipe_id], evaluation_version, now)
            final = FinalDisposition.SAFETY_REJECTED
        elif recipe_id in grounded_ids:
            # It bound a candidate and the gauntlet passed it.
            grounding = StageEvaluation(
                StageStatus.COMPLETED, _NO_REASON, evaluation_version, now)
            safety = StageEvaluation(
                StageStatus.COMPLETED, _NO_REASON, evaluation_version, now)
            final = FinalDisposition.ELIGIBLE
        else:
            # Grounding ran but nothing bound (no candidate) -> unbuildable; safety has nothing to check.
            grounding = StageEvaluation(
                StageStatus.COMPLETED, _NO_BINDING, evaluation_version, now)
            safety = StageEvaluation(
                StageStatus.NOT_EVALUATED, _NO_BINDING, evaluation_version, now)
            final = FinalDisposition.UNBUILDABLE

        evaluations.append(RecipeEvaluation(
            recipe_id=recipe_id,
            applicability=applicability,
            grounding=grounding,
            safety=safety,
            final_disposition=final,
            relevance_tier=relevance_tier,
        ))

    return evaluations
