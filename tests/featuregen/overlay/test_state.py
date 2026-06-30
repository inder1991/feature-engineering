from dataclasses import dataclass

from featuregen.overlay import facts
from featuregen.overlay.state import (
    DRAFT,
    PARTIALLY_CONFIRMED,
    REJECTED,
    REVERIFY,
    STALE,
    VERIFIED,
    fold_overlay_state,
)


@dataclass
class _Evt:
    type: str
    event_id: str
    payload: dict


def _proposed(eid="evt_draft", fact_type="grain", use_case=None):
    return _Evt(facts.OVERLAY_FACT_PROPOSED, eid, {
        "object_ref": "core.transactions", "fact_type": fact_type, "use_case": use_case,
        "proposal_fingerprint": "fp1", "evidence_ref": "eviu_1",
    })


def _confirmed(eid="evt_conf", value=None):
    return _Evt(facts.OVERLAY_FACT_CONFIRMED, eid, {
        "value": value or {"columns": ["id"], "is_unique": True},
        "confirmers": [{"subject": "owner_a", "role": "data_owner"}],
        "expires_at": "2026-12-31T00:00:00+00:00", "confirms_event_id": "evt_draft",
    })


def test_proposed_only_is_draft():
    st = fold_overlay_state([_proposed()])
    assert st.status == DRAFT
    assert st.value is None
    assert st.draft_event_id == "evt_draft"
    assert st.proposal_fingerprint == "fp1"
    assert st.object_ref == "core.transactions"
    assert st.fact_type == "grain"
    assert st.evidence_ref == "eviu_1"


def test_draft_to_verified():
    st = fold_overlay_state([_proposed(), _confirmed()])
    assert st.status == VERIFIED
    assert st.value == {"columns": ["id"], "is_unique": True}
    assert st.confirmers == [{"subject": "owner_a", "role": "data_owner"}]
    assert st.draft_event_id == "evt_draft"
    assert st.confirmed_event_id == "evt_conf"
    assert st.proposal_fingerprint == "fp1"


def test_draft_to_rejected():
    rej = _Evt(facts.OVERLAY_FACT_REJECTED, "evt_rej",
               {"rejected_by": "owner_a", "reason": "wrong", "target_event_id": "evt_draft"})
    st = fold_overlay_state([_proposed(), rej])
    assert st.status == REJECTED
    assert st.value is None


def test_approved_join_partial_then_verified_clears_partials():
    partial = _Evt(facts.OVERLAY_FACT_PARTIALLY_CONFIRMED, "evt_pc",
                   {"by_owner": "owner_a", "role": "data_owner", "draft_event_id": "evt_draft"})
    st_partial = fold_overlay_state([_proposed(fact_type="approved_join"), partial])
    assert st_partial.status == PARTIALLY_CONFIRMED
    assert st_partial.partial_confirmers == [{"subject": "owner_a", "role": "data_owner"}]
    st_done = fold_overlay_state([_proposed(fact_type="approved_join"), partial, _confirmed()])
    assert st_done.status == VERIFIED
    assert st_done.partial_confirmers == []


def test_verified_to_reverify_then_verified_again():
    expired = _Evt(facts.OVERLAY_FACT_EXPIRED, "evt_exp",
                   {"expires_confirmed_event_id": "evt_conf"})
    st_reverify = fold_overlay_state([_proposed(), _confirmed(), expired])
    assert st_reverify.status == REVERIFY
    assert st_reverify.value is None
    assert st_reverify.prior_value == {"columns": ["id"], "is_unique": True}
    reconf = _confirmed(eid="evt_conf2", value={"columns": ["id", "v"], "is_unique": True})
    st_back = fold_overlay_state([_proposed(), _confirmed(), expired, reconf])
    assert st_back.status == VERIFIED
    assert st_back.value == {"columns": ["id", "v"], "is_unique": True}
    assert st_back.prior_value is None


def test_verified_to_stale():
    staled = _Evt(facts.OVERLAY_FACT_STALED, "evt_stale",
                  {"catalog_change_ref": "chg_1", "stales_confirmed_event_id": "evt_conf"})
    st = fold_overlay_state([_proposed(), _confirmed(), staled])
    assert st.status == STALE
    assert st.value is None
    assert st.prior_value == {"columns": ["id"], "is_unique": True}


def test_reject_under_reverify_retains_prior_value():
    expired = _Evt(facts.OVERLAY_FACT_EXPIRED, "evt_exp",
                   {"expires_confirmed_event_id": "evt_conf"})
    rej = _Evt(facts.OVERLAY_FACT_REJECTED, "evt_rej",
               {"rejected_by": "owner_a", "reason": "retire", "target_event_id": "evt_conf",
                "retired_fingerprint": "fp1"})
    st = fold_overlay_state([_proposed(), _confirmed(), expired, rej])
    assert st.status == REJECTED
    assert st.value is None
    assert st.prior_value == {"columns": ["id"], "is_unique": True}


def test_stray_proposed_after_confirm_does_not_regress_to_draft():
    # A duplicate/stray PROPOSED arriving AFTER the fact is already VERIFIED must be ignored — the
    # fold must never regress a confirmed fact back to DRAFT (decision 6).
    stray = _proposed(eid="evt_draft2")
    st = fold_overlay_state([_proposed(), _confirmed(), stray])
    assert st.status == VERIFIED
    assert st.value == {"columns": ["id"], "is_unique": True}
    assert st.draft_event_id == "evt_draft"  # unchanged by the stray proposal
    assert st.confirmed_event_id == "evt_conf"
    # ...and the same holds once it is in RE-VERIFY (a stray PROPOSED does not re-open DRAFT)
    expired = _Evt(facts.OVERLAY_FACT_EXPIRED, "evt_exp",
                   {"expires_confirmed_event_id": "evt_conf"})
    st2 = fold_overlay_state([_proposed(), _confirmed(), expired, stray])
    assert st2.status == REVERIFY
    assert st2.prior_value == {"columns": ["id"], "is_unique": True}
