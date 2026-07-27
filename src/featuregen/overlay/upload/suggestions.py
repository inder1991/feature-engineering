"""P4 v1 — read-only per-table feature suggestions.

The engine already exists: `gate1._template_candidates` grounds the whole template registry against a
catalog and runs every candidate through the same gauntlet the LLM candidates clear — deterministically,
with NO intent, NO hypothesis and NO LLM. It simply had one call site, inside `build_considered_set`.
This module exposes it per table. It WRITES NOTHING.

Grounding is asked for THIS TABLE (`table=` on `_template_candidates`), not catalog-wide-then-filtered.
The engine yields at most one candidate per template, so a catalog-wide pass hands each recipe to
whichever table binds it first (ties break on table name) and every other table shows nothing for it:
on a four-table fixture two tables — including the widest — showed ZERO suggestions, and two recipes
whose distinctive columns exist on only one table were lost entirely, having grounded their entity
need on the alphabetically-first table and then been rejected NO_JOIN_PATH there. Per-table grounding
is also CHEAPER (grounding cost scales with the columns considered, so a page view is O(recipes ×
table columns) instead of O(recipes × catalog columns)) and it removed the second grounding pass the
rejection list used to need: every rejection now comes from a candidate grounded on this table's
columns alone, so the engine's list is already this table's and needs no re-attribution.

The catalog-wide question is a DIFFERENT question and still has its own caller: `build_considered_set`
(the hypothesis-driven feature-generation flow) asks what the CATALOG can produce and passes no
`table`, so that path is untouched.
"""
from __future__ import annotations

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.contract._serial import requirements_to_json
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.recipe_grounding_context import RecipeGroundingContextV1


def suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=()) -> dict:
    """Every template candidate this catalog can ground on ``table``, grouped by entity.

    ``target_ref=None, now=None``: there is no hypothesis to leak into and no clock to fail freshness
    against — the gauntlet's remaining checks (type / additivity / units / point-in-time / grain /
    join authority) still run exactly as they do on the governed path. ``roles`` is the caller's read
    scope: a column the caller may not see is not a grounding candidate, so it cannot be suggested.

    ``table_known`` is a FOURTH state, and the load-bearing one for honesty: a table this catalog does
    not hold produces exactly the same zero-suggestion payload as a table whose columns carry no
    concepts, so without it the screen diagnoses a NONEXISTENT table as "your columns don't carry
    business concepts". Resolved from ``graph_node`` alone, before the engine runs."""
    known = _resolve_table(conn, catalog_source, table)
    if known is None:
        return {"catalog_source": catalog_source, "table": table, "table_known": False,
                "summary": {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0},
                "groups": [], "rejections": []}
    table = known                                   # the catalog's own bare name — the engine's key
    ideas, rejections, _grounded, _rejected, binding_by_id, _incomplete, contexts, keys_by_recipe = (
        _template_candidates(conn, catalog_source=catalog_source, roles=roles,
                             target_ref=None, now=None,           # no intent, no clock, no LLM
                             table=table))                        # ...and only THIS table's columns
    # Grounding is now scoped to the table, so every idea is already this table's — but a template
    # whose needs are ALL optional and all unmet grounds with no bindings at all and therefore no
    # grain table, and such an idea is not a suggestion FOR this table. The filter is what makes the
    # per-table guarantee a property of this function rather than of the engine's internals.
    mine = [idea for idea in ideas if idea.grain_table == table]
    # Keyed on the entity REF alone: keying on (ref, label) lets one column open two groups, which
    # the screen then renders with the same React key.
    groups: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    for idea in mine:
        ref, label = _entity_of(idea, contexts, keys_by_recipe)
        groups.setdefault(ref, []).append(_suggestion(idea, binding_by_id, ref))
        if label and not labels.get(ref):
            labels[ref] = label
    clean = sum(1 for idea in mine if idea.validation_status == "DESIGN_CHECKED")
    return {
        "catalog_source": catalog_source, "table": table, "table_known": True,
        "summary": {"suggested": len(mine), "clean_ready": clean,
                    "needs_review": len(mine) - clean,
                    # an UNLABELLED bucket is not an entity — it is the ideas whose entity could not
                    # be named, and no entity heading is rendered for it.
                    "entities": sum(1 for ref in groups if ref)},
        # the unlabelled bucket sorts LAST: headingless cards above the named groups read as the
        # page's lead.
        "groups": [{"entity_ref": ref, "entity_label": labels.get(ref, ""), "suggestions": items}
                   for ref, items in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0]))],
        # Every rejection came from a candidate grounded on THIS table's columns alone, so the
        # engine's list is already this table's — see the module docstring.
        "rejections": rejections,
    }


