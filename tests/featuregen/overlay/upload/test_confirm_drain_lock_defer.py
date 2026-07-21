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
