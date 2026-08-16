"""C-D6 — environment scoping, as ONE coordinated change.

The gate is *"the target schema and backfill are written"*. What matters more is the failure the
coordination prevents, and it is silent rather than loud: with an environment-aware trigger and an
environment-blind `next_revision_seq`, environment B's first publication computes `max(seq)+1`
ACROSS both environments — a value that DOES strictly extend B's empty sequence, so the trigger
passes and B's history starts at an arbitrary seq. The trigger is precisely the mechanism that would
otherwise have made the wrong seq visible.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from featuregen.materialize.publish import (
    ActiveRevision,
    PublishMechanism,
    next_revision_seq,
    read_active_revision,
    record_active_revision,
)

MIGRATION = Path("src/featuregen/db/migrations/1085_environment_scoped_publication.sql")
GROUP = "account_daily"


def _revision(seq: int, *, environment_id: str | None, revision_id: str) -> ActiveRevision:
    return ActiveRevision(
        revision_id=revision_id, logical_group_name=GROUP, generation_id="gen-1", run_id="run-1",
        published_object="sandbox_feature.account_daily",
        capability_attestation_id="att-1", mechanism=PublishMechanism.VERSIONED_POINTER, seq=seq,
        activated_at="2026-08-16T00:00:00Z", environment_id=environment_id)


# ══ THE BUG THE COORDINATION PREVENTS ════════════════════════════════════════════════════════════
def test_A_SECOND_ENVIRONMENTS_FIRST_PUBLICATION_STARTS_AT_ZERO(db, monkeypatch):
    """The silent failure. Environment A has published three times; B has never published. A blind
    `max(seq)` would hand B seq 3 — which strictly extends B's empty sequence, so the trigger would
    pass and B's history would start at an arbitrary number."""
    _seed_generation(db)
    for seq in range(3):
        record_active_revision(db, _revision(seq, environment_id="env-a",
                                             revision_id=f"rev-a-{seq}"))

    assert next_revision_seq(db, GROUP, environment_id="env-a") == 3
    assert next_revision_seq(db, GROUP, environment_id="env-b") == 0, (
        "environment B has never published; its next seq is 0, not A's 3")


def test_ENVIRONMENT_B_READS_ITS_OWN_POINTER_NOT_AS(db):
    """The other half: a blind read hands B a row belonging to A."""
    _seed_generation(db)
    record_active_revision(db, _revision(0, environment_id="env-a", revision_id="rev-a"))

    assert read_active_revision(db, GROUP, environment_id="env-a").revision_id == "rev-a"
    assert read_active_revision(db, GROUP, environment_id="env-b") is None, (
        "B has published nothing; 'not published yet' is the truthful answer")


def test_the_trigger_scopes_ordering_PER_ENVIRONMENT(db):
    """Two environments may each hold seq 0 — they are different sequences."""
    _seed_generation(db)
    record_active_revision(db, _revision(0, environment_id="env-a", revision_id="rev-a0"))
    record_active_revision(db, _revision(0, environment_id="env-b", revision_id="rev-b0"))

    assert read_active_revision(db, GROUP, environment_id="env-a").revision_id == "rev-a0"
    assert read_active_revision(db, GROUP, environment_id="env-b").revision_id == "rev-b0"


def test_the_trigger_STILL_REFUSES_a_non_extending_seq_within_one_environment(db):
    import psycopg

    _seed_generation(db)
    record_active_revision(db, _revision(0, environment_id="env-a", revision_id="rev-a0"))
    with pytest.raises(psycopg.errors.RaiseException, match="does not extend group"):
        record_active_revision(db, _revision(0, environment_id="env-a", revision_id="rev-a0-dup"))


# ══ legacy rows keep exactly what they were written under ════════════════════════════════════════
def test_LEGACY_ROWS_ARE_A_SCOPE_OF_THEIR_OWN(db):
    """`None` is the legacy scope, and `IS NOT DISTINCT FROM` is what reaches it — `NULL = NULL` is
    NULL, so `=` would match no legacy row at all."""
    _seed_generation(db)
    record_active_revision(db, _revision(0, environment_id=None, revision_id="rev-legacy"))

    assert read_active_revision(db, GROUP, environment_id=None).revision_id == "rev-legacy"
    assert read_active_revision(db, GROUP, environment_id="env-a") is None
    assert next_revision_seq(db, GROUP, environment_id=None) == 1
    assert next_revision_seq(db, GROUP, environment_id="env-a") == 0


