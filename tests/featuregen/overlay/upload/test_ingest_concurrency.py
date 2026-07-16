"""#3 — same-source ingestion has NO concurrency serialization.

``build_graph`` is DELETE-this-source-then-reinsert, so two concurrent uploads of the SAME
``catalog_source`` used to clobber each other's graph last-writer-wins (and the drift snapshot
could diverge from the graph). The fix: ``ingest_upload`` takes a transaction-scoped,
SOURCE-scoped advisory lock ONCE at its very top, on the request connection — same-source
ingests serialize, different sources never block each other.

HERMETIC by design (no racing threads): the ingest is run on the test connection, whose
transaction stays open, and contention is proven with a ``pg_try_advisory_xact_lock`` probe
from a SECOND session — deterministic, and it pins the exact key derivation.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_source_lock_key, ingest_upload


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


@pytest.fixture
def probe_conn(_dsn):
    """A SECOND session on the same database. Autocommit: each ``pg_try_advisory_xact_lock``
    probe is its own single-statement transaction, so a successful probe releases immediately
    and can never linger to poison a later assertion (or another test)."""
    with psycopg.connect(_dsn, autocommit=True) as c:
        yield c


def _try_lock(probe, key: int) -> bool:
    return probe.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]


def test_lock_key_is_stable_namespaced_sha256_of_source():
    # The EXACT derivation (must stay stable across releases — two versions hashing differently
    # would stop excluding each other during a rolling deploy): sha256 over a dedicated
    # 'overlay_ingest:' namespace, first 8 bytes, big-endian, signed — the worker.py convention.
    expected = int.from_bytes(
        hashlib.sha256(b"overlay_ingest:deposits").digest()[:8], "big", signed=True)
    assert ingest_source_lock_key("deposits") == expected
    # Source-scoped: a different source is a different key (never contends).
    assert ingest_source_lock_key("loans") != ingest_source_lock_key("deposits")
    # No collision with ANY other advisory-lock key space in the codebase (deadlock /
    # false-contention safety): the three fixed constants + the worker's sha256 namespaces.
    others = {
        7_000_007,                # security-audit chain (security/audit.py)
        6157423001,               # migration deploy serialization (db/migrations.py)
        4_201_873_355_201_001,    # global_seq allocation (events/store.py)
        int.from_bytes(hashlib.sha256(b"overlay_renewal").digest()[:8], "big", signed=True),
        int.from_bytes(
            hashlib.sha256(b"overlay_drift:deposits").digest()[:8], "big", signed=True),
    }
    assert ingest_source_lock_key("deposits") not in others


def test_ingest_holds_source_lock_until_tx_end(db, probe_conn):
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("deposits", "accounts", "balance", "numeric")]

    # Regression: a normal single ingest still works end-to-end under the lock.
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=now)
    assert res.status == "ingested"
    assert db.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'deposits'"
                      ).fetchone()[0] > 0

    # The ingest transaction is still open (test conn commits nothing), so the source lock is
    # HELD: a second session's try-lock on the SAME key must report contended. This is the
    # serialization proof — a concurrent same-source ingest would block right here, before its
    # brake/snapshot/facts/graph, instead of racing build_graph's DELETE+reinsert.
    assert _try_lock(probe_conn, ingest_source_lock_key("deposits")) is False
    # A DIFFERENT source hashes to a different key: never blocked by this ingest.
    assert _try_lock(probe_conn, ingest_source_lock_key("loans")) is True

    # Transaction-scoped: ending the request tx releases the lock with nothing to clean up.
    db.rollback()
    assert _try_lock(probe_conn, ingest_source_lock_key("deposits")) is True
