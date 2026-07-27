"""P4 v1 — read-only per-table feature suggestions.

The engine already exists: `gate1._template_candidates` grounds the whole template registry against a
catalog and runs every candidate through the same gauntlet the LLM candidates clear — deterministically,
with NO intent, NO hypothesis and NO LLM. It simply had one call site, inside `build_considered_set`.
This module exposes it per table. It WRITES NOTHING.
"""
from __future__ import annotations

from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.feature_assist import FeatureIdea


def suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=()) -> dict:
    """Every template candidate this catalog can ground on ``table``, grouped by entity.

    ``target_ref=None, now=None``: there is no hypothesis to leak into and no clock to fail freshness
    against — the gauntlet's remaining checks (type / additivity / units / point-in-time / grain /
    join authority) still run exactly as they do on the governed path. ``roles`` is the caller's read
    scope: a column the caller may not see is not a grounding candidate, so it cannot be suggested."""
    ideas, rejections, _grounded, _rejected, binding_by_id, _incomplete, _ctx, _keys = (
        _template_candidates(conn, catalog_source=catalog_source, roles=roles,
                             target_ref=None, now=None))          # no intent, no clock, no LLM
    mine = [idea for idea in ideas if idea.grain_table == table]
    groups: dict[str, list[dict]] = {}
    for idea in mine:
        entity_ref = idea.grain_ref[1] if idea.grain_ref else ""
        groups.setdefault(entity_ref, []).append(_suggestion(idea, binding_by_id))
    clean = sum(1 for idea in mine if idea.validation_status == "DESIGN_CHECKED")
    return {
        "catalog_source": catalog_source, "table": table,
        "summary": {"suggested": len(mine), "clean_ready": clean,
                    "needs_review": len(mine) - clean, "entities": len(groups)},
        "groups": [{"entity_ref": ref, "entity_label": _entity_label(ref),
                    "suggestions": items} for ref, items in sorted(groups.items())],
        "rejections": rejections,
    }


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
