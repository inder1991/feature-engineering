"""Composition audit finding [9] — every human confirm surface's synchronous drain-then-project
(``project_verified_join`` / ``project_verified_table_fact`` / ``project_verified_semantic_binding``)
must DEFER to its fail-closed projection-lag path instead of BLOCKING when a concurrent ingest holds
the 'overlay' ``projection_checkpoints`` row (held to ingest commit across the D4/Pass-B LLM stages).

Each surface takes the checkpoint row ``FOR UPDATE NOWAIT`` first: on lock-unavailable it returns
"pending" (fact stays VERIFIED; the next caught-up ingest reproject makes it operational) and
increments a dedicated ``…projection_skipped_lock`` counter — the discriminator proving the deferral
took the NEW lock path, not any other pending reason. The 'overlay' checkpoint row is pre-seeded
committed by migration 0507."""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.facts import ENTITY_ASSIGNMENT
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair
from featuregen.overlay.upload.join_governance import project_verified_join
from featuregen.overlay.upload.semantic_bindings.projection import (
    project_verified_semantic_binding,
)
from featuregen.overlay.upload.table_fact_governance import project_verified_table_fact
from featuregen.runtime.observability import counters


@pytest.fixture
def lock_holder(_dsn):
    """A SECOND session that HOLDS the committed 'overlay' checkpoint row lock across an OPEN
    transaction — exactly what an in-flight ingest's in-tx _drain_projection does (holds the row to
    commit). Rolled back + closed on teardown so the lock never lingers."""
    c = psycopg.connect(_dsn)
    c.execute("SELECT 1 FROM projection_checkpoints WHERE projection_name = 'overlay' FOR UPDATE")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _count(name: str) -> int:
    return counters.snapshot()["counters"].get(name, 0)


def test_join_confirm_defers_to_lock_path_when_ingest_holds_the_checkpoint(db, lock_holder):
    ref = ApprovedJoinRef(
        from_ref=CatalogObjectRef("src", "column", "public", "accounts", "customer_id"),
        to_ref=CatalogObjectRef("src", "column", "public", "customers", "customer_id"),
        column_pairs=(ColumnPair("customer_id", "customer_id"),), cardinality="N:1")
    before = _count("overlay.join_governance.projection_skipped_lock")
    # Must NOT block (finishing proves it — the pre-fix drain's plain FOR UPDATE would hang here).
    assert project_verified_join(db, "src", ref, now=None) == "pending"
    assert _count("overlay.join_governance.projection_skipped_lock") == before + 1
    db.rollback()


def test_table_fact_confirm_defers_to_lock_path_when_ingest_holds_the_checkpoint(db, lock_holder):
    ref = CatalogObjectRef("src", "table", "public", "accounts")
    before = _count("overlay.table_fact_governance.projection_skipped_lock")
    assert project_verified_table_fact(db, "src", ref, "grain", now=None) == "pending"
    assert _count("overlay.table_fact_governance.projection_skipped_lock") == before + 1
    db.rollback()


def test_semantic_binding_confirm_defers_to_lock_path_when_ingest_holds_the_checkpoint(
        db, lock_holder):
    ref = CatalogObjectRef("src", "column", "public", "accounts", "customer_id")
    before = _count("overlay.semantic_binding.projection_skipped_lock")
    assert project_verified_semantic_binding(db, "src", ref, ENTITY_ASSIGNMENT, now=None) == "pending"
    assert _count("overlay.semantic_binding.projection_skipped_lock") == before + 1
    db.rollback()


def test_semantic_binding_list_defers_drain_when_ingest_holds_the_checkpoint(db, lock_holder):
    """Finding [9], gap (b): the E2 LIST view (``list_semantic_binding_proposals`` — a live
    governance GET) drains OverlayProjection on the request connection too. Pre-fix its drain took
    the checkpoint row with a plain FOR UPDATE — a BLOCK, not an exception, so the best-effort
    try/except never fired and the read hung behind the whole in-flight ingest tx. It must probe the
    lock NOWAIT and fall through to the documented possibly-stale read (its existing drain-fault
    semantics). ``lock_timeout`` bounds the pre-fix block so red FAILS (via the drain_error path)
    rather than hanging; the fixed path never waits on the lock at all."""
    from featuregen.overlay.upload.semantic_binding_governance import (
        list_semantic_binding_proposals,
    )

    db.execute("SET LOCAL lock_timeout = '2s'")
    skip_before = _count("overlay.semantic_binding_governance.drain_skipped_lock")
    err_before = _count("overlay.semantic_binding_governance.drain_error")
    assert list_semantic_binding_proposals(db, "src", roles=["platform-admin"]) == []
    assert _count("overlay.semantic_binding_governance.drain_skipped_lock") == skip_before + 1
    assert _count("overlay.semantic_binding_governance.drain_error") == err_before  # NOT the error path
    db.rollback()
