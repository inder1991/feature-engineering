from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from featuregen.overlay import facts

# Canonical persisted status values (§3.4). Display REVERIFY as RE-VERIFY at the UI edge.
DRAFT = "DRAFT"
PARTIALLY_CONFIRMED = "PARTIALLY_CONFIRMED"
VERIFIED = "VERIFIED"
REJECTED = "REJECTED"
STALE = "STALE"
REVERIFY = "REVERIFY"


@dataclass
class OverlayState:
    status: str | None = None
    value: object | None = None
    confirmers: list = field(default_factory=list)
    expires_at: str | None = None
    confirmed_event_id: str | None = None
    draft_event_id: str | None = None
    proposal_fingerprint: str | None = None
    partial_confirmers: list = field(default_factory=list)
    prior_value: object | None = None
    evidence_ref: str | None = None
    object_ref: str | None = None
    fact_type: str | None = None
    use_case: str | None = None


def fold_overlay_state(stream: Iterable) -> OverlayState:
    """Fold an overlay_fact event stream (stream_version ASC) into the current lifecycle state
    (§3.4). Mirrors run_lifecycle.py's inline fold. Each item exposes `.type`, `.event_id`,
    `.payload`. EXPIRED/STALED move the current value into prior_value; reject under
    REVERIFY/STALE retains prior_value (the retired value stays visible as history).

    DEFENSIVE (decision 6 — no VERIFIED→DRAFT regression): a PROPOSED only (re)opens a DRAFT on an
    empty stream or after a REJECTED. A stray PROPOSED that arrives once the fact has already been
    confirmed or partially confirmed (PARTIALLY_CONFIRMED / VERIFIED / REVERIFY / STALE) is
    IGNORED — the fold must never regress a confirmed fact back to DRAFT."""
    st = OverlayState()
    for event in stream:
        payload = event.payload
        if event.type == facts.OVERLAY_FACT_PROPOSED:
            if st.status not in (None, REJECTED):
                continue  # stray PROPOSED after a confirm — ignore, do not regress to DRAFT
            st.status = DRAFT
            st.draft_event_id = event.event_id
            st.proposal_fingerprint = payload["proposal_fingerprint"]
            st.object_ref = payload["object_ref"]
            st.fact_type = payload["fact_type"]
            st.use_case = payload.get("use_case")
            st.evidence_ref = payload.get("evidence_ref")
            # A fresh (re)proposal after REJECTED must clear all prior-cycle carry-over (I1) —
            # mirror the projection's PROPOSED reset (decision 6/18) so get_task_proposal never
            # surfaces a stale retired value (or stale confirmers/expiry) on the new DRAFT.
            st.prior_value = None
            st.value = None
            st.confirmers = []
            st.partial_confirmers = []
            st.expires_at = None
            st.confirmed_event_id = None
        elif event.type == facts.OVERLAY_FACT_PARTIALLY_CONFIRMED:
            st.status = PARTIALLY_CONFIRMED
            st.partial_confirmers = st.partial_confirmers + [
                {"subject": payload["by_owner"], "role": payload["role"]}
            ]
        elif event.type == facts.OVERLAY_FACT_CONFIRMED:
            st.status = VERIFIED
            st.value = payload["value"]
            st.confirmers = list(payload["confirmers"])
            st.expires_at = payload.get("expires_at")
            st.confirmed_event_id = event.event_id
            st.prior_value = None
            st.partial_confirmers = []
        elif event.type == facts.OVERLAY_FACT_REJECTED:
            st.status = REJECTED
            st.value = None
        elif event.type == facts.OVERLAY_FACT_EXPIRED:
            st.status = REVERIFY
            st.prior_value = st.value
            st.value = None
        elif event.type == facts.OVERLAY_FACT_STALED:
            st.status = STALE
            st.prior_value = st.value
            st.value = None
    return st
