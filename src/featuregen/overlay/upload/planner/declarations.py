"""Phase-3B.3c C2–C8 — the contract compiler: the data spine, the declaration checks
(connectivity; the temporal declaration, which runs FIRST in the compile pipeline; aggregation;
composition; the physical-read set; freshness), the §10 precedence fold (``compile_contract``),
and the per-run batched context builder (``build_compiler_context``).

One IMMUTABLE, conn-free ``CompilerContext`` is batch-loaded per shadow run; every declaration
check is a pure function over that context and a plan. The impure boundary (F8) is deliberately
tiny: ``revalidate_freshness`` (with its ``bridge_fingerprint`` helper) takes a connection
because freshness is an OBSERVATION of current state, never a declaration — and
``build_compiler_context`` reads once to snapshot everything else. ``CompileBudget`` is the ONE
deliberately mutable exception: the per-run compile allowance owned by ``run_shadow_planner``
(C8), decremented by ``plan_bindings``' compile pass. Consumed ONLY by the shadow path behind
the route-read ``FEATUREGEN_INTENT_CONTRACT_COMPILE`` kill-switch (default off)."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from featuregen.overlay.catalog_changes import drift_head_seq, drift_watermark
from featuregen.overlay.config import OverlayConfig, overlay_config_from_env
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.bridge_projection import ActiveBridgeV1, active_bridges
from featuregen.overlay.upload.catalog_realizations import (
    derive_catalog_realizations,
    realization_fingerprint,
    table_of,
)
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.need_metadata import ResolvedNeedMetadataV1, derive_need_metadata
from featuregen.overlay.upload.planner.contracts import (
    ADDITIVITY_RULE_VERSION,
    AGGREGATION_RULE_VERSION,
    DRIFT_FRESHNESS_SLA_VERSION,
    PLANNER_BOUNDS_VERSION,
    RANKING_VERSION,
    SAFETY_EVALUATOR_VERSION,
    TEMPORAL_RULE_VERSION,
    AdditivityClass,
    AdditivityProvenanceV1,
    AdditivitySource,
    AggregationAxisKind,
    AggregationFunction,
    AggregationValidation,
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingSafety,
    CatalogScopeV1,
    CatalogStateStampKind,
    CatalogStateStampV1,
    ColumnRole,
    ContractResolutionStatus,
    DeclarationStatus,
    HopAggregationV1,
    IngredientAggregationV1,
    IngredientBindingV1,
    ParamBindingV1,
    PathResolutionStatus,
    PhysicalColumnReadV1,
    PhysicalReadSetV1,
    PlannerReplayEnvelopeV1,
    ReasonCode,
    ReplayStrength,
    SegmentKind,
    StampConsistency,
    TemporalDeclarationV1,
    WindowSpecV1,
    canonical_reason_codes,
    make_contract_id,
    to_additivity_class,
)
from featuregen.overlay.upload.planner.fingerprint import compiler_input_fingerprint
from featuregen.overlay.upload.planner.safety import evaluate_column_safety
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
)
from featuregen.overlay.upload.templates import Template, _Col, _load_columns
from featuregen.projections.runner import _checkpoint_seq

# The declared-function registry: ``(recipe_id, need_role) ->`` the recipe's DECLARED aggregation
# function. Populated in production from the durable ``recipe_aggregation_declaration`` registry
# (H3a) by ``load_aggregation_declarations``; empty until that table has active rows (validate,
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
    # H3a: the (recipe_id, need_role) keys whose durable registry has >= 2 ACTIVE declarations —
    # a governance CONFLICT the compiler must fail (never a silent pick). Empty in production until
    # a conflict exists; empty-registry runs are byte-identical to the pre-H3a compiler.
    agg_declaration_conflicts: frozenset[tuple[str, str]] = frozenset()

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
        object.__setattr__(self, "agg_declaration_conflicts",
                           frozenset(self.agg_declaration_conflicts))


@dataclass(slots=True)
class CompileBudget:
    """DELIBERATELY MUTABLE (the one exception to the frozen convention): the per-run compile
    allowance — a remaining-plan count and a MONOTONIC-clock deadline — owned by ``run_shadow_planner``
    and decremented by ``plan_bindings``' compile pass (C8). A run past either bound records
    ``compile_budget_exhausted`` instead of compiling further plans.

    D6/F17: the deadline is a REAL elapsed-time bound over an injected ``clock`` (default
    ``time.monotonic`` in the caller), NOT the deterministic ``now`` — so a compile pass that genuinely
    overruns is truncated. Because the wall-clock changes the observed result, ``deadline_monotonic``/
    ``clock`` NEVER enter a hash or verdict; instead a budget-truncated (incomplete) execution is
    EXCLUDED from deterministic identity comparisons. ``stopped_by_time`` remembers WHICH bound fired
    FIRST (None until one does, then True=deadline / False=count) so the store labels
    ``budget_time`` vs ``budget_count``."""

    remaining: int
    deadline_monotonic: float
    clock: Callable[[], float]
    stopped_by_time: bool | None = None


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


def _ordering_column_available(
        hop_seg_idx: int, exec_cat: str, exec_table: str, anchor_pos: PathPositionV1 | None,
        fanin_hops: Iterable[tuple[int, int, object, object, object, object, str, str]]) -> bool:
    """Is the temporal ORDERING column provably present, at ROW grain, at this fan-in hop — the
    precondition ``take_latest`` needs (F14: ``anchor_binding is not None`` alone is NOT sufficient).
    Fail-closed — proven ONLY when the anchor is bound and on-path (``anchor_pos``), sits STRICTLY
    BEFORE this hop IN THE SAME EXECUTION CATALOG on the many-side rows — never the hop's grouped
    OUTPUT/to-side table (``check_connectivity`` places a to-side table AT ``hop_seg_idx``, so an
    anchor there is already collapsed to the target grain) and never a bridge crossing — and NO
    fan-in hop between the anchor's position and this hop already grouped the rows away. Everything
    else fails closed."""
    if anchor_pos is None or anchor_pos.catalog_source != exec_cat:
        return False
    if anchor_pos.segment_index >= hop_seg_idx or anchor_pos.table == exec_table:
        return False    # on the hop's grouped to-side grain (or after it) — not row grain
    return not any(anchor_pos.segment_index <= h[1] < hop_seg_idx for h in fanin_hops)


def _take_latest_validation(ordering_available: bool
                            ) -> tuple[AggregationValidation, ReasonCode | None, tuple[str, ...]]:
    """``take_latest`` is sound ONLY with a proven temporal ordering column at the hop (F14); without
    one it is honestly inputs_missing, never a silent latest-of-nothing."""
    if ordering_available:
        return AggregationValidation.sound, None, ()
    return AggregationValidation.inputs_missing, ReasonCode.aggregation_ordering_column_missing, ()


def _validate_stage(
        selected: AdditivityClass, declared: AggregationFunction | None,
        time_axis_aggregating: bool, need_role: str, bound_roles: frozenset[str],
        ordering_available: bool,
) -> tuple[AggregationValidation, ReasonCode | None, tuple[str, ...]]:
    """The §4 validation matrix for ONE measure stage on a fan-in hop:
    (validation, matrix reason, missing_inputs). ``ordering_available`` (F14) is the proven presence
    of the temporal ordering column at this hop — required for a ``take_latest`` stage to be sound."""
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
        if declared in _ORDER_SAFE_DECLARED:
            return AggregationValidation.sound, None, ()
        if declared is AggregationFunction.take_latest:
            return _take_latest_validation(ordering_available)
        return _validate_declared_inputs(declared, need_role, bound_roles)
    # non_additive: no sound default exists — everything must be declared and provable
    if declared is None:
        return AggregationValidation.undeclared, ReasonCode.aggregation_strategy_missing, ()
    if declared is AggregationFunction.sum:
        return (AggregationValidation.incompatible,
                ReasonCode.aggregation_incompatible_with_additivity, ())
    if declared in _ORDER_SAFE_DECLARED:
        return AggregationValidation.sound, None, ()
    if declared is AggregationFunction.take_latest:
        return _take_latest_validation(ordering_available)
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
    # F14: where the temporal ORDERING column enters the path (the bound anchor's placement) — the
    # take_latest guard proves it survives, at row grain, to each aggregation hop.
    none_temporal = str(TemporalRole.NONE)
    anchor_pos: PathPositionV1 | None = None
    if temporal.anchor_binding is not None:
        anchor_role = next(
            (b.need_role for b in plan.ingredient_bindings
             if b.bound_object_ref == temporal.anchor_binding
             and b.temporal_role and b.temporal_role != none_temporal), None)
        if anchor_role is not None:
            anchor_pos = placement.get(anchor_role)
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
        key = (plan.recipe_id, b.need_role)
        declared = ctx.agg_declarations.get(key)
        codes: list[ReasonCode] = []
        missing: tuple[str, ...] = ()
        if key in ctx.agg_declaration_conflicts:
            # H3a: >= 2 active durable declarations for this key — a governance conflict, never a
            # silent pick. Fails the stage regardless of additivity/cardinality (fail-closed).
            validation = AggregationValidation.incompatible
            codes.append(ReasonCode.aggregation_declaration_conflict)
        elif card is None:
            validation = AggregationValidation.undeclared   # can't validate an unknown fan-in
            codes.append(ReasonCode.physical_cardinality_unavailable)
        else:
            ordering_available = _ordering_column_available(hop[1], hop[6], hop[7], anchor_pos, hops)
            validation, matrix_reason, missing = _validate_stage(
                provenance.selected, declared, temporal.time_axis_aggregating,
                b.need_role, bound_roles, ordering_available)
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


# ─── C6: the physical-read set + reason-bearing universal safety ──────────────────────────────
#
# UNIVERSAL safety only (F13): leakage anchors + protected/special attributes — the concerns that
# hold for EVERY caller, evaluated by safety.evaluate_column_safety (parity-locked to
# _safe_to_bind). PII/read-scope is AUTHORIZATION, already enforced by the read-scoped column
# load, and is never re-gated here. The read set inventories EVERY column the contract would
# read — ingredients AND join/bridge keys AND temporal anchors — because a leakage anchor read
# through a JOIN KEY leaks exactly as much as one read through an ingredient.

# JoinRole values that make an ingredient's bound column a physical JOIN-KEY read.
_KEY_JOIN_ROLES = frozenset(str(r) for r in (
    JoinRole.SOURCE_ENTITY_KEY, JoinRole.TARGET_ENTITY_KEY, JoinRole.INTERMEDIATE_ENTITY_KEY))


def safety_of_ref(ctx: CompilerContext, catalog_source: str,
                  object_ref: str) -> tuple[BindingSafety, ReasonCode | None]:
    """One physical ref's universal safety. A ref with no loaded ``_Col`` (a bare bridge/join key
    the read-scoped column load never saw) is STRUCTURALLY ``not_evaluated`` +
    ``safety_evaluation_incomplete`` — an honest evidence gap, NOT a safety violation, and never
    silently safe. Pure over ``ctx`` — no connection."""
    col = ctx.columns_by_catalog.get(catalog_source, {}).get(object_ref)
    if col is None:
        return BindingSafety.not_evaluated, ReasonCode.safety_evaluation_incomplete
    return evaluate_column_safety(col)


def build_physical_read_set(ctx: CompilerContext, plan: BindingPlanV1) -> PhysicalReadSetV1:
    """The immutable inventory of every column the plan's contract would read, MULTI-ROLE: each
    ingredient's bound column (+ ``join_key`` when its join_role is an entity-key role, +
    ``temporal_anchor`` when it carries a real temporal role), each path realization's from/to
    key (``join_key``), and each bridge segment's BOTH endpoint columns (``bridge_key``).
    Duplicate ``(catalog, object_ref)`` reads merge into ONE ``PhysicalColumnReadV1`` with the
    UNION of roles; per-column safety + reason from :func:`safety_of_ref`. A segment whose
    realization/bridge ref the context cannot resolve contributes no reads — that plan already
    fails C2 connectivity, fail-closed there. Deterministic: columns sorted by
    ``(catalog_source, object_ref)``, roles value-sorted + deduped. Pure over ``ctx``."""
    none_temporal = str(TemporalRole.NONE)
    roles_of: dict[tuple[str, str], set[ColumnRole]] = {}

    def _read(catalog: str, ref: str, role: ColumnRole) -> None:
        roles_of.setdefault((catalog, ref), set()).add(role)

    for b in plan.ingredient_bindings:
        _read(b.bound_catalog_source, b.bound_object_ref, ColumnRole.ingredient)
        if b.join_role in _KEY_JOIN_ROLES:
            _read(b.bound_catalog_source, b.bound_object_ref, ColumnRole.join_key)
        if b.temporal_role and b.temporal_role != none_temporal:
            _read(b.bound_catalog_source, b.bound_object_ref, ColumnRole.temporal_anchor)
    for seg in plan.path_segments:
        if seg.realization_ref is not None:
            r = next((x for x in ctx.realizations_by_catalog.get(seg.catalog_source, ())
                      if x.realization_id == seg.realization_ref), None)
            if r is not None:
                _read(seg.catalog_source, r.from_key_ref, ColumnRole.join_key)
                _read(seg.catalog_source, r.to_key_ref, ColumnRole.join_key)
        elif seg.bridge_fact_key is not None:
            br = next((x for x in ctx.active_bridges if x.fact_key == seg.bridge_fact_key), None)
            if br is not None:
                _read(br.left_catalog_source, br.left_object_ref, ColumnRole.bridge_key)
                _read(br.right_catalog_source, br.right_object_ref, ColumnRole.bridge_key)

    columns: list[PhysicalColumnReadV1] = []
    for catalog, ref in sorted(roles_of):
        safety, reason = safety_of_ref(ctx, catalog, ref)
        columns.append(PhysicalColumnReadV1(
            object_ref=ref, catalog_source=catalog,
            roles=tuple(sorted(roles_of[(catalog, ref)])),
            safety=safety, reason_codes=(reason,) if reason is not None else ()))
    return PhysicalReadSetV1(columns=tuple(columns))


def stage_safety(read_set: PhysicalReadSetV1) -> tuple[BindingSafety, tuple[ReasonCode, ...]]:
    """Fold the per-column verdicts into ONE stage verdict: any ``unsafe`` column → ``unsafe``
    with ALL unsafe columns' reason codes (canonical order); else any structural gap →
    ``not_evaluated`` (incomplete evidence is NEVER safe); else ``safe``. The caller (C8) maps
    unsafe → ``safety_rejected`` and not_evaluated → ``unresolved_safety_evaluation``."""
    unsafe = [col for col in read_set.columns if col.safety is BindingSafety.unsafe]
    if unsafe:
        return BindingSafety.unsafe, canonical_reason_codes(
            code for col in unsafe for code in col.reason_codes)
    if any(col.safety is BindingSafety.not_evaluated for col in read_set.columns):
        return BindingSafety.not_evaluated, (ReasonCode.safety_evaluation_incomplete,)
    return BindingSafety.safe, ()


# ─── C7: freshness (the ONE impure boundary) + fingerprint consistency + audit envelope ───────
#
# Freshness is an OBSERVATION of current state, never a declaration (F7: it is excluded from
# contract_id), so revalidate_freshness — and its bridge_fingerprint helper — are the ONLY
# functions in the whole compiler that take a connection (F8). The fingerprint recheck (F9/F11)
# exists because head_seq is INSUFFICIENT: a graph rebuild (re-upload, column add/drop) rewrites
# graph_node/graph_edge WITHOUT moving the drift watermark, so only comparing the
# realization/bridge fingerprints taken at scope-start against compile-end state catches a
# mutation mid-compile. audit_envelope stays pure — evidence in, envelope out.


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    """``revalidate_freshness``'s verdict: the freshness-axis status (``resolved`` or
    ``unresolved_freshness`` — NEVER a declaration status), the canonical observation reason
    codes, one ``CatalogStateStampV1`` per participating catalog (honestly empty when no
    watermark exists — never fabricated), and whether the scope-start fingerprints HELD to
    compile-end. Observation-time evidence only: none of this enters contract_id (F7)."""

    status: ContractResolutionStatus
    reason_codes: tuple[ReasonCode, ...]
    stamps: tuple[CatalogStateStampV1, ...]
    stamp_consistency: StampConsistency


def bridge_fingerprint(conn) -> str:
    """A deterministic hash of the CURRENT active-bridge fact-set (the VERIFIED projected
    crossings). Taken once at scope-start by C8's context builder (``bridge_fingerprint_at_start``)
    and recomputed at compile-end by :func:`revalidate_freshness` — a bridge verified, rejected,
    or expired mid-compile changes the set and fails the consistency recheck. Impure (reads the
    projection); order-insensitive (sorted fact keys)."""
    material = "|".join(sorted(b.fact_key for b in active_bridges(conn)))
    return hashlib.sha256(material.encode()).hexdigest()


def revalidate_freshness(conn, ctx: CompilerContext, plan: BindingPlanV1) -> FreshnessResult:
    """The compile-END freshness + consistency observation over ``plan.participating_catalogs``
    — the ONE impure check (F8). Per catalog: no drift watermark → ``freshness_stamp_unavailable``;
    ``ctx.now - watermark > drift_freshness_sla`` → ``participating_catalog_stale``; the overlay
    projection checkpoint behind the drift head_seq → ``projection_lagging`` (a just-staled fact
    may not be applied to the read model yet). Consistency (F9/F11): a catalog whose
    ``realization_fingerprint`` — or the active-bridge fact-set — changed since the ctx snapshot
    → ``catalog_mutated_during_compile`` + ``stamp_consistency=unverifiable``; head_seq alone
    CANNOT catch this (a graph rebuild never moves the drift watermark). Every catalog is
    stamped with what was actually observed. Reason codes canonical; deterministic given state."""
    codes: list[ReasonCode] = []
    stamps: list[CatalogStateStampV1] = []
    consistency = StampConsistency.consistent
    checkpoint = _checkpoint_seq(conn, "overlay")
    for src in plan.participating_catalogs:
        wm = drift_watermark(conn, src)
        head = drift_head_seq(conn, src)
        if wm is None:
            codes.append(ReasonCode.freshness_stamp_unavailable)
        elif (ctx.now - wm) > ctx.config.drift_freshness_sla:
            codes.append(ReasonCode.participating_catalog_stale)
        if head is not None and checkpoint < head:
            codes.append(ReasonCode.projection_lagging)
        if realization_fingerprint(conn, src) != ctx.catalog_fingerprint_at_start.get(src):
            codes.append(ReasonCode.catalog_mutated_during_compile)
            consistency = StampConsistency.unverifiable
        stamps.append(CatalogStateStampV1(
            catalog_source=src, head_seq=head or 0,
            last_completed_at=wm.isoformat() if wm is not None else "",
            stamp_kind=CatalogStateStampKind.drift_watermark,
            # 3B.4 (F3/F14): the compile-time replay drift signal — the compiler-input fingerprint (covers
            # the classifier's real read-set, which realization_fingerprint omits) + the projection
            # checkpoint (a LAG invariant). The scope.py pre-compile stamp keeps defaults (no ctx there).
            compiler_input_fingerprint=compiler_input_fingerprint(ctx, src),
            projection_checkpoint=checkpoint))
    if bridge_fingerprint(conn) != ctx.bridge_fingerprint_at_start:
        codes.append(ReasonCode.catalog_mutated_during_compile)
        consistency = StampConsistency.unverifiable
    reason_codes = canonical_reason_codes(codes)
    status = (ContractResolutionStatus.unresolved_freshness if reason_codes
              else ContractResolutionStatus.resolved)
    return FreshnessResult(status=status, reason_codes=reason_codes, stamps=tuple(stamps),
                           stamp_consistency=consistency)


def recipe_content_hash(template: Template) -> str:
    """The STABLE canonical hash of the template's identity — id, family, intent, the sorted
    ``(role, concept)`` need pairs, and the sorted params with their full allowed-value tuples.
    Deterministic across runs and construction order (needs-tuple order and params-dict insertion
    order are canonicalized away); pure. Pinned on the audit envelope so a replay can prove WHICH
    recipe content the contract was compiled against (§9)."""
    needs = ";".join(f"{n.role}:{n.concept}"
                     for n in sorted(template.needs, key=lambda n: (n.role, n.concept)))
    params = ";".join(f"{name}={','.join(str(v) for v in values)}"
                      for name, values in sorted(template.params.items()))
    material = f"{template.id}|{template.family}|{template.intent}|{needs}|{params}"
    return "rh_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def audit_envelope(ctx: CompilerContext, plan: BindingPlanV1, template: Template,
                   base_envelope: PlannerReplayEnvelopeV1,
                   stamps: tuple[CatalogStateStampV1, ...],
                   stamp_consistency: StampConsistency) -> PlannerReplayEnvelopeV1:
    """The §9 audit envelope: the planner-time base extended with the full compiler rule-version
    set, the canonical recipe content hash, the caller's sorted+deduped role claims, the
    compile-end catalog state stamps, and their consistency verdict. Pure — evidence in, envelope
    out. ``replay_strength`` is pinned ``audit_only``: drift watermarks CORRELATE state for audit;
    they never permit deterministic re-execution (no row-level snapshot exists)."""
    del plan    # uniform compile-step signature; the plan carries its own evidence fields
    return replace(
        base_envelope,
        aggregation_rule_version=AGGREGATION_RULE_VERSION,
        additivity_rule_version=ADDITIVITY_RULE_VERSION,
        temporal_rule_version=TEMPORAL_RULE_VERSION,
        safety_evaluator_version=SAFETY_EVALUATOR_VERSION,
        drift_freshness_sla_version=DRIFT_FRESHNESS_SLA_VERSION,
        planner_bounds_version=PLANNER_BOUNDS_VERSION,
        ranking_version=RANKING_VERSION,
        recipe_content_hash=recipe_content_hash(template),
        authz_role_claims=tuple(sorted(set(ctx.roles))),
        catalog_state_stamps=stamps,
        stamp_consistency=stamp_consistency,
        replay_strength=ReplayStrength.audit_only)


# ─── C8: the §10 precedence fold + compile_contract + the per-run batched context builder ─────
#
# compile_contract runs the C2–C7 checks in pipeline order and FOLDS: declaration_status is the
# freshness-FREE, identity-bearing verdict (hashed into contract_id); contract_resolution_status
# additionally folds the freshness OBSERVATION in. Only a source_to_target_resolved plan is ever
# compiled — every other plan stays honestly not_compiled. build_compiler_context is the third
# (and last) sanctioned impure function: it exists precisely to batch every read ONCE per run.

# The temporal reason codes that BLOCK the declaration (an anchor problem, not a mere annotation).
_TEMPORAL_BLOCKING_CODES = frozenset(
    {ReasonCode.temporal_anchor_missing, ReasonCode.temporal_anchor_ambiguous})

# Declaration-blocking aggregation evidence — the C4 HANDOFF: a stage can be validation=sound yet
# carry additivity_source_conflict in its reason_codes (contract_id hashes stage VALIDATION, never
# stage reason codes), so the fold must read stage.reason_codes and promote ANY of these to
# unresolved_aggregation_declaration or the conflict would be invisible to the contract identity.
_AGGREGATION_BLOCKING_CODES = frozenset({
    ReasonCode.aggregation_strategy_missing,
    ReasonCode.aggregation_incompatible_with_additivity,
    ReasonCode.aggregation_weight_missing,
    ReasonCode.aggregation_components_missing,
    ReasonCode.aggregation_ordering_column_missing,
    ReasonCode.aggregation_axis_unsupported,
    ReasonCode.aggregation_composition_unsupported,
    ReasonCode.semi_additive_temporal_strategy_missing,
    ReasonCode.additivity_source_conflict,
    ReasonCode.physical_cardinality_unavailable,
    ReasonCode.aggregation_declaration_conflict,
})


def _strongest(codes: Iterable[ReasonCode]) -> ReasonCode | None:
    """The first code in canonical (registry) order — the failing level's primary."""
    ordered = canonical_reason_codes(codes)
    return ordered[0] if ordered else None


