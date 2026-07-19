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
(governed) signal, and it is EQUIVALENT to ``read_column_facts``'s ``authority == "governed"``: it is
granted for exactly the GOVERNED fields that reader wires — the two decision fields it governs via
``is_feature_eligible`` (:data:`_GOVERNED_DECISION_FIELD` = ``additivity`` / ``logical_representation``)
plus the fact-governed ``is_grain`` / ``is_as_of``. The implication is ONE-DIRECTIONAL against
``is_feature_eligible``: ``resolved`` ⇒ ``is_feature_eligible`` (live + non-retired + load-bearing),
but NOT the converse — C1 may REFUSE (``hash_mismatch`` on a degraded/tampered read, or
``not_operational`` for a field ``read_column_facts`` treats as a hint) where ``is_feature_eligible``
is still ``True``. C1 never claims governed authority where ``read_column_facts`` says ``hint``.

# FOLLOW-UP (C0→C2-C4): the C0 snapshot builder (``feature_metadata_snapshot``) still sources value +
# governance from ``read_column_facts`` directly, which has NO tamper gate (no fork/hash/projection
# verification) — a tampered flat value or drifted evidence set is served unquestioned there. The
# later deliveries (C2-C4) should RE-SOURCE onto this module's verified reads so those gates protect
# the whole chain, not just C1's direct callers.

BASE fail-closed cases (C1-T1):

* NO decision -> ``status="no_decision"`` (value echoes the display column if any; producer/strength
  ``None``).
* RETIRED latest decision -> ``status="retired"``; never served as operational (producer/strength
  ``None``), consistent with ``is_feature_eligible`` being ``False``. The value may still echo the
  flat display column, but the status shows it is not load-bearing.
* genuine CONFLICT -> ``status="conflict"`` (the resolver could not pick one value).
* live but NON-operational -> ``status="no_value"`` (a RECOMMENDATION-ceiling field, or an
  OPERATIONAL field whose evidence did not satisfy the operational rule / a disqualifier fired);
  ``conflict_status`` carries the precise reason.
* live + load-bearing but NOT a governed decision projection (F2) -> ``status="not_operational"``: a
  field with a real load-bearing decision that ``read_column_facts`` nonetheless governs as a HINT
  (unit / currency / sensitivity / data_type / semantic_type / temporal_role / entity / …). The
  decision + evidence-derived producer/strength are carried, but no governed authority is claimed —
  so C1 AGREES with ``read_column_facts`` rather than fabricating governance over an unverified value.
* operational (GOVERNED) -> ``status="resolved"`` (a governed decision field with a hash-verified
  load-bearing value; ⇒ feature-eligible, see the one-directional note above).

Note on ``status="conflict"`` vs ``status="no_value"`` (a deliberate refinement of the spec's
shorthand "``conflict_status != 'resolved'`` -> conflict"): only the resolver's genuine ``conflict``
reason maps to ``"conflict"``; every other non-operational reason (authority-insufficient,
influence-not-operational, disqualified, specialized-fact, floor-only) maps to ``"no_value"`` so the
caller can tell an irreconcilable disagreement from a field that is simply not load-bearing. The raw
``conflict_status`` is carried verbatim either way.

FAIL-CLOSED VERIFICATION GATES (C1-T2). Before an assembled value is served as operational, three
verification gates run so an ambiguous / tampered / degraded read returns NO operational value with
a distinct reason — never a fabricated authority. Each sets ``value=None`` + ``producer/strength``
None and carries a machine reason in ``conflict_status`` (see :func:`_fail_closed`):

* GATE 3 — ``status="projection_unavailable"``: the load-bearing overlay projection is DEGRADED or
  LAGGED at read time (reused verbatim from :func:`check_projection_readiness`), so every downstream
  read is potentially stale. Checked FIRST, before any read is trusted.
* GATE 1 — ``status="fork"``: the decision log has no single unambiguous latest non-retired head
  (see :func:`_forked_head_reason`).
