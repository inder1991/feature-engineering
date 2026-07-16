"""Phase-3B.3c C2/C3 — the contract compiler's data spine + the declaration checks
(connectivity; the temporal declaration, which runs FIRST in the compile pipeline).

One IMMUTABLE, conn-free ``CompilerContext`` is batch-loaded per shadow run (the production
builder ``build_compiler_context`` arrives in C8); every declaration check is a pure function
over that context and a plan — no check in this module takes a connection. ``CompileBudget`` is
the ONE deliberately mutable exception: the per-run compile allowance owned by
``run_shadow_planner`` (C8). Behaviour-neutral until C8 threads ``compile_contracts`` through
the shadow planner — nothing imports this module yet."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from featuregen.overlay.config import OverlayConfig
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.bridge_projection import ActiveBridgeV1
from featuregen.overlay.upload.catalog_realizations import table_of
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.need_metadata import ResolvedNeedMetadataV1, derive_need_metadata
from featuregen.overlay.upload.planner.contracts import (
    AdditivityClass,
    AdditivityProvenanceV1,
    AdditivitySource,
    AggregationAxisKind,
    AggregationFunction,
    AggregationValidation,
    BindingPathSegmentV1,
    BindingPlanV1,
    CatalogStateStampV1,
    HopAggregationV1,
    IngredientAggregationV1,
    IngredientBindingV1,
    ParamBindingV1,
    ReasonCode,
    SegmentKind,
    TemporalDeclarationV1,
    WindowSpecV1,
    canonical_reason_codes,
    to_additivity_class,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
)
from featuregen.overlay.upload.templates import Template, _Col

# The injectable declared-function registry: ``(recipe_id, need_role) ->`` the recipe's DECLARED
# aggregation function. EMPTY in production until a governed declaration source exists (validate,
# never fabricate — an absent key is an honest ``undeclared``, never a guessed function).
AggregationDeclarationRegistry = Mapping[tuple[str, str], AggregationFunction]


@dataclass(frozen=True, slots=True)
class CompilerContext:
    """Everything the contract compiler reads, batch-loaded ONCE per run — realizations, active
    bridges, read-scoped columns, the scope-start fingerprints the compile-end recheck (C7)
    revalidates, the freshness stamps, and the injected declaration registry. GENUINELY
    immutable: every Mapping field is re-wrapped in a read-only proxy over a private copy at
    construction, so no caller-held dict reference can mutate the context afterwards."""

    realizations_by_catalog: Mapping[str, tuple[CatalogEntityRelationshipV1, ...]]
    active_bridges: tuple[ActiveBridgeV1, ...]
    columns_by_catalog: Mapping[str, Mapping[str, _Col]]     # catalog -> object_ref -> _Col
    catalog_fingerprint_at_start: Mapping[str, str]
    bridge_fingerprint_at_start: str
    catalog_stamps: Mapping[str, CatalogStateStampV1]
    config: OverlayConfig
    roles: tuple[str, ...]
    now: datetime
    agg_declarations: AggregationDeclarationRegistry

    def __post_init__(self) -> None:
        object.__setattr__(self, "realizations_by_catalog",
                           MappingProxyType(dict(self.realizations_by_catalog)))
        object.__setattr__(self, "columns_by_catalog", MappingProxyType(
            {cat: MappingProxyType(dict(cols))
             for cat, cols in self.columns_by_catalog.items()}))
        object.__setattr__(self, "catalog_fingerprint_at_start",
                           MappingProxyType(dict(self.catalog_fingerprint_at_start)))
        object.__setattr__(self, "catalog_stamps", MappingProxyType(dict(self.catalog_stamps)))
        object.__setattr__(self, "agg_declarations", MappingProxyType(dict(self.agg_declarations)))


@dataclass(slots=True)
class CompileBudget:
    """DELIBERATELY MUTABLE (the one exception to the frozen convention): the per-run compile
    allowance — a remaining-plan count and a wall-clock deadline — owned and decremented by
    ``run_shadow_planner`` (C8). A run past either bound records ``compile_budget_exhausted``
    instead of compiling further plans."""

    remaining: int
    deadline: datetime


@dataclass(frozen=True, slots=True)
class PathPositionV1:
    """WHERE on the plan's physical path an ingredient's table sits: the index into
    ``plan.path_segments`` of the hop that holds it. ``segment_index=0`` is the pre-first-hop
    SOURCE position (the source-key binding's table)."""

    segment_index: int
    catalog_source: str
    table: str


@dataclass(frozen=True, slots=True)
class ConnectivityResult:
    """``check_connectivity``'s verdict: every ingredient placed on the path (or co-located with
    the source-key table), or the honest list of roles that are not. The caller (C8) attaches
    ``ReasonCode.ingredient_not_connected_to_path`` per disconnected role — this result carries
    evidence, not reason codes."""

    connected: bool
    disconnected_roles: tuple[str, ...]
    placement: Mapping[str, PathPositionV1]     # need_role -> position

    def __post_init__(self) -> None:
        object.__setattr__(self, "placement", MappingProxyType(dict(self.placement)))


def check_connectivity(ctx: CompilerContext, plan: BindingPlanV1) -> ConnectivityResult:
    """Every ingredient's bound ``(catalog, table)`` must be ON the plan's physical path — a
    realization endpoint table, a bridge endpoint table, or co-located with the source-key
    binding's table. Pure over ``ctx`` (no conn).

    Path tables: each segment carrying a ``realization_ref`` contributes the realization's
    from/to tables (stored as TABLE object_refs); each segment carrying a ``bridge_fact_key``
    contributes BOTH bridge endpoint tables (endpoints are COLUMN refs, left/right storage order
    is unordered); the source-key table is the pre-first-hop position. A segment whose ref cannot
    be resolved in the context contributes NOTHING (fail closed — its ingredients honestly
    report disconnected). Placement: the source-key table places at ``segment_index=0``
    (checked first — it wins even when a hop also touches that table); otherwise the EARLIEST
    hop whose to-side/endpoint is the table; a from-side-only table falls back to 0."""
    source_key_role = str(JoinRole.SOURCE_ENTITY_KEY)
    source_pos: tuple[str, str] | None = None
    for b in plan.ingredient_bindings:
        if b.join_role == source_key_role:
            source_pos = (b.bound_catalog_source, table_of(b.bound_object_ref))
            break

    path_tables: set[tuple[str, str]] = set()
    hop_of: dict[tuple[str, str], int] = {}     # earliest hop whose to-side/endpoint is the table
    if source_pos is not None:
        path_tables.add(source_pos)
    for idx, seg in enumerate(plan.path_segments):
        if seg.realization_ref is not None:
            r = next((x for x in ctx.realizations_by_catalog.get(seg.catalog_source, ())
                      if x.realization_id == seg.realization_ref), None)
            if r is None:
                continue    # unresolvable ref — contributes no tables (fail closed)
            # from_object_ref/to_object_ref are already TABLE refs (catalog_realizations stores
            # tables, not columns) — table_of() here would over-strip them to the bare schema.
            path_tables.add((seg.catalog_source, r.from_object_ref))
            to_table = (seg.catalog_source, r.to_object_ref)
            path_tables.add(to_table)
            hop_of.setdefault(to_table, idx)
        elif seg.bridge_fact_key is not None:
            br = next((x for x in ctx.active_bridges if x.fact_key == seg.bridge_fact_key), None)
            if br is None:
                continue    # not an active VERIFIED bridge — contributes no tables (fail closed)
            for cat, col_ref in ((br.left_catalog_source, br.left_object_ref),
                                 (br.right_catalog_source, br.right_object_ref)):
                endpoint = (cat, table_of(col_ref))
                path_tables.add(endpoint)
                hop_of.setdefault(endpoint, idx)

    placement: dict[str, PathPositionV1] = {}
    disconnected: list[str] = []
    for b in plan.ingredient_bindings:
        key = (b.bound_catalog_source, table_of(b.bound_object_ref))
        if key not in path_tables:
            disconnected.append(b.need_role)
            continue
        index = 0 if key == source_pos else hop_of.get(key, 0)
        placement[b.need_role] = PathPositionV1(
            segment_index=index, catalog_source=key[0], table=key[1])
    return ConnectivityResult(
        connected=not disconnected, disconnected_roles=tuple(disconnected), placement=placement)


# ─── C3: temporal declaration on representative params ───────────────────────────────────────

# The corpus's window params, in lookup order: trailing DAY windows ("window") and the real-time
# family's trailing MINUTE windows ("window_min" — templates.py declares those minutes/hours,
# NEVER trailing days; recording them as days would falsify the hashed temporal signature).
_WINDOW_PARAM_UNITS: tuple[tuple[str, str], ...] = (("window", "days"), ("window_min", "minutes"))

# Roles that can serve as the recipe's primary PIT anchor, in PRECEDENCE order: an AS_OF_TIME
# need outranks EVENT_TIME when both are present (e.g. margin_call_intensity — the as-of is the
# evaluation date, the event axis is the measured one), so coexistence is NOT ambiguity (F17).
_PRIMARY_ANCHOR_PRECEDENCE: tuple[TemporalRole, ...] = (
    TemporalRole.AS_OF_TIME, TemporalRole.EVENT_TIME)


def compile_temporal(ctx: CompilerContext, plan: BindingPlanV1,
                     template: Template) -> TemporalDeclarationV1:
    """The plan's temporal declaration, compiled FIRST in the pipeline (its window/anchor output
    feeds C4's semi-additive single-PIT-vs-across-time decision). Pure — template + plan material
    only, resolved through ``derive_need_metadata`` (F17: works for INJECTED templates; the
    static ``RESOLVED_NEED_METADATA`` registry would KeyError on them).

    Declared, never fabricated: each param binds its FIRST allowed value and the result is
    honestly flagged ``is_representative`` (F7) — one instantiation, not a parameter-space
    validation. A ``window``/``window_min`` param becomes a TYPED trailing ``WindowSpecV1``
    (days/minutes respectively) and makes the recipe time-axis-aggregating; no window param
    means a pure point-in-time read. The primary PIT anchor is the highest-precedence temporal
    need role; ``valid_from``+``valid_to`` together are a VALID bitemporal interval (never a
    primary anchor, never ambiguity). ``temporal_anchor_ambiguous`` fires ONLY for genuinely
    incompatible anchors — the winning role's needs bound to ≥2 DISTINCT columns; a declared
    anchor role that no ingredient supplies is ``temporal_anchor_missing``."""
    del ctx     # uniform check signature; conn-free like every check in this module
    metas = derive_need_metadata(template)

    param_binding = ParamBindingV1(
        values=tuple(sorted(
            (name, str(allowed[0])) for name, allowed in template.params.items())),
        is_representative=True)

    window: WindowSpecV1 | None = None
    for key, unit in _WINDOW_PARAM_UNITS:
        allowed = template.params.get(key)
        if allowed:
            window = WindowSpecV1(length=int(allowed[0]), unit=unit,
                                  boundary="trailing", inclusive=True)
            break

    anchor_metas: list[ResolvedNeedMetadataV1] = []
    for role in _PRIMARY_ANCHOR_PRECEDENCE:
        anchor_metas = [m for m in metas if m.temporal_role == role]
        if anchor_metas:
            break

    pit_anchor: str | None = None
    anchor_binding: str | None = None
    codes: list[ReasonCode] = []
    if anchor_metas:
        bound_by_role = {b.need_role: b.bound_object_ref for b in plan.ingredient_bindings}
        bound_refs = {bound_by_role[m.role] for m in anchor_metas if m.role in bound_by_role}
        if len(bound_refs) > 1:
            codes.append(ReasonCode.temporal_anchor_ambiguous)      # genuinely competing columns
        else:
            pit_anchor = str(anchor_metas[0].temporal_role)
            if bound_refs:
                anchor_binding = next(iter(bound_refs))
            else:
                codes.append(ReasonCode.temporal_anchor_missing)    # declared but unsupplied

    return TemporalDeclarationV1(
        pit_anchor=pit_anchor, anchor_binding=anchor_binding, window=window,
        param_binding=param_binding, time_axis_aggregating=window is not None,
        reason_codes=canonical_reason_codes(codes))


# ─── C4: per-ingredient aggregation + additivity + physical/bridge cardinality ────────────────
#
# VALIDATE, NEVER FABRICATE (versioned by AGGREGATION_RULE_VERSION): the ONLY auto-derivations are
# `additive` fan-in → SUM and `semi_additive` entity-axis single-PIT → SUM, both expressed as
# validation=sound with declared_function=None — the derived SUM is never written into the
# DECLARED slot. Every other function must come from ctx.agg_declarations (empty in production);
# anything unprovable resolves undeclared/incompatible/inputs_missing, never a guessed function.

# hop_physical_cardinality's `source` vocabulary (F4): where the fan-in evidence came from.
CARDINALITY_SOURCE_REALIZATION = "realization"
CARDINALITY_SOURCE_BRIDGE = "bridge_construction"
CARDINALITY_SOURCE_UNAVAILABLE = "unavailable"

# Declared functions that are provably duplication/order-safe on ANY fan-in without extra inputs.
_ORDER_SAFE_DECLARED = frozenset(
    {AggregationFunction.count, AggregationFunction.min, AggregationFunction.max})


def resolve_additivity(ctx: CompilerContext, binding: IngredientBindingV1) -> AdditivityProvenanceV1:
    """§4.1 — which additivity governs this ingredient, with full provenance. Precedence:
    the UPLOADED column additivity when the bound column asserts one (anything but absent/blank/
    n/a) → else the CONCEPT's additivity when the concept is registered → else honest ``unknown``
    (which downstream is NEVER treated as additive — no silent SUM). BOTH raw values are kept
    (F15) and a disagreement between two ASSERTED sources sets ``conflict`` — recorded for the
    plan-level ``additivity_source_conflict`` diagnostic, never silently resolved."""
    col = ctx.columns_by_catalog.get(binding.bound_catalog_source, {}).get(
        binding.bound_object_ref)
    uploaded = to_additivity_class(col.additivity) if col is not None else None
    con = concept(binding.concept)
    concept_raw = con.additivity if con is not None else None
    concept_add = to_additivity_class(concept_raw)
    selected: AdditivityClass
    if uploaded is not None and uploaded is not AdditivityClass.not_applicable:
        selected, source = uploaded, AdditivitySource.uploaded_column
    elif con is not None:
        selected, source = concept_add, AdditivitySource.concept
    else:
        selected, source = AdditivityClass.unknown, AdditivitySource.unknown
    conflict = (uploaded is not None and uploaded is not AdditivityClass.not_applicable
                and concept_add not in (AdditivityClass.unknown, AdditivityClass.not_applicable)
                and uploaded is not concept_add)
    return AdditivityProvenanceV1(
        uploaded_value=col.additivity if col is not None else None, concept_value=concept_raw,
        selected=selected, source=source, conflict=conflict)


def _hop_evidence(
        ctx: CompilerContext, segment: BindingPathSegmentV1,
) -> tuple[Cardinality | None, str, tuple[str, ...], str, str]:
    """One hop segment's physical evidence: (cardinality, source, grouping_keys,
    execution_catalog, execution_table). Realized hop → the REALIZATION's declared_cardinality
    (F4/F8 — the physical authority; the segment's semantic cardinality string is never
    consulted), its to-side key as the GROUP BY, its to-table as the execution site. Bridge-ROLLUP
    hop → many_to_one BY CONSTRUCTION (the bridge anchors an E2-key FK column to an E2-grain far
    table), grouped at the far (target-grain) endpoint — the endpoint in the segment's catalog
    (endpoint storage order is unordered). Anything the context cannot resolve →
    ``(None, "unavailable", (), <segment catalog>, "")`` — fail closed, never a guessed fan-in."""
    if segment.realization_ref is not None:
        r = next((x for x in ctx.realizations_by_catalog.get(segment.catalog_source, ())
                  if x.realization_id == segment.realization_ref), None)
        if r is None:
            return None, CARDINALITY_SOURCE_UNAVAILABLE, (), segment.catalog_source, ""
        return (r.declared_cardinality, CARDINALITY_SOURCE_REALIZATION, (r.to_key_ref,),
                segment.catalog_source, r.to_object_ref)
    if segment.bridge_fact_key is not None:
        br = next((x for x in ctx.active_bridges
                   if x.fact_key == segment.bridge_fact_key), None)
        if br is not None:
            far = [(cat, ref)
                   for cat, ref in ((br.left_catalog_source, br.left_object_ref),
                                    (br.right_catalog_source, br.right_object_ref))
                   if cat == segment.catalog_source]
            if len(far) == 1:   # exactly one endpoint on the segment's (far) side, else fail closed
                cat, ref = far[0]
                return (Cardinality.MANY_TO_ONE, CARDINALITY_SOURCE_BRIDGE, (ref,),
                        cat, table_of(ref))
        return None, CARDINALITY_SOURCE_UNAVAILABLE, (), segment.catalog_source, ""
    return None, CARDINALITY_SOURCE_UNAVAILABLE, (), segment.catalog_source, ""


def hop_physical_cardinality(
        ctx: CompilerContext, segment: BindingPathSegmentV1,
) -> tuple[Cardinality | None, str, tuple[str, ...]]:
    """The PHYSICAL fan-in of one hop-realizing segment: (cardinality, source, grouping_keys).
    Valid for hop realizers (a realized roll-up or a rollup governed_bridge) — a same-entity
    reposition bridge is not a hop and is never passed here by ``compile_aggregation``."""
    cardinality, source, keys, _cat, _table = _hop_evidence(ctx, segment)
    return cardinality, source, keys


def _semantic_hops(
        plan: BindingPlanV1,
) -> list[tuple[int, int, BindingPathSegmentV1, BindingPathSegmentV1 | None]]:
    """The plan's semantic hops as (semantic_hop_index, segment_index, announcing segment,
    realizer-or-None). The assembler emits each hop as a ``semantic_rollup`` announcement followed
    by its realizer — ``intra_catalog_realization``, or a rollup ``governed_bridge`` carrying the
    SAME from/to entities (the entity match keeps a same-entity REPOSITION bridge from being
    mistaken for a realizer; repositions cross on the GRAIN key — 1:1 by construction — and are
    never hops). A semantic_rollup with no realizer stays a hop with no physical evidence (fail
    closed: its cardinality resolves unavailable)."""
    out: list[tuple[int, int, BindingPathSegmentV1, BindingPathSegmentV1 | None]] = []
    segs = plan.path_segments
    sem_idx = -1
    for idx, seg in enumerate(segs):
        if seg.segment_kind is not SegmentKind.semantic_rollup:
            continue
        sem_idx += 1
        if seg.realization_ref is not None or seg.bridge_fact_key is not None:
            out.append((sem_idx, idx, seg, seg))    # self-realized single-segment hop
            continue
        realizer: BindingPathSegmentV1 | None = None
        seg_index = idx
        nxt = segs[idx + 1] if idx + 1 < len(segs) else None
        if nxt is not None and nxt.segment_kind is not SegmentKind.semantic_rollup:
            if nxt.realization_ref is not None:
                realizer, seg_index = nxt, idx + 1
            elif (nxt.bridge_fact_key is not None
                    and (nxt.from_entity, nxt.to_entity) == (seg.from_entity, seg.to_entity)):
                realizer, seg_index = nxt, idx + 1
        out.append((sem_idx, seg_index, seg, realizer))
    return out


def _validate_declared_inputs(
        declared: AggregationFunction, need_role: str, bound_roles: frozenset[str],
) -> tuple[AggregationValidation, ReasonCode | None, tuple[str, ...]]:
    """The declared strategies that need MORE inputs than the measure itself. The input-role
    convention (versioned under AGGREGATION_RULE_VERSION — no governed weight/component
    declaration source exists yet): ``<role>_weight`` (or a plan-wide ``weight``) for
    weighted_average; ``<role>_numerator``/``<role>_denominator`` (or plan-wide
    ``numerator``/``denominator``) for ratio_recompute. Unbound inputs → inputs_missing with the
    missing roles recorded — never a silently degraded plain average/ratio."""
    if declared is AggregationFunction.weighted_average:
        if f"{need_role}_weight" in bound_roles or "weight" in bound_roles:
            return AggregationValidation.sound, None, ()
        return (AggregationValidation.inputs_missing, ReasonCode.aggregation_weight_missing,
                (f"{need_role}_weight",))
    if declared is AggregationFunction.ratio_recompute:
        missing = tuple(
            f"{need_role}_{part}" for part in ("numerator", "denominator")
            if f"{need_role}_{part}" not in bound_roles and part not in bound_roles)
        if missing:
            return (AggregationValidation.inputs_missing,
                    ReasonCode.aggregation_components_missing, missing)
        return AggregationValidation.sound, None, ()
    # any other declared function reaching here has no compatibility proof — fail closed
    return (AggregationValidation.incompatible,
            ReasonCode.aggregation_incompatible_with_additivity, ())


def _validate_stage(
        selected: AdditivityClass, declared: AggregationFunction | None,
        time_axis_aggregating: bool, need_role: str, bound_roles: frozenset[str],
) -> tuple[AggregationValidation, ReasonCode | None, tuple[str, ...]]:
    """The §4 validation matrix for ONE measure stage on a fan-in hop:
    (validation, matrix reason, missing_inputs)."""
    if selected is AdditivityClass.not_applicable:
        # a non-aggregating measure sitting on a fan-in hop is structurally wrong on ANY axis
        return AggregationValidation.incompatible, ReasonCode.aggregation_axis_unsupported, ()
    if selected is AdditivityClass.unknown:
        # unknown is NEVER treated as additive, and no declared function can be validated
        # against an unknown additivity — honest undeclared, no silent SUM
        return AggregationValidation.undeclared, ReasonCode.aggregation_strategy_missing, ()
    if selected is AdditivityClass.additive:
        if declared is None or declared is AggregationFunction.sum \
                or declared in _ORDER_SAFE_DECLARED:
            return AggregationValidation.sound, None, ()    # undeclared → the versioned SUM rule
        return (AggregationValidation.incompatible,
                ReasonCode.aggregation_incompatible_with_additivity, ())
    if selected is AdditivityClass.semi_additive:
        if declared is None:
            if time_axis_aggregating:   # a stock rolled ACROSS time needs a declared strategy
                return (AggregationValidation.undeclared,
                        ReasonCode.semi_additive_temporal_strategy_missing, ())
            return AggregationValidation.sound, None, ()    # entity-axis single-PIT SUM rule
        if declared is AggregationFunction.sum:
            if time_axis_aggregating:   # the classic error: summing a balance over a window
                return (AggregationValidation.incompatible,
                        ReasonCode.aggregation_incompatible_with_additivity, ())
            return AggregationValidation.sound, None, ()
        if declared is AggregationFunction.take_latest or declared in _ORDER_SAFE_DECLARED:
            return AggregationValidation.sound, None, ()
        return _validate_declared_inputs(declared, need_role, bound_roles)
    # non_additive: no sound default exists — everything must be declared and provable
    if declared is None:
        return AggregationValidation.undeclared, ReasonCode.aggregation_strategy_missing, ()
    if declared is AggregationFunction.sum:
        return (AggregationValidation.incompatible,
                ReasonCode.aggregation_incompatible_with_additivity, ())
    if declared is AggregationFunction.take_latest or declared in _ORDER_SAFE_DECLARED:
        return AggregationValidation.sound, None, ()
    return _validate_declared_inputs(declared, need_role, bound_roles)


def compile_aggregation(
        ctx: CompilerContext, plan: BindingPlanV1, template: Template,
        temporal: TemporalDeclarationV1,
        placement: Mapping[str, PathPositionV1]) -> tuple[HopAggregationV1, ...]:
    """Per-(hop × ingredient) aggregation evidence for every FAN-IN hop of the plan's path. A hop
    is fan-in when its PHYSICAL cardinality (realization / bridge-construction — never the
    semantic segment string) is many_to_one/many_to_many; a hop whose cardinality the context
    cannot resolve is kept fail-closed (its stages carry ``physical_cardinality_unavailable``);
    a provably 1:1 / 1:N hop needs no aggregation and emits nothing. The execution site is the
    hop's TO-side table (the realization's to-table / the bridge's far endpoint table — not the
    C2 diagnostic placement map). Each MEASURE ingredient (join_role=measure; keys/time are never
    aggregated) is staged EXACTLY ONCE, at the FIRST fan-in hop at/after its placement position —
    whether that hop's OUTPUT re-aggregates at later hops is C5's composition guard. Deterministic:
    hops by segment_index, stages by need_role. Pure over ``ctx`` — no connection."""
    del template    # uniform check signature; the template's OUTPUT additivity is C5's input
    hops: list[tuple[int, int, BindingPathSegmentV1,
                     Cardinality | None, str, tuple[str, ...], str, str]] = []
    for sem_idx, seg_idx, announce, realizer in _semantic_hops(plan):
        card, source, keys, exec_cat, exec_table = _hop_evidence(
            ctx, realizer if realizer is not None else announce)
        if card in (Cardinality.ONE_TO_ONE, Cardinality.ONE_TO_MANY):
            continue    # provably no fan-in — nothing aggregates at this hop
        hops.append((sem_idx, seg_idx, announce, card, source, keys, exec_cat, exec_table))

    bound_roles = frozenset(b.need_role for b in plan.ingredient_bindings)
    measure_role = str(JoinRole.MEASURE)
    stages_by_hop: dict[int, list[IngredientAggregationV1]] = {h[1]: [] for h in hops}
    for b in sorted(plan.ingredient_bindings, key=lambda x: x.need_role):
        if b.join_role != measure_role:
            continue    # join-key / source-key / time ingredients are carried, never aggregated
        pos = placement.get(b.need_role)
        if pos is None:
            continue    # off-path — C2 connectivity's verdict, not an aggregation stage
        hop = next((h for h in hops if h[1] >= pos.segment_index), None)
        if hop is None:
            continue    # no fan-in at/after its position — carried at grain, nothing to validate
        card = hop[3]
        provenance = resolve_additivity(ctx, b)
        declared = ctx.agg_declarations.get((plan.recipe_id, b.need_role))
        codes: list[ReasonCode] = []
        missing: tuple[str, ...] = ()
        if card is None:
            validation = AggregationValidation.undeclared   # can't validate an unknown fan-in
            codes.append(ReasonCode.physical_cardinality_unavailable)
        else:
            validation, matrix_reason, missing = _validate_stage(
                provenance.selected, declared, temporal.time_axis_aggregating,
                b.need_role, bound_roles)
            if matrix_reason is not None:
                codes.append(matrix_reason)
        if provenance.conflict:
            codes.append(ReasonCode.additivity_source_conflict)
        stages_by_hop[hop[1]].append(IngredientAggregationV1(
            need_role=b.need_role, bound_object_ref=b.bound_object_ref,
            additivity=provenance.selected, provenance=provenance, physical_cardinality=card,
            axis=AggregationAxisKind.entity, declared_function=declared, validation=validation,
            missing_inputs=missing, reason_codes=canonical_reason_codes(codes)))

    return tuple(
        HopAggregationV1(
            semantic_hop_index=sem_idx, segment_index=seg_idx,
            from_entity=announce.from_entity or "", to_entity=announce.to_entity or "",
            execution_catalog=exec_cat, execution_table=exec_table,
            physical_cardinality=card, cardinality_source=source, grouping_keys=keys,
            ingredient_stages=tuple(stages_by_hop[seg_idx]))
        for sem_idx, seg_idx, announce, card, source, keys, exec_cat, exec_table in hops)


# ─── C5: cross-hop composition (the conservative fail-closed guard, spec §4.2) ────────────────
#
# The recipe corpus has NO structured output algebra, so this is NOT an expression evaluator:
# a measure aggregated at hop k produces an intermediate-grain result that FLOWS into every later
# fan-in hop and is implicitly re-aggregated there (C4 stages each measure exactly once, at its
# FIRST fan-in hop). The guard passes ONLY what it can PROVE sound; everything else is one honest
# aggregation_composition_unsupported — never a fabricated composition proof.


@dataclass(frozen=True, slots=True)
class CompositionResult:
    """``check_composition``'s verdict: the cross-hop composition is provably sound, or the
    canonical (ordered + deduped) reason codes saying why it is not — only ever
    ``aggregation_composition_unsupported`` from this check."""

    composable: bool
    reason_codes: tuple[ReasonCode, ...]


def _stage_composes_by_sum(stage: IngredientAggregationV1) -> bool:
    """Is this stage's OUTPUT provably re-aggregable by SUM at the later fan-in hops? Provable
    ONLY as: an ``additive`` measure aggregated by SUM — declared, or the versioned additive
    auto-rule (declared None) — and individually sound (a stage on a cardinality-unavailable hop
    is NOT: its fan-in is unproven, so its output's shape is too). SUM of an additive measure is
    itself additive, so SUM∘SUM composes; every other (additivity, function) pair — an averaging/
    latest/count intermediate, a semi-additive or non-additive or unknown input — has no
    composition proof and fails closed."""
    return (stage.additivity is AdditivityClass.additive
            and (stage.declared_function is None
                 or stage.declared_function is AggregationFunction.sum)
            and stage.validation is AggregationValidation.sound)


def _grouping_survives(earlier: HopAggregationV1, later: HopAggregationV1) -> bool:
    """Does the earlier fan-in hop's grouping provably survive into the later one? Provable ONLY
    as: both hops actually grouped somewhere known (non-empty grouping keys + execution table —
    a cardinality-unavailable hop has neither), the entity axis is continuous (the earlier hop's
    output grain IS the later hop's from-side entity; an intervening skipped hop breaks the chain
    and honestly fails), and both execute in the SAME catalog — an intra-catalog realized chain
    carries the group rows table-to-table by construction. A bridge crossing (the execution
    catalog changes) is NEVER confirmable from hop evidence alone (``HopAggregationV1`` carries
    no from-side keys for the later hop), so it fails closed."""
    if not (earlier.grouping_keys and earlier.execution_table
            and later.grouping_keys and later.execution_table):
        return False
    if not earlier.to_entity or earlier.to_entity != later.from_entity:
        return False
    return earlier.execution_catalog == later.execution_catalog


def check_composition(
        hop_aggregations: tuple[HopAggregationV1, ...],
        output_additivity: AdditivityClass) -> CompositionResult:
    """§4.2 — is the composition ACROSS the path's fan-in hops provably sound? Pure over C4's
    output tuples plus the recipe's declared OUTPUT additivity (``template.additivity`` through
    ``to_additivity_class`` — F13); no context, no connection, no expression algebra.

    Zero or one fan-in hop composes trivially: nothing crosses a hop boundary, and a single
    hop's aggregation — e.g. the SUM(interest)/SUM(principal) ratio — is C4's per-ingredient
    concern, never a composition failure. With two or more, every measure staged BEFORE the last
    fan-in hop flows downstream and must compose: provably sound ONLY as an additive SUM stage
    whose grouping survives every remaining hop boundary (SUM∘SUM). Anything else — a non-SUM or
    non-additive/semi-additive/unknown intermediate re-aggregated downstream (average-of-average),
    a bridge crossing or broken entity chain (grouping unconfirmable), or a pure-SUM chain whose
    declared OUTPUT is not additive (F13: an intended-but-undeclared ratio/rate) →
    ``aggregation_composition_unsupported``. Deterministic: hops ordered by ``segment_index``;
    codes canonical + deduped."""
    hops = sorted(hop_aggregations, key=lambda h: h.segment_index)
    if len(hops) <= 1:
        return CompositionResult(composable=True, reason_codes=())

    survives = [_grouping_survives(hops[k], hops[k + 1]) for k in range(len(hops) - 1)]
    codes: list[ReasonCode] = []
    any_cross_hop = False
    for i, hop in enumerate(hops[:-1]):     # a stage at the LAST fan-in hop flows nowhere further
        for stage in hop.ingredient_stages:
            any_cross_hop = True
            if not (_stage_composes_by_sum(stage) and all(survives[i:])):
                codes.append(ReasonCode.aggregation_composition_unsupported)
    # F13 output cross-check: a chain that composed purely by SUM must DECLARE an additive
    # output; a non-additive/semi-additive/unknown/n-a output over it is a ratio/rate the recipe
    # intends but never declared as algebra — not provably the intended output, fail closed.
    # (When a chain already failed above, the code dedups to the same honest verdict.)
    if any_cross_hop and output_additivity is not AdditivityClass.additive:
        codes.append(ReasonCode.aggregation_composition_unsupported)
    reason_codes = canonical_reason_codes(codes)
    return CompositionResult(composable=len(reason_codes) == 0, reason_codes=reason_codes)
