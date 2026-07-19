"""Phase 3C.2b-i-A · Task 1 — the SPIKE helpers proving the reuse premise.

Three thin adapters over the EXISTING single-source frontier + per-path compiler, so a
multi-source operand's roll-up to a common landing reuses the governed machinery unchanged:

* :func:`injected_operand_template` — a one-need (measure/counted/time) injected ``Template`` for
  a single pinned operand column, plus a SECOND bound temporal need when an ``anchor_concept`` is
  given (the anchor must be a bound need, not a bare string, so the reused ``compile_temporal`` can
  validate ``take_latest`` — ``declarations.py``).
* :func:`build_operand_context` — A's OWN ``CompilerContext``. It mirrors the production
  ``build_compiler_context`` field-for-field EXCEPT it supplies a NON-empty ``agg_declarations``
  keyed by ``(injected_recipe_id, need_role)`` (production hard-codes ``{}``) and scopes columns by
  the caller's ``roles`` covering every operand/anchor/key column.
* :func:`run_operand_rollup` — drives ``semantic_rollup_paths`` + ``assemble_paths`` from a
  hand-built ``_Position`` (NOT ``_assemble_rollups``, which derives the source from a
  ``SOURCE_ENTITY_KEY`` need and returns empty for a single-measure injected template) and returns
  the first RESOLVED cross-catalog ``BindingPlanV1`` the frontier produces.

Read-only over the frontier; ``assemble_paths``/``compile_*`` are CALLED, never edited (the §12
behaviour-neutrality golden test).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.config import overlay_config_from_env
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.catalog_realizations import (
    derive_catalog_realizations,
    realization_fingerprint,
)
from featuregen.overlay.upload.planner.assembly import (
    _Position,
    assemble_paths,
    semantic_rollup_paths,
)
from featuregen.overlay.upload.planner.contracts import (
    BindingPlanV1,
    CatalogScopeV1,
    IngredientBindingV1,
    PlanResolutionStatus,
)
from featuregen.overlay.upload.planner.declarations import (
    AggregationDeclarationRegistry,
    CompilerContext,
    bridge_fingerprint,
)
from featuregen.overlay.upload.templates import Need, Template, _load_columns


def injected_operand_template(
        *, recipe_id: str, need_role: str, concept: str, source_entity: str,
        anchor_concept: str | None = None) -> Template:
    """A synthetic ``Template`` for ONE pinned operand column: a required ``MEASURE`` need for the
    operand concept, plus (when ``anchor_concept`` is set) a SECOND bound ``TIME`` need with an
    explicit ``AS_OF_TIME`` temporal role so the reused ``compile_temporal`` finds the ordering
    anchor and ``_take_latest`` can validate. ``id=recipe_id`` keys A's ``agg_declarations``; the
    output ``additivity``/``aggregation`` labels are placeholders (this template is never grounded,
    only compiled)."""
    needs: list[Need] = [Need(role=need_role, concept=concept, join_role=JoinRole.MEASURE)]
    if anchor_concept is not None:
        needs.append(Need(role=f"{need_role}_anchor", concept=anchor_concept,
                          join_role=JoinRole.TIME, temporal_role=TemporalRole.AS_OF_TIME))
    return Template(
        id=recipe_id, family="multisource", intent="injected multi-source operand roll-up",
        needs=tuple(needs), params={}, aggregation="sum", additivity="additive", explain="M",
        use_cases=(), pit="trailing", source_entity=source_entity, source_entity_need_role=None)


def build_operand_context(
        conn, *, catalogs: Iterable[str], roles: Iterable[str], now: datetime,
        agg_declarations: AggregationDeclarationRegistry) -> CompilerContext:
    """A's OWN per-run ``CompilerContext`` — the production ``build_compiler_context`` loads with a
    HARD-CODED empty ``agg_declarations`` (``declarations.py``); A supplies a POPULATED registry so
    the reused ``compile_aggregation`` validates declared (e.g. ``take_latest``) strategies instead
    of resolving everything ``undeclared``. Every other field is batch-loaded the same way
    (realizations, active bridges, read-scoped columns, scope-start fingerprints). ``catalog_stamps``
    is empty (the freshness observation isn't part of the reuse chain); ``config`` comes from the
    deployment env loader. Immutable + conn-free once built."""
    roles = tuple(roles)
    catalogs = tuple(catalogs)
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
        catalog_stamps={},
        config=overlay_config_from_env(),
        roles=roles,
        now=now,
        agg_declarations=dict(agg_declarations))


def run_operand_rollup(
        conn, ctx: CompilerContext, *, source_position: _Position, target_entity: str,
        template: Template, scope: CatalogScopeV1,
        ingredient_bindings: tuple[IngredientBindingV1, ...]) -> BindingPlanV1 | None:
    """Enumerate governed paths from the hand-built ``source_position`` to ``target_entity`` and
    return the first RESOLVED cross-catalog ``BindingPlanV1`` the frontier produces (or ``None`` when
    no governed path resolves). ``ctx`` is A's compiler context — carried for the caller's per-path
    compile; the frontier itself reads the connection directly (``assemble_paths`` is conn-bound)."""
    del ctx     # the frontier reads `conn`; A's context is consumed by the per-path compile pass
    paths, _status = semantic_rollup_paths(source_position.entity, target_entity)
    for path in paths:
        assembly = assemble_paths(
            conn, source_position=source_position, semantic_path=path, scope=scope,
            ingredient_bindings=ingredient_bindings, template=template,
            target_entity=target_entity)
        for plan in assembly.complete:
            if plan.resolution_status is PlanResolutionStatus.resolved:
                return plan
    return None
