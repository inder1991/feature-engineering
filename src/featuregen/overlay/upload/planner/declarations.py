"""Phase-3B.3c C2 — the contract compiler's data spine + the first declaration check.

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
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.bridge_projection import ActiveBridgeV1
from featuregen.overlay.upload.catalog_realizations import table_of
from featuregen.overlay.upload.planner.contracts import (
    AggregationFunction,
    BindingPlanV1,
    CatalogStateStampV1,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import CatalogEntityRelationshipV1
from featuregen.overlay.upload.templates import _Col

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
