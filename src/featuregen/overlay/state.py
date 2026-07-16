from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from featuregen.overlay import facts
from featuregen.overlay._types import FactStatus, FactType

# Canonical persisted status values (§3.4). Display REVERIFY as RE-VERIFY at the UI edge.
DRAFT: FactStatus = "DRAFT"
PARTIALLY_CONFIRMED: FactStatus = "PARTIALLY_CONFIRMED"
VERIFIED: FactStatus = "VERIFIED"
REJECTED: FactStatus = "REJECTED"
STALE: FactStatus = "STALE"
REVERIFY: FactStatus = "REVERIFY"


@dataclass
class OverlayState:
    status: FactStatus | None = None
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
    fact_type: FactType | None = None
    use_case: str | None = None
    # #10 honest authority attribution: set from a source-declared CONFIRMED event
    # (authority_basis="source_declared" + origin_type + the acting principal's role_claims).
    # None/[] on the confirmer path — human confirms AND legacy pre-#10 events alike.
    authority_basis: str | None = None
    origin_type: str | None = None
    role_claims: list = field(default_factory=list)

    @property
    def authority_provenance(self) -> str | None:
        """How this fact's confirmation is attributed: `source_declared` when the event carried an
        authority basis; `legacy_unspecified` for any confirmer-based confirmation (a genuine human
        confirm or a pre-#10 auto-confirm — the v1 shape has no discriminator and is NEVER
        retroactively reclassified); None when nothing is confirmed."""
        if self.authority_basis is not None:
            return self.authority_basis
        if self.confirmers:
            return facts.AUTHORITY_LEGACY_UNSPECIFIED
        return None


def fold_overlay_state(stream: Iterable) -> OverlayState:
    """Fold an overlay_fact event stream (stream_version ASC) into the current lifecycle state
    (§3.4). Mirrors run_lifecycle.py's inline fold. Each item exposes `.type`, `.event_id`,
    `.payload`. EXPIRED/STALED move the current value into prior_value; reject under
    REVERIFY/STALE retains prior_value (the retired value stays visible as history).

    DEFENSIVE (no VERIFIED→DRAFT regression): a PROPOSED only (re)opens a DRAFT on an
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
            # A fresh (re)proposal after REJECTED must clear all prior-cycle carry-over —
            # mirror the projection's PROPOSED reset so get_task_proposal never
            # surfaces a stale retired value (or stale confirmers/expiry) on the new DRAFT.
            st.prior_value = None
            st.value = None
            st.confirmers = []
            st.partial_confirmers = []
            st.expires_at = None
            st.confirmed_event_id = None
            st.authority_basis = None
            st.origin_type = None
            st.role_claims = []
        elif event.type == facts.OVERLAY_FACT_PARTIALLY_CONFIRMED:
            st.status = PARTIALLY_CONFIRMED
            st.partial_confirmers = st.partial_confirmers + [
                {"subject": payload["by_owner"], "role": payload["role"]}
            ]
        elif event.type == facts.OVERLAY_FACT_CONFIRMED:
            st.status = VERIFIED
            st.value = payload["value"]
            if "authority_basis" in payload:
                # #10 source-declared: the source (upload/connector/resolution) is the authority —
                # record the honest basis + origin + the actor's real role_claims; NO confirmer is
                # fabricated. Operationally identical to the confirmer path (same VERIFIED).
                st.authority_basis = payload["authority_basis"]
                st.origin_type = payload.get("origin_type")
                st.role_claims = list(payload.get("role_claims") or [])
                st.confirmers = []
            else:
                # Human confirmation — and every legacy pre-#10 event, which used this same shape
                # for auto-confirms too. Folds exactly as before (`legacy_unspecified` provenance
                # via authority_provenance); NEVER retroactively reclassified as source_declared.
                st.confirmers = list(payload["confirmers"])
                st.authority_basis = None
                st.origin_type = None
                st.role_claims = []
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