def compile_contract(conn, ctx: CompilerContext, plan: BindingPlanV1, template: Template, *,
                     base_envelope: PlannerReplayEnvelopeV1) -> BindingPlanV1:
    """Run the declaration checks in pipeline order over ONE source_to_target_resolved plan and
    fold the verdicts (§10). Any other plan is returned UNTOUCHED (honest not_compiled — the
    contract axes only ever describe an executable source→target path).

    The fold: declaration_status = the §10 precedence MINUS freshness — connectivity →
    safety_rejected → safety-evaluation gap → temporal → aggregation — first failing wins;
    contract_resolution_status additionally folds the freshness observation (C7) in at rank 6.
    contract_primary_reason_code is the strongest reason by the FULL precedence;
    contract_reason_codes preserves EVERY check's codes (canonical order, deduped).
    contract_id is minted over the DECLARATION material only (F7: the freshness codes are
    excluded inside make_contract_id), so a stale recompile keeps its identity. The one impure
    step is revalidate_freshness (the sanctioned conn use); everything else is pure over ctx."""
    if plan.path_resolution_status is not PathResolutionStatus.source_to_target_resolved:
        return plan

    connectivity = check_connectivity(ctx, plan)
    temporal = compile_temporal(ctx, plan, template)
    hop_aggregations = compile_aggregation(ctx, plan, template, temporal, connectivity.placement)
    composition = check_composition(hop_aggregations, to_additivity_class(template.additivity))
    read_set = build_physical_read_set(ctx, plan)
    safety_verdict, safety_codes = stage_safety(read_set)
    freshness = revalidate_freshness(conn, ctx, plan)

    connectivity_codes = tuple(
        ReasonCode.ingredient_not_connected_to_path for _ in connectivity.disconnected_roles)
    stage_codes = tuple(code for h in hop_aggregations for s in h.ingredient_stages
                        for code in s.reason_codes)
    aggregation_codes = canonical_reason_codes(stage_codes + composition.reason_codes)
    aggregation_sound = (
        composition.composable
        and all(s.validation is AggregationValidation.sound
                for h in hop_aggregations for s in h.ingredient_stages)
        and not any(code in _AGGREGATION_BLOCKING_CODES for code in aggregation_codes))
    temporal_blocking = tuple(
        code for code in temporal.reason_codes if code in _TEMPORAL_BLOCKING_CODES)

    if not connectivity.connected:
        declaration = DeclarationStatus.unresolved_ingredient_connectivity
        primary: ReasonCode | None = ReasonCode.ingredient_not_connected_to_path
    elif safety_verdict is BindingSafety.unsafe:
        declaration = DeclarationStatus.safety_rejected
        primary = _strongest(safety_codes)
    elif safety_verdict is BindingSafety.not_evaluated:
        declaration = DeclarationStatus.unresolved_safety_evaluation
        primary = _strongest(safety_codes)
    elif temporal_blocking:
        declaration = DeclarationStatus.unresolved_temporal_declaration
        primary = _strongest(temporal_blocking)
    elif not aggregation_sound:
        declaration = DeclarationStatus.unresolved_aggregation_declaration
        primary = _strongest(aggregation_codes)
    else:
        declaration = DeclarationStatus.resolved
        primary = None

    if declaration is DeclarationStatus.resolved:
        # freshness (rank 6) folds in ONLY once every declaration check passed
        contract_status = freshness.status
        if freshness.status is ContractResolutionStatus.unresolved_freshness:
            primary = _strongest(freshness.reason_codes)
    else:
        contract_status = ContractResolutionStatus(declaration.value)

    all_codes = canonical_reason_codes(
        connectivity_codes + temporal.reason_codes + stage_codes + composition.reason_codes
        + safety_codes + freshness.reason_codes)

    enriched = replace(
        plan,
        declaration_status=declaration,
        contract_resolution_status=contract_status,
        contract_primary_reason_code=primary,
        contract_reason_codes=all_codes,
        hop_aggregations=hop_aggregations,
        temporal_declaration=temporal,
        physical_read_set=read_set,
        audit_envelope=audit_envelope(ctx, plan, template, base_envelope,
                                      freshness.stamps, freshness.stamp_consistency),
        resolved_at_compilation=ctx.now)
    return replace(enriched,
                   contract_id=make_contract_id(enriched, resolved_at_compilation=ctx.now))


