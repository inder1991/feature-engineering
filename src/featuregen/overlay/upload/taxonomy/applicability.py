"""Phase-1A Task 3 — the applicability evaluator (recognised scope -> in-scope recipe ids).

This turns a *confirmed* use-case scope into the concrete recipe-id sets Phase-1B will hand to
grounding. It is built now, in shadow, so the eval harness (Task 5) can measure **applicability
recall / false-narrowing** against the gold set before any filtering goes live — nothing here changes
what grounds today.

Two value objects and two functions:

* :class:`ScopeExpansion` — ``EXACT`` (the confirmed ids only) vs ``INCLUDE_DESCENDANTS`` (a confirmed
  parent also pulls in every recipe whose objective sits *below* it). The recognizer only ever proposes
  selectable leaves, so it emits ``EXACT``; broadening a confirmed leaf to its parent's descendants is a
  later, deliberate manual action, never something the recognizer does on its own.
* :class:`ConfirmedScope` — the confirmed ``primary`` leaf, the confirmed ``secondary`` leaves, the
  expansion mode, and the ``unscoped`` fail-open flag.
* :func:`scope_from_recognition` — folds a :class:`RecognitionResult` into a :class:`ConfirmedScope`
  (``UNSCOPED``/``TECHNICAL_FAILURE`` -> ``unscoped=True``; otherwise read the candidates).
* :func:`in_scope_recipes` — maps a scope to ``(primary_scoped, supporting_scoped)`` recipe-id sets
  over :data:`ALL_TEMPLATES`, using each recipe's derived :class:`ApplicabilitySpec`.

Fail-open by construction: an ``unscoped`` scope grounds **every** recipe (full grounding continues),
and the supporting (secondary-match) set is **never capped** — a recipe that also serves a confirmed
objective as a secondary is always retained. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 3.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.taxonomy.recipe_applicability import recipe_applicability
from featuregen.overlay.upload.taxonomy.recognition import RecognitionResult, RecognitionStatus
from featuregen.overlay.upload.taxonomy.use_cases import descendants
from featuregen.overlay.upload.templates import ALL_TEMPLATES

# Statuses that mean "no confirmed scope" — the recogniser found nothing to narrow on, or a technical
# failure occurred. Both fail open to full grounding (unscoped=True).
_UNSCOPED_STATUSES: frozenset[RecognitionStatus] = frozenset(
    {RecognitionStatus.UNSCOPED, RecognitionStatus.TECHNICAL_FAILURE})


class ScopeExpansion(StrEnum):
    """How broadly a confirmed scope reaches. ``EXACT`` matches only recipes whose applicability
    primary IS a confirmed id; ``INCLUDE_DESCENDANTS`` additionally matches recipes whose primary is a
    *descendant* of a confirmed id (a confirmed bare parent pulls in its whole subtree)."""

    EXACT = "exact"
    INCLUDE_DESCENDANTS = "include_descendants"


@dataclass(frozen=True, slots=True)
class ConfirmedScope:
    """A confirmed use-case scope: the ``primary`` objective, the ``secondary`` objectives, the
    ``expansion`` mode, and the ``unscoped`` fail-open flag. When ``unscoped`` is set, ``primary`` is
    ``None`` and :func:`in_scope_recipes` grounds every recipe.

    Phase-2B adds the two human-confirmed intent DIMENSIONS the recognizer proposes and the human
    confirms at Gate #1 — ``modelling_contexts`` (0+ confirmed regulatory framework/regime ids) and a
    single soft ``target_entity`` (the confirmed prediction grain). Both default empty so every
    Phase-1 caller and a dimension-free scope are unchanged; the dimensions never affect
    :func:`in_scope_recipes` (applicability narrows only on the use-case tree)."""

    primary: str | None
    secondary: tuple[str, ...] = ()
    expansion: ScopeExpansion = ScopeExpansion.EXACT
    unscoped: bool = False
    modelling_contexts: tuple[str, ...] = ()
    target_entity: str | None = None


def scope_from_recognition(result: RecognitionResult) -> ConfirmedScope:
    """Fold a :class:`RecognitionResult` into a :class:`ConfirmedScope`.

    ``UNSCOPED``/``TECHNICAL_FAILURE`` -> ``ConfirmedScope(primary=None, unscoped=True)`` (fail open to
    full grounding). Otherwise the ``primary`` is the ``use_case_id`` of the primary candidate (or
    ``None`` if none is present) and ``secondary`` are the secondary candidates' ids, in order. The
    expansion is always ``EXACT``: the recognizer proposes selectable leaves, and broadening to a
    parent's descendants is a later manual action, not a recognizer output."""
    if result.status in _UNSCOPED_STATUSES:
        return ConfirmedScope(primary=None, unscoped=True)

    primary = next(
        (c.use_case_id for c in result.candidates if c.relationship == "primary"), None)
    if primary is None:
        # No confident primary objective (e.g. an AMBIGUOUS result carrying only alternatives). Do NOT
        # narrow on it — relevance-uncertain fails OPEN to full grounding (the plan's fail-open asymmetry),
        # never to a primary-less scope that would ground nothing.
        return ConfirmedScope(primary=None, unscoped=True)
    secondary = tuple(c.use_case_id for c in result.candidates if c.relationship == "secondary")
    return ConfirmedScope(primary=primary, secondary=secondary, expansion=ScopeExpansion.EXACT)


