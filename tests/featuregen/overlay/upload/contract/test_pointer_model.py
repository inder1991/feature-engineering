"""Delivery H2b — the pointer-model confirm WRITE path.

``confirm_contract`` now writes an IMMUTABLE contract version + write-once ``contract_input_column``
lineage + an atomic compare-and-swap of the ``feature_current_contract`` pointer, under a per-feature
advisory lock taken FIRST. The pointer + input rows are the AUTHORITATIVE write;
``feature``/``feature_derives_from`` are demoted to the current-pointer compatibility projection.

Risk 3 (the CAS/advisory serialization) is proven TWO ways:
  * hermetically — a ``pg_try_advisory_xact_lock`` probe shows the lock is HELD mid-confirm (pins the
    exact key derivation), mirroring the ingest-concurrency suite;
  * with a REAL two-connection race — two threads confirm the SAME feature identity concurrently; the
    advisory lock serializes them so the pointer advances monotonically (no lost update), exactly one
    feature exists (B4), both contract versions + both input-row sets exist, and exactly one SUPERSEDED
    event is emitted per advance. This mirrors the second-connection harness in the snapshot/ingest
    concurrency suites. The committed rows are scoped to PRIVATE identities; the write-once chain
    (contract_input_column / validation events) cannot be deleted, so the session's ephemeral cluster
    tears them down.
"""
from __future__ import annotations

import hashlib
import threading

import psycopg
import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    feature_contract_lock_key,
)
from featuregen.overlay.upload.graph import build_graph


def _bank(conn, source="bank"):
    build_graph(conn, source, [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True)])


def _draft(name="avg_balance_90d", source="bank"):
    return ContractDraft(name, "Average 90-day ledger balance.", "accounts",
                         "avg_90d", "posted_at", ["public.accounts.balance"],
                         derives_pairs=((source, "public.accounts.balance"),))


def _inputs(conn, contract_id):
    rows = conn.execute(
        "SELECT source, physical_ref, role, item_hash FROM contract_input_column "
        "WHERE contract_id = %s ORDER BY role", (contract_id,)).fetchall()
    return rows


