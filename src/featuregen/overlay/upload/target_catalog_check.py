"""Catalog resolution for a target rule.

Deliberately separate from `target_contract`: the contract is a pure unit testable without a
database, and these are the checks that genuinely need one. Returns REASONS rather than raising,
because the authoring conversation shows them all at once rather than one per round trip.
"""
from __future__ import annotations

from collections.abc import Iterable

from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.target_contract import TargetRuleV1, refs_read


def selectable_entities(conn, catalog_source: str, *,
                        roles: Iterable[str] = ()) -> list[dict]:
    """The entities this catalog can genuinely anchor a label on — those with a keyed spine table.

    The person CHOOSES from this list (owner's decision, 2026-09-02). Not the 38-name vocabulary,
    which offers entities nothing can key on, and not the recogniser's `target_entity`, which
    returned None for a hypothesis beginning "Customers" on the live cluster and carries no
    confidence band. A closed, catalog-derived list cannot be wrong in the way a guess can.

    An empty result means this catalog cannot anchor a label at all — the caller must SAY that
    rather than render a blank dropdown, which reads as a bug.
    """
    rows = conn.execute(
        "SELECT DISTINCT entity, table_name, object_ref FROM graph_node"
        " WHERE kind = 'column' AND catalog_source = %s AND is_grain"
        "   AND entity IS NOT NULL AND entity <> '' AND visible_requires <@ %s"
        " ORDER BY entity, object_ref",
        (catalog_source, allowed_sensitivities(roles))).fetchall()
    return [{"entity": entity.lower(), "spine_table": table, "spine_ref": ref}
            for entity, table, ref in rows]


def check_target_against_catalog(conn, rule: TargetRuleV1, *,
                                 roles: Iterable[str] = ()) -> tuple[str, ...]:
    """Every reason this rule cannot be registered against this catalog; empty when it resolves."""
    reasons: list[str] = []
    wanted = refs_read(rule)
    # Matched on the (catalog_source, object_ref) PAIR, never on the ref alone. `object_ref` is
    # only `public.{table}.{column}`, so a bare match lets a same-named column in another catalog
    # answer for this one — the M3 defect `_column_meta` is scoped to avoid.
    #
    # `(catalog_source, object_ref) = ANY(%s)` reads better but psycopg cannot send a list of
    # tuples — "input of anonymous composite types is not implemented". Two parallel arrays
    # unnested into a join is the portable form and keeps the filtering in SQL.
    rows = conn.execute(
        "SELECT g.catalog_source, g.object_ref, g.is_as_of FROM graph_node g"
        " JOIN unnest(%s::text[], %s::text[]) AS w(catalog_source, object_ref)"
        "   ON w.catalog_source = g.catalog_source AND w.object_ref = g.object_ref"
        " WHERE g.kind = 'column' AND g.visible_requires <@ %s",
        ([c for c, _ in wanted], [r for _, r in wanted],
         allowed_sensitivities(roles))).fetchall()
    visible = {(catalog, ref): is_as_of for catalog, ref, is_as_of in rows}

    for pair in wanted:
        if pair not in visible:
            catalog, ref = pair
            reasons.append(
                f"{ref} does not resolve to a readable column in catalog {catalog}")

    # THE GRAIN CHECK. The person picked an entity from `selectable_entities`; this confirms the
    # anchor really is that entity's key. Choosing `customer` while anchoring on a column that is
    # not the customer key makes every row of the label the wrong shape, and nothing else catches
    # it. Compared case-insensitively: the catalog stores what ingestion wrote ("Customer"), the
    # rule carries the governed vocabulary's form ("customer").
    grain = conn.execute(
        "SELECT entity FROM graph_node WHERE kind = 'column' AND catalog_source = %s"
        "   AND object_ref = %s AND is_grain AND visible_requires <@ %s",
        (rule.header.anchor_catalog, rule.header.grain_ref,
         allowed_sensitivities(roles))).fetchone()
    if grain is None:
        reasons.append(
            f"grain_ref {rule.header.grain_ref} is not a grain column in "
            f"{rule.header.anchor_catalog} — a label cannot be anchored on a column that does not "
            "key its table")
    elif (grain[0] or "").lower() != rule.header.entity.lower():
        reasons.append(
            f"grain_ref {rule.header.grain_ref} keys {grain[0]!r}, but the rule declares entity "
            f"{rule.header.entity!r} — one row per {rule.header.entity} cannot be produced from a "
            f"{grain[0]} key")

    as_of = (rule.header.anchor_catalog, rule.header.as_of_ref)
    if as_of in visible and not visible[as_of]:
        reasons.append(
            f"as_of_ref {as_of[1]} is not an as-of column — the label's anchor date must be one, "
            "or the window is measured from something that does not move with time")
    return tuple(reasons)
