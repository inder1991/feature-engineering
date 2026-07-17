"""Phase-3B.4 — replay fingerprints. Two INPUTS-ONLY identities + a separate OUTPUT hash:

  * ``planner_input_hash``  — the FULL candidate/ranking universe (every authorized column + realizations
    + scope-filtered bridges + roles + versions). Determines WHICH plan is selected → SELECTION stability.
  * ``contract_input_hash`` — the SELECTED plan's consumed inputs (its read-set columns + used realizations
    /bridges + the canonical physical path + recipe content + representative params + the relevant
    aggregation declarations + target entity + versions). Determines the VERDICT → VERDICT stability.
  * ``compiler_input_fingerprint`` — a PER-CATALOG hash over the classifier's real read-set (the ``_Col``
    fields — additivity/is_as_of/entity/sensitivity — that ``realization_fingerprint`` omits) + realizations.
    The replay drift signal persisted on the compile-time stamp.
  * ``declarations_output_hash`` — the OUTPUTS (declarations + verdict), hashed SEPARATELY so a bug that
    changes an output reads as instability, never as a changed input.

All INPUT hashes hash pre-classification state only. Pure over the compiler context (no DB).
"""
from __future__ import annotations

import dataclasses
import hashlib

from featuregen.overlay.upload.planner.contracts import (
    ADDITIVITY_RULE_VERSION,
    AGGREGATION_RULE_VERSION,
    PLAN_CONTRACT_VERSION,
    PLANNER_VERSION,
    READ_SCOPE_POLICY_VERSION,
    ROLE_RESOLUTION_VERSION,
    SAFETY_EVALUATOR_VERSION,
    TEMPORAL_RULE_VERSION,
    BindingPlanV1,
)
from featuregen.overlay.upload.planner.shadow_store import canonical_json
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION

# The version set every fingerprint pins — a producer/rule change is a distinct input identity.
_VERSIONS = (
    ("planner", PLANNER_VERSION), ("plan_contract", PLAN_CONTRACT_VERSION),
    ("aggregation", AGGREGATION_RULE_VERSION), ("additivity", ADDITIVITY_RULE_VERSION),
    ("temporal", TEMPORAL_RULE_VERSION), ("safety", SAFETY_EVALUATOR_VERSION),
    ("read_scope", READ_SCOPE_POLICY_VERSION), ("role_resolution", ROLE_RESOLUTION_VERSION),
    ("graph", GRAPH_VERSION),
)


def _hash(material: object) -> str:
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def _col_tuple(col: object) -> list:
    """The classifier-relevant fields of a _Col (what a verdict actually reads) — NOT just the join graph."""
    return [str(getattr(col, "object_ref", "")), str(getattr(col, "additivity", "")),
            bool(getattr(col, "is_as_of", False)), str(getattr(col, "entity", "")),
            str(getattr(col, "sensitivity", "")), str(getattr(col, "concept", "")),
            bool(getattr(col, "is_grain", False)), str(getattr(col, "data_type", ""))]


def _realization_tuple(r: object) -> list:
    return [str(getattr(r, "realization_id", "")), str(getattr(r, "declared_cardinality", "")),
            str(getattr(r, "from_object_ref", "")), str(getattr(r, "to_object_ref", "")),
            str(getattr(r, "from_key_ref", "")), str(getattr(r, "to_key_ref", ""))]


def _catalog_cols(ctx, catalog_source: str) -> list:
    cols = ctx.columns_by_catalog.get(catalog_source, {})
    return sorted((_col_tuple(c) for c in cols.values()), key=lambda t: t[0])


def _catalog_realizations(ctx, catalog_source: str) -> list:
    rs = ctx.realizations_by_catalog.get(catalog_source, ())
    return sorted((_realization_tuple(r) for r in rs), key=lambda t: t[0])


def _scoped_bridge_fact_keys(ctx, authorized: set[str]) -> list:
    return sorted(
        str(b.fact_key) for b in ctx.active_bridges
        if b.left_catalog_source in authorized and b.right_catalog_source in authorized)


