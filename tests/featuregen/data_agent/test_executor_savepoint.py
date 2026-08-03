"""A failed probe's evidence must survive the aborted statement — Task 18.

`DirectSqlExecutor.observe_relationship` turns a probe failure into a typed failure observation,
but the failure observation only matters if it can be PERSISTED. On a psycopg connection the
failed statement aborts the caller's transaction, so the store's very next INSERT raised
`InFailedSqlTransaction` and the evidence was lost — the observation object existed, its whole
purpose did not.

The fix is an opportunistic savepoint: when the borrowed connection offers psycopg's
`Connection.transaction()` the probe runs inside it (psycopg emits a SAVEPOINT when nested), so a
failure rolls back to the savepoint and the outer transaction stays usable. A DB-API-only engine
(PyHive, impyla) has no such method and no enclosing Postgres transaction to protect —
`test_driver_seam.py` pins that path unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.data_agent.test_relationship_observation_store import _seed_realization

from featuregen.data_agent.executor import DirectSqlExecutor
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship_observation import (
    RelationshipColumnPairV2,
    RelationshipMethod,
    RelationshipObservationPlanV2,
    RelationshipObservationScopeV1,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.data_agent.store import record_relationship_observation

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _plan_probing_a_missing_table(db) -> RelationshipObservationPlanV2:
    """A fully-governed plan whose physical tables do not exist in the test database.

    The seeded realization's bindings point at `banking.customers` / `banking.transactions`,
    which no migration creates — so the probe itself fails (UndefinedTable) while every
    store-side foreign key the failure observation needs is satisfied.
    """
    revision, left_binding, right_binding = _seed_realization(db)
    pairs = (RelationshipColumnPairV2("customer_id", "customer_id"),)
    scope = RelationshipObservationScopeV1(
        scope_id=revision.applicability_scope.scope_id,
        left_binding_revision_id=left_binding.binding_revision_id,
        right_binding_revision_id=right_binding.binding_revision_id,
        left_partitions=None,
        right_partitions=None,
        normalization_ids=tuple(pair.normalization_id for pair in pairs),
        snapshot_or_as_of="2026-07-30",
        execution_principal="profile-service",
        method=RelationshipMethod.EXACT,
    )
    return RelationshipObservationPlanV2(
        left_binding=left_binding,
        right_binding=right_binding,
        column_pairs=pairs,
        scope=scope,
        policy=ProfilePolicyV1(exact_distinct=True),
        realization_revision_id=revision.realization_revision_id,
        left_source_snapshot_id="left-snapshot-1",
        right_source_snapshot_id="right-snapshot-1",
        shortlist_position=1,
        shortlist_size=1,
    )


def test_a_failed_probe_leaves_the_transaction_usable_for_the_failure_record(db) -> None:
    # The whole point of the failure observation is to be PERSISTED; an aborted
    # tx makes it unstorable and buries the cause (report §4).
    plan = _plan_probing_a_missing_table(db)
    executor = DirectSqlExecutor(db, PostgresDialect())
    observation = executor.observe_relationship(plan, observed_at=NOW)
    assert observation.complete is False
    assert observation.failures
    db.execute("SELECT 1")            # old behavior: InFailedSqlTransaction


def test_the_failure_observation_is_storable_in_the_same_transaction(db) -> None:
    """The actual point of the fix: the evidence reaches the store, not just the caller."""
    plan = _plan_probing_a_missing_table(db)
    observation = DirectSqlExecutor(db, PostgresDialect()).observe_relationship(
        plan, observed_at=NOW)
    assert observation.complete is False

    record_relationship_observation(db, observation, expected_pointer_version=0)

    assert db.execute(
        "SELECT complete, row_coverage FROM relationship_observation_revision "
        "WHERE observation_revision_id=%s",
        (observation.observation_revision_id,),
    ).fetchone() == (False, "partial")


def test_a_successful_probe_still_behaves_identically_under_the_savepoint(db) -> None:
    """The savepoint must be invisible on success: same numbers, transaction still usable."""
    db.execute("CREATE SCHEMA IF NOT EXISTS banking")
    db.execute("CREATE TABLE banking.customers (customer_id text)")
    db.execute("CREATE TABLE banking.transactions (customer_id text)")
    for value in ("A", "B"):
        db.execute("INSERT INTO banking.customers VALUES (%s)", (value,))
        db.execute("INSERT INTO banking.transactions VALUES (%s)", (value,))
    plan = _plan_probing_a_missing_table(db)  # the tables exist now, so it is not missing
    observation = DirectSqlExecutor(db, PostgresDialect()).observe_relationship(
        plan, observed_at=NOW)
    assert observation.complete is True
    assert observation.left.row_count == 2
    assert observation.right.row_count == 2
    db.execute("SELECT 1")
