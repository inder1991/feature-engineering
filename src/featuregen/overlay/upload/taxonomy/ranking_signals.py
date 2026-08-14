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
  derives the recipe's grain (the ``entity_link`` of its entity-role need); the grain *relationship* to the
  target is now resolved by the governed entity graph (:func:`resolve_entity_compatibility` over
  :data:`ENTITY_GRAPH`, Phase 3A — seed regression-equivalent) rather than a hardcoded roll-up map: equal is
  ``EXACT``, a graph-derivable roll-up (child grain -> coarser parent) is ``DERIVABLE``, and anything else
  (incl. no target) is ``UNKNOWN``. There is deliberately **no** ``INCOMPATIBLE`` — a hard entity reject is
  deferred to Phase 3; ``target_entity`` is only ever a soft grain nudge + a grain warning, and is NEVER
  used to reject a recipe anywhere.

``semantic_group`` is the near-duplicate key: the source template id, which every grounded variant of a
template carries. Behaviour-neutral, read-only — nothing here touches grounding or the considered-set.

TWO UNIVERSES LIVE HERE (E4 follow-up, 2026-08-14). The derivations above are authored against the
LEGACY ``Template``; :func:`v2_rank_profiles` and its two folds at the bottom of this module derive
the same five axes over the ATOMIC V2 registry — the universe the engine actually plans and disposes
since the cutover. The serving ranker consumes the V2 profiles; the legacy derivations stay for the
legacy per-table grounding pass that still feeds the asset-detail column dossier.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import crosswalk
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    BindingResolution,
    GroundedFeature,
    SourceEntityRoleResolution,
    Template,
    resolve_source_entity_need_role,
)


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
    resolutions = {binding.resolution for binding in gf.binding_resolutions}
    if BindingResolution.AMBIGUOUS in resolutions:
        return BindingQuality.AMBIGUOUS
    if BindingResolution.MISSING in resolutions:
        return BindingQuality.ACCEPTABLE
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


def pit_completeness_v2(compiled) -> PITCompleteness:
    """BR-4: PIT completeness for a V2 recipe consumes the temporal COMPILER's verdict — never
    keyword markers. ``compiled`` is a ``recipe_temporal_v2.CompiledTemporalV1``: a compiled
    contract is COMPLETE by construction (the text was rendered from typed fields, placeholder-
    free); a blocked one is PARTIAL — a typed intent exists but a load-bearing piece is missing
    and NAMED in ``blockers``. COMPLETE is unreachable while any blocker exists, which is the
    acceptance rule "PIT status cannot be complete when the temporal anchor is missing, ambiguous
    or ungoverned" made structural. The legacy keyword path above is untouched — it dies with the
    legacy registry at BR-17."""
    return (PITCompleteness.COMPLETE if compiled.status == "compiled"
            else PITCompleteness.PARTIAL)


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
    resolved = resolve_source_entity_need_role(t)
    if resolved.resolution == SourceEntityRoleResolution.AMBIGUOUS or resolved.role is None:
        return None
    need = next(need for need in t.needs if need.role == resolved.role)
    c = concept(need.concept)
    return c.entity_link if c is not None else None


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


# ──────────────────────────────────────────────────────────────────────────────────────────────────
# The V2 recipe universe — the SAME five axes, derived over the registry the engine plans
# ──────────────────────────────────────────────────────────────────────────────────────────────────
# Every derivation above is authored against a legacy :class:`Template`. Since the E4 cutover the
# universe the engine plans, disposes and ranks is the ATOMIC V2 registry, so keying the ranker on
# the legacy registry silently dropped the 126 recipes that have no legacy twin — an eligible recipe
# that could never be ordered, never selected for the initial view, and never offered to formula
# shadow capture. These derivations close that: one profile per V2 recipe, TOTAL over the registry.
#
# Where the V2 contract carries the fact, it is read directly (``family``, ``output_grain``, the
# temporal COMPILER's verdict). Three of the ranker's inputs have no V2 field at all —
# explainability, funnel journey, and regulatory modelling context are legacy AUTHORING metadata —
# so they are bridged through ``replaces_legacy_ids``, which is source-controlled and explicit
# (never heuristic, the same rule the alias map lives by). A V2-only recipe declares no legacy
# source, so it carries an honest ABSENCE: no journey, no framework, and an unauthored
# explainability that the ranker's documented total order sorts last on that axis — never an
# invented ``"H"``.
@dataclass(frozen=True, slots=True)
class V2RankProfileV1:
    """One V2 recipe's DESIGN-TIME ranking facts — everything the signal bundle needs that does not
    depend on the request. Computed once for the whole registry (see :func:`v2_rank_profiles`); the
    two request-dependent axes (confirmed modelling contexts, confirmed target entity) fold over it."""

    recipe_id: str
    family: str
    semantic_group: str
    explainability: str                    # "H" | "M" | "L", or "" when unauthored
    journey_model_id: str | None
    journey_stage_id: str | None
    pit_completeness: PITCompleteness
    own_modelling_contexts: frozenset[str]
    grain_entity: str | None


