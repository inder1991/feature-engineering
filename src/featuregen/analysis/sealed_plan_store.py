"""Release-B Task 9 — SEAL an analysis plan, replay it, and revalidate it before it executes.

**No second replay table.** The plan's own instruction, and the right one: ``structured_result``
(migration 1039) is already the shared content-addressed store for validated typed results, and
migration 1046 already added the generic subject/current pointer. This module writes a row of
``result_type = "analysis_execution_plan"``, ``result_version = 2`` and nothing else — no
migration, no new table, no second notion of "the plan we recorded".

**The input key IS the plan identity.** ``find_structured_result`` replays by
``(result_type, result_version, input_content_hash)``, and here ``input_content_hash`` is
``AnalysisExecutionIRV2.plan_hash``. The output is the plan's own canonical content, so the seal is
a fixed point — and :func:`_record_from_output` CHECKS it on every read, which is what turns the
fixed point into an invariant. The store's own two guards cannot: ``output_content_hash`` and the
``sres_`` identity are both functions of the output alone, so a rewritten row re-derives both and
passes. ``input_content_hash`` is the one value that does not move with the bytes. This is also why
``question`` is outside the sealed content — two wordings of one computation are one plan, and
putting the wording inside would make the second wording a corruption report.

**A REFUSED PLAN SEALS NOTHING.** ``seal_refs_from_selections`` already refuses an unresolved
preview; :func:`seal_analysis_plan` never sees one. A plan that could not decide where its data
comes from is a question for a person, and recording it would replay later as a decision nobody
made.

**Revalidation is the `authorize_execution_realizations` doctrine, not runprep's.** That reader
(``materialize/ir.py:327-340``) re-reads the CURRENT authority immediately before execution and
refuses on drift rather than re-deriving — because a plan authorized under one state and executed
under another is exactly the class of defect that produces a confident wrong answer. Here the same
rule applies to the six pins: every one is re-read against current catalog state, the FIRST drift
is named, and nothing is ever silently re-derived. The refusal says WHAT moved and from what to
what, because "the plan is stale" sends the reader nowhere.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from featuregen.analysis.sealed_plan import (
    ANALYSIS_PLAN_CONTRACT,
    ANALYSIS_PLAN_CONTRACT_VERSION,
    SEALED_PLAN_SOURCE_UNREADABLE,
    SEALED_PLAN_STALE_DATASET_PROFILE,
    SEALED_PLAN_STALE_PHYSICAL_BINDING,
    SEALED_PLAN_STALE_ROW_SELECTION,
    SEALED_PLAN_STALE_SERVING_POLICY,
    SEALED_PLAN_STALE_SOURCE_SELECTION,
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    AnalysisExecutionIRV2,
    SealedDecisionRefsV1,
    SealedPlanError,
    SealedRowDecisionV1,
    SealedSourceDecisionV1,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.structured_results import (
    StructuredResultCorruption,
    StructuredResultPointerConflict,
    find_structured_result,
    record_structured_result,
    set_current_structured_result,
)

logger = logging.getLogger(__name__)

#: The subject a sealed plan's CURRENT pointer is filed under. One pointer per QUESTION, so
#: "what plan is current for this question" is a pointer read rather than a scan of output_json —
#: which is the entire reason migration 1046 exists.
SEALED_PLAN_SUBJECT_KIND = "analysis_question"


def analysis_question_ref(question: str) -> str:
    """A stable subject ref for one question's current plan.

    Derived from the question's normalized TEXT and nothing else. Deliberately NOT
    ``stable_analysis_request_id``, which folds in the catalog snapshot: that is right for a
    learning gap (three retries under one catalog state are one thing to decide) and wrong here,
    because it would fragment "the current plan for this question" across every catalog change and
    the pointer would never supersede anything.
    """
    normalized = " ".join(str(question or "").split()).lower()
    if not normalized:
        raise SealedPlanError("a sealed plan needs the question it answers to file its pointer")
    return "aq_" + materialize_hash({"question": normalized})[:32]


@dataclass(frozen=True, slots=True)
class SealedPlanRecordV1:
    """A sealed plan, read back. Immutable, content-verified by the store on the way out."""

    plan_hash: str
    structured_result_id: str
    contract_version: int
    computation: Mapping[str, Any]
    decisions: SealedDecisionRefsV1

    @property
    def sources(self) -> tuple[SealedSourceDecisionV1, ...]:
        return self.decisions.sources

    @property
    def rows(self) -> tuple[SealedRowDecisionV1, ...]:
        return self.decisions.rows


@dataclass(frozen=True, slots=True)
class SealedPlanRefusalV1:
    """A typed refusal from replay or revalidation. Names WHAT moved, and from what to what."""

    code: str
    subject_refs: tuple[str, ...]
    detail: str
    sealed_value: str = ""
    current_value: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "subjects": list(self.subject_refs), "detail": self.detail,
                "sealed": self.sealed_value, "current": self.current_value}


def _record_from_output(output: Mapping[str, Any], *, structured_result_id: str,
                        plan_hash: str) -> SealedPlanRecordV1:
    # THE FIXED POINT, CHECKED. The seal's whole claim is that ``input_content_hash`` IS
    # ``materialize_hash`` of the bytes filed under it, so one plan identity can only ever name one
    # computation and one set of decisions. The store's own two guards cannot enforce that: both
    # ``output_content_hash`` and the ``sres_`` identity are functions of the OUTPUT alone, so
    # anyone who can rewrite the row re-derives them and both pass — and the rewritten plan then
    # executes cleanly under the identity a person approved. This is the one comparison that does
    # not move with the bytes, so it is the one that has to be made on every read.
    computed = materialize_hash(dict(output))
    if computed != plan_hash:
        raise StructuredResultCorruption(
            f"a sealed analysis plan filed under {plan_hash!r} contains bytes whose own identity is "
            f"{computed!r}; one plan identity names one computation and one set of decisions, so "
            "these bytes may not be replayed as the plan that identity was approved as")
    contract = output.get("contract")
    version = output.get("contract_version")
    if contract != ANALYSIS_PLAN_CONTRACT or version != ANALYSIS_PLAN_CONTRACT_VERSION:
        raise SealedPlanError(
            f"a stored analysis plan claims {contract!r}@{version!r}, not "
            f"{ANALYSIS_PLAN_CONTRACT}@{ANALYSIS_PLAN_CONTRACT_VERSION}; a build that cannot read "
            "a stored contract must refuse, never guess at its meaning")
    return SealedPlanRecordV1(
        plan_hash=plan_hash,
        structured_result_id=structured_result_id,
        contract_version=int(version),
        computation=dict(output.get("computation") or {}),
        decisions=SealedDecisionRefsV1.from_payload(output.get("decisions") or {}))


def seal_analysis_plan(conn, ir: AnalysisExecutionIRV2, *, sealed_by: str,
                       question: str = "") -> SealedPlanRecordV1:
    """Persist the validated IR and its decision refs. Idempotent by construction.

    Re-sealing the same plan returns the same row: the store's content-address dedupes on
    ``(result_type, result_version, input_content_hash)`` and only appends a provenance record for
    the second producer, which is the honest reading — two people planned the same thing.
    """
    if not isinstance(ir, AnalysisExecutionIRV2):
        raise SealedPlanError("only a validated AnalysisExecutionIRV2 may be sealed")
    if not str(sealed_by or "").strip():
        raise SealedPlanError("a sealed plan records WHO planned it; the producer ref is required")
    plan_hash = ir.plan_hash
    stored = record_structured_result(
        conn,
        result_type=ANALYSIS_PLAN_CONTRACT,
        result_version=ANALYSIS_PLAN_CONTRACT_VERSION,
        input_content_hash=plan_hash,
        output=ir.content_payload(),
        producer_kind="analysis_plan",
        producer_ref=sealed_by,
        # The AUTHORITY the plan was made under travels with it (D2 triple shape, and the warnings
        # that rode a resolved decision). A `PROPOSED_AUTHORITY_USED` plan that sealed without it
        # would read later as one made on confirmed classifications.
        authority={"execution_tiers": sorted({s.execution_tier for s in ir.decisions.sources}),
                   "warnings": list(ir.decisions.warnings)})
    subject = analysis_question_ref(question or ir.question)
    try:
        set_current_structured_result(
            conn, subject_kind=SEALED_PLAN_SUBJECT_KIND, subject_ref=subject,
            result_type=ANALYSIS_PLAN_CONTRACT,
            structured_result_id=stored.structured_result_id)
    except StructuredResultPointerConflict:
        # A concurrent planner made a DIFFERENT plan current for this question. The revision above
        # is immutable and already written, and execution addresses a plan by its hash, never
        # through the pointer — so losing this race costs nothing and must not fail the request.
        # Overwriting would be the 1033/1038 violation: work started against an older snapshot may
        # not clobber a newer answer.
        logger.info("another planner advanced the current plan for %s", subject)
    return _record_from_output(stored.output, structured_result_id=stored.structured_result_id,
                               plan_hash=plan_hash)


def replay_sealed_plan(conn, plan_hash: str) -> SealedPlanRecordV1 | None:
    """The one canonical sealed plan for an identity, or ``None`` when nothing sealed it."""
    if not str(plan_hash or "").strip():
        return None
    found = find_structured_result(
        conn, result_type=ANALYSIS_PLAN_CONTRACT,
        result_version=ANALYSIS_PLAN_CONTRACT_VERSION, input_content_hash=plan_hash.strip())
    if found is None:
        return None
    return _record_from_output(found.output, structured_result_id=found.structured_result_id,
                               plan_hash=plan_hash.strip())


def current_sealed_plan(conn, question: str) -> SealedPlanRecordV1 | None:
    """The plan currently filed for one QUESTION, through the 1046 pointer. ``None`` when the
    pointer is absent or withdrawn — a withdrawn plan is not a current one."""
    from featuregen.overlay.upload.structured_results import (
        load_current_structured_result,
        load_structured_result,
    )

    pointer = load_current_structured_result(
        conn, subject_kind=SEALED_PLAN_SUBJECT_KIND, subject_ref=analysis_question_ref(question),
        result_type=ANALYSIS_PLAN_CONTRACT)
    if pointer is None or pointer.lifecycle != "current":
        return None
    stored = load_structured_result(conn, pointer.structured_result_id)
    if stored is None:
        return None
    return _record_from_output(stored.output, structured_result_id=stored.structured_result_id,
                               plan_hash=stored.input_content_hash)


# ── revalidation: the six pins, re-read immediately before execution ─────────────────────────────


def _readable(conn, dataset_ref: str, roles: Sequence[str]) -> bool:
    from featuregen.overlay.upload.source_selection import (
        SelectionError,
        assert_dataset_refs_readable,
    )

    try:
        assert_dataset_refs_readable(conn, (dataset_ref,), roles=roles)
    except SelectionError:
        return False
    return True


def _revalidate_source(conn, decision: SealedSourceDecisionV1, *,
                       roles: Sequence[str]) -> SealedPlanRefusalV1 | None:
    from featuregen.data_agent.binding_store import binding_revision_exists, resolve_table
    from featuregen.data_agent.connection import ConnectionError_
    from featuregen.overlay.upload.object_ref import parse_ref
    from featuregen.overlay.upload.serving_policy_store import current_serving_policy
    from featuregen.overlay.upload.source_selector import current_dataset_profile_hash

    ref = decision.dataset_ref
    # ZERO. Read scope first: everything below reads the catalog on this caller's behalf, and a
    # drift report about a dataset they may not see would be an existence oracle dressed as an
    # error message. Same words as "no such dataset", always (D11).
    if not _readable(conn, ref, roles):
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_SOURCE_UNREADABLE, subject_refs=(ref,),
            detail=(f"{ref!r} is not a readable dataset for this caller, so the plan sealed "
                    "against it cannot be executed on their behalf"))

    # PIN 1 — dataset_profile. Re-ASSEMBLED, not re-read from a cache: D12.10 says a narrative edit
    # re-keys a dataset profile, and the point of pinning the hash is that such an edit surfaces
    # here instead of quietly changing what the plan meant.
    current_profile = current_dataset_profile_hash(conn, ref)
    if current_profile != decision.dataset_profile_hash:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_DATASET_PROFILE, subject_refs=(ref,),
            detail=(f"the semantic profile of {ref!r} has changed since this plan was sealed; its "
                    "meaning is not what the decision was made on"),
            sealed_value=decision.dataset_profile_hash, current_value=current_profile)

    # PIN 2 — serving_policy. A policy APPEARING is drift exactly as a policy changing is: the plan
    # was made where nothing declared which copy may serve, and now something does.
    found = current_serving_policy(
        conn, entity_id=decision.entity_id, need_role=decision.need_role,
        serving_purpose=decision.serving_purpose)
    current_policy_id = None if found is None else found[0].revision_id
    if current_policy_id != decision.serving_policy_revision_id:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_SERVING_POLICY,
            subject_refs=(f"{decision.entity_id}:{decision.need_role}:"
                          f"{decision.serving_purpose}",),
            detail=("the serving policy that decides which copy answers this need is no longer "
                    "the one the plan was sealed under"),
            sealed_value=decision.serving_policy_revision_id or "",
            current_value=current_policy_id or "")

    # PIN 4 — physical_binding, in two halves. The revision must still EXIST (the observation
    # store's FK reads it, `binding_revision_exists` is the authoring-time check this reuses), and
    # `resolve_table` must still route this dataset to that same revision — a re-pointed catalog
    # engine changes the address without touching a single decision.
    if not binding_revision_exists(conn, decision.binding_revision_id):
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_PHYSICAL_BINDING, subject_refs=(ref,),
            detail=(f"the physical binding revision this plan pinned for {ref!r} is no longer "
                    "persisted, so the address it names cannot be replayed"),
            sealed_value=decision.binding_revision_id)
    source, _schema, table, _column = parse_ref(ref)
    try:
        routed = resolve_table(conn, catalog_source=source, table=table)
    except ConnectionError_ as exc:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_PHYSICAL_BINDING, subject_refs=(ref,),
            detail=(f"{ref!r} can no longer be addressed on this caller's behalf ({exc.code}); a "
                    "withdrawn grant is a governance answer, not a missing configuration"),
            sealed_value=decision.binding_revision_id)
    current_binding = None if routed is None else routed[0].binding_revision_id
    if current_binding != decision.binding_revision_id:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_PHYSICAL_BINDING, subject_refs=(ref,),
            detail=(f"{ref!r} now addresses a different physical object than the one this plan was "
                    "sealed against"),
            sealed_value=decision.binding_revision_id, current_value=current_binding or "")

    # PIN 3 — source_selection, the COMPOSITE. Checked last because the four components above name
    # what a person would fix; a bare composite mismatch after they all match means the sealed
    # bytes themselves do not reproduce their own identity, which is corruption rather than drift.
    rebuilt = decision.rebuild()
    if rebuilt.content_hash != decision.source_selection_hash:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_SOURCE_SELECTION, subject_refs=(ref,),
            detail=("the sealed source decision does not reproduce the identity it is filed "
                    "under; it may not be replayed as a decision"),
            sealed_value=decision.source_selection_hash, current_value=rebuilt.content_hash)
    return None


def _revalidate_row(conn, decision: SealedRowDecisionV1, *,
                    roles: Sequence[str]) -> SealedPlanRefusalV1 | None:
    from featuregen.overlay.upload.source_selector import current_dataset_profile_hash
    from featuregen.overlay.upload.temporal_policy_store import current_temporal_policy

    ref = decision.dataset_ref
    if not _readable(conn, ref, roles):
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_SOURCE_UNREADABLE, subject_refs=(ref,),
            detail=(f"{ref!r} is not a readable dataset for this caller, so the row rule sealed "
                    "against it cannot be executed on their behalf"))
    current_profile = current_dataset_profile_hash(conn, ref)
    if current_profile != decision.dataset_profile_hash:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_DATASET_PROFILE, subject_refs=(ref,),
            detail=(f"the semantic profile of {ref!r} has changed since its row rule was sealed"),
            sealed_value=decision.dataset_profile_hash, current_value=current_profile)

    # PIN 5 — temporal_policy. THE case this whole task exists for: someone re-declares how a
    # dimension table stores history, and every already-planned answer would silently start
    # classifying customers by a different row.
    found = current_temporal_policy(conn, ref)
    current_policy_id = None if found is None else found[0].revision_id
    if current_policy_id != decision.temporal_policy_revision_id:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_TEMPORAL_POLICY, subject_refs=(ref,),
            detail=(f"the temporal policy for {ref!r} is no longer the one this plan's row rule "
                    "was sealed under, so 'which row classifies each member' has moved"),
            sealed_value=decision.temporal_policy_revision_id,
            current_value=current_policy_id or "")

    # PIN 6 — row_selection, the composite; corruption rather than drift, as above.
    rebuilt = decision.rebuild()
    if rebuilt.content_hash != decision.row_selection_hash:
        return SealedPlanRefusalV1(
            code=SEALED_PLAN_STALE_ROW_SELECTION, subject_refs=(ref,),
            detail=("the sealed row decision does not reproduce the identity it is filed under; "
                    "it may not be replayed as a decision"),
            sealed_value=decision.row_selection_hash, current_value=rebuilt.content_hash)
    return None


def revalidate_sealed_plan(conn, record: SealedPlanRecordV1, *,
                           roles: Sequence[str] = ()) -> SealedPlanRefusalV1 | None:
    """Re-read all six pins against CURRENT state. ``None`` means the plan may execute.

    Sources first, then rows, each in the D6 pin order, and the FIRST drift wins. Order is not
    cosmetic: a moved source makes its row rule's dataset profile a different thing, so reporting
    the row rule first would send a person to re-declare a temporal policy for a table that is no
    longer the one being read.

    **Nothing is re-derived.** A drifted pin refuses; it never re-runs the selector and substitutes
    today's answer, because that would silently give a person the plan they did not approve under
    the identity of the one they did.
    """
    for source in record.sources:
        refusal = _revalidate_source(conn, source, roles=roles)
        if refusal is not None:
            return refusal
    for row in record.rows:
        refusal = _revalidate_row(conn, row, roles=roles)
        if refusal is not None:
            return refusal
    return None


__all__ = [
    "SEALED_PLAN_SUBJECT_KIND",
    "SealedPlanRecordV1",
    "SealedPlanRefusalV1",
    "analysis_question_ref",
    "current_sealed_plan",
    "replay_sealed_plan",
    "revalidate_sealed_plan",
    "seal_analysis_plan",
]
