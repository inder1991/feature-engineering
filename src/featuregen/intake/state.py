from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from featuregen.intake import events


class FeatureContractStatus(str, Enum):
    """The closed Feature Contract lifecycle vocabulary (overview §4.6, spec §11). FOLDED from the
    feature_contract event stream — never a stored enum, never a projection row."""

    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    MINIMUM_CONTRACT_VALIDATED = "MINIMUM_CONTRACT_VALIDATED"
    CONFIRMED = "CONFIRMED"
    OUT_OF_SCOPE = (
        "OUT_OF_SCOPE"  # TERMINAL reject → reject_intent / RUN_REJECTED (banking-boundary)
    )
    PROHIBITED_DATA_CLASS = (
        "PROHIBITED_DATA_CLASS"  # TERMINAL reject → reject_intent / RUN_REJECTED (blocked-class)
    )
    NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"  # the ONLY hold — a folded status (from USE_CASE_ONBOARDING_REQUESTED), NOT a waiting_on_fact park


# CONFIRMED + the two banking-boundary TERMINAL REJECTS (OUT_OF_SCOPE / PROHIBITED_DATA_CLASS →
# reject_intent → RUN_REJECTED) are no-regression-locked (a later, conflicting event never moves the
# fold off them). NEEDS_USE_CASE_ONBOARDING is NOT terminal — it is the single non-terminal HOLD, a
# folded status (from USE_CASE_ONBOARDING_REQUESTED), NOT a waiting_on_fact park (that field is SP-1's
# fact-resume key, run_lifecycle.py:41/112) — that a governance flow (out of SP-2 scope) may later
# resume, so it is NOT locked here.
TERMINAL_STATUSES: frozenset[FeatureContractStatus] = frozenset(
    {
        FeatureContractStatus.CONFIRMED,
        FeatureContractStatus.OUT_OF_SCOPE,
        FeatureContractStatus.PROHIBITED_DATA_CLASS,
    }
)


@dataclass(frozen=True)
class FeatureContractState:
    """The authoritative folded state of the feature_contract aggregate — the value every SP-2
    command handler gates on inline before appending (spec §11), mirroring OverlayState. Carries the
    ONE union field set (overview R3) both the inline guards and P8's get_contract read model need."""

    status: FeatureContractStatus | None = None
    open_fields: tuple[str, ...] = ()
    requester: str | None = None  # R4 — the INTENT_SUBMITTED event actor.subject
    request_id: str | None = None
    run_id: str | None = None
    intake_mode: str | None = None
    draft_doc_id: str | None = None
    assumption_ledger_ref: str | None = None
    confirmed_doc_id: str | None = None
    candidate_doc_ids: tuple[str, ...] = ()
    catalog_version: str | None = None
    classification: str | None = None
    matched_class: str | None = None
    proposed_feature_name: str | None = None
    feature_name: str | None = None
    field_scores: Mapping[str, Any] = field(default_factory=dict)
    open_questions: tuple[Any, ...] = ()
    requires_independent_validation: bool = False
    selected_candidate: str | None = None
    rejection_classification: str | None = None
    rejection_reason: str | None = None
    confirmed_by: str | None = None
    llm_call_refs: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_confirmed(self) -> bool:
        return self.status is FeatureContractStatus.CONFIRMED

    @property
    def mcv_passed(self) -> bool:
        return self.status in (
            FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED,
            FeatureContractStatus.CONFIRMED,
        )


