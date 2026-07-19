"""Delivery C4 Task 2 — the feature-contract validation STATE PROJECTION (event -> state fold).

Exercises the fold rules per event type, the sequence guard / idempotent replay, the load-bearing
reset==rebuild==live-catch-up equivalence, contract-version scoping, and fail-open-but-audited
degraded handling. Synthetic events are INSERTED directly (C4-T3 emits the real ASSESSED; Delivery I
the signed EXTERNAL_PASSED/FAILED) — this task only folds whatever the append-only log holds.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from featuregen.overlay.upload import feature_validation_projection as fvp
from featuregen.overlay.upload.feature_validation_projection import (
    PROJECTION_NAME,
    apply_event,
    catch_up,
    is_read_ready,
    lock_checkpoint,
    read_state,
    rebuild,
    reset,
)

# --------------------------------------------------------------------------------------------------
# Seeding helpers (mirror tests/featuregen/db/test_migration_1009.py's parent-row shapes).
# --------------------------------------------------------------------------------------------------
_CTR = 0


def _contract(conn, contract_id: str) -> str:
    feature_id = f"f_{contract_id}"
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, feature_id))
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) "
        "VALUES (%s, %s, %s, 1)", (contract_id, feature_id, feature_id))
    return contract_id


def _event(conn, contract_id: str, event_type: str, *, payload: object | None = None) -> dict:
    """Append one validation event (DB assigns the monotonic ``seq``); return the inserted row."""
    global _CTR
    _CTR += 1
    event_id = f"ev_{_CTR}"
    body = Jsonb(payload if payload is not None else {})
    conn.execute(
        "INSERT INTO feature_contract_validation_event "
        "(event_id, contract_id, event_type, payload) "
        "VALUES (%s, %s, %s, %s)", (event_id, contract_id, event_type, body))
    row = conn.execute(
        "SELECT event_id, contract_id, seq, event_type, payload "
        "FROM feature_contract_validation_event WHERE event_id = %s", (event_id,)).fetchone()
    return {"event_id": row[0], "contract_id": row[1], "seq": row[2],
            "event_type": row[3], "payload": row[4]}


def _requirement(conn, contract_id: str, *, requirement_id: str, blocking: bool = True,
                 content_hash: str = "sha256:r") -> str:
    conn.execute(
        "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
        "requirement_schema_version, metadata_input_fingerprint, code, blocking, content_hash) "
        "VALUES (%s, %s, 'v1', 'fp:abc', 'TYPE_IS_NUMERIC', %s, %s)",
        (requirement_id, contract_id, blocking, content_hash))
    return requirement_id


def _status(conn, contract_id: str) -> tuple[str, str, int] | None:
    row = read_state(conn, contract_id)
    if row is None:
        return None
    return row["validation_status"], row["effective_verification"], row["applied_seq"]


# --------------------------------------------------------------------------------------------------
# 1 + 2 — ASSESSED effective-state rules.
# --------------------------------------------------------------------------------------------------
def test_assessed_with_blocking_requirement_needs_external(conn) -> None:
    cid = _contract(conn, "c_needs_ext")
    _requirement(conn, cid, requirement_id="req_a", blocking=True)
    ev = _event(conn, cid, "ASSESSED")
    apply_event(conn, ev)
    assert _status(conn, cid) == ("needs_external_validation", "UNVERIFIED", ev["seq"])


def test_assessed_without_blocking_requirement_design_checked(conn) -> None:
    cid = _contract(conn, "c_design")
    ev = _event(conn, cid, "ASSESSED")  # no requirement rows -> deterministic pass, no blocker
    apply_event(conn, ev)
    assert _status(conn, cid) == ("design_checked", "DESIGN-CHECKED", ev["seq"])


def test_assessed_hard_reject(conn) -> None:
    cid = _contract(conn, "c_hard_reject")
    _requirement(conn, cid, requirement_id="req_hr", blocking=True)  # present, but hard-reject wins
    ev = _event(conn, cid, "ASSESSED", payload={"validation_status": "rejected"})
    apply_event(conn, ev)
    assert _status(conn, cid) == ("rejected", "UNVERIFIED", ev["seq"])


# --------------------------------------------------------------------------------------------------
# 3 — ASSESSED(needs_external) -> EXTERNAL_PASSED(all blocking) -> DATA-CHECKED -> INVALIDATED.
# --------------------------------------------------------------------------------------------------
def test_external_passed_promotes_to_data_checked_then_invalidated_demotes(conn) -> None:
    cid = _contract(conn, "c_promote")
    _requirement(conn, cid, requirement_id="req_p1", blocking=True, content_hash="sha256:p1")
    _requirement(conn, cid, requirement_id="req_p2", blocking=True, content_hash="sha256:p2")

    apply_event(conn, _event(conn, cid, "ASSESSED"))
    assert _status(conn, cid)[:2] == ("needs_external_validation", "UNVERIFIED")

    apply_event(conn, _event(conn, cid, "EXTERNAL_PASSED", payload={"requirement_id": "req_p1"}))
    assert _status(conn, cid)[:2] == ("needs_external_validation", "UNVERIFIED")  # not all yet

    apply_event(conn, _event(conn, cid, "EXTERNAL_PASSED", payload={"requirement_id": "req_p2"}))
    assert _status(conn, cid)[:2] == ("design_checked", "DATA-CHECKED")  # every blocker passed

    inv = _event(conn, cid, "INVALIDATED")
    apply_event(conn, inv)
    assert _status(conn, cid) == ("needs_external_validation", "UNVERIFIED", inv["seq"])


# --------------------------------------------------------------------------------------------------
# 4 — EXTERNAL_FAILED on a blocking requirement -> rejected, never DATA-CHECKED.
# --------------------------------------------------------------------------------------------------
def test_external_failed_rejects_never_data_checked(conn) -> None:
    cid = _contract(conn, "c_fail")
    _requirement(conn, cid, requirement_id="req_f", blocking=True)
    apply_event(conn, _event(conn, cid, "ASSESSED"))
    apply_event(conn, _event(conn, cid, "EXTERNAL_PASSED", payload={"requirement_id": "req_f"}))
    assert _status(conn, cid)[:2] == ("design_checked", "DATA-CHECKED")

    apply_event(conn, _event(conn, cid, "EXTERNAL_FAILED", payload={"requirement_id": "req_f"}))
    status, verification, _ = _status(conn, cid)
    assert status == "rejected"
    assert verification == "UNVERIFIED" and verification != "DATA-CHECKED"


# --------------------------------------------------------------------------------------------------
# 5 — Sequence guard: same event twice + an out-of-order lower seq never corrupt the state.
# --------------------------------------------------------------------------------------------------
def test_sequence_guard_idempotent_and_out_of_order(conn) -> None:
    cid = _contract(conn, "c_seq_guard")
    a1 = _event(conn, cid, "ASSESSED")                 # -> design_checked / DESIGN-CHECKED
    a2 = _event(conn, cid, "INVALIDATED")              # higher seq -> needs_external / UNVERIFIED

    assert apply_event(conn, a1) is True
    assert apply_event(conn, a1) is False              # replay of the SAME event is a no-op
    assert _status(conn, cid) == ("design_checked", "DESIGN-CHECKED", a1["seq"])

    assert apply_event(conn, a2) is True
    assert _status(conn, cid) == ("needs_external_validation", "UNVERIFIED", a2["seq"])

    # Now replay the OLDER (lower-seq) event: must not regress the state back to DESIGN-CHECKED.
    assert apply_event(conn, a1) is False
    assert _status(conn, cid) == ("needs_external_validation", "UNVERIFIED", a2["seq"])


# --------------------------------------------------------------------------------------------------
# 6 — reset == rebuild == live-catch-up equivalence (the load-bearing invariant).
# --------------------------------------------------------------------------------------------------
def test_reset_replay_equivalence(conn) -> None:
    # Two contracts, interleaved events, built incrementally with a live catch_up between batches.
    c1 = _contract(conn, "c_eq_1")
    c2 = _contract(conn, "c_eq_2")
    _requirement(conn, c1, requirement_id="req_e1", blocking=True)

    _event(conn, c1, "ASSESSED")                                   # c1: needs_external
    _event(conn, c2, "ASSESSED")                                   # c2: design_checked
    assert catch_up(conn) == 2
    _event(conn, c1, "EXTERNAL_PASSED", payload={"requirement_id": "req_e1"})  # c1: DATA-CHECKED
    _event(conn, c2, "INVALIDATED")                                # c2: needs_external / UNVERIFIED
    assert catch_up(conn) == 2

    live = {c: _status(conn, c) for c in (c1, c2)}
    assert live[c1][:2] == ("design_checked", "DATA-CHECKED")
    assert live[c2][:2] == ("needs_external_validation", "UNVERIFIED")

    rebuild(conn)  # reset() + full replay from seq 0
    replayed = {c: _status(conn, c) for c in (c1, c2)}
    assert replayed == live, "rebuild must reproduce the exact state a live catch_up produced"


# --------------------------------------------------------------------------------------------------
# 7 — contract-version scoping: an event for C1 never touches C2's state row.
# --------------------------------------------------------------------------------------------------
def test_contract_scoping(conn) -> None:
    c1 = _contract(conn, "c_scope_1")
    c2 = _contract(conn, "c_scope_2")
    apply_event(conn, _event(conn, c2, "ASSESSED"))  # c2: design_checked / DESIGN-CHECKED
    before = _status(conn, c2)

    # A whole lifecycle on C1 must leave C2 byte-for-byte unchanged.
    _requirement(conn, c1, requirement_id="req_s1", blocking=True)
    apply_event(conn, _event(conn, c1, "ASSESSED"))
    apply_event(conn, _event(conn, c1, "EXTERNAL_FAILED", payload={"requirement_id": "req_s1"}))
    assert _status(conn, c1)[0] == "rejected"
    assert _status(conn, c2) == before  # C2 untouched


# --------------------------------------------------------------------------------------------------
# 8 — degraded: a poison event marks its contract degraded + skips, sparing other contracts.
# --------------------------------------------------------------------------------------------------
def test_poison_event_marks_degraded_without_corrupting_others(conn) -> None:
    poison_c = _contract(conn, "c_poison")
    clean_c = _contract(conn, "c_clean")
    # A malformed ASSESSED (validation_status outside the vocabulary) is poison; a real contract, so
    # the FK holds — the malformed payload is what the fold rejects.
    _event(conn, poison_c, "ASSESSED", payload={"validation_status": "CORRUPT"})
    clean = _event(conn, clean_c, "ASSESSED")  # higher seq, clean

    applied = catch_up(conn)
    assert applied == 1  # only the clean event applied

    # The clean contract's state is correct and uncorrupted.
    assert _status(conn, clean_c) == ("design_checked", "DESIGN-CHECKED", clean["seq"])
    # The poison contract has NO state row (its apply rolled back to the savepoint).
    assert read_state(conn, poison_c) is None

    # The poison is marked degraded (keyed on the contract) AND recorded in the skip ledger.
    degraded = conn.execute(
        "SELECT aggregate_id, poison_event_id, poison_seq FROM projection_degraded "
        "WHERE projection_name = %s", (PROJECTION_NAME,)).fetchall()
    assert len(degraded) == 1 and degraded[0][0] == poison_c
    assert degraded[0][1] is None  # poison_event_id NULL: event isn't in the shared events table
    skips = conn.execute(
        "SELECT count(*) FROM projection_skips WHERE projection_name = %s",
        (PROJECTION_NAME,)).fetchone()[0]
    assert skips == 1

    # The checkpoint advanced PAST the poison (fail-open), so the projection is not stuck.
    ck = conn.execute(
        "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
        (PROJECTION_NAME,)).fetchone()[0]
    assert ck == clean["seq"]


def test_superseded_retains_row_and_demotes_stamp(conn) -> None:
    cid = _contract(conn, "c_superseded")
    apply_event(conn, _event(conn, cid, "ASSESSED"))  # design_checked / DESIGN-CHECKED
    sup = _event(conn, cid, "SUPERSEDED")
    apply_event(conn, sup)
    # Row RETAINED as history: validation_status kept, effective stamp demoted to UNVERIFIED.
    assert _status(conn, cid) == ("design_checked", "UNVERIFIED", sup["seq"])
    # MF-4: the terminal superseded marker is set on the state row.
    assert read_state(conn, cid)["superseded"] is True


def test_superseded_is_terminal_late_external_passed_cannot_resurrect(conn) -> None:
    """MF-4 stickiness: once SUPERSEDED, a late/redelivered EXTERNAL_PASSED that would otherwise
    promote every blocking requirement to DATA-CHECKED must NOT resurrect the retired version."""
    cid = _contract(conn, "c_sup_terminal")
    _requirement(conn, cid, requirement_id="req_st", blocking=True)
    apply_event(conn, _event(conn, cid, "ASSESSED"))          # needs_external / UNVERIFIED
    apply_event(conn, _event(conn, cid, "SUPERSEDED"))        # retired: superseded / UNVERIFIED
    # A late EXTERNAL_PASSED for the (only) blocking requirement — would normally reach DATA-CHECKED.
    late = _event(conn, cid, "EXTERNAL_PASSED", payload={"requirement_id": "req_st"})
    apply_event(conn, late)
    status, verification, applied = _status(conn, cid)
    assert verification == "UNVERIFIED" and verification != "DATA-CHECKED"  # NOT resurrected
    assert read_state(conn, cid)["superseded"] is True
    assert applied == late["seq"]                             # folded, but the promotion was gated

    # reset==replay: a full rebuild reproduces the identical terminal-superseded state.
    rebuild(conn)
    assert read_state(conn, cid)["superseded"] is True
    assert read_state(conn, cid)["effective_verification"] == "UNVERIFIED"


def test_reset_clears_state_and_ledgers(conn) -> None:
    cid = _contract(conn, "c_reset")
    apply_event(conn, _event(conn, cid, "ASSESSED"))
    catch_up(conn)
    assert read_state(conn, cid) is not None
    reset(conn)
    assert read_state(conn, cid) is None
    ck = conn.execute(
        "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
        (PROJECTION_NAME,)).fetchone()[0]
    assert ck == 0


# --------------------------------------------------------------------------------------------------
# MF-2 — catch_up's fold loop catches ONLY ProjectionApplyError (poison); any other exception (a
# transient deadlock/lock-timeout) PROPAGATES, so the event is never silently skipped + degraded.
# --------------------------------------------------------------------------------------------------
def test_transient_error_propagates_not_treated_as_poison(conn, monkeypatch) -> None:
    cid = _contract(conn, "c_transient")
    ev = _event(conn, cid, "ASSESSED")

    def _boom(_conn, _row):
        raise RuntimeError("deadlock detected")  # a transient DB error surrogate — NOT poison

    monkeypatch.setattr(fvp, "apply_event", _boom)
    with pytest.raises(RuntimeError, match="deadlock detected"):
        catch_up(conn)

    # NOT degraded, NOT skipped — a transient failure is not poison.
    assert conn.execute("SELECT count(*) FROM projection_degraded WHERE projection_name = %s",
                        (PROJECTION_NAME,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM projection_skips WHERE projection_name = %s",
                        (PROJECTION_NAME,)).fetchone()[0] == 0
    # The checkpoint did NOT advance past the un-applied event (the final UPDATE never ran).
    ck = conn.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
                      (PROJECTION_NAME,)).fetchone()[0]
    assert ck < ev["seq"]


def test_genuine_poison_still_degrades_and_skips(conn) -> None:
    # A malformed ASSESSED (validation_status outside the vocabulary) is a SIGNALLED poison
    # (ProjectionApplyError) — it still fails-open-but-audited: degraded + skipped + advanced past.
    cid = _contract(conn, "c_poison_mf2")
    poison = _event(conn, cid, "ASSESSED", payload={"validation_status": "CORRUPT"})
    assert catch_up(conn) == 0                              # nothing applied
    assert conn.execute("SELECT count(*) FROM projection_degraded WHERE projection_name = %s",
                        (PROJECTION_NAME,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM projection_skips WHERE projection_name = %s",
                        (PROJECTION_NAME,)).fetchone()[0] == 1
    ck = conn.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
                      (PROJECTION_NAME,)).fetchone()[0]
    assert ck == poison["seq"]                              # advanced PAST the poison (fail-open)


# --------------------------------------------------------------------------------------------------
# MF-1 — concurrent confirms serialize (lock BEFORE insert): both events fold, no skip / no regress.
# --------------------------------------------------------------------------------------------------
def _seed_committed_contract(conn, contract_id: str) -> None:
    feature_id = f"f_{contract_id}"
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, feature_id))
    conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
                 "VALUES (%s, %s, %s, 1)", (contract_id, feature_id, feature_id))


def _emit_assessed(conn, contract_id: str) -> None:
    conn.execute("INSERT INTO feature_contract_validation_event "
                 "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'ASSESSED', %s)",
                 (f"ev_race_{contract_id}", contract_id, Jsonb({"validation_status": "design_checked"})))


def test_concurrent_confirms_serialize_no_skip_or_regress(_dsn) -> None:
    """MF-1 (load-bearing): two GENUINELY interleaved confirms on two REAL connections. Each confirm
    locks the checkpoint FOR UPDATE BEFORE inserting its ASSESSED event, then folds. Connection B
    BLOCKS on that lock while A holds it (proving seq-assignment is serialized WITH the fold), and
    after A commits, B folds its own higher-seq event over A's committed prefix. Both events end up
    folded, the checkpoint == head (never regresses, nothing permanently skipped), both contracts'
    state is correct, and is_read_ready is TRUE + CONSISTENT."""
    ca = psycopg.connect(_dsn)
    cb = psycopg.connect(_dsn)
    cid_a, cid_b = "c_race_a", "c_race_b"
    fid_a, fid_b = f"f_{cid_a}", f"f_{cid_b}"
    try:
        # Seed both contracts + the checkpoint row, COMMITTED, so both real sessions see them.
        _seed_committed_contract(ca, cid_a)
        _seed_committed_contract(ca, cid_b)
        fvp._ensure_checkpoint(ca, PROJECTION_NAME, is_analytics=True)
        ca.execute("UPDATE projection_checkpoints SET checkpoint_seq = 0 WHERE projection_name = %s",
                   (PROJECTION_NAME,))
        ca.commit()

        # --- Confirm A: take the checkpoint lock FIRST, BEFORE inserting any event (the
        #     lock-before-insert discipline). No event emitted / no fold yet. ---
        lock_checkpoint(ca)

        # --- Confirm B interleaves in the window BEFORE A has emitted or folded: its lock_checkpoint
        #     must BLOCK on A's held row-lock. This is what isolates the fix — a plain (unlocked)
        #     SELECT would NOT block here (A has taken no other lock yet), so seq-assignment would
        #     race. Proven deterministically with a short lock_timeout -> LockNotAvailable. ---
        cb.execute("SET lock_timeout = '500ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            lock_checkpoint(cb)
        cb.rollback()                       # clear B's aborted transaction
        cb.execute("SET lock_timeout = '0'")
        cb.commit()

        # --- A now emits ASSESSED(A) + folds UNDER the held lock, then commits: its event + advanced
        #     checkpoint become visible to B. ---
        _emit_assessed(ca, cid_a)
        catch_up(ca)
        ca.commit()

        # --- B now proceeds under the (freed) lock: emit ASSESSED(B), fold over A's committed prefix. ---
        lock_checkpoint(cb)
        _emit_assessed(cb, cid_b)
        catch_up(cb)
        cb.commit()

        # --- Both folded; checkpoint == head; both states correct; read-ready + consistent. ---
        head = ca.execute("SELECT max(seq) FROM feature_contract_validation_event").fetchone()[0]
        ck = ca.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
                        (PROJECTION_NAME,)).fetchone()[0]
        assert ck == head                                    # no regress, nothing skipped
        for cid in (cid_a, cid_b):
            st = read_state(ca, cid)
            assert st is not None, f"{cid} was permanently skipped (never folded)"
            assert st["validation_status"] == "design_checked"
            assert st["effective_verification"] == "DESIGN-CHECKED"
        assert is_read_ready(ca) is True
    finally:
        ca.rollback()
        cb.rollback()
        # Committed rows persist in the session DB — clean up so nothing leaks into other tests.
        # TRUNCATE bypasses the write-once triggers (allowed in the superuser test cluster).
        with psycopg.connect(_dsn, autocommit=True) as cc:
            cc.execute("TRUNCATE feature_contract_validation_event, "
                       "feature_validation_requirement, feature_contract_validation_state")
            cc.execute("UPDATE projection_checkpoints SET checkpoint_seq = 0, head_seq = 0 "
                       "WHERE projection_name = %s", (PROJECTION_NAME,))
            cc.execute("DELETE FROM projection_skips WHERE projection_name = %s", (PROJECTION_NAME,))
            cc.execute("DELETE FROM projection_degraded WHERE projection_name = %s", (PROJECTION_NAME,))
            cc.execute("DELETE FROM contract WHERE contract_id IN (%s, %s)", (cid_a, cid_b))
            cc.execute("DELETE FROM feature WHERE feature_id IN (%s, %s)", (fid_a, fid_b))
        ca.close()
        cb.close()
