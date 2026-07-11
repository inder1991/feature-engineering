from featuregen.overlay.field_decision import read_field_decisions, record_field_decision


def test_field_decision_is_append_only_and_replayable(db):
    e1 = record_field_decision(
        db,
        logical_ref="public.accounts.balance",
        field_name="concept",
        event_type="resolved",
        selected_evidence_ids=("e1",),
        evidence_set_hash="es1",
        display_value_hash="dh",
        load_bearing_value_hash=None,
        conflict_status="none",
        reason_codes=("authority_insufficient",),
        field_policy_version="v1",
        resolver_version="r1",
        actor_ref=None,
        supersedes_event_id=None,
    )
    e2 = record_field_decision(
        db,
        logical_ref="public.accounts.balance",
        field_name="concept",
        event_type="confirmed",
        selected_evidence_ids=("e1", "e2"),
        evidence_set_hash="es2",
        display_value_hash="dh",
        load_bearing_value_hash="lh",
        conflict_status="none",
        reason_codes=(),
        field_policy_version="v1",
        resolver_version="r1",
        actor_ref="alice",
        supersedes_event_id=e1,
    )
    rows = read_field_decisions(db, "public.accounts.balance", "concept")
    assert [r.event_type for r in rows] == ["resolved", "confirmed"]
    assert rows[-1].supersedes_event_id == e1 and rows[-1].load_bearing_value_hash == "lh"
    # the minted ids are distinct fde_ ids (a supersession is a NEW row, never an update)
    assert e1 != e2
    assert e1.startswith("fde_") and e2.startswith("fde_")
    # jsonb list fields round-trip; the frozen record's contract is an immutable tuple
    assert rows[0].selected_evidence_ids == ("e1",)
    assert rows[0].reason_codes == ("authority_insufficient",)
    assert rows[1].selected_evidence_ids == ("e1", "e2")
    assert rows[1].reason_codes == ()
