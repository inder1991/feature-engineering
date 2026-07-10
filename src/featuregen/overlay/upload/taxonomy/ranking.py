"""Phase-2A Task A2 — the deterministic presentation-priority RANKER.

Ranking is a **presentation-priority attribute**, never a disposition and never a predictive-utility
score. This module consumes a **precomputed set of rankable recipe ids** (``rankable_recipe_ids``) plus
a typed :class:`RankSignals` per id and produces, per recipe, TWO deliberately-separate projections:

* ``canonical_rank`` — the 1-based position in a total, tiered ordering of the *whole* rankable set.
* ``selected_for_initial_view`` — whether the recipe is in the first-screen slice, decided by a
  SECOND pass (diversity + capacity) that **never** rewrites ``canonical_rank``.

The ranker is deliberately **disposition-agnostic**: it never reads ``FinalDisposition``. Whoever calls
it hands over the already-decided rankable set (today the Phase-1B ``ELIGIBLE`` ids; after the policy
initiative, the post-policy eligible ids) — so the ranker survives that future change untouched.

Canonical order (each axis descending; the next axis breaks a tie; ``recipe_id`` ascending is the final,
total tie-break):

1. ``relevance_tier`` — ``primary`` > ``supporting``.
2. ``modelling_context_fit`` — ``REQUIRED_MATCH`` > ``COMPATIBLE`` > ``NEUTRAL`` > ``CONFLICT``.
3. ``binding_quality`` — ``EXACT`` > ``STRONG`` > ``ACCEPTABLE`` > ``AMBIGUOUS``.
4. ``pit_completeness`` — ``COMPLETE`` > ``NOT_APPLICABLE`` > ``PARTIAL`` > ``UNKNOWN``.
5. ``explainability`` — ``H`` > ``M`` > ``L``.
6. ``recipe_id`` ascending.

Binding quality sits ABOVE explainability on purpose: a structurally cleaner bind is preferred over a
more-explainable but weaker one. Every signal in :class:`RankSignals` is consumed — the five axes above,
plus ``family``/``semantic_group``/``journey_*`` in the initial-view diversity pass and
``entity_compatibility`` in the reason stream.

Initial-view projection — a SEPARATE walk over the canonically-ordered list. First the
**binding-acceptability gate**: a recipe whose ``binding_quality`` is ``AMBIGUOUS`` is NEVER selected
(no modelling-context match can promote a structurally weak bind). Then a deterministic **relaxation**
fills up to ``initial_view_size``:

* Pass 1 (strict): one representative per ``semantic_group``; at most ``per_family_cap`` per ``family``;
  prefer covering distinct ``journey_stage_id`` values *within the same* ``journey_model_id``.
* Pass 2: relax the stage-diversity preference.
* Pass 3: relax the family cap **incrementally** — one extra per family per round — with the group-dedup
  still enforced.
* Pass 4 (last resort): relax the one-per-``semantic_group`` rule ONLY if the eligible set cannot fill
  the size any other way.

Fewer than ``initial_view_size`` are returned only when fewer eligible recipes exist. The two reason
streams are stamped independently: ``rank_reasons`` (the ordering factors, positive AND notable-negative)
and ``initial_view_reasons`` (selected, or *why not*).

Determinism: the output is a pure function of the *set* of ranked ids and their signals — independent of
the iteration order of ``rankable_recipe_ids`` or of the ``signals`` mapping. ``ranking_version`` is a
provenance token the caller pins; it is deliberately NOT an ordering input, so a version bump never
mutates a prior projection.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.taxonomy.ranking_signals import (
    BindingQuality,
    EntityCompatibility,
    ModellingContextFit,
    PITCompleteness,
)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Structured reason codes — two disjoint vocabularies, one per projection
# ──────────────────────────────────────────────────────────────────────────────────────────────────
class RankReasonCode(StrEnum):
    """Why a recipe landed where it did in the CANONICAL order — positive factors that lifted it and
    notable-negative factors that held it down. A separate vocabulary from :class:`InitialViewReasonCode`
    so the two projections never share codes."""

    # positive
    PRIMARY_USE_CASE_MATCH = "primary_use_case_match"
    SUPPORTING_MATCH = "supporting_match"
    REQUIRED_CONTEXT_MATCH = "required_context_match"
    EXACT_BINDING = "exact_binding"
    PIT_COMPLETE = "pit_complete"
    HIGH_EXPLAINABILITY = "high_explainability"
    # notable-negative
    LOW_BINDING_QUALITY = "low_binding_quality"
    PIT_METADATA_INCOMPLETE = "pit_metadata_incomplete"
    ENTITY_GRAIN_UNKNOWN = "entity_grain_unknown"


class InitialViewReasonCode(StrEnum):
    """Why a recipe is — or is NOT — in the initial view. ``SELECTED_INITIAL_VIEW`` marks a selection;
    the ``*_NOT_IN_INITIAL_VIEW`` codes each name the constraint that kept a recipe out; ``STAGE_DIVERSITY``
    marks a recipe the stage-diversity preference deferred (its journey stage was already covered)."""

    SELECTED_INITIAL_VIEW = "selected_initial_view"
    DUPLICATE_VARIANT_NOT_IN_INITIAL_VIEW = "duplicate_variant_not_in_initial_view"
    FAMILY_CAP_NOT_IN_INITIAL_VIEW = "family_cap_not_in_initial_view"
    AMBIGUOUS_BINDING_NOT_IN_INITIAL_VIEW = "ambiguous_binding_not_in_initial_view"
    STAGE_DIVERSITY = "stage_diversity"


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The typed inputs + outputs
# ──────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RankSignals:
    """The typed signal bundle the ranker orders a single recipe on. ``relevance_tier`` is
    ``"primary"``/``"supporting"`` and ``explainability`` is ``"H"``/``"M"``/``"L"`` (the recipe's
    design-time labels); the four enums are the Task-A1 derivations. ``family`` + ``semantic_group`` +
    ``journey_model_id``/``journey_stage_id`` drive the initial-view diversity pass. Every field is
    consumed by :func:`rank_eligible`."""

    relevance_tier: str
    binding_quality: BindingQuality
    modelling_context_fit: ModellingContextFit
    pit_completeness: PITCompleteness
    explainability: str
    family: str
    journey_model_id: str | None
    journey_stage_id: str | None
    semantic_group: str
    entity_compatibility: EntityCompatibility


@dataclass(frozen=True, slots=True)
class RankedRecipe:
    """One recipe's two projections. ``canonical_rank`` (1-based) is the total-order position and is
    NEVER changed by the initial-view pass; ``selected_for_initial_view`` is the first-screen decision.
    The two reason tuples are stamped independently from the two disjoint code vocabularies."""

    recipe_id: str
    canonical_rank: int
    selected_for_initial_view: bool
    rank_reasons: tuple[RankReasonCode, ...]
    initial_view_reasons: tuple[InitialViewReasonCode, ...]


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Canonical ordering — total, deterministic, tiered
# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Each map is best -> 0 (ascending sort = best first). An unrecognised label sorts LAST (``_WORST``),
# deterministically, rather than raising — the ordering stays total for any input.
_WORST = 1_000

_RELEVANCE_ORDER: dict[str, int] = {"primary": 0, "supporting": 1}
_CONTEXT_ORDER: dict[ModellingContextFit, int] = {
    ModellingContextFit.REQUIRED_MATCH: 0,
    ModellingContextFit.COMPATIBLE: 1,
    ModellingContextFit.NEUTRAL: 2,
    ModellingContextFit.CONFLICT: 3,
}
_BINDING_ORDER: dict[BindingQuality, int] = {
    BindingQuality.EXACT: 0,
    BindingQuality.STRONG: 1,
    BindingQuality.ACCEPTABLE: 2,
    BindingQuality.AMBIGUOUS: 3,
}
_PIT_ORDER: dict[PITCompleteness, int] = {
    PITCompleteness.COMPLETE: 0,
    PITCompleteness.NOT_APPLICABLE: 1,
    PITCompleteness.PARTIAL: 2,
    PITCompleteness.UNKNOWN: 3,
}
_EXPLAIN_ORDER: dict[str, int] = {"H": 0, "M": 1, "L": 2}


def _admit_any(_s: RankSignals) -> bool:
    """The last-resort admit predicate: accept any candidate (used only when the eligible set cannot
    otherwise fill the view, e.g. every recipe is a variant of one semantic group)."""
    return True


def _canonical_key(recipe_id: str, s: RankSignals) -> tuple[int, int, int, int, int, str]:
    """The total sort key: the five tiered axes (best -> 0) then ``recipe_id`` ascending as the final,
    unique tie-break — so the order is stable regardless of input iteration order."""
    return (
        _RELEVANCE_ORDER.get(s.relevance_tier, _WORST),
        _CONTEXT_ORDER.get(s.modelling_context_fit, _WORST),
        _BINDING_ORDER.get(s.binding_quality, _WORST),
        _PIT_ORDER.get(s.pit_completeness, _WORST),
        _EXPLAIN_ORDER.get(s.explainability, _WORST),
        recipe_id,
    )


def _rank_reasons(s: RankSignals) -> tuple[RankReasonCode, ...]:
    """The ordering factors, positive AND notable-negative, in canonical-axis order (so the tuple is
    itself deterministic). Every signal is reflected: tier, context, binding, pit, explainability and
    the soft entity-grain nudge."""
    reasons: list[RankReasonCode] = []
    if s.relevance_tier == "primary":
        reasons.append(RankReasonCode.PRIMARY_USE_CASE_MATCH)
    elif s.relevance_tier == "supporting":
        reasons.append(RankReasonCode.SUPPORTING_MATCH)

    if s.modelling_context_fit is ModellingContextFit.REQUIRED_MATCH:
        reasons.append(RankReasonCode.REQUIRED_CONTEXT_MATCH)

    if s.binding_quality is BindingQuality.EXACT:
        reasons.append(RankReasonCode.EXACT_BINDING)
    elif s.binding_quality in (BindingQuality.ACCEPTABLE, BindingQuality.AMBIGUOUS):
        reasons.append(RankReasonCode.LOW_BINDING_QUALITY)

    if s.pit_completeness is PITCompleteness.COMPLETE:
        reasons.append(RankReasonCode.PIT_COMPLETE)
    elif s.pit_completeness in (PITCompleteness.PARTIAL, PITCompleteness.UNKNOWN):
        reasons.append(RankReasonCode.PIT_METADATA_INCOMPLETE)

    if s.explainability == "H":
        reasons.append(RankReasonCode.HIGH_EXPLAINABILITY)

    if s.entity_compatibility is EntityCompatibility.UNKNOWN:
        reasons.append(RankReasonCode.ENTITY_GRAIN_UNKNOWN)

    return tuple(reasons)


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Initial-view projection — a separate pass; never mutates canonical_rank
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def _select_initial_view(
    order: list[str],
    signals: Mapping[str, RankSignals],
    initial_view_size: int,
    per_family_cap: int,
) -> tuple[set[str], dict[str, tuple[InitialViewReasonCode, ...]]]:
    """Decide the initial-view slice over the already-canonically-ordered ``order``.

    Returns the selected id set + the per-id ``initial_view_reasons`` (selected, or the why-not). The
    ``canonical_rank`` is untouched — this pass only reads the canonical order, it never re-sorts it.
    """
    selected: list[str] = []
    selected_set: set[str] = set()
    group_rep: set[str] = set()
    family_count: dict[str, int] = defaultdict(int)
    stage_covered: dict[str, set[str]] = defaultdict(set)

    # The binding-acceptability gate: an AMBIGUOUS bind is excluded from EVERY selection pass, so no
    # relaxation and no context match can ever promote it into the view.
    candidates = [rid for rid in order
                  if signals[rid].binding_quality is not BindingQuality.AMBIGUOUS]

    def _select(rid: str) -> None:
        s = signals[rid]
        selected.append(rid)
        selected_set.add(rid)
        group_rep.add(s.semantic_group)
        family_count[s.family] += 1
        if s.journey_model_id is not None and s.journey_stage_id is not None:
            stage_covered[s.journey_model_id].add(s.journey_stage_id)

    def _walk(admit: Callable[[RankSignals], bool]) -> None:
        """Walk candidates in canonical order, selecting those ``admit`` accepts, until the view fills."""
        for rid in candidates:
            if len(selected) >= initial_view_size:
                return
            if rid in selected_set:
                continue
            if admit(signals[rid]):
                _select(rid)

    def _stage_covered(s: RankSignals) -> bool:
        return (s.journey_model_id is not None and s.journey_stage_id is not None
                and s.journey_stage_id in stage_covered[s.journey_model_id])

    def _admit_under_cap(cap: int) -> Callable[[RankSignals], bool]:
        """An admit predicate that enforces the group dedup + a family cap of ``cap`` (the live
        ``group_rep``/``family_count`` are captured by reference, so in-pass selections are seen)."""
        def admit(s: RankSignals) -> bool:
            return s.semantic_group not in group_rep and family_count[s.family] < cap
        return admit

    # Pass 1 — strict: one per semantic group, family cap, prefer distinct journey stages within a model.
    _walk(lambda s: (s.semantic_group not in group_rep
                     and family_count[s.family] < per_family_cap
                     and not _stage_covered(s)))

    # Pass 2 — relax the stage-diversity preference (group dedup + family cap still enforced).
    if len(selected) < initial_view_size:
        _walk(_admit_under_cap(per_family_cap))

    # Pass 3 — relax the family cap INCREMENTALLY, one extra per family per round (group dedup still on).
    # Entering here every family holds <= per_family_cap, so round k (cap = per_family_cap + k) admits at
    # most one more per family. Stop once the view fills or only group-duplicates remain; the cap bound
    # guarantees termination.
    cap = per_family_cap
    while len(selected) < initial_view_size and cap <= len(candidates):
        if not any(rid not in selected_set and signals[rid].semantic_group not in group_rep
                   for rid in candidates):
            break  # only semantic-group duplicates remain — the family cap can no longer help
        cap += 1
        _walk(_admit_under_cap(cap))

    # Pass 4 — last resort: relax the one-per-semantic-group rule ONLY because the eligible set cannot
    # otherwise fill the size (e.g. every eligible recipe is a variant of one template).
    if len(selected) < initial_view_size:
        _walk(_admit_any)

    reasons: dict[str, tuple[InitialViewReasonCode, ...]] = {}
    for rid in order:
        if rid in selected_set:
            reasons[rid] = (InitialViewReasonCode.SELECTED_INITIAL_VIEW,)
        else:
            reasons[rid] = _not_selected_reason(
                signals[rid], group_rep, family_count, stage_covered, per_family_cap)
    return selected_set, reasons


def _not_selected_reason(
    s: RankSignals,
    group_rep: set[str],
    family_count: dict[str, int],
    stage_covered: dict[str, set[str]],
    per_family_cap: int,
) -> tuple[InitialViewReasonCode, ...]:
    """Derive the why-not, re-read against the FINAL selection state, most-fundamental constraint first:
    the gate (ambiguous) > semantic-group dedup > family cap > stage diversity. A recipe blocked by none
    of these is simply below the initial-view cut-off by canonical rank and carries no code."""
    if s.binding_quality is BindingQuality.AMBIGUOUS:
        return (InitialViewReasonCode.AMBIGUOUS_BINDING_NOT_IN_INITIAL_VIEW,)
    if s.semantic_group in group_rep:
        return (InitialViewReasonCode.DUPLICATE_VARIANT_NOT_IN_INITIAL_VIEW,)
    if family_count.get(s.family, 0) >= per_family_cap:
        return (InitialViewReasonCode.FAMILY_CAP_NOT_IN_INITIAL_VIEW,)
    if (s.journey_model_id is not None and s.journey_stage_id is not None
            and s.journey_stage_id in stage_covered.get(s.journey_model_id, set())):
        return (InitialViewReasonCode.STAGE_DIVERSITY,)
    return ()  # ranked below the initial-view cut-off purely by canonical position


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def rank_eligible(
    rankable_recipe_ids: Sequence[str],
    signals: Mapping[str, RankSignals],
    *,
    ranking_version: str,
    initial_view_size: int = 15,
    per_family_cap: int = 3,
) -> list[RankedRecipe]:
    """Rank the precomputed ``rankable_recipe_ids`` into a canonical order + an initial-view slice.

    Only ids present in ``rankable_recipe_ids`` are ranked; duplicates are ranked once and an id with no
    entry in ``signals`` is deterministically skipped (it cannot be ordered without a signal bundle). The
    result is sorted by :func:`_canonical_key`, so it is a pure function of the *set* of ranked ids +
    their signals — identical regardless of the iteration order of either argument.

    ``ranking_version`` is a provenance token the caller pins before ranking; it is deliberately NOT an
    ordering input, so a version bump never reorders or mutates a prior projection.
    """
    # Dedup while preserving nothing about input order (we sort next). Skip ids lacking a signal bundle.
    to_rank: list[str] = []
    seen: set[str] = set()
    for rid in rankable_recipe_ids:
        if rid in seen or rid not in signals:
            continue
        seen.add(rid)
        to_rank.append(rid)

    order = sorted(to_rank, key=lambda rid: _canonical_key(rid, signals[rid]))
    selected_set, initial_view_reasons = _select_initial_view(
        order, signals, initial_view_size, per_family_cap)

    return [
        RankedRecipe(
            recipe_id=rid,
            canonical_rank=rank,
            selected_for_initial_view=rid in selected_set,
            rank_reasons=_rank_reasons(signals[rid]),
            initial_view_reasons=initial_view_reasons[rid],
        )
        for rank, rid in enumerate(order, start=1)
    ]
