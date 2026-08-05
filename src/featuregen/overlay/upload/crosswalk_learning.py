"""Which crosswalk refusals are DECISIONS somebody owes, and which are simply the answer.

Release C Task 13. The rule the plan states and this module enforces: *route missing business
decisions to clarification/open gaps; fan-out, duplicates and overlap are data-quality or safety
refusals, not automatic ontology gaps.*

**Why that distinction is load-bearing rather than tidy.** ``analysis_learning_event`` is a queue of
things a human must DECIDE, and its whole value is that everything in it is closable by a person on
a screen. A measured fan-out is not closable by anyone: two customers really do share one
correspondent account, and no amount of deciding changes it. Filing it as an ontology gap would put
a permanent un-closable row in a reviewer's queue, teach the demand ranking that somebody is waiting
on a decision nobody can make, and — worst — imply to the reviewer that approving something would
make the crosswalk safe. That last implication is the exact false claim Task 13 exists to delete.
``learning.REFUSAL_TO_GAP`` already argues the same case for ``ATTRIBUTION_OVERLAPPING_RECORDS`` and
``TEMPORAL_SCD_OVERLAP``; this is the third instance of one principle, not a new one.

**The families are D5's three, not a fourth vocabulary.** Every ``CrosswalkAdmissionReason`` maps to
exactly one of ``undecided`` / ``needs_data_check`` / ``structurally_unsuitable``, which is what the
UI already renders for unresolved profile fields — so a crosswalk's "why not" reads in the same words
as everything else on the page, and NONE of the three is spelled as a failure. Only ``undecided``
files a gap: it is definitionally the family where a person is the missing input.

**The table is CLOSED in both directions and total.** A reason absent from it is a hard error rather
than a silent default, because the two possible defaults are both wrong — defaulting to "file a gap"
invents decisions from outages, and defaulting to "say nothing" makes a new refusal invisible.
``test_crosswalk_learning`` asserts totality against the enum itself, so adding a reason without
classifying it fails the suite rather than shipping.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.data_agent.learning import (
    AnalysisLearningEventV1,
    LearningStage,
    RequiredAction,
    record_gap,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.crosswalk import CrosswalkDefinitionRevisionV1
from featuregen.overlay.upload.crosswalk_admission import (
    CrosswalkAdmissionDecisionV1,
    CrosswalkAdmissionReason,
)

__all__ = [
    "CROSSWALK_REASON_FAMILY",
    "CROSSWALK_DECISION_GAPS",
    "CrosswalkReasonFamily",
    "crosswalk_learning_events",
    "record_crosswalk_gaps",
    "reason_families",
    "stable_crosswalk_request_id",
]


class CrosswalkReasonFamily(StrEnum):
    """D5's three families, verbatim. None of them is spelled as a failure."""

    #: A person is the missing input. THE ONLY family that files a learning gap.
    UNDECIDED = "undecided"
    #: A measurement is the missing input. Nobody is waiting on a decision.
    NEEDS_DATA_CHECK = "needs_data_check"
    #: The answer is in and it is no. Neither a decision nor a measurement changes it.
    STRUCTURALLY_UNSUITABLE = "structurally_unsuitable"


_U = CrosswalkReasonFamily.UNDECIDED
_D = CrosswalkReasonFamily.NEEDS_DATA_CHECK
_S = CrosswalkReasonFamily.STRUCTURALLY_UNSUITABLE