# ── TEST 1 — first confirm writes the pointer + inputs ──────────────────────────────────────────────
def test_first_confirm_writes_pointer_and_input_rows(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    assert c.version == 1

    # ONE contract version.
    assert db.execute("SELECT count(*) FROM contract WHERE feature_id = %s",
                      (c.feature_id,)).fetchone()[0] == 1

    # contract_input_column: one role-labelled row per reconciled input — derives + grain + as_of, each
    # with a non-empty item_hash.
    rows = _inputs(db, c.contract_id)
    by_role = {r[2]: r for r in rows}
    assert set(by_role) == {"derives", "grain", "as_of"}
    assert by_role["derives"][:2] == ("bank", "public.accounts.balance")
    assert by_role["grain"][1] == "accounts"
    assert by_role["as_of"][1] == "posted_at"
    assert all(r[3] for r in rows)  # every row carries an item_hash

    # the pointer is version 1 and points at THIS contract.
    ptr = db.execute("SELECT contract_id, pointer_version FROM feature_current_contract "
                     "WHERE feature_id = %s", (c.feature_id,)).fetchone()
    assert ptr == (c.contract_id, 1)

    # an ASSESSED validation event was emitted for the contract.
    assert db.execute("SELECT count(*) FROM feature_contract_validation_event "
                      "WHERE contract_id = %s AND event_type = 'ASSESSED'",
                      (c.contract_id,)).fetchone()[0] == 1

    # the 1011 INITIAL stamp columns mirror the confirm-time re-run (the same values the mutable 1003
    # columns + the ASSESSED event carry) — the at-confirm INITIAL axis.
    init = db.execute("SELECT initial_verification, initial_validation_status, verification, "
                      "validation_status FROM contract WHERE contract_id = %s",
                      (c.contract_id,)).fetchone()
    assert init[0] == "DESIGN-CHECKED" == init[2]          # initial_verification == verification
    assert init[1] == init[3]                              # initial_validation_status == validation_status

    # compat projection matches the draft.
    pair = db.execute("SELECT catalog_source, object_ref FROM feature_derives_from "
                      "WHERE feature_id = %s", (c.feature_id,)).fetchone()
    assert pair == ("bank", "public.accounts.balance")


# ── TEST 2 — re-confirm advances the pointer; the prior version + its inputs are immutable ───────────
def test_reconfirm_advances_pointer_and_supersedes_prior(db):
    _bank(db)
    c1 = confirm_contract(db, _draft(), actor="ds1")
    v1_inputs = {r[3] for r in _inputs(db, c1.contract_id)}
    assert v1_inputs  # v1 has input rows

    c2 = confirm_contract(db, _draft(), actor="ds1")
    assert c2.version == 2 and c2.feature_id == c1.feature_id
    assert c2.contract_id != c1.contract_id

    # pointer now points at v2 with pointer_version 2.
    ptr = db.execute("SELECT contract_id, pointer_version FROM feature_current_contract "
                     "WHERE feature_id = %s", (c1.feature_id,)).fetchone()
    assert ptr == (c2.contract_id, 2)

    # the PRIOR contract got exactly one SUPERSEDED event.
    assert db.execute("SELECT count(*) FROM feature_contract_validation_event "
                      "WHERE contract_id = %s AND event_type = 'SUPERSEDED'",
                      (c1.contract_id,)).fetchone()[0] == 1

    # v1's input rows are UNCHANGED (immutable) and v2 wrote its OWN set.
    assert {r[3] for r in _inputs(db, c1.contract_id)} == v1_inputs
    assert {r[3] for r in _inputs(db, c2.contract_id)}  # v2 has its own rows
    # v1 and v2 item_hashes differ (contract_id is in the hash basis).
    assert v1_inputs.isdisjoint({r[3] for r in _inputs(db, c2.contract_id)})

    # exactly ONE feature; compat projection reflects the new version.
    assert db.execute("SELECT count(*) FROM feature WHERE name = 'avg_balance_90d'"
                      ).fetchone()[0] == 1


# ── TEST 3 — Slice-3 reconciliation preserved: input rows reflect the RECONCILED grain/derives ───────
def test_input_rows_reflect_server_reconciled_grain_not_raw_draft(db):
    """The route reconciles grain_table/derives_from from the server-reconstructed chosen feature BEFORE
    calling confirm_contract; confirm consumes the already-reconciled draft. The contract_input_column
    rows must reflect the reconciled (server-authoritative) values — exactly like the feature/derives
    compat writes. Simulate the reconciliation here (the route's `replace(draft, grain_table=...,
    derives_from=...)`) and assert the grain input row carries the RECONCILED grain, not a tampered one."""
    from dataclasses import replace
    _bank(db)
    tampered = replace(_draft(), grain_table=None, derives_from=[])  # what a hostile client would send
    reconciled = replace(tampered, grain_table="accounts",           # the route's server overwrite
                         derives_from=["public.accounts.balance"])
    c = confirm_contract(db, reconciled, actor="ds1")
    by_role = {r[2]: r for r in _inputs(db, c.contract_id)}
    assert by_role["grain"][1] == "accounts"                # reconciled grain, not None
    assert ("bank", "public.accounts.balance") == by_role["derives"][:2]


# ── TEST 4 — input rows are write-once (H2a trigger) ─────────────────────────────────────────────────
def test_contract_input_column_is_write_once(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    # A savepoint per attempt (db.transaction()) so the write-once RAISE aborts only the savepoint, not
    # the confirm — proving BOTH UPDATE and DELETE are rejected by the 1011 trigger.
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("UPDATE contract_input_column SET role = 'tampered' WHERE contract_id = %s",
                   (c.contract_id,))
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("DELETE FROM contract_input_column WHERE contract_id = %s", (c.contract_id,))
    # the rows survived both rejected mutations.
    assert _inputs(db, c.contract_id)


# ── TEST 5 — the lock key is a stable, namespaced sha256 of the normalized feature identity ──────────
def test_lock_key_is_stable_namespaced_and_case_insensitive():
    expected = int.from_bytes(
        hashlib.sha256(b"contract_confirm:avg_balance_90d").digest()[:8], "big", signed=True)
    assert feature_contract_lock_key("avg_balance_90d") == expected
    # normalized (.strip().lower()): case/whitespace variants of ONE identity derive the SAME key.
    assert feature_contract_lock_key("  AVG_Balance_90d  ") == expected
    # a different feature is a different key (never falsely contends).
    assert feature_contract_lock_key("other") != expected
    # no collision with the other advisory-lock key spaces in the codebase.
    others = {
        7_000_007, 6157423001, 4_201_873_355_201_001,
        int.from_bytes(hashlib.sha256(b"overlay_ingest:avg_balance_90d").digest()[:8],
                       "big", signed=True),
    }
    assert feature_contract_lock_key("avg_balance_90d") not in others


# ── TEST 6 — hermetic: the per-feature advisory lock is HELD mid-confirm (pins the key derivation) ───
@pytest.fixture
def probe_conn(_dsn):
    with psycopg.connect(_dsn, autocommit=True) as c:
        yield c


def test_confirm_holds_the_feature_lock_until_tx_end(db, probe_conn):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    assert c.version == 1
    key = feature_contract_lock_key("avg_balance_90d")
    # db's transaction is still open (the test commits nothing), so the feature lock is HELD: a second
    # session's try-lock on the SAME key reports contended — the serialization proof.
    assert probe_conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is False
    # a DIFFERENT feature identity hashes to a different key: never blocked by this confirm.
    other = feature_contract_lock_key("some_other_feature")
    assert probe_conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (other,)).fetchone()[0] is True
    db.rollback()  # transaction-scoped: releasing the tx frees the lock
    assert probe_conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is True


# ── TEST 7 — REAL two-connection race: concurrent confirms serialize; pointer is monotonic ──────────
_RACE_SRC = "h2b_race_src"
_RACE_FEATURE = "h2b_race_feature"


def _race_draft():
    return ContractDraft(_RACE_FEATURE, "Race feature.", "accounts", "avg_90d", "posted_at",
                         ["public.accounts.balance"],
                         derives_pairs=((_RACE_SRC, "public.accounts.balance"),))


@pytest.fixture
def race_cleanup(_dsn):
    """Delete the DELETABLE committed rows this race writes on teardown, so the shared session cluster
    is not polluted (test_graph_build asserts an EMPTY graph_node; other suites read object_refs
    unscoped). The write-once chain (contract / contract_input_column / validation events) CANNOT be
    deleted — the 1011/1009 no-mutation triggers block it — so those private-scoped rows stay until the
    ephemeral cluster tears down; no suite asserts a global count on them (verified by the full sweep)."""
    yield
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (_RACE_SRC,))
        c.execute("DELETE FROM graph_node WHERE catalog_source = %s", (_RACE_SRC,))
        c.execute("DELETE FROM feature_current_contract WHERE feature_id IN "
                  "(SELECT feature_id FROM feature WHERE name = %s)", (_RACE_FEATURE,))
        c.execute("DELETE FROM feature_derives_from WHERE feature_id IN "
                  "(SELECT feature_id FROM feature WHERE name = %s)", (_RACE_FEATURE,))


