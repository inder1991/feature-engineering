"""Delivery C1 — the operational-facts read adapter: the RICH per-field operational authority.

Slice 3 shipped :class:`overlay.upload.column_authority.OperationalColumnFacts` — a three-axis
``{value, authority, provenance}`` view over one column field. It EXTENDS that shipped read
(it does NOT replace it) into the 12-axis :class:`OperationalValue` the catalog APIs and feature
generation consume: the SAME value, plus the influence ceiling, the selected evidence's
producer/strength, the decision-lifecycle status, the conflict status, the selected-evidence +
decision-event audit ids, the governed fact key/event id, and the policy/resolver versions.

READ-ONLY. No writes, no migration. Every axis is ASSEMBLED from the already-shipped reads —
authority is never re-implemented here:

* :func:`column_authority.read_column_facts` — the SINGLE value source (and the governed *_fact
  provenance). ``OperationalValue.value`` is byte-for-byte ``OperationalColumnFacts.value``
  for the same ``(logical_ref, field_name)``; value coverage equals ``read_column_facts``'s.
* :func:`field_decision.read_field_decisions` — the append-only decision lifecycle (oldest-first).
* :func:`field_evidence.read_active_field_evidence` — the selected evidence's producer/strength.
* :func:`field_policies.policy_for` — the influence ceiling (``policy.influence_max``).

Authority is NEVER manufactured from timestamp ordering: producer/strength/status come only from the
actual selected evidence + the decision lifecycle. ``status == "resolved"`` is the operational
signal and is exactly ``is_feature_eligible`` (a live, non-retired decision carrying a load-bearing
value) — a strict superset of the shipped ``authority == "governed"`` boolean.

BASE fail-closed cases (this task). The harder hardening — fork detection, hash-verifying the
decision's load-bearing hash against the projected value, and degraded-projection handling — is
C1-T2:

* NO decision -> ``status="no_decision"`` (value echoes the display column if any; producer/strength
  ``None``).
* RETIRED latest decision -> ``status="retired"``; never served as operational (producer/strength
  ``None``), consistent with ``is_feature_eligible`` being ``False``. The value may still echo the
  flat display column, but the status shows it is not load-bearing.
* genuine CONFLICT -> ``status="conflict"`` (the resolver could not pick one value).
* live but NON-operational -> ``status="no_value"`` (a RECOMMENDATION-ceiling field, or an
  OPERATIONAL field whose evidence did not satisfy the operational rule / a disqualifier fired);
  ``conflict_status`` carries the precise reason.
* operational -> ``status="resolved"`` (<=> a load-bearing value <=> feature-eligible).

Note on ``status="conflict"`` vs ``status="no_value"`` (a deliberate refinement of the spec's
shorthand "``conflict_status != 'resolved'`` -> conflict"): only the resolver's genuine ``conflict``
reason maps to ``"conflict"``; every other non-operational reason (authority-insufficient,
influence-not-operational, disqualified, specialized-fact, floor-only) maps to ``"no_value"`` so the
caller can tell an irreconcilable disagreement from a field that is simply not load-bearing. The raw
``conflict_status`` is carried verbatim either way.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.field_decision import FieldDecisionEventType, read_field_decisions
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.identity import fact_key as _fact_key
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_resolution import FIELD_POLICY_VERSION, RESOLVER_VERSION
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.upload_catalog import table_ref

# The SPECIALIZED_FACT column fields: authority comes from the governed *_fact_event_id link (the
# grain/availability fact stream), NEVER the generic field-decision log. They have no policy; the
# resolver never decides them. Mirrors column_authority._FACT_EVENT_COLUMN; the fact_type is the
# string the overlay fact stream keys on (table_fact_projection).
_FACT_FIELD_TYPE: dict[str, str] = {
    "is_grain": "grain",
    "is_as_of": "availability_time",
}

# Lifecycle event types that RETIRE a decision (a retired decision is never served as operational).
# Mirrors field_resolution._RETIRED_EVENTS — named here from the enum, not re-derived.
_RETIRED_EVENTS = frozenset(
    {
        FieldDecisionEventType.REJECTED.value,
        FieldDecisionEventType.STALED.value,
        FieldDecisionEventType.SUPERSEDED.value,
    }
)

# Weakest -> strongest: a total order over AssertionStrength for "the strongest selected evidence".
_STRENGTH_ORDER: tuple[AssertionStrength, ...] = (
    AssertionStrength.PROPOSED,
    AssertionStrength.SUPPORTED,
    AssertionStrength.ATTESTED,
    AssertionStrength.CONFIRMED,
)


@dataclass(frozen=True)
class OperationalValue:
    """The rich 12-axis operational authority for one ``(logical_ref, field_name)`` (Delivery C1).

    A superset of :class:`column_authority.OperationalColumnFacts`: ``value`` is the same flat
    display value; the remaining axes expose the influence ceiling, the winning evidence's
    producer/strength, the decision-lifecycle ``status`` (+ raw ``conflict_status``), the
    selected-evidence + decision-event audit ids, the governed ``fact_key``/``fact_event_id``, and
    the policy/resolver versions the read was assembled under."""

    value: object | None
    influence: InfluenceTier
    producer: EvidenceProducer | None
    strength: AssertionStrength | None
    status: str
    conflict_status: str | None
    selected_evidence_ids: tuple[str, ...]
    decision_event_id: str | None
    fact_key: str | None
    fact_event_id: str | None
    policy_version: str
    resolver_version: str | None


def _selected_authority(
    conn: DbConn, logical_ref: str, field_name: str, selected_ids: tuple[str, ...]
) -> tuple[EvidenceProducer | None, AssertionStrength | None]:
    """The (producer, strength) of the STRONGEST selected evidence for a LIVE decision.

    Resolves the decision's ``selected_evidence_ids`` against the currently-ACTIVE field evidence
    (via the shipped reader — never a raw query) and returns the strongest by
    :data:`_STRENGTH_ORDER`. ``(None, None)`` when the decision selected nothing, or when those
    records are no longer active (a degraded-projection case hardened in C1-T2)."""
    if not selected_ids:
        return None, None
    selected = set(selected_ids)
    candidates = [
        e for e in read_active_field_evidence(conn, logical_ref, field_name)
        if e.evidence_id in selected
    ]
    if not candidates:
        return None, None
    strongest = max(candidates, key=lambda e: _STRENGTH_ORDER.index(AssertionStrength(e.strength)))
    return EvidenceProducer(strongest.producer), AssertionStrength(strongest.strength)


def read_operational_value(conn: DbConn, logical_ref: str, field_name: str) -> OperationalValue:
    """Assemble the 12-axis :class:`OperationalValue` for ``(logical_ref, field_name)``.

    READ-ONLY. See the module docstring for the axis sources and the base fail-closed cases. The
    ``value`` axis is sourced from :func:`read_column_facts` so it is byte-for-byte consistent with
    the shipped ``OperationalColumnFacts.value``; ``status == "resolved"`` is exactly
    ``is_feature_eligible`` (a live, non-retired, load-bearing decision)."""
    policy = policy_for(field_name)
    # No policy => the field is not resolver-owned; treat as display-only (the lowest tier).
    # SPECIALIZED_FACT fields (is_grain/is_as_of) land here too — their operational authority is
    # carried on the fact_key/fact_event_id axes + status, not the influence ceiling.
    influence = policy.influence_max if policy is not None else InfluenceTier.DISPLAY

    # The SINGLE value source (+ the governed *_fact provenance for the fact fields below).
    col = read_column_facts(conn, logical_ref, field_name)
    value = col.value

    fact_type = _FACT_FIELD_TYPE.get(field_name)
    if fact_type is not None:
        # SPECIALIZED_FACT: governed iff the flag is true AND the *_fact_event_id link is non-null
        # (read_column_facts.authority == "governed"). Authority NEVER comes from the decision log.
        governed = col.authority == "governed"
        fact_event_id = col.provenance if governed else None
        governed_fact_key: str | None = None
        if governed:
            source, _schema, table, _column = parse_ref(logical_ref)
            governed_fact_key = _fact_key(table_ref(source, table), fact_type)
        status = "resolved" if governed else ("no_value" if value is not None else "no_decision")
        return OperationalValue(
            value=value, influence=influence, producer=None, strength=None, status=status,
            conflict_status=None, selected_evidence_ids=(), decision_event_id=None,
            fact_key=governed_fact_key, fact_event_id=fact_event_id,
            policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        )

    # Generic (decision-governed) fields: authority is the LATEST field-decision event.
    decisions = read_field_decisions(conn, logical_ref, field_name)
    if not decisions:
        return OperationalValue(
            value=value, influence=influence, producer=None, strength=None, status="no_decision",
            conflict_status=None, selected_evidence_ids=(), decision_event_id=None, fact_key=None,
            fact_event_id=None, policy_version=FIELD_POLICY_VERSION,
            resolver_version=RESOLVER_VERSION,
        )

    latest = decisions[-1]  # read_field_decisions is oldest-first
    if latest.event_type in _RETIRED_EVENTS:
        # Fail-closed: a retired decision is never served as operational — producer/strength stay
        # None (no manufactured authority). The value may still echo the flat display column.
        return OperationalValue(
            value=value, influence=influence, producer=None, strength=None, status="retired",
            conflict_status=latest.conflict_status,
            selected_evidence_ids=latest.selected_evidence_ids,
            decision_event_id=latest.decision_event_id, fact_key=None, fact_event_id=None,
            policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        )

    if latest.load_bearing_value_hash is not None:
        status = "resolved"                       # <=> is_feature_eligible (live + load-bearing)
    elif latest.conflict_status == "conflict":
        status = "conflict"                       # irreconcilable disagreement in the evidence
    else:
        status = "no_value"                       # live but non-operational (see module docstring)

    producer, strength = _selected_authority(
        conn, logical_ref, field_name, latest.selected_evidence_ids
    )
    return OperationalValue(
        value=value, influence=influence, producer=producer, strength=strength, status=status,
        conflict_status=latest.conflict_status,
        selected_evidence_ids=latest.selected_evidence_ids,
        decision_event_id=latest.decision_event_id, fact_key=None, fact_event_id=None,
        policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
    )