def aggregation_declaration_content_hash(
        recipe_id: str, need_role: str, function: str, declaration_version: int,
        authority: str) -> str:
    """The immutable content hash pinned on each durable declaration row (H3a) — over the
    identity-bearing fields (recipe_id, need_role, function, declaration_version, authority).
    Verified on read: a superuser-bypassed row mutation that changes any of these no longer matches
    the stored hash and is dropped fail-closed. Deterministic; fixed field order."""
    material = f"{recipe_id}|{need_role}|{function}|{declaration_version}|{authority}"
    return "ad_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def load_aggregation_declarations(
        conn, now: datetime,
) -> tuple[dict[tuple[str, str], AggregationFunction], frozenset[tuple[str, str]]]:
    """Load the ACTIVE recipe aggregation declarations from the durable ``recipe_aggregation_
    declaration`` registry (H3a) — the rows whose ``[effective_from, effective_to)`` interval
    contains ``now`` (``effective_to`` NULL = open). Returns ``(registry, conflicts)``:

      * ``registry`` — ``(recipe_id, need_role) -> AggregationFunction`` for every key with EXACTLY
        ONE active declaration; this is the ONLY source of a declared function (the compiler never
        infers one from the column).
      * ``conflicts`` — the keys with a CONFLICT: two or more overlapping active declarations, OR a
        row whose stored ``content_hash`` no longer matches its fields (tamper) / an unparseable
        function. A conflicted key is NEVER placed in the registry — never a silent pick; the
        compile fails it via ``ReasonCode.aggregation_declaration_conflict``.

    Deterministic (ORDER BY recipe_id, need_role, declaration_version); the ONE new sanctioned read,
    called once per run by :func:`build_compiler_context`."""
    rows = conn.execute(
        "SELECT recipe_id, need_role, function, declaration_version, authority, content_hash"
        " FROM recipe_aggregation_declaration"
        " WHERE effective_from <= %s AND (effective_to IS NULL OR effective_to > %s)"
        " ORDER BY recipe_id, need_role, declaration_version",
        (now, now)).fetchall()
    registry: dict[tuple[str, str], AggregationFunction] = {}
    conflicts: set[tuple[str, str]] = set()
    for recipe_id, need_role, function, version, authority, content_hash in rows:
        key = (recipe_id, need_role)
        if key in conflicts:
            continue    # already conflicted — stays conflicted, never resolved
        expected = aggregation_declaration_content_hash(
            recipe_id, need_role, function, version, authority)
        try:
            fn: AggregationFunction | None = AggregationFunction(function)
        except ValueError:
            fn = None
        if content_hash != expected or fn is None:
            registry.pop(key, None)     # tampered / unparseable — fail closed for this key
            conflicts.add(key)
            continue
        if key in registry:
            registry.pop(key, None)     # a SECOND active declaration — conflict, never a pick
            conflicts.add(key)
            continue
        registry[key] = fn
    return registry, frozenset(conflicts)