def test_a_legacy_row_and_a_scoped_row_do_not_collide(db):
    _seed_generation(db)
    record_active_revision(db, _revision(0, environment_id=None, revision_id="rev-legacy"))
    record_active_revision(db, _revision(0, environment_id="env-a", revision_id="rev-a0"))
    assert read_active_revision(db, GROUP, environment_id=None).revision_id == "rev-legacy"


# ══ the parameter is REQUIRED, never defaulted ═══════════════════════════════════════════════════
@pytest.mark.parametrize("fn", [read_active_revision, next_revision_seq])
def test_environment_id_HAS_NO_DEFAULT(fn):
    """A default would make the blind read the easy one to write, and the blind read IS the defect."""
    parameter = inspect.signature(fn).parameters["environment_id"]
    assert parameter.default is inspect.Parameter.empty, fn.__name__
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_revision_CARRIES_its_environment():
    """Beyond two columns: the write side records it, or every read is scoped against nothing."""
    import dataclasses

    assert "environment_id" in {f.name for f in dataclasses.fields(ActiveRevision)}


# ══ the migration file — written, and shaped as the house does it ════════════════════════════════
def test_the_migration_is_WRITTEN():
    assert MIGRATION.exists()


def test_it_adds_a_NULLABLE_column_with_NO_backfill():
    """House precedent (1069, 1070): legacy rows keep a permanent, truthful NULL. A NOT NULL DEFAULT
    would assert every existing publication happened in an environment nobody recorded — and both
    target tables carry append-only triggers a backfill UPDATE would have to fight."""
    sql = MIGRATION.read_text()
    assert "ADD COLUMN IF NOT EXISTS environment_id   text" in sql
    # comments STRIPPED: the file's prose explains why `NOT NULL DEFAULT` was rejected, and a
    # whole-file grep would read that explanation as the thing it warns against.
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "NOT NULL DEFAULT" not in statements
    assert "UPDATE group_binding" not in statements
    assert "UPDATE feature_active_revision" not in statements


def test_it_uses_PARTIAL_indexes_so_legacy_uniqueness_is_not_lost():
    """`UNIQUE (environment_id, logical_group_name)` over a nullable column treats every legacy NULL
    as distinct, silently permitting duplicates the dropped constraint forbade."""
    sql = MIGRATION.read_text()
    assert "WHERE environment_id IS NOT NULL" in sql
    assert "WHERE environment_id IS NULL" in sql
    assert "group_binding_legacy_name" in sql
    assert "feature_active_revision_legacy_seq" in sql


def test_it_drops_the_UNNAMED_inline_constraint_by_its_generated_name():
    """1034:88 declares an inline `UNIQUE`, which PostgreSQL auto-names — there is no name in the
    source to drop, so the generated one is what the swap must use."""
    sql = MIGRATION.read_text()
    assert "DROP CONSTRAINT IF EXISTS group_binding_logical_group_name_key" in sql


def test_the_trigger_uses_IS_NOT_DISTINCT_FROM():
    sql = MIGRATION.read_text()
    assert "environment_id IS NOT DISTINCT FROM NEW.environment_id" in sql
    assert "environment_id = NEW.environment_id" not in sql


def test_THE_V1_V2_NAMESPACE_IS_RECONCILED_EXPLICITLY():
    """The plan asks for this and notes environment_id alone does not solve it. Resolved in favour
    of ONE flat namespace per environment: two groups named the same in one environment publish to
    one physical table whatever language authored them, so a language discriminator in the KEY would
    permit a real collision. The language is recorded for audit."""
    sql = MIGRATION.read_text()
    assert "formula_language" in sql
    assert "CHECK (formula_language IN ('v1', 'v2'))" in sql
    # ...and it is NOT part of either uniqueness rule
    assert "(environment_id, formula_language" not in sql
    assert "formula_language, logical_group_name" not in sql


def test_the_migration_does_NOT_touch_the_group_NAMESPACE():
    """Environment is deployment placement, not feature meaning — and `physical_target_for` /
    `derive_namespace` feed the sealed bytes, so mangling it into the name would move
    `generated_project_hash` and invalidate every manifest and execution proof."""
    sql = MIGRATION.read_text()
    for hash_bearing in ("physical_target", "derive_namespace", "SANDBOX_NAMESPACE"):
        assert f"ALTER TABLE group_binding\n    ALTER COLUMN {hash_bearing}" not in sql
    assert "UPDATE group_binding SET physical_target" not in sql


def _seed_generation(db) -> None:
    """`feature_active_revision.generation_id` REFERENCES `materialization_generation`."""
    db.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        ("gen-1", GROUP, "sha256:c", "sha256:p", "sha256:g", "2026-08-16T00:00:00Z"))
