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
      ``frtb``-only recipe under confirmed ``ifrs9``) — a warning, not a rejection;
    * else → ``NEUTRAL`` (a total, defensive fallback).
    """
    if not confirmed_contexts:
        return ModellingContextFit.NEUTRAL
    own = _own_modelling_contexts(t)
    confirmed = set(confirmed_contexts)
    if own & confirmed:
        return ModellingContextFit.REQUIRED_MATCH
    if not own:
        return ModellingContextFit.COMPATIBLE
    if own.isdisjoint(confirmed):
        return ModellingContextFit.CONFLICT
    return ModellingContextFit.NEUTRAL


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# EntityCompatibility — Phase-2A stub (Task B3 supplies the real grain logic)
# ──────────────────────────────────────────────────────────────────────────────────────────────────
class EntityCompatibility(StrEnum):
    """Soft grain fit of a recipe to a confirmed ``target_entity``. ``EXACT`` = the recipe's grain is the
    target entity; ``DERIVABLE`` = it can be rolled up/down to it; ``UNKNOWN`` = no target entity, or the
    grain relationship is not known. There is deliberately **no** ``INCOMPATIBLE`` — a hard entity reject
    is deferred to Phase 3; ``target_entity`` is only ever a soft grain nudge."""

    EXACT = "exact"
    DERIVABLE = "derivable"
    UNKNOWN = "unknown"


# A small, CONSERVATIVE roll-up map: a child grain that can be aggregated UP to a coarser parent entity
# WITHOUT needing join semantics we don't have yet. Deliberately tiny — only the roll-ups we can assert
# by declaration (the full relational grain graph, and any hard INCOMPATIBLE reject, are Phase-3 work).
# Chains compose transitively (``transaction -> account -> customer``); :func:`_rolls_up_to` walks them.
_ENTITY_ROLLUP: dict[str, str] = {
    "account": "customer",
    "card_account": "customer",
    "transaction": "account",
    "facility": "obligor",
    "policy": "customer",
}


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


def _rolls_up_to(grain: str, target: str) -> bool:
    """True iff ``grain`` rolls up to ``target`` along the declared roll-up chain (transitively, with a
    cycle guard). ``account`` rolls up to ``customer`` directly; ``transaction`` rolls up to ``customer``
    via ``account``. A grain that only rolls DOWN to (never UP to) the target does not match."""
    seen: set[str] = set()
    cur: str | None = grain
    while cur is not None and cur not in seen:
        seen.add(cur)
        cur = _ENTITY_ROLLUP.get(cur)
        if cur == target:
            return True
    return False


def entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility:
    """The SOFT grain fit of the recipe to a confirmed ``target_entity``. A grain/groundability signal —
    a low rank tie-break and (on ``DERIVABLE``) a grain warning — NEVER an applicability reject.

    * ``target_entity is None`` → ``UNKNOWN`` (no target confirmed; ranking is unaffected);
    * the recipe's grain == ``target_entity`` → ``EXACT`` (the recipe already predicts at the target grain);
    * the recipe's grain rolls up to ``target_entity`` via the declared roll-up map → ``DERIVABLE`` (a real
      grain mismatch that a roll-up can bridge — e.g. an ``account``-grain recipe under ``customer``);
    * otherwise → ``UNKNOWN`` (no known grain relationship, or the recipe declares no grain).

    There is deliberately **no** ``INCOMPATIBLE`` — a hard entity reject is Phase-3 work; a grain that
    does not roll up to the target is ``UNKNOWN`` (soft), never a rejection."""
    if target_entity is None:
        return EntityCompatibility.UNKNOWN
    grain = _grain_entity(t)
    if grain is None:
        return EntityCompatibility.UNKNOWN
    if grain == target_entity:
        return EntityCompatibility.EXACT
    if _rolls_up_to(grain, target_entity):
        return EntityCompatibility.DERIVABLE
    return EntityCompatibility.UNKNOWN


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# semantic_group — the near-duplicate key
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def semantic_group(t: Template) -> str:
    """The near-duplicate group id = the source template id. Every grounded variant of a template
    (e.g. ``balance_trend_90d`` / ``balance_trend_60d``) carries ``template_id == 'balance_trend'``, so
    they all share this group; the ranker keeps only one variant per group in the initial view (A2)."""
    return t.id
