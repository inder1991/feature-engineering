"""SE-4b — the authority-bootstrap funnel's read model: proposed concepts, grouped to confirm.

The measured problem (plan §1): every concept fact on the live catalog is ``llm/proposed`` and
every V2 operand's suggestion floor is ``declared`` — nothing can clear a floor until humans
confirm the load-bearing columns, and per-field confirmation does not scale. This queue makes
bulk BY-EXCEPTION confirmation possible without inventing any new authority machinery:

* grouped by CONCEPT, not column — "these N columns carry proposed ``customer_id``: confirm
  the batch, untick the exceptions";
* ordered by how LOAD-BEARING the concept is (how many V2 recipe operands reference it,
  computed from the registry) — the binder needs the ~dozens of referenced concepts confirmed,
  not the whole catalog; unreferenced concepts stay reachable behind ``include_unreferenced``;
* every row carries the exact CAS anchor (`latest_decision_id`, `evidence_set_hash`,
  `policy_version`) the field-correction command re-checks — the bulk POST echoes these per
  column, so a concurrent evidence append fails THAT column closed (409) without touching its
  batch siblings;
* the funnel metric (`active`, `human_confirmed`, `confirmed_share`) is what SE-0's
  authority-distribution gate watches move.

Read-scoped like every read model: a column the caller may not see is absent, not redacted.
All reads are batched — one query per store, never per column (the load-once rule).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from featuregen.overlay.upload.field_resolution import FIELD_POLICY_VERSION
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.read_scope import allowed_sensitivities

def _set_hash(triples) -> str:
    """`field_resolution._evidence_set_hash`'s formula over (producer, strength,
    proposed_value_hash) triples — order-independent. Kept formula-identical by a pin test
    rather than by constructing full FieldEvidence records for a hash input."""
    from featuregen.overlay.upload.field_resolution import canonical_hash

    return canonical_hash(sorted(f"{p}:{s}:{h}" for p, s, h in triples))


@dataclass(frozen=True, slots=True)
class ConceptConfirmationColumnV1:
    """One column awaiting a decision on its proposed concept, with its CAS anchor."""

    object_ref: str
    table: str
    column: str
    logical_ref: str
    evidence_id: str
    producer: str
    strength: str
    latest_decision_id: str | None
    evidence_set_hash: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class ConceptConfirmationGroupV1:
    concept: str
    operand_reference_count: int         # how many V2 operands reference this concept
    columns: tuple[ConceptConfirmationColumnV1, ...]


def _operand_reference_counts() -> Counter:
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    return Counter(op.concept for recipe in V2_RECIPES for op in recipe.operands)


def concept_confirmation_queue(conn, *, catalog_source: str, roles=(),
                               include_unreferenced: bool = False) -> dict:
    """The queue + the funnel metric for one catalog. Batched: three queries total."""
    columns = conn.execute(
        "SELECT object_ref, schema_name, table_name, column_name "
        "FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND visible_requires <@ %s "
        "ORDER BY table_name, column_name",
        (catalog_source, allowed_sensitivities(roles))).fetchall()
    by_ref: dict[str, tuple[str, str, str]] = {}
    for object_ref, schema_name, table_name, column_name in columns:
        logical = normalize_ref(catalog_source, schema_name, table_name, column_name)
        by_ref[logical] = (object_ref, table_name, column_name)
    refs = list(by_ref)

    evidence_rows = conn.execute(
        "SELECT logical_ref, evidence_id, producer, strength, proposed_value, "
        "       proposed_value_hash "
        "FROM field_evidence "
        "WHERE field_name = 'concept' AND lifecycle = 'active' AND logical_ref = ANY(%s) "
        "ORDER BY created_at, evidence_id",
        (refs,)).fetchall() if refs else []
    evidence_by_ref: dict[str, list] = {}
    for row in evidence_rows:
        evidence_by_ref.setdefault(row[0], []).append(row)

    decision_rows = conn.execute(
        "SELECT logical_ref, decision_event_id, event_type "
        "FROM field_decision_event "
        "WHERE field_name = 'concept' AND logical_ref = ANY(%s) "
        "ORDER BY created_at, decision_event_id",
        (refs,)).fetchall() if refs else []
    latest_decision: dict[str, tuple[str, str]] = {}
    for logical_ref, decision_event_id, event_type in decision_rows:
        latest_decision[logical_ref] = (decision_event_id, event_type)   # last write wins: newest

    reference_counts = _operand_reference_counts()
    pending: dict[str, list[ConceptConfirmationColumnV1]] = {}
    active_total = 0
    settled_total = 0
    for logical_ref, rows in evidence_by_ref.items():
        active_total += 1
        decision = latest_decision.get(logical_ref)
        # Settled = the AUTHORITY moved, read from the evidence itself: a human confirm appends
        # a human/confirmed ACTIVE row (and a reject retires the proposal from the active set
        # entirely, so the column exits the funnel's denominator). Decision-event types are NOT
        # the detector — a concept confirm cascades a re-resolution whose RESOLVED rows would
        # mask the confirm if we read the newest event.
        if any(r[3] != "proposed" for r in rows):
            settled_total += 1
            continue
        # The set hash over the SAME active rows the resolver reads — the formula of
        # field_resolution._evidence_set_hash verbatim (a test pins the equality), so the
        # anchor a client echoes back is the one the command's CAS recheck verifies.
        set_hash = _set_hash((r[2], r[3], r[5]) for r in rows)
        newest = rows[-1]                             # newest active proposal leads the group
        object_ref, table_name, column_name = by_ref[logical_ref]
        entry = ConceptConfirmationColumnV1(
            object_ref=object_ref, table=table_name, column=column_name,
            logical_ref=logical_ref, evidence_id=newest[1], producer=newest[2],
            strength=newest[3],
            latest_decision_id=decision[0] if decision else None,
            evidence_set_hash=set_hash, policy_version=FIELD_POLICY_VERSION)
        concept_value = str(newest[4]).strip('"')     # jsonb scalar round-trips quoted
        pending.setdefault(concept_value, []).append(entry)

    groups = [
        ConceptConfirmationGroupV1(
            concept=concept,
            operand_reference_count=reference_counts.get(concept, 0),
            columns=tuple(entries))
        for concept, entries in pending.items()
        if include_unreferenced or reference_counts.get(concept, 0) > 0
    ]
    groups.sort(key=lambda g: (-g.operand_reference_count, -len(g.columns), g.concept))
    omitted = sum(1 for concept in pending
                  if not include_unreferenced and reference_counts.get(concept, 0) == 0)

    return {
        "catalog_source": catalog_source,
        "groups": groups,
        "unreferenced_groups_omitted": omitted,      # honest: the filter names what it hid
        "funnel": {
            "active": active_total,
            "human_confirmed": settled_total,
            "confirmed_share": round(settled_total / active_total, 4) if active_total else 0.0,
        },
    }


__all__ = ["ConceptConfirmationColumnV1", "ConceptConfirmationGroupV1",
           "concept_confirmation_queue"]
