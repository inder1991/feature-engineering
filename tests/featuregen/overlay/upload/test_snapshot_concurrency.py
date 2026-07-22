"""Delivery C0 Task 6 — the SNAPSHOT-CONCURRENCY proof (the invariant that justifies C0's
REPEATABLE READ design).

The core C0 guarantee under test: when a feature-generation request builds its metadata snapshot
(``build_metadata_snapshot``) on a REPEATABLE READ connection, it observes ONE consistent
point-in-time of committed catalog state — EITHER the pre-ingestion state OR the post-ingestion
state, as a COMPLETE view — even if an ingestion commits a change to those very columns PARTWAY
THROUGH the snapshot's reads. It is NEVER a torn mix (pre-change for some refs, post-change for
others). The C0-T3 builder tests prove determinism/immutability/isolation-guard on a single
connection; a rolled-back single-connection harness CANNOT show cross-transaction isolation, so this
suite drives TWO REAL interleaved connections against the test cluster (mirroring the second-session
pattern in the dispatch/ingest-concurrency suites).

The interleave (see :func:`test_snapshot_is_torn_free_across_a_concurrent_ingestion_commit`):
  1. Connection A (feature-gen, REPEATABLE READ) seeds a two-column graph and COMMITS it, so both
     connections see the baseline.
  2. A issues its FIRST read — this PINS A's REPEATABLE READ snapshot at the pre-change point (a PG
     RR transaction snapshots at its first statement, not at COMMIT).
  3. Connection B (ingestion) COMMITS a change to BOTH columns and closes — fully durable.
  4. A runs ``build_metadata_snapshot`` to completion. Because A's snapshot was pinned in step 2
     (before B committed), every value A captures is the PRE-B value for ALL refs — internally
     consistent, never torn.
  5. A fresh REPEATABLE READ connection C (a new feature request AFTER B) sees the POST-B state
     COMPLETELY.

A/C never COMMIT their snapshots (they roll back), so the only durable rows this suite writes are the
committed graph seed for its private ``catalog_source`` — cleaned up in a ``finally``.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_metadata_snapshot import build_metadata_snapshot
from featuregen.overlay.upload.graph import build_graph

# A private source so the committed seed never collides with another test's catalog and is trivially
# cleaned up (delete-by-catalog_source).
_SRC = "c0_snapshot_conc"
_BAL = "public.accounts.balance"
_FEE = "public.accounts.fee"
_REFS = [(_SRC, _BAL), (_SRC, _FEE)]

# Pre-ingestion (baseline) additivity values, and the DISTINCT values B commits. The two columns
# start with DIFFERENT values and both move, so a torn read (pre-B for one ref, post-B for the other)
# would be a concretely observable mix — not maskable by the two refs sharing a value.
_PRE = {_BAL: "semi_additive", _FEE: "additive"}
_POST = {_BAL: "non_additive", _FEE: "semi_additive"}


def _rr(conn) -> None:
    """Pin REPEATABLE READ BEFORE the connection's first query (mirrors the C0-T2 feature-generation
    connection; psycopg refuses a mid-transaction isolation change)."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _seed(conn) -> None:
    build_graph(conn, _SRC, [
        CanonicalRow(_SRC, "accounts", "balance", "numeric", additivity=_PRE[_BAL]),
        CanonicalRow(_SRC, "accounts", "fee", "numeric", additivity=_PRE[_FEE])])


def _read_additivity(conn, object_ref: str) -> str | None:
    return conn.execute(
        "SELECT additivity FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (_SRC, object_ref)).fetchone()[0]


def _commit_change(conn) -> None:
    """Connection B's ingestion-shaped change: overwrite the additivity of BOTH columns, committed."""
    for object_ref, value in _POST.items():
        conn.execute(
            "UPDATE graph_node SET additivity = %s "
            "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
            (value, _SRC, object_ref))
    conn.commit()


def _snapshot_additivity(conn, snapshot_id: str) -> dict[str, str]:
    """The additivity value PERSISTED into each snapshot item (read on the builder's own connection,
    from its uncommitted rows) — proves the durable snapshot agrees with the in-memory context."""
    rows = conn.execute(
        "SELECT graph_ref, value_json->>'value' FROM catalog_metadata_snapshot_item "
        "WHERE snapshot_id = %s AND field_or_fact_type = 'additivity'",
        (snapshot_id,)).fetchall()
    return dict(rows)


@pytest.fixture
def cleanup_seed(_dsn):
    """Delete the committed graph seed for this suite's private source on teardown (the snapshot rows
    A/C write are rolled back, so only the seed is durable). Autocommit: one-statement cleanup."""
    yield
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (_SRC,))
        c.execute("DELETE FROM graph_node WHERE catalog_source = %s", (_SRC,))


