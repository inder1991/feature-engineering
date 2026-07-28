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
    rows = conn.execute(
        "SELECT g.gap_key, min(g.code), min(g.required_action), "
        "       count(DISTINCT g.analysis_request_id), min(g.subject_refs::text) "
        "FROM analysis_learning_event g "
        "WHERE g.kind = 'gap' AND NOT EXISTS ("
        "    SELECT 1 FROM analysis_learning_event r "
        "    WHERE r.kind = 'resolution' AND r.gap_key = g.gap_key) "
        "GROUP BY g.gap_key ORDER BY count(DISTINCT g.analysis_request_id) DESC, min(g.code)"
    ).fetchall()
    out = []
    for gap_key, code, action, blocked, subjects in rows:
        parsed = tuple(subjects.strip("{}").split(",")) if subjects else ()
        out.append(GapDemand(gap_key=gap_key, code=code, subject_refs=parsed,
                             required_action=action, blocked_requests=int(blocked),
                             choices=CHOICE_VOCABULARIES.get(code, ())))
    return tuple(out)