#: EVERY member of :class:`CrosswalkAdmissionReason`, classified. Totality is asserted by the suite.
CROSSWALK_REASON_FAMILY: dict[str, CrosswalkReasonFamily] = {
    # ── a person is the missing input ───────────────────────────────────────────────────────────
    #
    # Both of these say the same thing from two directions: nobody has declared how the MAPPING
    # dataset's rows are chosen. `api/routes/dataset_policies.py` is the screen, `DECLARE_TEMPORAL_
    # POLICY` is the action, and until somebody uses it, uniqueness measured over an SCD mapping
    # table is a claim about all of history rather than about the rows a traversal would read.
    CrosswalkAdmissionReason.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value: _U,
    CrosswalkAdmissionReason.MAPPING_ROWS_NOT_TEMPORALLY_FILTERED.value: _U,

    # ── a measurement is the missing input ──────────────────────────────────────────────────────
    #
    # "Discoverable, unmeasured" and every weaker-evidence variant. Each is closed by profiling,
    # which is a job, not a judgement — so none of them belongs in a decision queue.
    CrosswalkAdmissionReason.NOT_MEASURED.value: _D,
    CrosswalkAdmissionReason.OBSERVATION_INCOMPLETE.value: _D,
    CrosswalkAdmissionReason.OBSERVATION_NOT_EXACT.value: _D,
    CrosswalkAdmissionReason.OBSERVATION_NOT_FULL_COVERAGE.value: _D,
    CrosswalkAdmissionReason.OBSERVATION_TOO_OLD.value: _D,
    CrosswalkAdmissionReason.BELOW_MINIMUM_ROWS.value: _D,
    CrosswalkAdmissionReason.DIRECTIONAL_UNIQUENESS_NOT_PROVED.value: _D,
    # The partition rule's two halves. Both are answered by re-measuring at the right breadth, and
    # neither is anybody's decision: the scope was already declared, and the evidence simply does
    # not cover it (or covers more than it claims).
    CrosswalkAdmissionReason
    .PARTITION_SCOPED_EVIDENCE_CANNOT_VALIDATE_UNRESTRICTED.value: _D,
    CrosswalkAdmissionReason.SCOPE_REQUIRES_PARTITION_EVIDENCE.value: _D,

    # ── the answer is in, and it is no ──────────────────────────────────────────────────────────
    #
    # THE THREE THE PLAN NAMES. Fan-out is a measured property of the data; duplicates are two
    # governed records nobody may silently pick between; null mapping rows connect nothing. Each
    # renders as a refusal a user can read and none files a gap, because there is no decision that
    # closes it — retiring one of two rival mappings is a catalog edit, not an ontology answer, and
    # a queue row saying otherwise would promise the reviewer a power they do not have.
    CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value: _S,
    CrosswalkAdmissionReason.MULTIPLE_ACTIVE_MAPPINGS.value: _S,
    CrosswalkAdmissionReason.MAPPING_NULL_ROWS.value: _S,
    # Containment below policy is the same shape: the data does not overlap enough, and a decision
    # cannot create rows.
    CrosswalkAdmissionReason.LOW_SOURCE_CONTAINMENT.value: _S,
    CrosswalkAdmissionReason.LOW_TARGET_CONTAINMENT.value: _S,
    # A transform outside the closed normalization vocabulary. Transformed-link execution is
    # EXPLICITLY DEFERRED until a versioned normalization vocabulary is designed, so this is a
    # capability boundary — the ontology is not missing anything.
    CrosswalkAdmissionReason.NORMALIZATION_NOT_ADMITTED.value: _S,
    # Staleness and mismatch: the inputs do not describe one crosswalk right now. Re-deriving fixes
    # them; nobody decides anything.
    CrosswalkAdmissionReason.DEFINITION_REVISION_NOT_CURRENT.value: _S,
    CrosswalkAdmissionReason.OBSERVATION_NOT_FOR_DEFINITION_REVISION.value: _S,
    CrosswalkAdmissionReason.OBSERVATION_SCOPE_MISMATCH.value: _S,
    CrosswalkAdmissionReason.MAPPING_BINDING_MISMATCH.value: _S,
    CrosswalkAdmissionReason.LEG_NOT_CURRENTLY_EXECUTABLE.value: _S,
}

#: The ``undecided`` reasons and the gap each files. Both temporal reasons are ONE decision — which
#: is why they share a gap code, and therefore a ``gap_key``: a reviewer who declares the mapping
#: dataset's row policy closes both at once, and two rows would double-count one piece of demand.
CROSSWALK_DECISION_GAPS: dict[str, tuple[str, RequiredAction]] = {
    CrosswalkAdmissionReason.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value: (
        "TEMPORAL_MODEL_UNRESOLVED", RequiredAction.DECLARE_TEMPORAL_POLICY),
    CrosswalkAdmissionReason.MAPPING_ROWS_NOT_TEMPORALLY_FILTERED.value: (
        "TEMPORAL_MODEL_UNRESOLVED", RequiredAction.DECLARE_TEMPORAL_POLICY),
}


