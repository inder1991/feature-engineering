"""Catalog resolution for a target rule.

Deliberately separate from `target_contract`: the contract is a pure unit testable without a
database, and these are the checks that genuinely need one. Returns REASONS rather than raising,
because the authoring conversation shows them all at once rather than one per round trip.
"""
from __future__ import annotations

from collections.abc import Iterable

from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.target_contract import TargetRuleV1, refs_read


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

    as_of = (rule.header.anchor_catalog, rule.header.as_of_ref)
    if as_of in visible and not visible[as_of]:
        reasons.append(
            f"as_of_ref {as_of[1]} is not an as-of column — the label's anchor date must be one, "
            "or the window is measured from something that does not move with time")
    return tuple(reasons)