def in_scope_recipes(scope: ConfirmedScope) -> tuple[set[str], set[str]]:
    """Map a confirmed scope to ``(primary_scoped, supporting_scoped)`` recipe-id sets.

    * ``scope.unscoped`` -> ``({every recipe id}, set())`` — fail open: everything grounds.
    * Otherwise, with ``confirmed = {primary} | set(secondary)`` (dropping ``None``), each recipe's
      derived :class:`ApplicabilitySpec` places it as follows:

      - **primary_scoped** when its ``primary`` IS a confirmed id, OR — only under
        ``INCLUDE_DESCENDANTS`` — its ``primary`` is a *descendant* of a confirmed id. A recipe whose
        primary is a descendant of a confirmed id is NOT auto-included under ``EXACT`` (a bare confirmed
        parent matches nothing directly).
      - **supporting_scoped** (never primary) when its ``primary`` is NOT confirmed but one of its
        ``secondary`` objectives IS confirmed. Supporting is **never capped** — every such recipe is
        retained (an exact secondary match; descendant expansion does not apply to secondaries).
    """
    all_ids = {t.id for t in ALL_TEMPLATES}
    if scope.unscoped:
        return all_ids, set()

    confirmed: set[str] = {uid for uid in (scope.primary, *scope.secondary) if uid is not None}
    if not confirmed:                    # defense: an empty scope fails OPEN — never scopes to nothing
        return all_ids, set()
    expand = scope.expansion is ScopeExpansion.INCLUDE_DESCENDANTS
    descendant_ids: set[str] = set()
    if expand:
        for uid in confirmed:
            descendant_ids.update(descendants(uid))

    primary_scoped: set[str] = set()
    supporting_scoped: set[str] = set()
    for template in ALL_TEMPLATES:
        spec = recipe_applicability(template)
        if spec.primary in confirmed or (expand and spec.primary in descendant_ids):
            primary_scoped.add(template.id)
        elif any(sec in confirmed for sec in spec.secondary):
            supporting_scoped.add(template.id)

    return primary_scoped, supporting_scoped


# Reason codes stamped per recipe on the single applicability decision. One tuple per relationship so a
# downstream disposition lens (Phase-1B Task 5) can explain *why* a recipe was placed where it was.
_PRIMARY_REASON: tuple[str, ...] = ("primary_match",)
_SUPPORTING_REASON: tuple[str, ...] = ("secondary_match",)
_OUT_OF_SCOPE_REASON: tuple[str, ...] = ("no_confirmed_use_case_match",)


@dataclass(frozen=True, slots=True)
class ApplicabilityResult:
    """The one-and-only applicability decision for a confirmed scope (Phase-1B Task 3).

    Computed *once* per generation run and consumed by both grounding and the disposition lens, so the
    library is never rescanned to re-ask "is this applicable?" (the scale story as it grows past 153).

    * ``by_recipe`` — EVERY recipe id in :data:`ALL_TEMPLATES` mapped to exactly one relationship:
      ``"primary"``, ``"supporting"``, or ``"out_of_scope"``. The exactly-one invariant is enforced at
      construction by :func:`applicability_result`.
    * ``eligible_ids`` — the ``primary`` ∪ ``supporting`` ids (the non-``out_of_scope`` recipes that
      grounding evaluates).
    * ``reason_codes`` — a reason tuple per recipe explaining its placement.
    """

    by_recipe: dict[str, str]
    eligible_ids: frozenset[str]
    reason_codes: dict[str, tuple[str, ...]]


def applicability_result(scope: ConfirmedScope) -> ApplicabilityResult:
    """Compute the single :class:`ApplicabilityResult` for a confirmed scope.

    Calls :func:`in_scope_recipes` **exactly once** to get ``(primary_scoped, supporting_scoped)`` and
    classifies every recipe in :data:`ALL_TEMPLATES` into exactly one relationship: in ``primary_scoped``
    -> ``"primary"``; else in ``supporting_scoped`` -> ``"supporting"``; else ``"out_of_scope"``. An
    ``unscoped`` scope therefore classifies every recipe as ``"primary"`` (fail open).

    Enforces the exactly-one invariant as an internal contract (not user input): the classified id-set
    must equal the full registry and the primary/supporting sets must be disjoint. A violation means
    ``in_scope_recipes`` or the registry drifted and is raised, not swallowed.
    """
    primary_scoped, supporting_scoped = in_scope_recipes(scope)

    overlap = primary_scoped & supporting_scoped
    if overlap:
        raise ValueError(
            f"applicability invariant violated: recipes classified as BOTH primary and supporting: "
            f"{sorted(overlap)}")

    by_recipe: dict[str, str] = {}
    reason_codes: dict[str, tuple[str, ...]] = {}
    for template in ALL_TEMPLATES:
        rid = template.id
        if rid in primary_scoped:
            by_recipe[rid] = "primary"
            reason_codes[rid] = _PRIMARY_REASON
        elif rid in supporting_scoped:
            by_recipe[rid] = "supporting"
            reason_codes[rid] = _SUPPORTING_REASON
        else:
            by_recipe[rid] = "out_of_scope"
            reason_codes[rid] = _OUT_OF_SCOPE_REASON

    all_ids = {t.id for t in ALL_TEMPLATES}
    if set(by_recipe) != all_ids:
        raise AssertionError(
            "applicability invariant violated: by_recipe must classify every recipe exactly once")

    return ApplicabilityResult(
        by_recipe=by_recipe,
        eligible_ids=frozenset(primary_scoped | supporting_scoped),
        reason_codes=reason_codes,
    )
