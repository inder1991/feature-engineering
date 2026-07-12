"""Resolve-and-project (spec §4/§6/§8): the payoff that turns per-field evidence into a decision +
a display projection, keeping DISPLAY strictly separate from AUTHORITY.

For each ``(logical_ref, field-with-a-policy)`` this:

1. reads the ACTIVE :mod:`field_evidence` for the field and projects it to resolver views;
2. resolves it with :func:`overlay.field_authority.resolve_field_authority` under the field's
   :func:`overlay.upload.field_policies.policy_for` policy;
3. records ONE immutable :func:`overlay.field_decision.record_field_decision` event carrying BOTH the
   display value hash and the load-bearing value hash (the latter present only when authority
   suffices); and
4. PROJECTS the DISPLAY value into the flat ``graph_node`` column AND sets a companion ``*_decision_id``
   link back to the decision.

The load-bearing invariant (must-prove #4/#5): the flat ``graph_node`` column is DISPLAY only — what a
reviewer sees. Whether a field may drive feature construction is read from the DECISION via
:func:`is_feature_eligible` (load-bearing value present), NEVER from the flat column. So an
LLM-proposed ``concept`` is SHOWN yet not feature-eligible.

``sensitivity`` is special (§7, review #8): the taxonomy ``sensitivity_floor`` evidence is mapped
from the CONCEPT vocab into ``safety_floor.SENSITIVITY_ORDER`` and run through
:func:`safety_floor.apply_sensitivity_floor` to set a most-restrictive ``effective_restriction`` — a
floor RESTRICTS but does not CERTIFY, so ``classification_status`` stays ``proposed`` until a
source/human sensitivity confirms.

SCHEMA IDENTITY (review #14): ``field_evidence.logical_ref`` is the case-folded
``overlay.upload.object_ref.normalize_ref`` (``source::schema.table.column``), while ``graph_node`` is
keyed ``(catalog_source, object_ref="schema.table.column")``. We reconcile by parsing the logical_ref
back to its components and matching ``graph_node.object_ref`` case-insensitively — the SAME normalized
identity, never a mismatch.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from featuregen.contracts import DbConn
from featuregen.overlay.field_authority import FieldResolution, resolve_field_authority
from featuregen.overlay.field_decision import (
    FieldDecisionEventType,
    read_field_decisions,
    record_field_decision,
)
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    canonical_hash,
    read_active_field_evidence,
    to_view,
)
from featuregen.overlay.safety_floor import apply_sensitivity_floor
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.object_ref import parse_ref

# Bumped when the policy set or the resolver's projection contract changes (recorded on each decision).
FIELD_POLICY_VERSION = "upload-field-policy-v1"
RESOLVER_VERSION = "upload-resolve-and-project-v1"

_SENSITIVITY_FIELD = "sensitivity"
_SENSITIVITY_FLOOR_FIELD = "sensitivity_floor"

# CONCEPT sensitivity vocab -> safety_floor.SENSITIVITY_ORDER. A concept-registry floor speaks the
# concept vocab (public|pii|protected_attribute|special_category|proxy); the safety floor speaks
# SENSITIVITY_ORDER (public<internal<confidential<restricted<prohibited). Mapping is fail-safe: any
# unmapped label falls through to apply_sensitivity_floor, which itself fails closed to `prohibited`.
_CONCEPT_SENSITIVITY_TO_RESTRICTION: dict[str, str] = {
    "public": "public",
    "proxy": "confidential",
    "pii": "restricted",
    "protected_attribute": "restricted",
    "special_category": "restricted",
}

# Which flat graph_node column each resolved field's DISPLAY value projects into. A field absent here
# is decision-only (recorded + auditable) with no flat display column to overwrite.
_DISPLAY_COLUMN: dict[str, str] = {
    "concept": "concept",
    "definition": "definition",
    "domain": "domain",
    "additivity": "additivity",
}

# The companion *_decision_id link column per projected field (the display ≠ authority pointer).
# ``logical_representation`` owns ``logical_type_decision_id``; ``semantic_type`` stays decision-only
# (recorded + is_feature_eligible-visible) to avoid two fields clobbering one link column.
_DECISION_LINK_COLUMN: dict[str, str] = {
    "concept": "concept_decision_id",
    "definition": "definition_decision_id",
    "domain": "domain_decision_id",
    "additivity": "additivity_decision_id",
    "logical_representation": "logical_type_decision_id",
}


def _graph_key(source: str, logical_ref: str) -> tuple[str, str]:
    """The ``(catalog_source, object_ref_lowercased)`` graph_node key for a ``logical_ref``.

    Parses the logical_ref back to its (already case-folded) ``schema.table.column`` and joins it the
    way ``graph.build_graph`` keys column nodes. ``catalog_source`` is the caller's ``source`` (the
    same string ``build_graph`` was called with)."""
    _ref_source, schema, table, column = parse_ref(logical_ref)
    object_ref = ".".join(p for p in (schema, table, column) if p is not None)
    return source, object_ref.lower()


def _evidence_set_hash(evidence: Sequence[FieldEvidence]) -> str:
    """A stable fingerprint of the active evidence SET the decision reasoned over — order-independent
    over each record's ``(producer, strength, proposed_value_hash)``. Changes iff the input set
    changes, so a replay can detect a drifted decision."""
    return canonical_hash(
        sorted(f"{e.producer}:{e.strength}:{e.proposed_value_hash}" for e in evidence)
    )


def _record(
    conn: DbConn,
    *,
    logical_ref: str,
    field_name: str,
    evidence: Sequence[FieldEvidence],
    display_value: str | None,
    load_bearing_value: str | None,
    conflict_status: str,
    reason_codes: Sequence[str],
    now: datetime | None,
) -> str:
    """Record one RESOLVED field-decision event and return its id. Display + load-bearing values are
    hashed with the SAME :func:`canonical_hash` the evidence store uses (``None`` -> ``None`` hash)."""
    return record_field_decision(
        conn,
        logical_ref=logical_ref,
        field_name=field_name,
        event_type=FieldDecisionEventType.RESOLVED,
        selected_evidence_ids=[e.evidence_id for e in evidence],
        evidence_set_hash=_evidence_set_hash(evidence),
        display_value_hash=canonical_hash(display_value) if display_value is not None else None,
        load_bearing_value_hash=(
            canonical_hash(load_bearing_value) if load_bearing_value is not None else None
        ),
        conflict_status=conflict_status,
        reason_codes=list(reason_codes),
        field_policy_version=FIELD_POLICY_VERSION,
        resolver_version=RESOLVER_VERSION,
        actor_ref=None,
        supersedes_event_id=None,
        now=now,
    )


def _project_display(
    conn: DbConn,
    *,
    source: str,
    logical_ref: str,
    field_name: str,
    display_value: str | None,
    decision_id: str,
) -> None:
    """Write the DISPLAY value into the flat ``graph_node`` column (when one exists) AND set the
    companion ``*_decision_id`` link. A field with only a link column (``logical_representation``) sets
    the link without touching a display column. Case-insensitive object_ref match (schema identity)."""
    catalog_source, object_ref_lc = _graph_key(source, logical_ref)
    display_col = _DISPLAY_COLUMN.get(field_name)
    link_col = _DECISION_LINK_COLUMN.get(field_name)
    assignments: list[str] = []
    params: list[object] = []
    if display_col is not None:
        assignments.append(f"{display_col} = %s")   # column names are internal constants, not input
        params.append(display_value)
    if link_col is not None:
        assignments.append(f"{link_col} = %s")
        params.append(decision_id)
    if not assignments:
        return
    params.extend([catalog_source, object_ref_lc])
    conn.execute(
        f"UPDATE graph_node SET {', '.join(assignments)} "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        params,
    )


def _resolve_generic_field(
    conn: DbConn, *, source: str, logical_ref: str, field_name: str, now: datetime | None
) -> None:
    """Resolve a generic policy field, record its decision, and project the display value."""
    policy = policy_for(field_name)
    if policy is None:
        return
    evidence = read_active_field_evidence(conn, logical_ref, field_name)
    resolution: FieldResolution = resolve_field_authority(
        [to_view(e) for e in evidence], policy,
        active_disqualifiers=active_disqualifiers_for(conn, logical_ref, field_name),
    )
    reason_codes = [resolution.unresolved_reason] if resolution.unresolved_reason else []
    conflict_status = resolution.unresolved_reason or "resolved"
    decision_id = _record(
        conn,
        logical_ref=logical_ref,
        field_name=field_name,
        evidence=evidence,
        display_value=resolution.display_value,
        load_bearing_value=resolution.load_bearing_value,
        conflict_status=conflict_status,
        reason_codes=reason_codes,
        now=now,
    )
    _project_display(
        conn,
        source=source,
        logical_ref=logical_ref,
        field_name=field_name,
        display_value=resolution.display_value,
        decision_id=decision_id,
    )


def _to_restriction(value: object) -> str:
    """Map a sensitivity label to safety_floor.SENSITIVITY_ORDER. Concept-vocab labels are mapped;
    a value already in SENSITIVITY_ORDER passes through; anything else falls through unchanged for
    :func:`apply_sensitivity_floor` to fail-close to ``prohibited``."""
    label = str(value)
    if label in _CONCEPT_SENSITIVITY_TO_RESTRICTION:
        return _CONCEPT_SENSITIVITY_TO_RESTRICTION[label]
    return label


def _resolve_sensitivity(
    conn: DbConn, *, source: str, logical_ref: str, now: datetime | None
) -> None:
    """Resolve ``sensitivity`` (spec §7): the taxonomy floor sets a most-restrictive
    ``effective_restriction`` via :func:`apply_sensitivity_floor`; a source/human classification is
    what CERTIFIES (``classification_status``). The floor RESTRICTS but does not CERTIFY."""
    floor_evidence = read_active_field_evidence(conn, logical_ref, _SENSITIVITY_FLOOR_FIELD)
    class_evidence = read_active_field_evidence(conn, logical_ref, _SENSITIVITY_FIELD)

    # Most-restrictive floor across all taxonomy-derived floors (>= public); evidence can only RAISE.
    floor_labels = [_to_restriction(e.proposed_value) for e in floor_evidence]
    floor = apply_sensitivity_floor("public", floor_labels, now=now)

    # A source/human classification is the only thing that CERTIFIES (never llm-alone, never taxonomy).
    sensitivity_policy = policy_for(_SENSITIVITY_FIELD)
    assert sensitivity_policy is not None  # the sensitivity field always has a registered policy
    class_views = [to_view(e) for e in class_evidence]
    classification = resolve_field_authority(
        class_views, sensitivity_policy,
        active_disqualifiers=active_disqualifiers_for(conn, logical_ref, _SENSITIVITY_FIELD),
    )
    proposals = [_to_restriction(v.value) for v in class_views]
    effective_restriction = apply_sensitivity_floor(floor, proposals, now=now)
    certified = classification.load_bearing_value is not None
    classification_status = "confirmed" if certified else "proposed"

    all_evidence = [*floor_evidence, *class_evidence]
    decision_id = _record(
        conn,
        logical_ref=logical_ref,
        field_name=_SENSITIVITY_FIELD,
        evidence=all_evidence,
        display_value=effective_restriction,          # the shown, floor-guaranteed restriction
        load_bearing_value=classification.load_bearing_value,  # None until a source/human certifies
        conflict_status="resolved" if certified else (classification.unresolved_reason or "floor_only"),
        reason_codes=[] if certified else [classification.unresolved_reason or "floor_only"],
        now=now,
    )

    catalog_source, object_ref_lc = _graph_key(source, logical_ref)
    conn.execute(
        "UPDATE graph_node SET effective_restriction = %s, classification_status = %s, "
        "sensitivity_decision_id = %s "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        (effective_restriction, classification_status, decision_id, catalog_source, object_ref_lc),
    )


def _active_field_names(conn: DbConn, logical_ref: str) -> set[str]:
    """The distinct ACTIVE evidence field_names for one object — the fields with something to resolve."""
    rows = conn.execute(
        "SELECT DISTINCT field_name FROM field_evidence "
        "WHERE logical_ref = %s AND lifecycle = 'active'",
        (logical_ref,),
    ).fetchall()
    return {r[0] for r in rows}


def resolve_and_project(
    conn: DbConn, *, source: str, logical_refs: Sequence[str], now: datetime | None = None
) -> None:
    """Resolve every policy field with active evidence for each ``logical_ref`` and project the
    display values into ``graph_node`` (spec §4/§6/§8).

    ``source`` is the graph's ``catalog_source`` (the string ``build_graph`` was called with).
    Generic fields go through :func:`_resolve_generic_field`; ``sensitivity`` (triggered by a
    ``sensitivity`` or a taxonomy ``sensitivity_floor`` proposal) goes through the §7 special case.
    ``sensitivity_floor`` is an INPUT to the sensitivity decision (``policy_for`` returns ``None``),
    never resolved as a field of its own."""
    now = now or datetime.now(UTC)
    for logical_ref in logical_refs:
        present = _active_field_names(conn, logical_ref)
        for field_name in sorted(present):
            if field_name in (_SENSITIVITY_FIELD, _SENSITIVITY_FLOOR_FIELD):
                continue  # sensitivity handled by its §7 special case below
            _resolve_generic_field(
                conn, source=source, logical_ref=logical_ref, field_name=field_name, now=now
            )
        if present & {_SENSITIVITY_FIELD, _SENSITIVITY_FLOOR_FIELD}:
            _resolve_sensitivity(conn, source=source, logical_ref=logical_ref, now=now)


# Lifecycle event types that RETIRE a decision — a retired decision never confers eligibility.
_RETIRED_EVENTS = frozenset(
    {
        FieldDecisionEventType.REJECTED.value,
        FieldDecisionEventType.STALED.value,
        FieldDecisionEventType.SUPERSEDED.value,
    }
)


def is_feature_eligible(conn: DbConn, logical_ref: str, field_name: str) -> bool:
    """Whether ``field_name`` on ``logical_ref`` may drive feature construction — the display ≠
    authority boundary (must-prove #4/#5).

    Reads the DECISION log (NEVER the flat ``graph_node`` column): eligible iff the LATEST decision
    exists, is not retired (rejected/staled/superseded), and carries a load-bearing value hash. A
    field with no decision, or whose latest decision has no load-bearing value (e.g. an LLM-proposed
    concept, or a proposed-taxonomy additivity derivation), is NOT eligible — fail-closed."""
    decisions = read_field_decisions(conn, logical_ref, field_name)
    if not decisions:
        return False
    latest = decisions[-1]  # read_field_decisions is oldest-first
    if latest.event_type in _RETIRED_EVENTS:
        return False
    return latest.load_bearing_value_hash is not None
