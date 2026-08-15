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
rejection list used to need: every rejection now comes from a candidate grounded on this table's own
grounding set, so the engine's list is already this screen's and needs no re-attribution.

Grounding is asked for this table AND the tables a CLEARING join makes reachable from it. Per-table
grounding alone could no longer produce a cross-table candidate AT ALL — not even one a governed join
legitimately authorises: "average transaction amount per customer", where the amount lives on
`transactions` and the customer key comes over a VERIFIED join to `customers`, became invisible on
this screen. The neighbourhood comes from `join_path.clearing_neighbourhood`, which is the gauntlet's
OWN machinery (same fetch, same read-scope check, same clearing rule), so the columns this screen may
reach and the gauntlet's `JOIN_CONNECTIVITY` disposition cannot disagree. An UNVERIFIED join does NOT
widen — a candidate reached over one would arrive carrying a `JOIN_CONNECTIVITY` requirement or be
rejected `NO_JOIN_PATH`, which is precisely the noise per-table grounding removed.

The widening is BOUNDED, and that is a correctness property, not a speed one. It first walked the
join graph TRANSITIVELY, and on a real catalog almost every table reaches the customer/account hub
over some chain of joins — so opening a hub table ground the registry against most of the catalog: an
operation whose cost is a property of the CATALOG rather than of the request, i.e. with no
predictable resource bound (measured on a hub fixture with 40 directly-joined tables: 12,710
statements for one page view, against 6,284 capped — and the capped figure did not move when the
fixture grew from 24 neighbours to 40, while the transitive one rose with every join). An ordinary
two-table page is unchanged at 813 statements, against 812 before the cap. An automatic page load now
widens ONE HOP, into at most `MAX_NEIGHBOUR_TABLES` directly-joined tables and within a total
`MAX_COLUMNS_CONSIDERED` column budget, keeping the nearest neighbours in a deterministic order. What
that leaves out is REPORTED (`neighbourhood` on the payload) rather than silently dropped, and a
deliberate caller may still ask for more hops via `max_hops`. Choosing WHICH deeper join path to
follow is a governed, explicit act and wants its own picker UI — deferred, not solved here.

