from featuregen.overlay.conflict_review import (
    ConflictState,
    conflict_events,
    conflict_fingerprint,
    open_or_reopen_conflict,
    read_conflict,
    transition_conflict,
)


def _open(db, fingerprint="fp"):
    return open_or_reopen_conflict(
        db,
        fingerprint=fingerprint,
        logical_ref="r",
        field_name="sensitivity",
        severity="high",
        competing_evidence_ids=("e1", "e2"),
        competing_value_hashes=("h1", "h2"),
    )


def test_conflict_fingerprint_is_order_independent_and_policy_scoped():
    a = conflict_fingerprint("ref", "sensitivity", ("h1", "h2"), "policy@1")
    b = conflict_fingerprint("ref", "sensitivity", ("h2", "h1"), "policy@1")
    assert a == b  # sorting the value hashes makes it order-independent
    # the policy version participates in identity: same values, newer policy -> new fingerprint
    assert conflict_fingerprint("ref", "sensitivity", ("h1", "h2"), "policy@2") != a


def test_open_is_idempotent_on_fingerprint(db):
    cid1 = _open(db, fingerprint="fp1")
    cid2 = _open(db, fingerprint="fp1")
    assert cid1 == cid2  # same fingerprint -> same conflict_id, never a duplicate row
    assert cid1.startswith("cfl_")
    (count,) = db.execute(
        "SELECT count(*) FROM conflict_review WHERE fingerprint=%s", ("fp1",)
    ).fetchone()
    assert count == 1
    rec = read_conflict(db, cid1)
    assert rec.state == ConflictState.OPEN  # still OPEN — a non-terminal conflict is not reopened
    assert rec.competing_value_hashes == ("h1", "h2")
    # a re-open of a still-OPEN conflict appends NO extra history event
    assert [e.to_state for e in conflict_events(db, cid1)] == ["open"]


def test_resolved_then_same_fingerprint_reopens(db):
    cid = _open(db, fingerprint="fp2")
    transition_conflict(db, cid, ConflictState.RESOLVED, actor="bob", reason="tokenized")
    assert read_conflict(db, cid).state == ConflictState.RESOLVED
    cid2 = _open(db, fingerprint="fp2")  # a re-upload with the same fingerprint
    assert cid2 == cid  # idempotent: reopens the SAME conflict, no duplicate row
    assert read_conflict(db, cid).state == ConflictState.REOPENED
    (count,) = db.execute(
        "SELECT count(*) FROM conflict_review WHERE fingerprint=%s", ("fp2",)
    ).fetchone()
    assert count == 1
    assert [e.to_state for e in conflict_events(db, cid)] == ["open", "resolved", "reopened"]


def test_transitions_are_recorded_in_history(db):
    cid = open_or_reopen_conflict(
        db,
        fingerprint="fp",
        logical_ref="r",
        field_name="sensitivity",
        severity="high",
        competing_evidence_ids=("e1",),
        competing_value_hashes=("h1", "h2"),
    )
    transition_conflict(db, cid, ConflictState.ACKNOWLEDGED, actor="alice", reason="reviewing")
    transition_conflict(db, cid, ConflictState.RESOLVED, actor="bob", reason="tokenized")
    hist = conflict_events(db, cid)
    assert [(h.to_state, h.actor) for h in hist][-2:] == [
        ("acknowledged", "alice"),
        ("resolved", "bob"),
    ]
    # the audit event carries the from_state -> to_state edge and the reason
    ack = hist[-2]
    assert ack.from_state == "open"
    assert ack.to_state == "acknowledged"
    assert ack.reason == "reviewing"


def test_read_unknown_conflict_raises(db):
    import pytest

    with pytest.raises(KeyError):
        read_conflict(db, "cfl_does_not_exist")