# ── TEST 1 — the key proof: torn-free under a concurrent ingestion commit ───────────────────────────
def test_snapshot_is_torn_free_across_a_concurrent_ingestion_commit(_dsn, cleanup_seed) -> None:
    a = psycopg.connect(_dsn)
    c = psycopg.connect(_dsn)
    try:
        # 1. Connection A (feature-gen): REPEATABLE READ. Seed a two-column graph + COMMIT so both
        #    connections see the baseline.
        _rr(a)
        _seed(a)
        a.commit()

        # 2. Begin A's snapshot transaction: A's FIRST read PINS its REPEATABLE READ snapshot at the
        #    PRE-B point (a PG RR transaction snapshots at its first statement).
        assert _read_additivity(a, _BAL) == _PRE[_BAL]

        # 3. Connection B (ingestion): COMMIT a change to BOTH columns — fully durable — then close.
        b = psycopg.connect(_dsn)
        try:
            _commit_change(b)
        finally:
            b.close()
        # A truly-fresh autocommit reader proves B's change is committed (not just B-local).
        with psycopg.connect(_dsn, autocommit=True) as probe:
            assert _read_additivity(probe, _BAL) == _POST[_BAL]
            assert _read_additivity(probe, _FEE) == _POST[_FEE]

        # 4. Continue A's build to completion — under the snapshot pinned in step 2, BEFORE B's commit.
        ctx_a = build_metadata_snapshot(
            a, generation_run_id="fgr_conc_a", refs=_REFS, read_scope_hash="sha256:scope")

        # 5. ASSERT torn-free: every value A captured reflects ONE consistent point-in-time — the
        #    PRE-B state for ALL refs (RR isolates A from B's mid-transaction commit), never a mix of
        #    pre-B for one column and post-B for the other.
        assert ctx_a.facts(_SRC, _BAL, "additivity").value == _PRE[_BAL]
        assert ctx_a.facts(_SRC, _FEE, "additivity").value == _PRE[_FEE]
        # the DURABLE snapshot items agree with the in-memory context (one internally-consistent view)
        assert _snapshot_additivity(a, ctx_a.snapshot_id) == {
            _BAL: _PRE[_BAL], _FEE: _PRE[_FEE]}

        # A NEW feature request (fresh RR connection C, entered AFTER B) sees the POST-B state
        # COMPLETELY — its build's first statement snapshots after B's commit.
        _rr(c)
        ctx_c = build_metadata_snapshot(
            c, generation_run_id="fgr_conc_c", refs=_REFS, read_scope_hash="sha256:scope")
        assert ctx_c.facts(_SRC, _BAL, "additivity").value == _POST[_BAL]
        assert ctx_c.facts(_SRC, _FEE, "additivity").value == _POST[_FEE]
        assert _snapshot_additivity(c, ctx_c.snapshot_id) == {
            _BAL: _POST[_BAL], _FEE: _POST[_FEE]}

        # Pre vs post are genuinely different committed states, so their content hashes differ: A
        # sealed the old view whole, C sealed the new view whole — neither sealed a torn mix.
        assert ctx_a.content_hash != ctx_c.content_hash
    finally:
        a.rollback()   # discard A's uncommitted snapshot rows (never durable)
        a.close()
        c.rollback()   # discard C's uncommitted snapshot rows
        c.close()


# ── TEST 1b — the same interleave with the ORDER reversed proves symmetry (post-B seen whole) ───────
def test_a_request_starting_after_the_commit_sees_the_post_state_whole(_dsn, cleanup_seed) -> None:
    """The mirror of TEST 1: a feature request whose RR snapshot is pinned AFTER the ingestion commit
    observes the POST-B value for ALL refs (never a mix that still shows a stale column). Together
    with TEST 1 this brackets the invariant: the snapshot is one of the two whole states, never torn."""
    setup = psycopg.connect(_dsn)
    try:
        _rr(setup)
        _seed(setup)
        setup.commit()
    finally:
        setup.close()

    # Commit the ingestion change up front.
    b = psycopg.connect(_dsn)
    try:
        _commit_change(b)
    finally:
        b.close()

    a = psycopg.connect(_dsn)
    try:
        _rr(a)
        ctx = build_metadata_snapshot(
            a, generation_run_id="fgr_conc_post", refs=_REFS, read_scope_hash="sha256:scope")
        assert ctx.facts(_SRC, _BAL, "additivity").value == _POST[_BAL]
        assert ctx.facts(_SRC, _FEE, "additivity").value == _POST[_FEE]
    finally:
        a.rollback()
        a.close()
