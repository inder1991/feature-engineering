"""Phase-2A Task A1 — the typed ranking SIGNALS and their total derivations.

Ranking (Task A2) never consumes bare booleans or free-form labels. Every axis it orders on is one of
four **typed enums with a defined, total derivation**, computed here from a recipe's design-time
metadata (:class:`~featuregen.overlay.upload.templates.Template`) or its grounded candidate
(:class:`~featuregen.overlay.upload.templates.GroundedFeature`). "Total" = every recipe in
``ALL_TEMPLATES`` (and every grounded feature) yields a valid enum member; the derivations never raise
and never return ``None``.

Four signals + a grouping key:

* :class:`BindingQuality` — how cleanly the grounded feature bound (a *grounding-side* signal read off
  ``GroundedFeature.notes``). Deterministic grounding resolves single-candidate binds, so ``AMBIGUOUS``
  is RESERVED — grounding rejects ambiguous binds before they ever reach the rankable set, so it will
  rarely (in practice never) appear; the member exists so the ranker's binding-acceptability gate has a
  value to gate on if grounding is ever relaxed.
* :class:`PITCompleteness` — whether the recipe's point-in-time rule is a real declaration. Every recipe
  in the authored library bakes in a trailing-window / as-of PIT rule, so all resolve ``COMPLETE`` today;
  ``NOT_APPLICABLE`` (a non-time-dependent recipe), ``PARTIAL`` and ``UNKNOWN`` are reachable states a
  future or mis-authored recipe can land in.
* :class:`ModellingContextFit` — the fit of a recipe to the human-confirmed modelling context(s). Task B3
  derives it from the recipe's OWN modelling contexts (the ``modelling_context``-dimension targets of its
  ``use_cases`` tags, via the legacy crosswalk) vs the confirmed set: an overlap is ``REQUIRED_MATCH``, a
  context-free (generic) recipe is ``COMPATIBLE``, a recipe declaring only disjoint contexts is
  ``CONFLICT`` (a warning, NEVER a hard reject in Phase 2), and no confirmed context is ``NEUTRAL``.
* :class:`EntityCompatibility` — the SOFT grain fit of a recipe to the confirmed ``target_entity``. Task B3
  derives the recipe's grain (the ``entity_link`` of its entity-role need) and compares it to the target:
  equal is ``EXACT``, a declared roll-up (child grain -> coarser parent) is ``DERIVABLE``, and anything
  else (incl. no target) is ``UNKNOWN``. There is deliberately **no** ``INCOMPATIBLE`` — a hard entity
  reject is deferred to Phase 3; ``target_entity`` is only ever a soft grain nudge + a grain warning, and
  is NEVER used to reject a recipe anywhere.

``semantic_group`` is the near-duplicate key: the source template id, which every grounded variant of a
template carries. Behaviour-neutral, read-only — nothing here touches grounding or the considered-set.
"""
from __future__ import annotations

from enum import StrEnum

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import crosswalk
from featuregen.overlay.upload.templates import GroundedFeature, Template


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# BindingQuality — a grounding-side signal derived from GroundedFeature.notes
# ──────────────────────────────────────────────────────────────────────────────────────────────────
class BindingQuality(StrEnum):
    """How cleanly a grounded feature bound its needs. ``EXACT`` = every role bound its own concept with
    no substitution or degrade; ``STRONG`` = a concept-substitution / inherited-concept bind (a close
    registry concept stood in for a role); ``ACCEPTABLE`` = an optional need was unmet / a degrade path
    was taken (optional metadata incomplete); ``AMBIGUOUS`` = a weak / multi-candidate resolution —
    RESERVED (grounding resolves deterministically and rejects ambiguity, so this rarely appears)."""

    EXACT = "exact"
    STRONG = "strong"
    ACCEPTABLE = "acceptable"
    AMBIGUOUS = "ambiguous"


# Markers the grounding engine (templates.py) authors into ``GroundedFeature.notes``:
#   • a concept substitution reads e.g. "concept sub: entity uses 'customer_id' ..." (STRONG),
#   • an unmet optional need reads "optional need '<role>' (<concept>) unmet -> <degrade>" (ACCEPTABLE),
#   • an ambiguous bind (should never happen — grounding refuses it) would read "ambiguous binding ..."
# Matched case-insensitively on the joined notes.
_AMBIGUOUS_MARKERS: tuple[str, ...] = ("ambiguous binding", "multiple viable")
_DEGRADED_MARKERS: tuple[str, ...] = ("unmet", "degrade")
_SUBSTITUTION_MARKERS: tuple[str, ...] = ("concept sub", "substitut", "inherited")


