"""Display-axis projection — the per-column axes, filled from what the platform already knows
(ingestion-richness Task 3).

``project_display_axes(conn, catalog_source)`` runs four fill-only-NULL passes over one catalog's
column nodes and returns an :class:`AxisProjectionReport` (the provenance record — there are NO
decision-id stamps: this is a projection, not a decision):

* **sensitivity_display** — a dedicated display column (migration 1042), deliberately NOT
  ``graph_node.sensitivity``: that column is the read-scope TAG (0993 CHECK) and an INPUT to the
  GENERATED enforcement column ``visible_requires`` (1032), so writing display labels there would
  either violate the CHECK ('confidential') or CHANGE enforcement. Precedence: non-empty
  ``visible_requires`` → its strongest label; else the concept registry's class via the existing
  ``_CONCEPT_SENSITIVITY_TO_RESTRICTION`` mapping; else NULL — unknown is honest.
* **entity** — an identifier concept's ``entity_link``, display/planning context ONLY: the governed
  ``entity_assignment`` path stays the Delivery-E command, so any column under a governed entity
  fact (``entity_fact_key`` set, or ``entity_status='VERIFIED'``) is skipped-loud, never filled.
  ``entity`` feeds ``_SEARCH_DOC``'s domain slot (invariant #20), so every node this pass changes
  gets ``rebuild_search_doc`` in the same transaction.
* **additivity** — the concept registry's default (``'n/a'`` is not a default); an uploaded/human/
  resolved value is never overwritten (the fill-only-NULL guard is the whole rule).
* **party_role** — Task 1's deterministic ``normalize_party_role`` over the column NAME; ``None``
  (ambiguity) abstains. ADVISORY by contract: nothing may consume it in a join-candidacy or
  execution predicate (import-gated in the tests).

The projection is idempotent (every UPDATE is guarded ``WHERE <axis> IS NULL`` in SQL, so a re-run
— or a concurrent writer having filled the value first — changes nothing) and catalog-scoped. It
NEVER writes ``sensitivity``, ``effective_restriction`` or (therefore) ``visible_requires``.

``type_unknown`` (Step 5 honesty) rides the same report: the columns with neither an attested
``data_type`` nor a glossary ``declared_type`` — the uploader's fix list, surfaced in the
``axis_projection`` stage detail rather than silently rendered as "unknown".
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.safety_floor import SENSITIVITY_ORDER
from featuregen.overlay.upload.concepts import concept, display_entity

# The ONE concept-class -> restriction-level mapping (plan Step 2: do NOT build a new table).
from featuregen.overlay.upload.field_resolution import _CONCEPT_SENSITIVITY_TO_RESTRICTION
from featuregen.overlay.upload.graph import rebuild_search_doc
from featuregen.overlay.upload.party_vocab import normalize_party_role

_DISPLAY_RANK = {label: i for i, label in enumerate(SENSITIVITY_ORDER)}
# The display levels that mean something is required/known. 'public' maps to itself in the concept
# mapping but is NOT a display value — no restriction is honest NULL, never a badge.
_DISPLAY_LABELS = frozenset({"confidential", "restricted", "prohibited"})


@dataclass(frozen=True, slots=True)
class AxisSkip:
    """One recorded abstention — a column an axis pass deliberately did NOT fill, with the reason.
    Only genuine cant-fill cases are recorded (e.g. a governed entity fact owns the column); a
    column with nothing to say (no concept, no tokens) is simply not a candidate, not a skip."""

    object_ref: str
    axis: str
    reason: str


@dataclass(frozen=True, slots=True)
class AxisProjectionReport:
    """What one projection run actually set, per axis, as object_refs — the provenance record the
    stage detail summarizes (no decision-id columns are stamped; this is a projection)."""

    sensitivity_set: tuple[str, ...]
    entity_set: tuple[str, ...]
    additivity_set: tuple[str, ...]
    party_role_set: tuple[str, ...]
    skipped: tuple[AxisSkip, ...]
    type_unknown: tuple[str, ...]

    def stage_detail(self) -> dict:
        """A SMALL stage-detail dict (#22 house rule: counts, bounded lists, never row data).
        Zero-valued optional keys are omitted so the run manifest stays readable."""
        detail: dict = {
            "sensitivity_set": len(self.sensitivity_set),
            "entity_set": len(self.entity_set),
            "additivity_set": len(self.additivity_set),
            "party_role_set": len(self.party_role_set),
        }
        if self.skipped:
            reasons: dict[str, int] = {}
            for s in self.skipped:
                reasons[f"{s.axis}:{s.reason}"] = reasons.get(f"{s.axis}:{s.reason}", 0) + 1
            detail["skipped"] = len(self.skipped)
            detail["skip_reasons"] = reasons
        if self.type_unknown:
            detail["type_unknown_count"] = len(self.type_unknown)
            detail["type_unknown"] = list(self.type_unknown[:50])   # the fix list, bounded
        return detail


def _strongest_display(visible_requires: list[str]) -> str:
    """The strongest DISPLAY label for a non-empty enforcement set. Elements are the 1032
    vocabulary ('pii'/'restricted' tags, 'confidential'/'restricted'/'prohibited' floor levels);
    the tag 'pii' displays at its registry level ('restricted'). An unrecognized label fails
    closed to 'prohibited' (the safety_floor rule) — display must never understate."""
    mapped = [_CONCEPT_SENSITIVITY_TO_RESTRICTION.get(label, label) for label in visible_requires]
    mapped = [m if m in _DISPLAY_RANK else "prohibited" for m in mapped]
    return max(mapped, key=_DISPLAY_RANK.__getitem__)


def _concept_display(concept_name: str | None) -> str | None:
    """The concept-class display default ('restricted'/'confidential'), or None (public/unknown —
    honest NULL, never a guess)."""
    if not concept_name:
        return None
    record = concept(concept_name)
    if record is None:
        return None
    label = _CONCEPT_SENSITIVITY_TO_RESTRICTION.get(record.sensitivity)
    return label if label in _DISPLAY_LABELS else None


def _fill(conn, catalog_source: str, object_ref: str, column: str, value: str,
          extra_guard: str = "") -> bool:
    """One guarded fill-only-NULL UPDATE; True iff THIS call set the value (idempotency +
    concurrency safe — the guard re-checks in SQL). ``column`` is an internal constant, never
    input."""
    row = conn.execute(
        f"UPDATE graph_node SET {column} = %s "
        f"WHERE catalog_source = %s AND object_ref = %s AND kind = 'column' "
        f"AND ({column} IS NULL OR {column} = ''){extra_guard} RETURNING object_ref",
        (value, catalog_source, object_ref)).fetchone()
    return row is not None


def project_display_axes(conn, catalog_source: str) -> AxisProjectionReport:
    """Run the four display-axis passes for ONE catalog (see module docstring). Idempotent,
    fill-only-NULL, never touches enforcement (``sensitivity``/``visible_requires``) or a governed/
    human/uploaded value; every entity change rebuilds the node's search doc (#20)."""
    rows = conn.execute(
        "SELECT object_ref, column_name, concept, sensitivity_display, visible_requires, entity,"
        " entity_fact_key, entity_status, additivity, additivity_decision_id, party_role,"
        " data_type, declared_type"
        " FROM graph_node WHERE catalog_source = %s AND kind = 'column' ORDER BY object_ref",
        (catalog_source,)).fetchall()

    sensitivity_set: list[str] = []
    entity_set: list[str] = []
    additivity_set: list[str] = []
    party_role_set: list[str] = []
    skipped: list[AxisSkip] = []
    type_unknown: list[str] = []

    for (ref, column_name, concept_name, sensitivity_display, visible_requires, entity,
         entity_fact_key, entity_status, additivity, additivity_decision_id, party_role,
         data_type, declared_type) in rows:
        record = concept(concept_name) if concept_name else None

        # ── sensitivity display: enforcement first, else the concept class, else honest NULL ──
        if not sensitivity_display:
            display = (_strongest_display(list(visible_requires)) if visible_requires
                       else _concept_display(concept_name))
            if display is not None and _fill(conn, catalog_source, ref,
                                             "sensitivity_display", display):
                sensitivity_set.append(ref)

        # ── entity: identifier concepts' entity_link; the governed fact path is never touched ──
        if record is not None and record.group == "identifier" and record.entity_link:
            if entity_fact_key is not None or entity_status == "VERIFIED":
                if not entity:
                    # governed-owned but blank on the flat node (e.g. projection lag): recorded,
                    # never filled — the governed re-projection owns this column.
                    skipped.append(AxisSkip(ref, "entity", "entity_fact_present"))
            elif not entity and _fill(
                    conn, catalog_source, ref, "entity",
                    # NEW enrichment projections resolve the DISPLAY entity through the alias
                    # seam (semantic Task 2 / D12.1): counterparty_id fills `customer`.
                    display_entity(concept_name, record.entity_link),
                    extra_guard=(" AND entity_fact_key IS NULL"
                                 " AND entity_status IS DISTINCT FROM 'VERIFIED'")):
                entity_set.append(ref)
                # entity feeds the search doc's domain slot — same expression, same tx (#20).
                rebuild_search_doc(conn, catalog_source, ref)

        # ── additivity: the registry default; 'n/a' is not a default. A NULL display whose
        # additivity_decision_id is SET is not blank — the resolver deliberately cleared it
        # (a retired derivation / pending revalidation); the concept default must not shadow
        # that lifecycle. Skipped-loud, never silent. ──
        if (record is not None and not additivity
                and record.additivity in ("additive", "semi_additive", "non_additive")):
            if additivity_decision_id is not None:
                skipped.append(AxisSkip(ref, "additivity", "decision_cleared"))
            elif _fill(conn, catalog_source, ref, "additivity", record.additivity,
                       extra_guard=" AND additivity_decision_id IS NULL"):
                additivity_set.append(ref)

        # ── party_role: deterministic tokens; ambiguity abstains ──
        if not party_role:
            role = normalize_party_role(column_name)
            if role is not None and _fill(conn, catalog_source, ref, "party_role", role.value):
                party_role_set.append(ref)

        # ── Step 5 honesty: neither an attested type nor a declared one — the fix list ──
        if (data_type is None or str(data_type).strip().lower() in ("", "unknown")) \
                and not declared_type:
            type_unknown.append(ref)

    return AxisProjectionReport(
        sensitivity_set=tuple(sensitivity_set),
        entity_set=tuple(entity_set),
        additivity_set=tuple(additivity_set),
        party_role_set=tuple(party_role_set),
        skipped=tuple(skipped),
        type_unknown=tuple(type_unknown))
