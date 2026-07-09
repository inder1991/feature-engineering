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
    ``None`` and :func:`in_scope_recipes` grounds every recipe."""

    primary: str | None
    secondary: tuple[str, ...] = ()
    expansion: ScopeExpansion = ScopeExpansion.EXACT
    unscoped: bool = False


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