def _resolve_table(conn, catalog_source: str, table: str) -> str | None:
    """This catalog's own ``graph_node.table_name`` for ``table`` — ``None`` when it holds no such
    table. The bare name is the engine's key (``FeatureIdea.grain_table``), but a deep link naturally
    carries the schema-qualified table ``object_ref`` (``public.txns``), which is UNIQUE per catalog
    (the node's primary key), so accepting it too is unambiguous. A bare match wins the tie."""
    row = conn.execute(
        "SELECT table_name FROM graph_node WHERE catalog_source = %s "
        "AND (table_name = %s OR (kind = 'table' AND object_ref = %s)) "
        "ORDER BY (table_name = %s) DESC LIMIT 1",
        (catalog_source, table, table, table)).fetchone()
    return row[0] if row else None


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
    # No unambiguous source entity: the group is the table's grain COLUMN, and it has no entity
    # NAME. Labelling it with the column would put "cif_id features" beside "customer features" as
    # though the catalog had attested an entity called cif_id — it attested nothing.
    return (idea.grain_ref[1] if idea.grain_ref else ""), ""


def _suggestion(idea: FeatureIdea, binding_by_id: dict[str, str], entity_ref: str) -> dict:
    """One card. Every field is the engine's own: the status is the gauntlet's tri-state, the
    description is the template's SME intent, the requirements are the typed ones the gauntlet
    minted, and ``binding_quality`` is the signal the engine already returns per surviving template.
    Nothing is scored or invented here."""
    return {
        "name": idea.name,
        "description": idea.description,
        "grain_table": idea.grain_table,
        "validation_status": idea.validation_status,
        # The SINGLE requirement wire shape (`contract/_serial.py`). The private copy this used to
        # hold dropped `params` / `schema_version`, so E4a T3's "AI suggests AED" never reached the
        # card the reviewer actually reads. Additive emission keeps a no-param requirement's JSON
        # byte-identical.
        "requirements": requirements_to_json(idea.requirements),
        "uses": list(dict.fromkeys(ref for _src, ref in idea.derives_pairs)),
        "binding_quality": binding_by_id.get(idea.recipe_id or "", ""),
        "recipe": render_recipe(idea, entity_ref),
        "recipe_parts": _recipe_parts(idea, entity_ref),
    }


def render_recipe(idea: FeatureIdea, entity_ref: str) -> str:
    """The one-line recipe this feature computes, e.g. ``trend_90d(bal_amt) BY cif_id OVER 90d
    [as_of_dt]``.

    ``operation_kind`` is printed AS BOUND. It is a DOMAIN label (``trend``, ``inflow_outflow``,
    ``frequency_trend`` — ~152 of them), NOT a SQL verb: there is no label -> verb map here, because
    inventing one would print ``AVG(...)`` for an operation the system calls ``trend``. Clauses that
    do not apply are omitted, never emitted empty."""
    parts = _recipe_parts(idea, entity_ref)
    measures = ", ".join(parts["measures"])
    line = f"{parts['operation']}({measures})" if parts["operation"] else measures
    if parts["grain"]:
        line += f" BY {parts['grain']}"
    if parts["window"]:
        line += f" OVER {parts['window']}"
    if parts["time"]:
        line += f" [{parts['time']}]"
    return line


def _recipe_parts(idea: FeatureIdea, entity_ref: str) -> dict:
    """The rendered line's pieces, structured. ``measure_refs`` carries EVERY bound pair — the grain
    and point-in-time columns included — so both are subtracted here: a card listing the grain column
    as a measure would claim the feature aggregates its own key. Order is the engine's binding order
    (deduped), so the same idea always renders the same line.

    ``entity_ref`` is the recipe's OWN bound entity (:func:`_entity_of`) when one resolved. The ``BY``
    clause must name the column the card's HEADING names: ``idea.grain_ref`` is the table's single
    ``is_grain`` column, so an account-grained card otherwise read "per account" above a line saying
    ``BY cif_id``. It is subtracted from the measures for the same reason the grain is — it is the
    feature's key, not a quantity it aggregates. Empty (no unambiguous source entity) falls back to
    ``grain_ref``, unchanged."""
    dropped = {ref for ref in (idea.grain_ref, idea.time_ref) if ref is not None}
    measures = [ref for ref in dict.fromkeys(idea.measure_refs)
                if ref not in dropped and ref[1] != entity_ref]
    grain = entity_ref or (idea.grain_ref[1] if idea.grain_ref else "")
    return {
        "operation": idea.operation_kind,
        "measures": [_column(ref) for _src, ref in measures],
        "grain": _column(grain) if grain else "",
        "window": idea.window or "",
        "time": _column(idea.time_ref[1]) if idea.time_ref else "",
    }


def _column(object_ref: str) -> str:
    """The column name — the ref's last segment. A full ``schema.table.column`` ref is unreadable on
    a card, and nothing is invented by taking the name the catalog already holds."""
    return object_ref.rsplit(".", 1)[-1]
