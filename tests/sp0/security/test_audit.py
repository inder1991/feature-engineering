from sp0.identity.build import build_human_identity
from sp0.security.audit import record_security_event, verify_chain


def test_append_chains_and_verifies(db):
    a = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    first = record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="no matching authz policy",
        aggregate="feature",
        aggregate_id="feature_1",
    )
    second = record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="deprecate",
        decision="denied",
        reason="no matching authz policy",
    )
    assert first.startswith("sec_")
    assert second != first
    rows = db.execute(
        "SELECT prev_hash, entry_hash FROM security_audit ORDER BY seq ASC"
    ).fetchall()
    assert rows[0][0] is None                 # genesis prev_hash
    assert rows[1][0] == rows[0][1]           # chain links
    assert verify_chain(db) is True


def test_tampering_breaks_chain(db):
    a = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db, event_type="COMMAND_DENIED", actor=a,
        attempted_action="activate", decision="denied", reason="r1",
    )
    db.execute("UPDATE security_audit SET reason = 'edited' WHERE seq = 1")
    assert verify_chain(db) is False


def test_denial_lands_in_security_stream_not_events(db):
    from sp0.security.audit import record_denial
    from types import SimpleNamespace

    a = build_human_identity(subject="user:mallory", role_claims=["data_scientist"])
    cmd = SimpleNamespace(action="activate", aggregate="feature",
                          aggregate_id="feature_9", actor=a)
    record_denial(db, cmd, "no matching authz policy")
    assert db.execute("SELECT count(*) FROM security_audit").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_concurrent_appends_keep_single_chain(db):
    # Two genuinely concurrent committers must NOT fork the tamper-evident chain
    # (no two genesis rows; the second must chain off the first). The advisory xact
    # lock in record_security_event serializes them.
    import os
    import threading

    import psycopg

    dsn = os.environ.get("SP0_TEST_DSN", "postgresql:///sp0_test")
    actor = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    ready = threading.Barrier(2)

    def worker():
        conn = psycopg.connect(dsn)
        try:
            ready.wait()                       # maximize contention on the empty table
            record_security_event(
                conn, event_type="COMMAND_DENIED", actor=actor,
                attempted_action="activate", decision="denied", reason="race",
            )
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = db.execute(
        "SELECT prev_hash, entry_hash FROM security_audit ORDER BY seq ASC"
    ).fetchall()
    assert len(rows) == 2
    assert sum(1 for r in rows if r[0] is None) == 1     # exactly one genesis — no fork
    assert rows[1][0] == rows[0][1]                       # second chains off the first
    assert verify_chain(db) is True
