"""P4 v1 — read-only per-table feature suggestions.

The engine already exists: `gate1._template_candidates` grounds the whole template registry against a
catalog and runs every candidate through the same gauntlet the LLM candidates clear — deterministically,
with NO intent, NO hypothesis and NO LLM. It simply had one call site, inside `build_considered_set`.
This module exposes it per table. It WRITES NOTHING.
"""
from __future__ import annotations

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.contract.gate1 import (
    _ground_template_outcomes,
    _template_candidates,
)
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.recipe_grounding_context import RecipeGroundingContextV1
from featuregen.overlay.upload.templates import ALL_TEMPLATES


def suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=()) -> dict:
    """Every template candidate this catalog can ground on ``table``, grouped by entity.

    ``target_ref=None, now=None``: there is no hypothesis to leak into and no clock to fail freshness
    against — the gauntlet's remaining checks (type / additivity / units / point-in-time / grain /
    join authority) still run exactly as they do on the governed path. ``roles`` is the caller's read
    scope: a column the caller may not see is not a grounding candidate, so it cannot be suggested."""
    ideas, rejections, _grounded, _rejected, binding_by_id, _incomplete, contexts, keys_by_recipe = (
        _template_candidates(conn, catalog_source=catalog_source, roles=roles,
                             target_ref=None, now=None))          # no intent, no clock, no LLM
    mine = [idea for idea in ideas if idea.grain_table == table]
    groups: dict[tuple[str, str], list[dict]] = {}
    for idea in mine:
        groups.setdefault(_entity_of(idea, contexts, keys_by_recipe), []).append(
            _suggestion(idea, binding_by_id))
    clean = sum(1 for idea in mine if idea.validation_status == "DESIGN_CHECKED")
    return {
        "catalog_source": catalog_source, "table": table,
        "summary": {"suggested": len(mine), "clean_ready": clean,
                    "needs_review": len(mine) - clean,
                    # an UNLABELLED bucket is not an entity — it is the ideas whose entity could not
                    # be named, and no entity heading is rendered for it.
                    "entities": sum(1 for ref, _label in groups if ref)},
        "groups": [{"entity_ref": ref, "entity_label": label, "suggestions": items}
                   for (ref, label), items in sorted(groups.items())],
        "rejections": _rejections_here(conn, catalog_source, roles, table, rejections),
    }


def _entity_of(idea: FeatureIdea, contexts: dict[str, RecipeGroundingContextV1],
               keys_by_recipe: dict[str, tuple[str, ...]]) -> tuple[str, str]:
    """The (ref, entity) this feature is actually computed PER — the recipe's OWN bound source-entity
    role, read from the grounding context the engine already returns. ``idea.grain_ref`` is the table's
    single ``is_grain`` column, identical for every idea on the table, so grouping on it files an
    account-grained feature under the customer heading. Fall back to it only when the recipe has no
    unambiguous source entity: ``resolve_source_entity_need_role`` yields a role of ``None`` for
    exactly the ambiguous / not-applicable resolutions."""
    keys = keys_by_recipe.get(idea.recipe_id or "", ())
    ctx = contexts.get(keys[0]) if keys else None
    role = ctx.source_entity_need_role if ctx is not None else None
    binding = next((b for b in ctx.need_bindings if b.role == role), None) if role else None
    linked = concept(binding.expected_concept) if binding is not None else None
    if binding is not None and linked is not None and linked.entity_link:
        return binding.graph_object_ref, linked.entity_link
    ref = idea.grain_ref[1] if idea.grain_ref else ""
    return ref, _entity_label(ref)


def _rejections_here(conn, catalog_source: str, roles, table: str,
                     rejections: list[dict]) -> list[dict]:
    """This table's rejections. The engine's list is CATALOG-wide and each entry carries only
    ``{name, reason, code}`` — no grain — so ``name -> grain table(s)`` is rebuilt from the same
    grounding the engine ran (through gate-1's own seam, so a substituted grounder stays consistent).
    A name that grounds on more than one table is kept for each, never silently dropped."""
    if not rejections:
        return []
    grain_of: dict[str, set[str | None]] = {}
    for outcome in _ground_template_outcomes(conn, ALL_TEMPLATES, catalog_source=catalog_source,
                                             roles=roles):
        if outcome.feature is not None:
            grain_of.setdefault(outcome.feature.name, set()).add(outcome.feature.grain_table)
    return [r for r in rejections if table in grain_of.get(r["name"], ())]


def _suggestion(idea: FeatureIdea, binding_by_id: dict[str, str]) -> dict:
    """One card. Every field is the engine's own: the status is the gauntlet's tri-state, the
    description is the template's SME intent, the requirements are the typed ones the gauntlet
    minted, and ``binding_quality`` is the signal the engine already returns per surviving template.
    Nothing is scored or invented here."""
    return {
        "name": idea.name,
        "description": idea.description,
        "grain_table": idea.grain_table,
        "validation_status": idea.validation_status,
        "requirements": [{"code": r.code, "operand": list(r.operand), "detail": r.detail}
                         for r in idea.requirements],
        "uses": list(dict.fromkeys(ref for _src, ref in idea.derives_pairs)),
        "binding_quality": binding_by_id.get(idea.recipe_id or "", ""),
    }


def _entity_label(entity_ref: str) -> str:
    """The grain column's name — a display label, formatted from the ref, never invented."""
    return entity_ref.rsplit(".", 1)[-1] if entity_ref else ""