* GATE 2 — ``status="hash_mismatch"``: the head's evidence-set hash, or (where verifiable) its
  served value hash, does not recompute under the pinned resolver (see
  :func:`_hash_mismatch_reason`).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.field_decision import (
    FieldDecisionEvent,
    FieldDecisionEventType,
    read_field_decisions,
)
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    canonical_hash,
    read_active_field_evidence,
)
from featuregen.overlay.identity import fact_key as _fact_key
from featuregen.overlay.upload.column_authority import _DECISION_ID_COLUMN, read_column_facts
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CatalogProjectionUnavailable,
    check_projection_readiness,
)
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_resolution import (
    _SENSITIVITY_FIELD,
    _SENSITIVITY_FLOOR_FIELD,
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
    _evidence_set_hash,
)
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

# The GOVERNED decision fields (F2): EXACTLY the fields ``read_column_facts`` treats as
# ``authority == "governed"`` via ``is_feature_eligible`` — its ``_DECISION_ID_COLUMN`` keys
# (``additivity`` / ``logical_representation``). DERIVED from that shipped mapping so C1 can never
# DRIFT from ``read_column_facts``: a live, non-retired, load-bearing decision confers the
# operational ``status="resolved"`` (governed) ONLY for a field in this set, and its served value is
# verified against the decision's ``load_bearing_value_hash`` (GATE 2). EVERY OTHER policy field
# (unit / currency / sensitivity / data_type / semantic_type / temporal_role / entity / …) is
# ``authority == "hint"`` in ``read_column_facts`` — C1 returns ``status="not_operational"`` for
# those, so the two readers AGREE (no fabricated governance, no unverified load-bearing value served
# as authoritative). The fact-governed fields (``is_grain`` / ``is_as_of``) are governed on their own
# SPECIALIZED_FACT path below (non-null ``*_fact_event_id``), never through this set. The equivalence
# ``C1 governed ⇔ read_column_facts governed`` is asserted by the guard test in
# test_operational_facts_fail_closed.
_GOVERNED_DECISION_FIELD: frozenset[str] = frozenset(_DECISION_ID_COLUMN)


def _fail_closed(
    field_name: str,
    influence: InfluenceTier,
    *,
    status: str,
    reason: str | None,
    decision_event_id: str | None = None,
    selected_evidence_ids: tuple[str, ...] = (),
) -> OperationalValue:
    """A fail-CLOSED :class:`OperationalValue`: NO operational value is served (``value`` /
    ``producer`` / ``strength`` all ``None``), the gate's ``status`` is set, and the machine
    ``reason`` is carried verbatim in ``conflict_status``. Authority is NEVER manufactured — the
    ``policy``-derived ``influence`` and the carried audit ids (``decision_event_id`` /
    ``selected_evidence_ids``) are metadata for traceability, not an operational value."""
    return OperationalValue(
        value=None, influence=influence, producer=None, strength=None, status=status,
        conflict_status=reason, selected_evidence_ids=selected_evidence_ids,
        decision_event_id=decision_event_id, fact_key=None, fact_event_id=None,
        policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
    )