def test_concurrent_confirms_serialize_via_advisory_lock(_dsn, race_cleanup):
    # Seed the graph COMMITTED so both racing connections see the baseline.
    seed = psycopg.connect(_dsn)
    try:
        _bank(seed, source=_RACE_SRC)
        seed.commit()
    finally:
        seed.close()

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        conn = psycopg.connect(_dsn)
        try:
            barrier.wait(timeout=10)          # both threads reach confirm together
            c = confirm_contract(conn, _race_draft(), actor=tag)
            conn.commit()                     # releases the advisory lock; makes the version visible
            results[tag] = c.version
        except Exception as exc:              # noqa: BLE001 — record so the assert can surface it
            conn.rollback()
            results[tag] = exc
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "a confirm thread hung (advisory-lock deadlock?)"

    # Verify with a fresh probe connection reading COMMITTED state.
    probe = psycopg.connect(_dsn, autocommit=True)
    try:
        # BOTH confirms succeeded and serialized to versions {1, 2} — no lost update, no exception.
        assert sorted(results.values()) == [1, 2], f"expected serialized [1, 2], got {results}"

        # exactly ONE feature for the identity (advisory lock prevented B4 proliferation).
        feats = probe.execute("SELECT feature_id FROM feature WHERE name = %s",
                              (_RACE_FEATURE,)).fetchall()
        assert len(feats) == 1
        feature_id = feats[0][0]

        # TWO contract versions exist.
        versions = probe.execute("SELECT version FROM contract WHERE feature_id = %s ORDER BY version",
                                 (feature_id,)).fetchall()
        assert [v[0] for v in versions] == [1, 2]
        v1 = probe.execute("SELECT contract_id FROM contract WHERE feature_id = %s AND version = 1",
                           (feature_id,)).fetchone()[0]
        v2 = probe.execute("SELECT contract_id FROM contract WHERE feature_id = %s AND version = 2",
                           (feature_id,)).fetchone()[0]

        # the pointer is monotonic: pointer_version == 2, pointing at the v2 contract (not lost-updated).
        ptr = probe.execute("SELECT contract_id, pointer_version FROM feature_current_contract "
                            "WHERE feature_id = %s", (feature_id,)).fetchone()
        assert ptr == (v2, 2)

        # exactly ONE SUPERSEDED per advance — v1 was superseded exactly once, v2 never.
        assert probe.execute("SELECT count(*) FROM feature_contract_validation_event "
                             "WHERE contract_id = %s AND event_type = 'SUPERSEDED'",
                             (v1,)).fetchone()[0] == 1
        assert probe.execute("SELECT count(*) FROM feature_contract_validation_event "
                             "WHERE contract_id = %s AND event_type = 'SUPERSEDED'",
                             (v2,)).fetchone()[0] == 0

        # BOTH input-row sets exist and are disjoint (no torn/interleaved state).
        v1_inputs = {r[0] for r in probe.execute(
            "SELECT item_hash FROM contract_input_column WHERE contract_id = %s", (v1,)).fetchall()}
        v2_inputs = {r[0] for r in probe.execute(
            "SELECT item_hash FROM contract_input_column WHERE contract_id = %s", (v2,)).fetchall()}
        assert v1_inputs and v2_inputs and v1_inputs.isdisjoint(v2_inputs)
    finally:
        probe.close()