The catalog-wide question is a DIFFERENT question and still has its own caller: `build_considered_set`
(the hypothesis-driven feature-generation flow) asks what the CATALOG can produce and passes no
`table`, so that path is untouched.
"""
from __future__ import annotations

from typing import NamedTuple

from featuregen.overlay.upload import join_path
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.contract._serial import requirements_to_json
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.join_path import (
    JoinNeighbourhood,
    clearing_neighbourhood,
    table_of_ref,
)
from featuregen.overlay.upload.read_scope import allowed_classes
from featuregen.overlay.upload.recipe_grounding_context import RecipeGroundingContextV1
from featuregen.overlay.upload.suggestion_contract import (
    FeatureSuggestionPageV2,
    build_page_v2,
    recipe_parts,
    recipe_parts_to_json,
    render_recipe,
    unknown_table_page_v2,
)

# The recipe line is rendered by ONE producer, in `suggestion_contract`, and re-exported here: V1's
# card and V2's `recipe`/`recipe_parts` must be the same string built the same way, and the module
# that owns the V2 contract cannot import this one back without a cycle. Existing callers keep
# importing `render_recipe` from here.
__all__ = ["render_recipe", "suggest_features_for_table", "suggest_features_page_v2"]


class _Anchored(NamedTuple):
    """One grounding pass for one anchor table, shared by the V1 and V2 surfaces so the two can
    never ground differently for the same request."""

    table: str
    neighbourhood: JoinNeighbourhood
    candidates: object
    ideas: list[FeatureIdea]


def _ground_anchor(conn, catalog_source: str, table: str, roles, max_hops) -> _Anchored | None:
    """Resolve the table, bound the neighbourhood, ground once, keep the candidates that bind THIS
    table. ``None`` means this catalog holds no such table for this caller."""
    known = _resolve_table(conn, catalog_source, table, roles=roles)
    if known is None:
        return None
    # The BOUNDED join NEIGHBOURHOOD, decided by the gauntlet's own rule. Read-scoped: an edge with
    # an endpoint this caller cannot see is DENIED there, so it never widens anything here — and a
    # table the cap drops was, by construction, one this caller could already see, so truncation
    # changes HOW MUCH is grounded against and never WHAT may be.
    neighbourhood = clearing_neighbourhood(conn, catalog_source, known, roles=roles,
                                           max_hops=max_hops)
    # The engine's own result object. Read BY NAME — this screen consumes five of its members
    # (ideas, rejections, binding_by_id, contexts, keys_by_recipe) and ignores the rest; it never
    # rebuilds, re-derives or re-attributes any of them, and (rule 15) it never touches the decision
    # traces the engine minted: they are the gauntlet's answer to a different question, handed
    # whole to `suggestion_contract`, and reconstructing one here would be a second copy of the
    # decision.
    candidates = _template_candidates(conn, catalog_source=catalog_source, roles=roles,
                                      target_ref=None, now=None,   # no intent, no clock, no LLM
                                      table=known,                 # ...THIS table's columns...
                                      also_tables=neighbourhood.neighbours)  # ...+ what it joins to
    return _Anchored(known, neighbourhood, candidates,
                     [idea for idea in candidates.ideas if _binds(idea, known)])


def _empty_neighbourhood(max_hops: int | None) -> JoinNeighbourhood:
    """Zeroes are the truth for a table this catalog does not hold: it has no neighbours to have
    truncated. Reported anyway so the payload's shape never varies — and carried as the real value
    object, so the V1 adapter re-serializes it through its own producer instead of synthesizing a
    second zero block that could drift."""
    return JoinNeighbourhood(
        tables=(), tables_considered=0, tables_available=0, truncated=False,
        max_hops=(join_path.MAX_HOPS_DEFAULT if max_hops is None else max_hops),
        limit_reason=None)


def suggest_features_page_v2(conn, *, catalog_source: str, table: str, roles=(),
                             max_hops: int | None = None,
                             column_ref: str | None = None) -> FeatureSuggestionPageV2:
    """This table's suggestions as ``FeatureSuggestionPageV2`` — the SAME read as V1, projected onto
    the discovery contract instead of the v1 card shape.

    Release A is ``read_mode=on_demand``: there is no projection, so the page reports
    ``projection=None`` rather than claiming a currentness it never computed, and this call remains
    strictly read-only.

    The assembly itself lives in ``suggestion_contract``: this function's whole job is to run the
    engine once and hand the result over with the entity resolution V1 groups by, so the two
    surfaces can never disagree about which bucket a card belongs to."""
    anchored = _ground_anchor(conn, catalog_source, table, roles, max_hops)
    if anchored is None:
        return unknown_table_page_v2(catalog_source=catalog_source, requested_table=table,
                                     neighbourhood=_empty_neighbourhood(max_hops), roles=roles)
    contexts = anchored.candidates.contexts
    keys_by_recipe = anchored.candidates.keys_by_recipe
    return build_page_v2(
        conn, catalog_source=catalog_source, anchor_table_ref=anchored.table,
        anchored=[(idea, *_entity_of(idea, contexts, keys_by_recipe)) for idea in anchored.ideas],
        candidates=anchored.candidates, neighbourhood=anchored.neighbourhood, roles=roles,
        anchor_column_ref=column_ref)


def suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=(),
                               max_hops: int | None = None) -> dict:
    """Every template candidate this catalog can ground on ``table``, grouped by entity.

    ``target_ref=None, now=None``: there is no hypothesis to leak into and no clock to fail freshness
    against — the gauntlet's remaining checks (type / additivity / units / point-in-time / grain /
    join authority) still run exactly as they do on the governed path. ``roles`` is the caller's read
    scope: a column the caller may not see is not a grounding candidate, so it cannot be suggested.

    ``table_known`` is a FOURTH state, and the load-bearing one for honesty: a table this catalog does
    not hold produces exactly the same zero-suggestion payload as a table whose columns carry no
    concepts, so without it the screen diagnoses a NONEXISTENT table as "your columns don't carry
    business concepts". Resolved from ``graph_node`` before the engine runs — under THIS CALLER's
    read scope: a table none of whose columns are visible to the caller is, for that caller, a table
    this catalog does not hold (see :func:`_resolve_table`).

    ``max_hops`` is the EXPLICIT opt-in for a wider neighbourhood. ``None`` — what every automatic
    page load passes — is the capped default (``join_path.MAX_HOPS_DEFAULT``: one hop). A deliberate
    caller may ask for more; the table cap and the column budget still apply, so expansion changes
    which tables are ELIGIBLE, never how many are admitted."""
    anchored = _ground_anchor(conn, catalog_source, table, roles, max_hops)
    if anchored is None:
        return {"catalog_source": catalog_source, "table": table, "table_known": False,
                "summary": {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0},
                "groups": [], "rejections": [],
                "neighbourhood": _empty_neighbourhood(max_hops).as_metadata()}
    table = anchored.table                          # the catalog's own bare name — the engine's key
    neighbourhood = anchored.neighbourhood
    candidates = anchored.candidates
    binding_by_id = candidates.binding_by_id
    contexts = candidates.contexts
    keys_by_recipe = candidates.keys_by_recipe
    mine = anchored.ideas
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
        # Every rejection came from a candidate grounded on THIS table's grounding NEIGHBOURHOOD (its
        # own columns, plus whatever a clearing join reaches), so the engine's list is already this
        # screen's and needs no re-attribution — see the module docstring. With no join the
        # neighbourhood is the table itself and the list is exactly the table's, as before.
        "rejections": candidates.rejections,
        # WHAT WAS LEFT OUT. A page that grounds against a bounded slice of a table's join
        # neighbourhood must say so, or its empty states become false claims ("nothing else is
        # buildable here" when the truth is "we did not look"). These are the screen's numbers.
        "neighbourhood": neighbourhood.as_metadata(),
    }


def _binds(idea: FeatureIdea, table: str) -> bool:
    """Does this candidate actually READ a column of ``table``?

    This is what makes "a suggestion FOR this table" a property of this function rather than of the
    engine's internals, and it is the un-widened rule generalised, not a new one: with the candidate
    columns narrowed to one table, ``grain_table == table`` was exactly "bound at least one column"
    (a template whose needs are all optional and all unmet grounds with NO bindings, hence no grain
    table, and is nobody's suggestion). Once the set is widened across a join, the grain moves to the
    ENTITY's table — "average transaction amount per customer" is grained on ``customers`` — so a
    grain test would file the ledger's own feature away from the ledger. A candidate that binds
    nothing here is a pure neighbour candidate and belongs on the neighbour's screen.

    ``table_of_ref`` is the join BFS's own ref->table function, so the tables the widening reasoned
    about and the tables counted here are the same names."""
    return any(table_of_ref(ref) == table for _src, ref in idea.derives_pairs)


def _resolve_table(conn, catalog_source: str, table: str, roles=()) -> str | None:
    """This catalog's own ``graph_node.table_name`` for ``table`` — ``None`` when it holds no such
    table FOR THIS CALLER. The bare name is the engine's key (``FeatureIdea.grain_table``), but a
    deep link naturally carries the schema-qualified table ``object_ref`` (``public.txns``), which is
    UNIQUE per catalog (the node's primary key), so accepting it too is unambiguous. A bare match
    wins the tie.

    Existence is a READ-SCOPED fact, derived from at least one caller-VISIBLE column (Task 0C defect
    2). The table node itself is world-visible, so treating it as proof let a caller whose scope
    could see NO column of a table still learn the table exists through ``table_known`` — and would
    then be shown its rejections, neighbourhood metadata and counts. The visibility rule is the
    grounding set's own (``visible_requires <@ allowed_classes(roles)``, the migration-1032
    predicate every read-scope call site binds), so "the table exists for you" and "some column of
    it could ground for you" can never disagree: all-hidden resolves exactly like nonexistent, while
    one visible column keeps the resolution — and the payload — precisely what it always was. The
    deep-link spelling still resolves through the table node's own ref, but only NAMES the table;
    it never proves existence."""
    row = conn.execute(
        "SELECT c.table_name FROM graph_node c "
        "WHERE c.catalog_source = %s AND c.kind = 'column' "
        "AND c.visible_requires <@ %s "
        "AND (c.table_name = %s OR c.table_name = ("
        "    SELECT t.table_name FROM graph_node t "
        "    WHERE t.catalog_source = %s AND t.kind = 'table' AND t.object_ref = %s)) "
        "ORDER BY (c.table_name = %s) DESC LIMIT 1",
        (catalog_source, allowed_classes(roles), table, catalog_source, table, table)).fetchone()
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
        "recipe_parts": recipe_parts_to_json(recipe_parts(idea, entity_ref)),
    }

def semantic_parity_block(conn, *, catalog_source: str, table: str, roles=()) -> dict:
    """SE-13 — the deterministic page's ENGINE verdicts for one opened table.

    Runs the SAME lens the hypothesis Workbench serves from — frozen context, capability
    binder, eligibility fold, assembly — UNSCOPED (this page has no hypothesis, no target, no
    LLM; applicability narrows nothing here by design), then anchors to the opened table: a
    candidate appears when any of its bound operands lives on this table. The anchor is
    retrieval, never a forced measure — the engine bound whatever roles the columns can
    actually serve. Because both surfaces call one engine over one context, they CANNOT
    disagree about whether a recipe/column binding is semantically valid — the SE-13
    acceptance, held by construction and pinned by test."""
    from featuregen.overlay.upload.candidate_assembly import assemble_candidates
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_planning_lens import v2_recipe_candidates
    from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

    context = build_generation_semantic_context(
        conn, catalog_source=catalog_source, roles=roles)
    candidates = v2_recipe_candidates(
        conn, catalog_source=catalog_source, roles=roles,
        scope=ConfirmedScope(primary=None, unscoped=True), context=context)

    # The caller's spelling is resolved by the SAME function the deterministic half resolves it
    # with (:func:`_resolve_table`), and for the same reason: a deep link carries the
    # schema-qualified `object_ref` (`public.txns`) while the engine's key is the bare table name.
    # Anchoring on the raw parameter matched only the bare spelling, so a schema-qualified caller
    # got an EMPTY engine block beside a populated `hits` list — the two surfaces disagreeing about
    # one table, which is exactly what SE-13 exists to make impossible, and disagreeing SILENTLY,
    # which this codebase treats as worse than refusing. `None` (no such table for this caller)
    # anchors nothing, which is the same answer `table_known: false` gives on the other half.
    resolved = _resolve_table(conn, catalog_source, table, roles=roles)

    def _on_table(candidate) -> bool:
        if resolved is None:
            return False
        return any(
            verdict.selected_ref and verdict.selected_ref.split(".")[-2] == resolved
            for verdict in candidate.verdicts if verdict.status == "bound")

    anchored = [c for c in candidates if _on_table(c)]
    assembled = assemble_candidates(anchored)

    # D4 (UI-05): every entry carries the PROJECTED card — built by the same projection and
    # serialized by the SAME function the Workbench's considered set uses (imported from
    # gate1, never copied), so the two surfaces render one card model by construction.
    from featuregen.overlay.upload.contract.gate1 import _idea_json
    from featuregen.overlay.upload.semantic_projection import project_assembled_set

    projection = project_assembled_set(assembled, catalog_source=catalog_source)
    cards_by_definition = {
        idea.source_definition_id: _idea_json(idea)
        for idea in (*projection.ideas, *projection.actionable_ideas)
        if idea.source_definition_id}

    def _entry(item) -> dict:
        candidate = item.candidate
        return {
            "card": cards_by_definition.get(
                getattr(candidate, "variant_key", "") or candidate.recipe_id),
            "recipe_id": candidate.recipe_id,
            "binding_state": candidate.binding_state,
            "readiness": candidate.readiness,
            "review_current": candidate.review_current,
            "planning_request_hash": candidate.planning_request_hash,
            "verdicts": [
                {"role": v.role, "status": v.status, "selected_ref": v.selected_ref,
                 "reason_codes": list(v.reason_codes), "resolution": v.resolution}
                for v in candidate.verdicts],
            "corroborations": [
                {"origin": c.origin, "source_definition_id": c.source_definition_id}
                for c in item.corroborations],
        }
    return {
        "semantic_context_hash": context.context_hash(),
        "table": table,
        "ranked": [_entry(a) for a in assembled.ranked],
        "actionable": [_entry(a) for a in assembled.actionable],
        "order_basis": assembled.order_basis,
    }