def _forked_head_reason(decisions: list[FieldDecisionEvent]) -> str | None:
    """GATE 1 — detect an AMBIGUOUS decision head. Return a machine reason if the "latest
    non-retired head" for the field is not a single, unambiguous decision; else ``None``.

    How a fork can manifest given the write path. ``field_resolution`` appends one ``RESOLVED``
    event with ``supersedes_event_id=None`` per resolve, and ONLY a retiring ``STALED`` event
    supersedes a prior decision — so MULTIPLE non-superseding decisions are NORMAL and are
    disambiguated by "latest wins" (``read_field_decisions`` is ``ORDER BY created_at,
    decision_event_id``; ``decisions[-1]`` is the head). Under those invariants the head is always
    a strict, unique maximum, so a genuine fork is structurally impossible from normal resolution.
    These are therefore DEFENSIVE guards that fail closed if an invariant is ever violated (a pinned
    clock, tampering, a concurrent writer):

    * (A) TEMPORAL TIE — the append path stamps a DISTINCT per-decision ``created_at`` (the
      ``field_decision`` now-seam invariant), so the ordered head is normally a strict maximum. If
      >=2 decisions share the maximum ``created_at`` AND disagree on their operational outcome
      ``(is_retired, load_bearing_value_hash)``, the head is chosen only by an arbitrary
      ``decision_event_id`` tiebreak — an ambiguous head.
    * (B) FORKED SUPERSESSION CHAIN — a single parent decision superseded by >=2 NON-RETIRED
      branches. The write path supersedes a parent at most once (only via a retiring ``STALED``),
      so competing non-retired branches off one parent is a structural fork."""
    max_created = decisions[-1].created_at   # read_field_decisions is ORDER BY created_at, id
    top = [d for d in decisions if d.created_at == max_created]
    if len(top) > 1:
        outcomes = {
            (d.event_type in _RETIRED_EVENTS, d.load_bearing_value_hash) for d in top
        }
        if len(outcomes) > 1:
            return "forked_decision_head"
    branches = Counter(
        d.supersedes_event_id
        for d in decisions
        if d.supersedes_event_id is not None and d.event_type not in _RETIRED_EVENTS
    )
    if any(count > 1 for count in branches.values()):
        return "forked_supersession_chain"
    return None


def _named_active_evidence(
    conn: DbConn, logical_ref: str, field_name: str, selected: set[str]
) -> list[FieldEvidence]:
    """The currently-ACTIVE evidence records the decision NAMED (``selected``), gathered over the
    SAME field(s) the resolver hashed. All fields are single-field EXCEPT ``sensitivity`` (F1): its
    decision's evidence set spans BOTH the taxonomy ``sensitivity_floor`` AND the ``sensitivity``
    classification — :func:`field_resolution._resolve_sensitivity` records the decision over
    ``[*floor_evidence, *class_evidence]`` (two field_names). Mirroring that gather order here means
    the recomputed :func:`_evidence_set_hash` covers the same set; a single-``sensitivity``-field
    recompute could NEVER match a floor-carrying sensitivity decision, so every legitimately
    certified ``sensitivity`` would false-positive as ``hash_mismatch``. ``_evidence_set_hash`` is
    order-independent, so the gather order is not load-bearing — it is kept identical for clarity."""
    if field_name == _SENSITIVITY_FIELD:
        active = [
            *read_active_field_evidence(conn, logical_ref, _SENSITIVITY_FLOOR_FIELD),
            *read_active_field_evidence(conn, logical_ref, _SENSITIVITY_FIELD),
        ]
    else:
        active = read_active_field_evidence(conn, logical_ref, field_name)
    return [e for e in active if e.evidence_id in selected]