def compiler_input_fingerprint(ctx, catalog_source: str) -> str:
    """Per-catalog replay drift signal over the classifier's real read-set + realizations + versions."""
    return _hash({"catalog": catalog_source, "cols": _catalog_cols(ctx, catalog_source),
                  "realizations": _catalog_realizations(ctx, catalog_source), "versions": list(_VERSIONS)})


def planner_input_hash(ctx, template, scope) -> str:
    """The FULL candidate/ranking universe — selection stability. INPUTS ONLY."""
    authorized = set(scope.authorized_catalog_sources)
    universe = {
        "recipe_id": template.id,
        "catalogs": sorted(authorized),
        "cols": {c: _catalog_cols(ctx, c) for c in sorted(authorized)},
        "realizations": {c: _catalog_realizations(ctx, c) for c in sorted(authorized)},
        "bridges": _scoped_bridge_fact_keys(ctx, authorized),
        "roles": sorted(set(ctx.roles)),
        "versions": list(_VERSIONS),
    }
    return _hash(universe)


def _representative_params(template) -> dict:
    return {str(k): str(v[0]) for k, v in template.params.items() if v}


def contract_input_hash(ctx, plan: BindingPlanV1, template) -> str:
    """The SELECTED plan's consumed inputs — verdict stability. INPUTS ONLY (never declarations/verdict)."""
    # read-set columns actually consumed by this plan (ingredient bindings + physical-read set)
    read_refs: set[tuple[str, str]] = {
        (b.bound_catalog_source, b.bound_object_ref) for b in plan.ingredient_bindings}
    if plan.physical_read_set is not None:
        read_refs |= {(c.catalog_source, c.object_ref) for c in plan.physical_read_set.columns}
    read_cols = []
    for cat, ref in sorted(read_refs):
        col = ctx.columns_by_catalog.get(cat, {}).get(ref)
        read_cols.append([cat, ref] + (_col_tuple(col)[1:] if col is not None else ["__missing__"]))
    used_realizations = sorted({s.realization_ref for s in plan.path_segments if s.realization_ref})
    used_bridges = sorted({s.bridge_fact_key for s in plan.path_segments if s.bridge_fact_key})
    path = [[str(s.segment_kind), s.catalog_source, s.realization_ref or s.bridge_fact_key or ""]
            for s in plan.path_segments]
    agg_decls = sorted(
        (nr, str(fn)) for nr, fn in (
            (b.need_role, ctx.agg_declarations.get((plan.recipe_id, b.need_role)))
            for b in plan.ingredient_bindings) if fn is not None)
    material = {
        "recipe": {"id": template.id, "family": template.family, "intent": template.intent,
                   "needs": sorted((n.role, n.concept) for n in template.needs)},
        "params": _representative_params(template),
        "target_entity": plan.target_entity,
        "read_cols": read_cols,
        "realizations": [_realization_tuple(r) for cat in sorted(ctx.realizations_by_catalog)
                         for r in ctx.realizations_by_catalog[cat]
                         if getattr(r, "realization_id", None) in used_realizations],
        "bridges": used_bridges,
        "path": path,
        "agg_declarations": agg_decls,
        "versions": list(_VERSIONS),
    }
    return _hash(material)


def declarations_output_hash(plan: BindingPlanV1) -> str:
    """The OUTPUTS — hashed SEPARATELY from the inputs (a changed output => instability, not a new input)."""
    material = {
        "hop_aggregations": [dataclasses.asdict(h) for h in plan.hop_aggregations],
        "temporal_declaration": dataclasses.asdict(plan.temporal_declaration)
        if plan.temporal_declaration is not None else None,
        "physical_read_set": dataclasses.asdict(plan.physical_read_set)
        if plan.physical_read_set is not None else None,
        "declaration_status": str(plan.declaration_status),
        "contract_reason_codes": [str(r) for r in plan.contract_reason_codes],
    }
    return _hash(material)
