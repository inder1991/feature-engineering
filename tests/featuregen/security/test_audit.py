import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.security.audit import record_security_event, verify_chain


def test_append_chains_and_verifies(db):
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
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
    assert rows[0][0] is None  # genesis prev_hash
    assert rows[1][0] == rows[0][1]  # chain links
    assert verify_chain(db) is True


def test_security_audit_is_physically_append_only(db):
    # Defense in depth: even a privileged actor cannot edit or tail-truncate the audit chain.
    # A BEFORE UPDATE OR DELETE row trigger RAISEs, mirroring documents/feature_versions.
    import psycopg
    import pytest

    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
    )
    # Savepoints so an aborted statement does not discard the inserted row under test.
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), db.transaction():
        db.execute("UPDATE security_audit SET reason = 'edited' WHERE seq = 1")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), db.transaction():
        db.execute("DELETE FROM security_audit WHERE seq = 1")
    assert db.execute("SELECT count(*) FROM security_audit").fetchone()[0] == 1
    assert db.execute("SELECT reason FROM security_audit WHERE seq = 1").fetchone()[0] == "r1"


def test_tampering_breaks_chain(db):
    # The hash chain catches tampering that bypasses the physical trigger (e.g. a heap-level
    # edit or a disabled trigger). We simulate that bypass by disabling the append-only trigger.
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
    )
    db.execute("ALTER TABLE security_audit DISABLE TRIGGER security_audit_no_mutation")
    db.execute("UPDATE security_audit SET reason = 'edited' WHERE seq = 1")
    db.execute("ALTER TABLE security_audit ENABLE TRIGGER security_audit_no_mutation")
    assert verify_chain(db) is False


def test_editing_actor_role_claims_breaks_chain(db):
    # The hash must cover the FULL actor envelope, not just actor.subject. Editing a
    # non-subject field such as role_claims (with the physical trigger disabled, as the
    # tamper test does) must break verify_chain().
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
    )
    assert verify_chain(db) is True
    db.execute("ALTER TABLE security_audit DISABLE TRIGGER security_audit_no_mutation")
    db.execute(
        "UPDATE security_audit "
        "SET actor = jsonb_set(actor, '{role_claims}', '[\"admin\"]') WHERE seq = 1"
    )
    db.execute("ALTER TABLE security_audit ENABLE TRIGGER security_audit_no_mutation")
    assert verify_chain(db) is False


def test_editing_retention_class_breaks_chain(db):
    # retention_class is part of the hashed logical row.
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
    )
    db.execute("ALTER TABLE security_audit DISABLE TRIGGER security_audit_no_mutation")
    db.execute("UPDATE security_audit SET retention_class = 'forever' WHERE seq = 1")
    db.execute("ALTER TABLE security_audit ENABLE TRIGGER security_audit_no_mutation")
    assert verify_chain(db) is False


def test_denial_lands_in_security_stream_not_events(db):
    from types import SimpleNamespace

    from featuregen.security.audit import record_denial

    a = mint_test_identity(subject="user:mallory", role_claims=["data_scientist"])
    cmd = SimpleNamespace(action="activate", aggregate="feature", aggregate_id="feature_9", actor=a)
    record_denial(db, cmd, "no matching authz policy")
    assert db.execute("SELECT count(*) FROM security_audit").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_audit_chain_is_hmac_not_bare_hash(db):
    # BLOCKER #4: chain signatures must be KEYED (HMAC), not a bare SHA-256 that any writer
    # can recompute. A chain signed with one key must NOT verify under a different key.
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
        key="right-key",
    )
    assert verify_chain(db, key="right-key") is True
    assert verify_chain(db, key="wrong-key") is False


def test_altered_entry_hash_fails_verify(db):
    # A forged/altered entry_hash must fail verify_chain. verify_chain compares the freshly
    # recomputed secret-keyed MAC against the stored entry_hash with hmac.compare_digest
    # (constant-time), so a MAC-forgery timing side-channel is not leaked. Flipping one hex
    # char of the stored MAC exercises that compare path (it cannot prove constant-time, but
    # documents the intent and guards against a regression to a plain `!=`).
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="r1",
    )
    assert verify_chain(db) is True
    stored = db.execute("SELECT entry_hash FROM security_audit WHERE seq = 1").fetchone()[0]
    # Flip the first hex nibble so exactly one byte of the stored MAC differs.
    tampered = ("f" if stored[0] != "f" else "0") + stored[1:]
    db.execute("ALTER TABLE security_audit DISABLE TRIGGER security_audit_no_mutation")
    db.execute("UPDATE security_audit SET entry_hash = %s WHERE seq = 1", (tampered,))
    db.execute("ALTER TABLE security_audit ENABLE TRIGGER security_audit_no_mutation")
    assert verify_chain(db) is False


def test_verify_chain_empty_is_not_silently_ok(db):
    # BLOCKER #4: an empty chain where entries were expected must NOT verify True
    # (TRUNCATE-then-verify previously passed silently). Default preserves empty-is-ok.
    assert verify_chain(db, expect_nonempty=True) is False
    assert verify_chain(db) is True


def test_audit_signing_fails_closed_without_key(db, monkeypatch):
    # Fail closed: with no HMAC key configured, refuse to sign rather than fall back to a
    # default/unkeyed hash that would silently restore forgeability.
    import featuregen.security.audit as audit_mod

    monkeypatch.delenv("FEATUREGEN_AUDIT_HMAC_KEY", raising=False)
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    with pytest.raises(audit_mod.AuditKeyNotConfigured):
        record_security_event(
            db,
            event_type="COMMAND_DENIED",
            actor=a,
            attempted_action="activate",
            decision="denied",
            reason="r1",
        )


def test_concurrent_appends_keep_single_chain(db):
    # Two genuinely concurrent committers must NOT fork the tamper-evident chain
    # (no two genesis rows; the second must chain off the first). The advisory xact
    # lock in record_security_event serializes them.
    import threading

    import psycopg

    # Connect worker threads to the SAME cluster as the fixture (ephemeral or env-provided),
    # not a hard-coded default socket — the ephemeral test cluster has a dynamic DSN.
    dsn = db.info.dsn
    actor = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    ready = threading.Barrier(2)

    def worker():
        conn = psycopg.connect(dsn)
        try:
            ready.wait()  # maximize contention on the empty table
            record_security_event(
                conn,
                event_type="COMMAND_DENIED",
                actor=actor,
                attempted_action="activate",
                decision="denied",
                reason="race",
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
    assert sum(1 for r in rows if r[0] is None) == 1  # exactly one genesis — no fork
    assert rows[1][0] == rows[0][1]  # second chains off the first
    assert verify_chain(db) is True