def fold_feature_contract_state(stream: Iterable) -> FeatureContractState:
    """Fold a feature_contract event stream (stream_version ASC) into the current lifecycle state
    (spec §4.6, §11). Mirrors overlay/state.py::fold_overlay_state — each item exposes `.type`,
    `.event_id`, `.payload`. It is the AUTHORITATIVE state for command decisions, never a projection.

    NO-REGRESSION GUARD (spec §11): once the fold reaches a no-regression-locked status (CONFIRMED /
    OUT_OF_SCOPE / PROHIBITED_DATA_CLASS) only LLM_CALL_RECORDED accretes provenance; every other
    event is ignored, so a stray/duplicate/late event can never regress or re-advance a locked
    contract. MINIMUM_CONTRACT_VALIDATED ↔ NEEDS_CLARIFICATION is intentionally two-way: a
    CONTRACT_REFINED that re-opens a field drops MCV back to NEEDS_CLARIFICATION (that is refinement,
    not regression past a lock)."""
    status: FeatureContractStatus | None = None
    open_fields: tuple[str, ...] = ()
    requester = request_id = run_id = intake_mode = None
    draft_doc_id = assumption_ledger_ref = confirmed_doc_id = None
    candidate_doc_ids: tuple[str, ...] = ()
    catalog_version = classification = matched_class = confirmed_by = None
    proposed_feature_name = feature_name = None
    field_scores: Mapping[str, Any] = {}
    open_questions: tuple[Any, ...] = ()
    requires_independent_validation = False
    selected_candidate = rejection_classification = rejection_reason = None
    llm_call_refs: tuple[str, ...] = ()

    for event in stream:
        t = event.type
        p = event.payload
        if status in TERMINAL_STATUSES:
            if t == events.LLM_CALL_RECORDED:
                llm_call_refs = llm_call_refs + (p["llm_call_ref"],)
            continue
        if t == events.INTENT_SUBMITTED:
            status = FeatureContractStatus.NEEDS_CLARIFICATION
            request_id = p.get("request_id")
            run_id = p.get("run_id")
            intake_mode = p.get("intake_mode")
            catalog_version = p.get("catalog_version", catalog_version)
            actor = getattr(event, "actor", None)  # R4 — the request owner is this
            requester = getattr(actor, "subject", None) or p.get(
                "requester"
            )  # event's actor.subject
        elif t == events.DRAFT_CONTRACT_PRODUCED:
            draft_doc_id = p.get("draft_doc_id")
            assumption_ledger_ref = p.get("assumption_ledger_ref")
            open_fields = tuple(p.get("open_fields") or ())
            candidate_doc_ids = tuple(p.get("candidate_doc_ids") or ())
            proposed_feature_name = p.get("proposed_feature_name", proposed_feature_name)
            field_scores = p.get("field_scores", field_scores)
            open_questions = tuple(p.get("open_questions") or open_questions)
        elif t == events.CONTRACT_REFINED:
            draft_doc_id = p.get("draft_doc_id", draft_doc_id)
            open_fields = tuple(p.get("open_fields") or ())
            field_scores = p.get("field_scores", field_scores)
            open_questions = tuple(p.get("open_questions") or open_questions)
            if status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED and open_fields:
                status = FeatureContractStatus.NEEDS_CLARIFICATION
        elif t in (events.FIELD_AUTO_RESOLVED, events.CLARIFICATION_ANSWERED):
            resolved = p.get("field")
            if resolved is not None:
                open_fields = tuple(f for f in open_fields if f != resolved)
        elif t == events.MINIMUM_CONTRACT_VALIDATED:
            status = FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
        elif t == events.CONTRACT_CONFIRMED:
            status = FeatureContractStatus.CONFIRMED
            confirmed_doc_id = p.get("confirmed_doc_id")
            confirmed_by = p.get("confirmed_by")
            feature_name = p.get("feature_name", feature_name)
            requires_independent_validation = bool(
                p.get("requires_independent_validation", requires_independent_validation)
            )
            selected_candidate = p.get("selected_candidate", selected_candidate)
        elif t == events.INTENT_REJECTED:
            classification = p.get("classification")
            status = FeatureContractStatus(classification)  # OUT_OF_SCOPE | PROHIBITED_DATA_CLASS
            matched_class = p.get("matched_class")
            rejection_classification = classification
            rejection_reason = p.get("reason")
            catalog_version = p.get("catalog_version", catalog_version)
        elif t == events.USE_CASE_ONBOARDING_REQUESTED:
            status = FeatureContractStatus.NEEDS_USE_CASE_ONBOARDING
            catalog_version = p.get("catalog_version", catalog_version)
        elif t == events.LLM_CALL_RECORDED:
            llm_call_refs = llm_call_refs + (p["llm_call_ref"],)
        # CONTRACT_CRITIQUED / CLARIFICATION_REQUESTED: doubt/question shadows — no status change.

    return FeatureContractState(
        status=status,
        open_fields=open_fields,
        requester=requester,
        request_id=request_id,
        run_id=run_id,
        intake_mode=intake_mode,
        draft_doc_id=draft_doc_id,
        assumption_ledger_ref=assumption_ledger_ref,
        confirmed_doc_id=confirmed_doc_id,
        candidate_doc_ids=candidate_doc_ids,
        catalog_version=catalog_version,
        classification=classification,
        matched_class=matched_class,
        proposed_feature_name=proposed_feature_name,
        feature_name=feature_name,
        field_scores=field_scores,
        open_questions=open_questions,
        requires_independent_validation=requires_independent_validation,
        selected_candidate=selected_candidate,
        rejection_classification=rejection_classification,
        rejection_reason=rejection_reason,
        confirmed_by=confirmed_by,
        llm_call_refs=llm_call_refs,
    )


def actor_is_request_owner(state: FeatureContractState, actor) -> bool:
    """R4 — the ONE request-owner predicate every SP-2 command phase (P5/P6/P7/P8) calls. True iff the
    acting principal's `subject` matches the request owner folded from the INTENT_SUBMITTED event
    (`state.requester` = that event's `actor.subject`). SP-0's `submit_human_signal` checks
    role/scope/quorum only — never subject membership (overview §2.1) — so this subject guard is the
    request-owner check SP-0 does not provide; a mismatch is a denial (P5–P8 route it to record_denial
    + the security-audit stream)."""
    subject = getattr(actor, "subject", None)
    return bool(subject) and subject == state.requester