def binding_quality(gf: GroundedFeature) -> BindingQuality:
    """Derive the binding quality from the grounded feature's authoring/grounding notes.

    Worst-wins precedence, so a weaker marker overrides a stronger one when both appear: an ambiguous
    bind (``AMBIGUOUS``) beats an unmet optional (``ACCEPTABLE``) beats a concept substitution
    (``STRONG``); a clean bind with none of those markers is ``EXACT``.
    """
    notes = " ".join(gf.notes).lower()
    if any(marker in notes for marker in _AMBIGUOUS_MARKERS):
        return BindingQuality.AMBIGUOUS
    if any(marker in notes for marker in _DEGRADED_MARKERS):
        return BindingQuality.ACCEPTABLE
    if any(marker in notes for marker in _SUBSTITUTION_MARKERS):
        return BindingQuality.STRONG
    return BindingQuality.EXACT


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# PITCompleteness — derived from the template's design-time PIT declaration
# ──────────────────────────────────────────────────────────────────────────────────────────────────
class PITCompleteness(StrEnum):
    """Whether a recipe's point-in-time rule is a real, complete declaration. ``COMPLETE`` = a genuine
    trailing-window / point-in-time / as-of declaration; ``NOT_APPLICABLE`` = a non-time-dependent
    recipe (no window param, additive-neutral output, no PIT rule) where PIT simply does not apply;
    ``PARTIAL`` = a declaration that is present but short / marker-less; ``UNKNOWN`` = an empty PIT rule
    we cannot attest."""

    COMPLETE = "complete"
    NOT_APPLICABLE = "not_applicable"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


# A real PIT declaration names its point-in-time anchor: a trailing window, an explicit "point-in-time"
# STATE, or an as-of / as_of comparison. Every authored recipe carries one of these.
_PIT_DECLARATION_MARKERS: tuple[str, ...] = (
    "trailing window", "trailing typology window", "point-in-time", "as_of", "as-of")
# A PIT string that reduces to one of these is treated as absent.
_PIT_EMPTY: frozenset[str] = frozenset({"", "none", "n/a", "na", "-"})


def _has_window_param(t: Template) -> bool:
    """True iff the template is parameterised by a time window (``window`` or ``window_min``)."""
    return any(key.startswith("window") for key in t.params)


def pit_completeness(t: Template) -> PITCompleteness:
    """Derive PIT completeness from the template's design-time ``pit`` declaration.

    An empty PIT rule on a recipe with no time window AND an additive-neutral (``n/a``) output is a
    genuinely non-time-dependent recipe → ``NOT_APPLICABLE``; any other empty PIT is ``UNKNOWN``. A
    non-empty rule that names a PIT anchor (trailing window / point-in-time / as-of) is ``COMPLETE``; a
    non-empty rule with no such anchor is a ``PARTIAL`` statement of intent.
    """
    pit = (t.pit or "").strip()
    low = pit.lower()
    if low in _PIT_EMPTY:
        if not _has_window_param(t) and t.additivity == "n/a":
            return PITCompleteness.NOT_APPLICABLE
        return PITCompleteness.UNKNOWN
    if any(marker in low for marker in _PIT_DECLARATION_MARKERS):
        return PITCompleteness.COMPLETE
    return PITCompleteness.PARTIAL


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# ModellingContextFit — Phase-2A stub (Task B3 supplies the real fit)
# ──────────────────────────────────────────────────────────────────────────────────────────────────
class ModellingContextFit(StrEnum):
    """Fit of a recipe to the confirmed modelling context(s). ``REQUIRED_MATCH`` = the recipe is
    specific to a confirmed context; ``COMPATIBLE`` = it works under one; ``NEUTRAL`` = context does not
    bear on it (or none is confirmed); ``CONFLICT`` = it contradicts a confirmed context (a Task-B3
    warning, never a hard reject in Phase 2)."""

    REQUIRED_MATCH = "required_match"
    COMPATIBLE = "compatible"
    NEUTRAL = "neutral"
    CONFLICT = "conflict"