def reason_families(codes: Iterable[str]) -> dict[str, CrosswalkReasonFamily]:
    """``code -> family`` for the codes this module knows. Unknown codes are OMITTED, not guessed.

    A display surface renders what it gets and says nothing about the rest; the totality test is
    what stops "the rest" from ever being non-empty for a real admission decision.
    """
    return {code: CROSSWALK_REASON_FAMILY[code]
            for code in codes if code in CROSSWALK_REASON_FAMILY}


def stable_crosswalk_request_id(
    crosswalk_definition_revision_id: str, *, dependency_snapshot_id: str,
) -> str:
    """DERIVED, never minted — the ``retrieval.stable_analysis_request_id`` idiom.

    ``record_gap`` deduplicates on ``(analysis_request_id, gap_key, dependency_snapshot_id)``, so a
    per-call uuid would make every row unique by construction and the partial unique index at
    migration 1034 would never fire. Two analysts stopped by one undeclared mapping policy under one
    catalog state are ONE piece of demand, and this is what makes them so.

    No principal and no clock, for the same reason: neither changes what is undecided.
    """
    return "areq-" + materialize_hash({
        "crosswalk_definition_revision_id": crosswalk_definition_revision_id,
        "dependency_snapshot_id": dependency_snapshot_id,
    })[:16]


def crosswalk_learning_events(
    decision: CrosswalkAdmissionDecisionV1,
    *,
    definition: CrosswalkDefinitionRevisionV1,
    dependency_snapshot_id: str,
    analysis_request_id: str | None = None,
) -> tuple[AnalysisLearningEventV1, ...]:
    """The learning events ONE admission decision earns — the ``undecided`` family and nothing else.

    Reason codes are taken from the decision AND both directional verdicts, because a decision-level
    code and a per-direction one are both real answers and either may be the only place a shared
    reason appears. They are then filtered through :data:`CROSSWALK_REASON_FAMILY`, so a fan-out or
    a duplicate cannot reach a gap however it arrived.

    The SUBJECT is the mapping dataset, not the crosswalk: the decision owed is about that dataset's
    rows, it is made on that dataset's policy screen, and every crosswalk through the same mapping
    table is waiting on the same one. Keying on the crosswalk would split one decision into as many
    queue rows as there are crosswalks that need it.
    """
    request_id = analysis_request_id or stable_crosswalk_request_id(
        definition.revision_id, dependency_snapshot_id=dependency_snapshot_id)
    codes = sorted({
        *decision.reason_codes,
        *decision.forward.reason_codes,
        *decision.reverse.reason_codes,
    })
    seen: set[str] = set()
    events: list[AnalysisLearningEventV1] = []
    for code in codes:
        mapped = CROSSWALK_DECISION_GAPS.get(code)
        if mapped is None:
            continue
        gap_code, action = mapped
        if gap_code in seen:
            continue
        seen.add(gap_code)
        events.append(AnalysisLearningEventV1(
            analysis_request_id=request_id,
            stage=LearningStage.PLANNING,
            code=gap_code,
            subject_refs=(definition.mapping_dataset_ref,),
            required_action=action,
            dependency_snapshot_id=dependency_snapshot_id,
            candidate_refs_considered=tuple(sorted({
                definition.source_endpoint.logical_table_ref,
                definition.target_endpoint.logical_table_ref})),
            supporting_evidence_ids=tuple(
                ref.evidence_id for ref in decision.evidence_refs),
        ))
    return tuple(events)


def record_crosswalk_gaps(
    conn: DbConn,
    decision: CrosswalkAdmissionDecisionV1,
    *,
    definition: CrosswalkDefinitionRevisionV1,
    dependency_snapshot_id: str,
    now: datetime,
    analysis_request_id: str | None = None,
) -> tuple[str, ...]:
    """File every owed decision, idempotently. Returns the event ids, empty when nothing is owed.

    FAIL-SOFT is the caller's job, not this function's: a learning write must never turn a refusal
    into a 500, and the ``api/routes/analysis.py`` producer wraps its call in ``try/except`` for
    exactly that reason. What is NOT deferred to the caller is the deduplication — ``record_gap``'s
    ``ON CONFLICT`` repeats migration 1034's own ``WHERE kind = 'gap'`` predicate, without which
    PostgreSQL cannot match the partial index at all and the statement fails outright.
    """
    return tuple(
        record_gap(conn, event, now=now)
        for event in crosswalk_learning_events(
            decision, definition=definition,
            dependency_snapshot_id=dependency_snapshot_id,
            analysis_request_id=analysis_request_id)
    )
