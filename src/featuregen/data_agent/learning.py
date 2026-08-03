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

from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_SOURCE_AMBIGUOUS,
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SNAPSHOT_TIE,
)


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
    # Release B: the two authoring surfaces `api/routes/dataset_policies.py` ships. Named
    # separately from CONFIRM_BUSINESS_POLICY because "declare which dataset serves this need" and
    # "declare how this dataset stores history" are different screens, and an action a reviewer
    # cannot locate is not actionable.
    DECLARE_SERVING_POLICY = "declare_serving_policy"
    DECLARE_TEMPORAL_POLICY = "declare_temporal_policy"


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
    # Release B — source and row selection. Each is a decision a person can make on a real screen;
    # none is a technical failure or a capability limit.
    "DATASET_SOURCE_UNRESOLVED",        # which copy serves this need
    "DATASET_AUTHORITY_UNRESOLVED",     # which copy is authoritative enough for this tier
    "TEMPORAL_MODEL_UNRESOLVED",        # how this dataset stores history
    "HISTORICAL_SOURCE_UNAVAILABLE",    # nothing here holds the history the question needs
    "SNAPSHOT_TIE_BREAK_UNDECLARED",    # which column breaks a snapshot tie
})

#: The choices a decision-maker picks between, for the gaps whose answer is a closed vocabulary.
#: Recorded ON the event so the reviewer is not left to rediscover what the options were.
CHOICE_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "REVERSAL_AS_OF_UNRESOLVED": ("reversed_by_cutoff", "reversed_at_any_time"),
    "DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED": (
        "report_cutoff", "period_end_per_period", "transaction_event_time", "current_value"),
    "POPULATION_AS_OF_UNRESOLVED": (
        "membership_at_cutoff", "membership_today", "ever_a_member"),
    # Derived from the enum, not hand-typed: adding a storage model without updating this list is
    # the drift the `profile_vocab` vocabularies exist to prevent. `unknown` is excluded — it is
    # the absence this gap records, so offering it as an ANSWER would let a reviewer "resolve" a
    # gap by restating it.
    "TEMPORAL_MODEL_UNRESOLVED": tuple(
        m.value for m in TemporalStorageModel if m is not TemporalStorageModel.UNKNOWN),
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
#:   TEMPORAL_SCD_OVERLAP  (Release B)
#:       the SAME judgement, re-argued rather than assumed. The profile plan lists "SCD overlap"
#:       among its closed refusal codes, and it IS one — it refuses, and it renders as a
#:       clarification so a user is told why their question stopped. But it is not an ontology gap
#:       for exactly the reason recorded above for ATTRIBUTION_OVERLAPPING_RECORDS: nobody is
#:       waiting to DECIDE anything. Two history rows claiming the same key at the same instant is
#:       a defect in the source data, and recording it here would put a "decision" in the reviewer
#:       queue that no decision can close.
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

    # ── Release B: source and row selection (profile plan Task 7) ────────────────────────────────
    # Seven of the eight closed `SELECTION_REFUSAL_CODES` map here; TEMPORAL_SCD_OVERLAP does not,
    # for the reason argued in the "deliberately absent" block above.
    #
    # Which table is the population is the decision this platform has parked longest, and it is
    # already spelled POPULATION_UNRESOLVED — the selector reaching it from a different direction
    # does not make it a different gap.
    SELECTION_POPULATION_UNDECLARED: (
        "POPULATION_UNRESOLVED", RequiredAction.CONFIRM_POPULATION),
    SELECTION_SOURCE_AMBIGUOUS: (
        "DATASET_SOURCE_UNRESOLVED", RequiredAction.DECLARE_SERVING_POLICY),
    # An address nobody configured — the same gap the compile path already records, so the same
    # code and the same action.
    SELECTION_BINDING_MISSING: (
        "PHYSICAL_BINDING_MISSING", RequiredAction.BIND_PHYSICAL_SOURCE),
    # WHICH COPY IS AUTHORITATIVE is a governed classification, confirmed through the four-eyes
    # profile flow — not something a serving policy may quietly assert.
    SELECTION_AUTHORITY_INSUFFICIENT: (
        "DATASET_AUTHORITY_UNRESOLVED", RequiredAction.CONFIRM_BUSINESS_POLICY),
    TEMPORAL_MODEL_UNKNOWN: (
        "TEMPORAL_MODEL_UNRESOLVED", RequiredAction.DECLARE_TEMPORAL_POLICY),
    # "This table keeps no history" is a fact about the data; the DECISION is which dataset does.
    TEMPORAL_HISTORICAL_CURRENT_ONLY: (
        "HISTORICAL_SOURCE_UNAVAILABLE", RequiredAction.DECLARE_SERVING_POLICY),
    # A tie IS closable by a declaration — name a governed tie-breaker — which is exactly what
    # separates it from the overlap defect above.
    TEMPORAL_SNAPSHOT_TIE: (
        "SNAPSHOT_TIE_BREAK_UNDECLARED", RequiredAction.DECLARE_TEMPORAL_POLICY),
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

    THE DEDUPE IS DONE BY THE DATABASE, not by a read. This was a SELECT-then-INSERT, which is a
    check-then-act race across two connections — and it was invisible only because the production
    caller minted a fresh `areq-{uuid4}` per request, so no two writes ever shared a dedupe key. The
    moment request ids became stable (which is what makes the dedupe do anything at all) the race
    was live, and the loser died with a `UniqueViolation` the caller logs as "could not record a
    learning gap". So the INSERT is conditional on 1034's `analysis_learning_event_gap_idx` — the
    PARTIAL unique index over exactly (analysis_request_id, gap_key, dependency_snapshot_id) WHERE
    kind = 'gap' — and the conflicting writer reads back the row that won rather than raising.

    The index inference clause must repeat the index's own ``WHERE kind = 'gap'``: without it
    PostgreSQL cannot match a PARTIAL index and the statement fails outright.
    """
    inserted = conn.execute(
        "INSERT INTO analysis_learning_event (event_id, kind, analysis_request_id, stage, code, "
        "  gap_key, subject_refs, required_action, dependency_snapshot_id, "
        "  candidate_refs_considered, supporting_evidence_ids, resolves_event_id, created_at) "
        "VALUES (%s,'gap',%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s) "
        "ON CONFLICT (analysis_request_id, gap_key, dependency_snapshot_id) WHERE kind = 'gap' "
        "DO NOTHING RETURNING event_id",
        (event.event_id, event.analysis_request_id, str(event.stage), event.code, event.gap_key,
         list(event.subject_refs), str(event.required_action), event.dependency_snapshot_id,
         list(event.candidate_refs_considered), list(event.supporting_evidence_ids),
         now)).fetchone()
    if inserted is not None:
        return inserted[0]
    existing = conn.execute(
        "SELECT event_id FROM analysis_learning_event "
        "WHERE analysis_request_id = %s AND gap_key = %s AND dependency_snapshot_id = %s "
        "AND kind = 'gap'",
        (event.analysis_request_id, event.gap_key, event.dependency_snapshot_id)).fetchone()
    # The row is unreadable only under an isolation level that froze the snapshot before the winner
    # committed. Returning this event's own id would then name a row that does not exist, so the
    # caller is told there is nothing to point at instead.
    return existing[0] if existing else ""


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