def build_compiler_context(conn, scope: CatalogScopeV1, roles: Iterable[str],
                           now: datetime) -> CompilerContext:
    """The PRODUCTION per-run context builder — batch-loads EVERYTHING the compiler reads ONCE
    per shadow run (the §11 guard: no per-plan re-query): the realizations, the active governed
    crossings, the READ-SCOPED columns, and the scope-start fingerprints the compile-end recheck
    revalidates. Impure by design (the sanctioned context-build reads); the returned context is
    immutable and conn-free. The config comes from the deployment env loader; agg_declarations is
    loaded from the durable ``recipe_aggregation_declaration`` registry (H3a) — the ACTIVE
    declarations at ``now``, exactly one per (recipe_id, need_role) or a fail-closed conflict.
    Validate, never fabricate: an empty registry stays empty (no governed rows), keeping the
    compiler byte-identical to the pre-registry behaviour."""
    roles = tuple(roles)
    catalogs = scope.authorized_catalog_sources
    agg_declarations, agg_conflicts = load_aggregation_declarations(conn, now)
    return CompilerContext(
        realizations_by_catalog={
            src: derive_catalog_realizations(conn, src).realizations for src in catalogs},
        active_bridges=active_bridges(conn),
        columns_by_catalog={
            src: {col.object_ref: col for col in _load_columns(conn, src, roles)}
            for src in catalogs},
        catalog_fingerprint_at_start={
            src: realization_fingerprint(conn, src) for src in catalogs},
        bridge_fingerprint_at_start=bridge_fingerprint(conn),
        catalog_stamps={s.catalog_source: s for s in scope.catalog_state_stamps},
        config=overlay_config_from_env(),
        roles=roles,
        now=now,
        agg_declarations=agg_declarations,
        agg_declaration_conflicts=agg_conflicts)