def _replaced_templates(recipe) -> tuple[Template, ...]:
    """The legacy templates a V2 recipe DECLARES it replaces — source-controlled, never inferred.
    Empty for the V2-only recipes (no legacy twin exists to inherit authoring from)."""
    by_id = {t.id: t for t in ALL_TEMPLATES}
    return tuple(t for rid in recipe.replaces_legacy_ids if (t := by_id.get(rid)) is not None)


def _bridged_explainability(replaced: tuple[Template, ...]) -> str:
    """The explainability the V2 recipe inherits from the legacy template(s) it replaces — the
    WEAKEST of them when it replaces more than one, because a merged recipe is no more explainable
    than its least explainable half. ``""`` when nothing is inherited: unauthored, and the ranker
    orders an unrecognised label last on that axis rather than promoting a guess."""
    order = {"H": 0, "M": 1, "L": 2}
    labels = [t.explain for t in replaced if t.explain in order]
    return max(labels, key=lambda label: order[label]) if labels else ""


def _bridged_journey(replaced: tuple[Template, ...]):
    """The funnel position inherited from the ONE legacy template that authored one. Two replaced
    templates that disagree yield no journey — the diversity pass would otherwise spread the initial
    view over a stage nobody assigned this recipe."""
    from featuregen.overlay.upload.taxonomy.journey_stages import journey_metadata

    found = {(m.journey_model_id, m.journey_stage_id)
             for t in replaced
             if (m := journey_metadata(t)).journey_stage_id is not None}
    return found.pop() if len(found) == 1 else (None, None)


_V2_RANK_PROFILES: dict[str, V2RankProfileV1] | None = None


def v2_rank_profiles() -> dict[str, V2RankProfileV1]:
    """Every V2 recipe's design-time ranking profile, keyed by ``recipe_id`` — computed once and
    memoized (the registry is a frozen, import-time constant, so the profiles are too)."""
    global _V2_RANK_PROFILES
    if _V2_RANK_PROFILES is not None:
        return _V2_RANK_PROFILES

    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
    from featuregen.overlay.upload.recipe_temporal_v2 import compile_temporal

    profiles: dict[str, V2RankProfileV1] = {}
    for recipe in V2_RECIPES:
        replaced = _replaced_templates(recipe)
        model_id, stage_id = _bridged_journey(replaced)
        profiles[recipe.recipe_id] = V2RankProfileV1(
            recipe_id=recipe.recipe_id,
            family=recipe.family,
            # Atomic by contract: a V2 recipe IS its own near-duplicate group. (Its parameter
            # VARIANTS share the recipe id, which is exactly what the group dedup wants.)
            semantic_group=recipe.recipe_id,
            explainability=_bridged_explainability(replaced),
            journey_model_id=model_id,
            journey_stage_id=stage_id,
            # BR-4: the temporal COMPILER's verdict, never keyword markers on a prose PIT rule.
            pit_completeness=pit_completeness_v2(compile_temporal(recipe)),
            own_modelling_contexts=frozenset().union(
                *(_own_modelling_contexts(t) for t in replaced)) if replaced else frozenset(),
            # The grain the recipe computes AT — authored on the V2 contract, so no concept
            # archaeology is needed to recover what the legacy path inferred from its needs.
            grain_entity=recipe.output_grain or None,
        )
    _V2_RANK_PROFILES = profiles
    return profiles


def modelling_context_fit_v2(
    profile: V2RankProfileV1,
    confirmed_contexts: tuple[str, ...] = ()) -> ModellingContextFit:
    """:func:`modelling_context_fit`'s law, unchanged, over a V2 profile's own contexts."""
    if not confirmed_contexts:
        return ModellingContextFit.NEUTRAL
    confirmed = set(confirmed_contexts)
    if profile.own_modelling_contexts & confirmed:
        return ModellingContextFit.REQUIRED_MATCH
    if not profile.own_modelling_contexts:
        return ModellingContextFit.COMPATIBLE
    return ModellingContextFit.CONFLICT


def entity_compatibility_v2(
    profile: V2RankProfileV1, target_entity: str | None = None) -> EntityCompatibility:
    """:func:`entity_compatibility`'s law, unchanged, over the recipe's AUTHORED output grain —
    resolved by the same governed entity graph. No target, or a grain outside the graph's closed
    vocabulary, is ``UNKNOWN``; it is never an applicability reject."""
    if target_entity is None or profile.grain_entity is None:
        return EntityCompatibility.UNKNOWN
    return resolve_entity_compatibility(
        profile.grain_entity, target_entity, ENTITY_GRAPH).status