def _hash_mismatch_reason(
    conn: DbConn,
    logical_ref: str,
    field_name: str,
    decision: FieldDecisionEvent,
    value: object | None,
) -> str | None:
    """GATE 2 — verify the operational head's hashes under the pinned resolver. Return a machine
    reason on ANY mismatch (the value is NOT authoritative), else ``None``. Called only when the
    head would otherwise be served as a GOVERNED ``resolved`` value.

    * EVIDENCE-SET integrity (field-agnostic): recompute :func:`_evidence_set_hash` (REUSED from
      ``field_resolution`` — never re-implemented) over the ACTIVE evidence the decision NAMED
      (:func:`_named_active_evidence` — which spans the two ``sensitivity`` field_names, F1) and
      compare to the decision's stored ``evidence_set_hash``. A drifted or tampered evidence set no
      longer verifies.
    * VALUE integrity (scoped to :data:`_GOVERNED_DECISION_FIELD`): recompute
      ``canonical_hash(value)`` and compare to the decision's ``load_bearing_value_hash`` — a
      tampered flat value no longer hashes to the decision-authorized value. This covers ALL governed
      decision fields, so a tampered ``logical_representation`` value is caught too (F3). Only a
      governed field reaches this gate, so a field whose flat value is not a verified decision
      projection is never served as an unverified load-bearing value in the first place (F2)."""
    selected = set(decision.selected_evidence_ids)
    named = _named_active_evidence(conn, logical_ref, field_name, selected)
    if _evidence_set_hash(named) != decision.evidence_set_hash:
        return "evidence_set_hash_mismatch"
    if field_name in _GOVERNED_DECISION_FIELD and (
        canonical_hash(value) != decision.load_bearing_value_hash
    ):
        return "value_hash_mismatch"
    return None


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

    READ-ONLY. See the module docstring for the axis sources, the base fail-closed cases, and the
    three C1-T2 verification gates (projection-health / fork / hash) that serve NO operational value
    on a degraded / ambiguous / tampered read. The ``value`` axis is sourced from
    :func:`read_column_facts` so it is byte-for-byte consistent with the shipped
    ``OperationalColumnFacts.value``; ``status == "resolved"`` is granted for exactly the fields
    ``read_column_facts`` governs (``authority == "governed"``) and IMPLIES ``is_feature_eligible``,
    but not conversely — C1 may refuse (``not_operational`` / ``hash_mismatch``) where eligibility
    still holds (see the module docstring's one-directional note)."""
    policy = policy_for(field_name)
    # No policy => the field is not resolver-owned; treat as display-only (the lowest tier).
    # SPECIALIZED_FACT fields (is_grain/is_as_of) land here too — their operational authority is
    # carried on the fact_key/fact_event_id axes + status, not the influence ceiling.
    influence = policy.influence_max if policy is not None else InfluenceTier.DISPLAY

    # GATE 3 (projection health) — checked FIRST, before any read is trusted. A DEGRADED (poisoned)
    # or LAGGED load-bearing overlay projection makes EVERY downstream read (the flat graph_node
    # value AND the field-decision log) potentially STALE, so nothing can be served as operational.
    # Reuse the shipped readiness gate verbatim (never re-implement projection health); on
    # unavailability fail closed, carrying the projection's own detail as the reason.
    try:
        check_projection_readiness(conn)
    except CatalogProjectionUnavailable as exc:
        return _fail_closed(
            field_name, influence, status="projection_unavailable", reason=exc.detail
        )

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

    # GATE 1 (fork / ambiguous head) — the head must be a SINGLE unambiguous latest non-retired
    # decision. Checked before the retired/resolved logic so a tie between a retired and a
    # non-retired decision at the head is caught rather than silently resolved by the id tiebreak.
    fork_reason = _forked_head_reason(decisions)
    if fork_reason is not None:
        return _fail_closed(field_name, influence, status="fork", reason=fork_reason)

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
        # F2: a load-bearing head is served as GOVERNED ``resolved`` ONLY for a field
        # ``read_column_facts`` also governs (:data:`_GOVERNED_DECISION_FIELD`). For ANY other policy
        # field (unit/currency/sensitivity/data_type/…) ``read_column_facts`` says ``hint``, so C1
        # reports ``not_operational`` — the decision + evidence-derived producer/strength are carried
        # for traceability, but NO governed authority is claimed and the flat value is not served as
        # a load-bearing claim (it stays the same display/hint value ``read_column_facts`` returns).
        status = (
            "resolved" if field_name in _GOVERNED_DECISION_FIELD else "not_operational"
        )
    elif latest.conflict_status == "conflict":
        status = "conflict"                       # irreconcilable disagreement in the evidence
    else:
        status = "no_value"                       # live but non-operational (see module docstring)

    # GATE 2 (hash verification) — only a GOVERNED "resolved" head carries a load-bearing value
    # served as operational; not_operational/conflict/no_value are already non-operational. Verify
    # the head's hashes recompute under the pinned resolver before serving; any mismatch fails closed
    # as "hash_mismatch".
    if status == "resolved":
        hash_reason = _hash_mismatch_reason(conn, logical_ref, field_name, latest, value)
        if hash_reason is not None:
            return _fail_closed(
                field_name, influence, status="hash_mismatch", reason=hash_reason,
                decision_event_id=latest.decision_event_id,
                selected_evidence_ids=latest.selected_evidence_ids,
            )

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
