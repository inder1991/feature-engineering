"""Semantics-pending queue + owner completion (#22).

A column can land structurally vouched but semantically blank — no as-of axis, additivity,
unit, currency, or entity (the OpenMetadata connector does this BY DESIGN: structure is
vouched, semantics await a human owner). `semantics_pending_count` made that an honest COUNT
on the import summary (#25); this module makes it a real workflow: list the pending columns
and let an owner complete them.

ONE predicate (`missing_semantic_fields` / `semantics_pending`) backs both the connector's
count and the queue, so they can never disagree on what "pending" means: a column is pending
iff it lacks ALL five semantic fields — the same `not (as_of or additivity or unit or
currency or entity)` the count has always used.

Completion is a direct catalog_write edit of the column's flat `graph_node` attributes —
exactly how these fields are set when a file declares them (build_graph writes them straight
onto the node; no governed fact stream is involved). Values are validated against the SAME
closed vocabularies `validate_rows` enforces. Two things stay OUT of reach, fail-closed:

* grain/availability facts are GOVERNED (Pass B owner confirmation) — never touched here.
  `as_of_basis` in particular lives ONLY in the governed availability_time fact stream
  (graph_node carries just the is_as_of flag — see lineage._as_of_basis), so a declared
  basis is validated and recorded on the audit trail, never written to the node.
* sensitivity is not a semantic field and cannot be edited through this seam.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import _VALID_ADDITIVITY, _VALID_AS_OF_BASIS
from featuregen.overlay.upload.graph import rebuild_search_doc
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.security.audit import record_security_event

# The semantic set, in the order the count's docstring names it. `as_of` is the node's
# is_as_of flag; the other four are the flat graph_node columns build_graph writes.
SEMANTIC_FIELDS = ("as_of", "additivity", "unit", "currency", "entity")

# graph_node columns the completion write may touch. NOTHING else — not sensitivity, not
# grain, not definition/concept/domain (other seams own those).
_NODE_COLUMNS = {"additivity": "additivity", "unit": "unit", "currency": "currency",
                 "entity": "entity", "is_as_of": "is_as_of"}


class InvalidSemanticValue(ValueError):
    """A value outside the closed vocabulary validate_rows enforces (fail-closed 4xx)."""


class AsOfConflict(ValueError):
    """Setting is_as_of would give the table a SECOND as-of axis — the exact ambiguity
    validate_rows fails closed on (#17: a table asserts ONE availability basis)."""


def missing_semantic_fields(*, as_of: bool, additivity: str | None, unit: str | None,
                            currency: str | None, entity: str | None) -> tuple[str, ...]:
    """Which of the five semantic fields are absent. Blank ('' or NULL) means absent."""
    present = {"as_of": bool(as_of), "additivity": bool(additivity), "unit": bool(unit),
               "currency": bool(currency), "entity": bool(entity)}
    return tuple(f for f in SEMANTIC_FIELDS if not present[f])


def semantics_pending(*, as_of: bool, additivity: str | None, unit: str | None,
                      currency: str | None, entity: str | None) -> bool:
    """Pending ⟺ the column lacks the WHOLE semantic set — the count's original predicate
    (`not (as_of or additivity or unit or currency or entity)`), shared so list == count."""
    return len(missing_semantic_fields(as_of=as_of, additivity=additivity, unit=unit,
                                       currency=currency, entity=entity)) == len(SEMANTIC_FIELDS)


@dataclass(frozen=True, slots=True)
class SemanticsPendingItem:
    object_ref: str
    table: str
    column: str
    data_type: str | None
    missing: tuple[str, ...]


def list_semantics_pending(conn, catalog_source: str, *,
                           roles: Iterable[str] = ()) -> list[SemanticsPendingItem]:
    """The source's columns whose semantics are pending, READ-SCOPED like search/entity
    suggestions: a pending column whose sensitivity the caller's roles can't see is withheld.
    Filtered in Python by the SHARED predicate (not a parallel SQL re-statement of it), so the
    queue and `semantics_pending_count` are the same definition applied to different inputs."""
    rows = conn.execute(
        "SELECT object_ref, table_name, column_name, data_type, is_as_of, additivity, unit, "
        "currency, entity FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) ORDER BY object_ref",
        (catalog_source, allowed_sensitivities(roles))).fetchall()
    return [SemanticsPendingItem(ref, table, column, data_type,
                                 missing_semantic_fields(as_of=as_of, additivity=additivity,
                                                         unit=unit, currency=currency,
                                                         entity=entity))
            for ref, table, column, data_type, as_of, additivity, unit, currency, entity in rows
            if semantics_pending(as_of=as_of, additivity=additivity, unit=unit,
                                 currency=currency, entity=entity)]


def validate_semantic_values(*, additivity: str | None = None,
                             as_of_basis: str | None = None) -> None:
    """Reject values outside the closed vocabularies validate_rows enforces — the SAME sets
    (canonical._VALID_*), so a value an upload would quarantine is a value completion refuses."""
    if additivity and additivity not in _VALID_ADDITIVITY:
        raise InvalidSemanticValue(
            f"unrecognized additivity '{additivity}' "
            f"(expected one of: {', '.join(sorted(_VALID_ADDITIVITY))})")
    if as_of_basis and as_of_basis not in _VALID_AS_OF_BASIS:
        raise InvalidSemanticValue(
            f"unrecognized as_of_basis '{as_of_basis}' "
            f"(expected one of: {', '.join(sorted(_VALID_AS_OF_BASIS))})")


def complete_semantics(conn, catalog_source: str, object_ref: str, *,
                       actor: IdentityEnvelope,
                       additivity: str | None = None, unit: str | None = None,
                       currency: str | None = None, entity: str | None = None,
                       is_as_of: bool | None = None,
                       as_of_basis: str | None = None) -> dict | None:
    """Fill in a column's declared semantics: update the provided graph_node fields, rebuild
    the node's search_doc (#20 — entity feeds it), and append one SEMANTICS_COMPLETED event to
    the tamper-evident security_audit chain recording actor + every value declared (including
    as_of_basis, which is audited but never node-written — the basis is governed, see module
    docstring). Validation raises BEFORE any write, so a bad value writes NOTHING.

    Returns {field: value} for the node fields actually written, or None when no column node
    matches (caller 404s). Case-insensitive on object_ref, matching rebuild_search_doc.
    Idempotent-friendly: re-posting the same values re-writes the same state."""
    validate_semantic_values(additivity=additivity, as_of_basis=as_of_basis)
    provided = {name: value for name, value in (
        ("additivity", additivity), ("unit", unit), ("currency", currency),
        ("entity", entity), ("is_as_of", is_as_of)) if value is not None}
    node = conn.execute(
        "SELECT object_ref, table_name FROM graph_node WHERE catalog_source = %s "
        "AND lower(object_ref) = lower(%s) AND kind = 'column'",
        (catalog_source, object_ref)).fetchone()
    if node is None:
        return None
    ref, table = node
    if provided.get("is_as_of"):
        # A table asserts ONE availability axis (#17) — refuse a second is_as_of column
        # instead of silently creating the ambiguity validate_rows quarantines uploads for.
        other = conn.execute(
            "SELECT column_name FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
            "AND table_name = %s AND is_as_of AND object_ref <> %s",
            (catalog_source, table, ref)).fetchone()
        if other is not None:
            raise AsOfConflict(
                f"table '{table}' already has an as-of axis ('{other[0]}'); a table asserts "
                f"ONE availability basis — unset it first or complete that column instead")
    if provided:
        assignments = ", ".join(f"{_NODE_COLUMNS[name]} = %s" for name in provided)
        conn.execute(
            f"UPDATE graph_node SET {assignments} WHERE catalog_source = %s AND object_ref = %s",
            (*provided.values(), catalog_source, ref))
        rebuild_search_doc(conn, catalog_source, ref)
    declared = dict(provided)
    if as_of_basis:
        declared["as_of_basis"] = as_of_basis
    # decision='flagged' — the chain's closed vocabulary (denied | allowed_break_glass |
    # flagged) records a SUCCESSFUL action as reviewable evidence, the DEGRADED_RESOLVED /
    # allowed-AUDIT_READ precedent.
    record_security_event(
        conn, event_type="SEMANTICS_COMPLETED", actor=actor,
        attempted_action=f"complete_semantics on {catalog_source}:{ref}",
        decision="flagged",
        reason=", ".join(f"{k}={v}" for k, v in declared.items()),
        aggregate="graph_node", aggregate_id=f"{catalog_source}:{ref}")
    return provided
