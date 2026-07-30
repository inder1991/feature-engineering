"""Learning events — what a blocked question taught us about the ontology.

Three kinds of thing come out of an analysis, and conflating them is the failure this module exists
to prevent:

* a **data observation** is evidence about DATA (`store.py`);
* a **learning gap** is evidence that the ONTOLOGY or its configuration is incomplete — this module;
* a **technical failure** (Hive unreachable, Spark job died, generated project invalid) is neither.
  It belongs to run diagnostics. A connection timeout must never become "customer relationship
  missing", or the ontology fills with candidates manufactured by an outage.

The vocabulary is closed and a technical code is refused, so that boundary is enforced rather than
documented.

**Events are immutable.** Resolving a gap writes a RESOLUTION event referencing the original; it
never updates or deletes it. The question "what did we not know when that decision was made?" has to
stay answerable, and an overwritten record cannot answer it.

**Gap identity is separate from demand.** The same gap blocking two different questions is ONE thing
to decide with TWO questions waiting on it — which is exactly the signal a later projector needs to
prioritise. So identity is `(code, subject_refs)`, and demand is the count of distinct requests
carrying it. No LLM is involved: these are noticed deterministically by the planner.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class LearningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class LearningStage(StrEnum):
    GROUNDING = "grounding"
    PLANNING = "planning"
    VALIDATION = "validation"


class RequiredAction(StrEnum):
    """What a human would have to do to unblock the question."""

    CONFIRM_BUSINESS_POLICY = "confirm_business_policy"
    DEFINE_SEMANTIC_TERM = "define_semantic_term"
    CONFIRM_RELATIONSHIP = "confirm_relationship"
    PROFILE_DATA = "profile_data"
    BIND_PHYSICAL_SOURCE = "bind_physical_source"
    CONFIRM_POPULATION = "confirm_population"
    CONFIRM_TIME_SEMANTICS = "confirm_time_semantics"


#: Actionable FUNCTIONAL gaps only. A code absent from here is refused — which is how a technical
#: failure is prevented from becoming ontology evidence.
GAP_CODES: frozenset[str] = frozenset({
    "SEMANTIC_TERM_UNRESOLVED",
    "POPULATION_UNRESOLVED",
    "POPULATION_AS_OF_UNRESOLVED",
    "RELATIONSHIP_UNVERIFIED",
    "JOIN_CARDINALITY_UNKNOWN",
    "REVERSAL_AS_OF_UNRESOLVED",
    "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED",
    "POINT_IN_TIME_RULE_MISSING",
    "PHYSICAL_BINDING_MISSING",
    "MEASURE_DEFINITION_UNRESOLVED",
    "DIMENSION_UNRESOLVED",
})

#: The choices a decision-maker picks between, for the gaps whose answer is a closed vocabulary.
#: Recorded ON the event so the reviewer is not left to rediscover what the options were.
CHOICE_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "REVERSAL_AS_OF_UNRESOLVED": ("reversed_by_cutoff", "reversed_at_any_time"),
    "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED": (
        "report_cutoff", "period_end_per_period", "transaction_event_time", "current_value"),
    "POPULATION_AS_OF_UNRESOLVED": (
        "membership_at_cutoff", "membership_today", "ever_a_member"),
}


#: Which REFUSALS are ontology gaps, and what a human would do about each.
#:
#: This table is the enforcement point for the boundary at the top of this module, and it is CLOSED
#: in both directions: a refusal absent from it records nothing. That default is deliberate — an
#: unrecognised code is far more likely to be a capability limit, a malformed request or a technical
#: failure than a newly-discovered ontology gap, and the cost of the two mistakes is not symmetric.
#: Missing a gap loses one signal; inventing one puts a candidate into the ontology that no human
#: ever encountered, sourced from an outage.
#:
#: Deliberately absent, each for a stated reason:
#:   ANALYSIS_UNSUPPORTED_MEASURE / ATTRIBUTION_UNSUPPORTED_BASIS / _INTERVAL
#:       capability limits — this slice cannot compute it. The ontology is not missing anything.
#:   ANALYSIS_EMPTY_PERIOD / ELIGIBILITY_NO_STATUS_VALUES / _NO_NON_REVERSED_VALUES
#:       malformed requests — the caller built the plan wrong; no decision is waiting on anyone.
#:   ATTRIBUTION_OVERLAPPING_RECORDS
#:       a DATA defect in the dimension table. Real, and someone must fix it, but it belongs to data
#:       quality: the ontology already says what it should say.
REFUSAL_TO_GAP: dict[str, tuple[str, RequiredAction]] = {
    # The relationship was never observed, was observed for something else, or does not hold.
    "JOIN_EVIDENCE_MISSING": ("RELATIONSHIP_UNVERIFIED", RequiredAction.CONFIRM_RELATIONSHIP),
    "JOIN_EVIDENCE_MISMATCHED": ("RELATIONSHIP_UNVERIFIED", RequiredAction.CONFIRM_RELATIONSHIP),
    "JOIN_KEY_NOT_UNIQUE": ("RELATIONSHIP_UNVERIFIED", RequiredAction.CONFIRM_RELATIONSHIP),
    # Uniqueness could not be ASSERTED from an approximate probe — so cardinality is unknown, and
    # the action is to profile exactly rather than to decide anything.
    "JOIN_UNIQUENESS_UNKNOWN": ("JOIN_CARDINALITY_UNKNOWN", RequiredAction.PROFILE_DATA),
    # The two business decisions this platform has been parking: which instant classifies a
    # customer, and whether a transaction counts as reversed as of the cutoff or ever.
    "ANALYSIS_NO_ATTRIBUTION_POLICY": (
        "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED", RequiredAction.CONFIRM_BUSINESS_POLICY),
    "ATTRIBUTION_NO_CUTOFF": (
        "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED", RequiredAction.CONFIRM_TIME_SEMANTICS),
    "ELIGIBILITY_UNSUPPORTED_REVERSAL_MODE": (
        "REVERSAL_AS_OF_UNRESOLVED", RequiredAction.CONFIRM_BUSINESS_POLICY),
    # "Count the transactions" is not a definition until someone says WHICH transactions count.
    "ANALYSIS_NO_ELIGIBILITY_POLICY": (
        "MEASURE_DEFINITION_UNRESOLVED", RequiredAction.CONFIRM_BUSINESS_POLICY),
}


@dataclass(frozen=True, slots=True)
class AnalysisLearningEventV1:
    """One thing a question could not proceed without."""

    analysis_request_id: str
    stage: LearningStage
    code: str
    subject_refs: tuple[str, ...]
    required_action: RequiredAction
    dependency_snapshot_id: str
    candidate_refs_considered: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()
    event_id: str = ""

    def __post_init__(self) -> None:
        if self.code not in GAP_CODES:
            raise LearningError(
                "LEARNING_NOT_A_FUNCTIONAL_GAP",
                f"{self.code!r} is not an actionable ontology gap. A technical failure belongs to "
                "run diagnostics — an outage must not become an ontology candidate")
        if not self.subject_refs:
            raise LearningError(
                "LEARNING_NO_SUBJECT",
                "a gap with no subject cannot be actioned or deduplicated")
        object.__setattr__(self, "event_id", self.event_id or f"lrn-{uuid.uuid4().hex[:16]}")

    @property
    def gap_key(self) -> str:
        """Identity of the THING TO DECIDE — code plus subjects, deliberately excluding the request
        and the snapshot. Two questions blocked by one gap share this key, which is what lets demand
        be counted without duplicating the gap."""
        material = self.code + "|" + "|".join(sorted(self.subject_refs))
        return hashlib.sha256(material.encode()).hexdigest()[:24]

    @property
    def choices(self) -> tuple[str, ...]:
        return CHOICE_VOCABULARIES.get(self.code, ())


@dataclass(frozen=True, slots=True)
class GapDemand:
    """One gap and how many distinct questions are waiting on it."""

    gap_key: str
    code: str
    subject_refs: tuple[str, ...]
    required_action: str
    blocked_requests: int
    choices: tuple[str, ...] = field(default_factory=tuple)


def record_gap(conn, event: AnalysisLearningEventV1, *, now: datetime) -> str:
    """Append one learning event. Idempotent per (request, gap, snapshot).

    Re-running the same blocked question under the same dependencies is not new information. A NEW
    dependency snapshot is: the gap may have been resolved, so it must be re-evaluated and recorded
    again.
    """
    existing = conn.execute(
        "SELECT event_id FROM analysis_learning_event "
        "WHERE analysis_request_id = %s AND gap_key = %s AND dependency_snapshot_id = %s "
        "AND kind = 'gap'",
        (event.analysis_request_id, event.gap_key, event.dependency_snapshot_id)).fetchone()
    if existing:
        return existing[0]
    conn.execute(
        "INSERT INTO analysis_learning_event (event_id, kind, analysis_request_id, stage, code, "
        "  gap_key, subject_refs, required_action, dependency_snapshot_id, "
        "  candidate_refs_considered, supporting_evidence_ids, resolves_event_id, created_at) "
        "VALUES (%s,'gap',%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)",
        (event.event_id, event.analysis_request_id, str(event.stage), event.code, event.gap_key,
         list(event.subject_refs), str(event.required_action), event.dependency_snapshot_id,
         list(event.candidate_refs_considered), list(event.supporting_evidence_ids), now))
    return event.event_id


def record_refusal(conn, *, analysis_request_id: str, refusal_code: str,
                   subject_refs: tuple[str, ...], dependency_snapshot_id: str,
                   now: datetime, stage: LearningStage = LearningStage.PLANNING) -> str | None:
    """Record a refused plan as a gap — or record nothing, and say so by returning `None`.

    The return value is the honest signal: `None` means "this refusal is not evidence about the
    ontology", which is a normal outcome and not an error. Raising instead would push every caller
    into classifying refusals itself, and the classification would then drift per call site — which
    is exactly how a connection timeout ends up recorded as a missing relationship.
    """
    mapped = REFUSAL_TO_GAP.get(refusal_code)
    if mapped is None:
        return None
    code, action = mapped
    return record_gap(conn, AnalysisLearningEventV1(
        analysis_request_id=analysis_request_id, stage=stage, code=code,
        subject_refs=tuple(subject_refs), required_action=action,
        dependency_snapshot_id=dependency_snapshot_id), now=now)


def resolve_gap(conn, event_id: str, *, decision: str, actor: str, now: datetime) -> str:
    """Write a RESOLUTION event referencing the original. The original is never touched.

    "What did we not know when that decision was made?" must stay answerable, and an updated row
    cannot answer it.
    """
    row = conn.execute(
        "SELECT analysis_request_id, stage, code, gap_key, subject_refs, required_action, "
        "       dependency_snapshot_id FROM analysis_learning_event WHERE event_id = %s",
        (event_id,)).fetchone()
    if row is None:
        raise LearningError("LEARNING_UNKNOWN_EVENT", f"no learning event {event_id!r}")
    request_id, stage, code, gap_key, subjects, action, snapshot = row
    choices = CHOICE_VOCABULARIES.get(code, ())
    if choices and decision not in choices:
        raise LearningError(
            "LEARNING_DECISION_OUT_OF_VOCABULARY",
            f"{decision!r} is not one of {choices} for {code}")
    resolution_id = f"lrn-{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO analysis_learning_event (event_id, kind, analysis_request_id, stage, code, "
        "  gap_key, subject_refs, required_action, dependency_snapshot_id, "
        "  candidate_refs_considered, supporting_evidence_ids, resolves_event_id, decision, "
        "  decided_by, created_at) "
        "VALUES (%s,'resolution',%s,%s,%s,%s,%s,%s,%s,'{}','{}',%s,%s,%s,%s)",
        (resolution_id, request_id, stage, code, gap_key, subjects, action, snapshot,
         event_id, decision, actor, now))
    return resolution_id


def open_gaps(conn) -> tuple[GapDemand, ...]:
    """Unresolved gaps, most-blocking first — the prioritisation signal, derived rather than stored.

    A gap is open while no resolution event references any of its events. Ordering by how many
    distinct questions it blocks is what a later projector needs to turn repeated learning into
    prioritised ontology candidates.
    """
    # The subjects come back as a `text[]`, read from any one row of the group — every row sharing a
    # gap_key shares its subjects by construction, since the key is derived from them. They are
    # deliberately NOT aggregated as text: `min(subject_refs::text)` then splitting on commas was
    # wrong for any element PostgreSQL has to quote, so "customer segment" came back carrying its
    # quotes and a subject containing a comma became two. Physical column refs never trip it, which
    # is why it survived until the gaps were exposed over HTTP.
    rows = conn.execute(
        "SELECT g.gap_key, min(g.code), min(g.required_action), "
        "       count(DISTINCT g.analysis_request_id), "
        "       (SELECT s.subject_refs FROM analysis_learning_event s "
        "          WHERE s.gap_key = g.gap_key AND s.kind = 'gap' LIMIT 1) "
        "FROM analysis_learning_event g "
        "WHERE g.kind = 'gap' AND NOT EXISTS ("
        "    SELECT 1 FROM analysis_learning_event r "
        "    WHERE r.kind = 'resolution' AND r.gap_key = g.gap_key) "
        "GROUP BY g.gap_key ORDER BY count(DISTINCT g.analysis_request_id) DESC, min(g.code)"
    ).fetchall()
    out = []
    for gap_key, code, action, blocked, subjects in rows:
        out.append(GapDemand(gap_key=gap_key, code=code, subject_refs=tuple(subjects or ()),
                             required_action=action, blocked_requests=int(blocked),
                             choices=CHOICE_VOCABULARIES.get(code, ())))
    return tuple(out)