def _own_modelling_contexts(t: Template) -> frozenset[str]:
    """The recipe's OWN modelling contexts: the ``modelling_context``-dimension targets of its legacy
    ``use_cases`` tags (via :func:`crosswalk`). A recipe carrying ``ifrs9_staging`` declares ``ifrs9``;
    ``frtb`` declares ``frtb``; a recipe with no framework tag is *generic* (an empty set). Unknown tags
    and tags that route to any other dimension (a real use-case leaf, a measure, a journey stage …)
    contribute nothing — only a genuine regulatory-framework/regime tag counts."""
    return frozenset(
        entry["target"] for tag in t.use_cases
        if (entry := crosswalk(tag)) is not None and entry["dimension"] == "modelling_context")


def modelling_context_fit(
    t: Template, confirmed_contexts: tuple[str, ...] = ()) -> ModellingContextFit:
    """Fit the recipe to the human-confirmed modelling context(s) — a rank signal for Task A2 and (on
    ``CONFLICT``) a surfaced warning, NEVER a hard reject in Phase 2.

    * no ``confirmed_contexts`` → ``NEUTRAL`` (nothing to fit; 2A ranking is unaffected);
    * a confirmed context IS one of the recipe's own contexts → ``REQUIRED_MATCH`` (the recipe is
      specific to a confirmed framework — e.g. an ``ifrs9_staging`` recipe under confirmed ``ifrs9``);
    * the recipe declares NO modelling context (generic) → ``COMPATIBLE`` (it works under any context);
    * the recipe declares only context(s) DISJOINT from the confirmed set → ``CONFLICT`` (e.g. an
      ``frtb``-only recipe under confirmed ``ifrs9``) — a warning, not a rejection.
    """
    if not confirmed_contexts:
        return ModellingContextFit.NEUTRAL
    own = _own_modelling_contexts(t)
    confirmed = set(confirmed_contexts)
    if own & confirmed:
        return ModellingContextFit.REQUIRED_MATCH
    if not own:
        return ModellingContextFit.COMPATIBLE
    # The only remaining state is necessarily DISJOINT: ``own`` is non-empty (the COMPATIBLE check above
    # returned for an empty ``own``) AND ``own & confirmed`` is empty (the REQUIRED_MATCH check returned
    # on any overlap), so the recipe declares only contexts the confirmed set does not contain → CONFLICT.
    return ModellingContextFit.CONFLICT


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# EntityCompatibility — Phase-3A: the grain relationship is resolved by the governed entity graph
# (``EntityCompatibility`` is imported from ``entity_relationships`` + re-exported here for callers).
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def _grain_entity(t: Template) -> str | None:
    """The recipe's GRAIN entity: the ``entity_link`` of the concept of the recipe's entity-role need
    (the FIRST need whose concept carries an ``entity_link`` — e.g. a ``customer_id`` need fixes the
    grain at ``customer``, a ``facility_id`` need at ``facility``). A recipe with no entity-linking need
    has no derivable grain → ``None``."""
    for need in t.needs:
        c = concept(need.concept)
        if c is not None and c.entity_link is not None:
            return c.entity_link
    return None


def entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility:
    """The SOFT grain fit of the recipe to a confirmed ``target_entity`` — a grain/groundability signal
    (a low rank tie-break + an ``entity_grain_mismatch`` warning on ``DERIVABLE``), NEVER an
    applicability reject. Phase-3A: the grain relationship is resolved by the governed entity graph
    (:func:`resolve_entity_compatibility` over :data:`ENTITY_GRAPH`) instead of a hardcoded map — the
    seed is regression-equivalent, so outputs match the old map exactly. ``target_entity is None`` or a recipe
    with no derivable grain → ``UNKNOWN``."""
    if target_entity is None:
        return EntityCompatibility.UNKNOWN
    source = _grain_entity(t)
    if source is None:
        return EntityCompatibility.UNKNOWN
    return resolve_entity_compatibility(source, target_entity, ENTITY_GRAPH).status


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# semantic_group — the near-duplicate key
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def semantic_group(t: Template) -> str:
    """The near-duplicate group id = the source template id. Every grounded variant of a template
    (e.g. ``balance_trend_90d`` / ``balance_trend_60d``) carries ``template_id == 'balance_trend'``, so
    they all share this group; the ranker keeps only one variant per group in the initial view (A2)."""
    return t.id
