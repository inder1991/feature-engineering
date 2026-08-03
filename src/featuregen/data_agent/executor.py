"""Direct SQL executor — runs one observation plan and returns the shared typed result.

The simplest executor there is: one bounded statement over a read-only connection, aggregates back.
It is deliberately the *first* one (roadmap §3c) because a handful of `COUNT`/`MIN`/`MAX` aggregates
does not justify generating and shipping a Spark project.

**It enforces nothing.** Partition pruning, caps, identifier safety and the exact/approximate choice
are all decided when the plan is constructed, so a later Spark executor cannot forget them by
skipping this file. What lives here is transport: apply the timeout, run the statement, shape the
answer, and turn a failure into typed coverage rather than an exception.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from featuregen.data_agent.observation import ObservationPlanV1, SchemaObservationPlanV1
from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationPlanV2,
    RelationshipObservationV2,
    failed_relationship_observation,
    utc_now,
)
from featuregen.data_agent.results import (
    ColumnObservationV1,
    ColumnTypeObservationV1,
    DataObservationResultV1,
    SchemaObservationResultV1,
)

logger = logging.getLogger(__name__)


class Dialect(Protocol):
    """What an engine must render. Adding an engine means adding one of these, not a second plan."""

    name: str

    def render_column_profile(self, plan: ObservationPlanV1) -> str: ...

    def render_relationship_probe(self, plan: RelationshipObservationPlanV2) -> str: ...

    def render_schema_observation(self, plan: SchemaObservationPlanV1) -> str:
        """The engine's schema read — `DESCRIBE` on Hive, information_schema on PostgreSQL. A
        metadata statement, never a scan: it is the ONLY statement `graph_node.data_type` may be
        upgraded from (a glossary's declared type is not evidence of the physical schema)."""
        ...

    def timeout_statements(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2 | SchemaObservationPlanV1
    ) -> tuple[str, ...]:
        """Statements bounding one query's runtime. Engine-specific: PostgreSQL is transaction
        milliseconds, Hive is session seconds. Empty for an engine with no reliable bound."""
        ...

    def effective_method(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2
    ) -> str:
        """What this engine ACTUALLY does — `exact` or `approximate`. Not what the plan requested:
        an engine without an approximate function silently runs a census, and the result must say
        so."""
        ...


class DirectSqlExecutor:
    """Run an observation over an existing read-only connection.

    The connection is BORROWED and deliberately DB-API-2.0-generic (only ``cursor()`` is assumed,
    so PyHive and impyla work). On psycopg the relationship probe additionally runs inside a
    savepoint (``Connection.transaction()`` emits a SAVEPOINT when nested), so a failed probe
    never aborts the caller's transaction — the failure observation this executor returns must
    remain storable through the same connection, and an aborted transaction would reject the
    store's INSERT and bury the evidence. On DB-API-only engines (Hive) there is no enclosing
    Postgres transaction to protect, so behavior there is unchanged.
    """

    def __init__(self, conn, dialect: Dialect) -> None:
        self._conn = conn
        self._dialect = dialect

    def observe(self, plan: ObservationPlanV1) -> DataObservationResultV1:
        """Execute one plan. Never raises for a data-side failure.

        A partial observation must report exact coverage and never masquerade as complete: raising
        would discard the columns that did succeed and, worse, invite a caller to retry the whole
        scan. So a failure becomes `complete=False` plus a redacted reason.
        """
        physical_id = plan.binding.identity.table_id
        partitions = tuple(plan.partitions.values) if plan.partitions else ()
        statement = self._dialect.render_column_profile(plan)

        try:
            row = self._run(plan, statement)
        except Exception as exc:                      # noqa: BLE001 — any engine error is coverage
            # The message is the engine's and may name a column, which is metadata rather than data.
            # It must never carry a VALUE, so nothing from a result row is included.
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            logger.info("observation failed for %s: %s", physical_id, reason)
            return DataObservationResultV1(
                physical_id=physical_id, row_count=0, columns=(), partitions_read=partitions,
                method=self._method(plan), complete=False, failures=(reason,))

        return self._shape(plan, row, physical_id, partitions)

    def observe_relationship(
        self,
        plan: RelationshipObservationPlanV2,
        *,
        observed_at: datetime | None = None,
    ) -> RelationshipObservationV2:
        """Execute one tuple-aware pair probe through the same cursor, timeout and method seam."""
        timestamp = observed_at or utc_now()
        statement = self._dialect.render_relationship_probe(plan)
        method = self._method(plan)
        transaction = getattr(self._conn, "transaction", None)
        try:
            if callable(transaction):
                # psycopg3: a nested transaction block is a SAVEPOINT. A failure rolls back to
                # it AND re-raises, so the except below still shapes the failure observation
                # while the caller's transaction stays usable to store it.
                with self._conn.transaction():
                    row = self._run(plan, statement)
            else:
                row = self._run(plan, statement)
        except Exception as exc:  # noqa: BLE001 — data-side failures are typed coverage
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            logger.info(
                "relationship observation failed for %s -> %s: %s",
                plan.left_binding.identity.table_id,
                plan.right_binding.identity.table_id,
                reason,
            )
            return failed_relationship_observation(
                plan, method=method, observed_at=timestamp, reason=reason)
        return self._shape_relationship(
            plan, row, method=method, observed_at=timestamp)

    def observe_schema(self, plan: SchemaObservationPlanV1) -> SchemaObservationResultV1:
        """Execute one schema read. Never raises for a data-side failure.

        Same transport and timeout seam as :meth:`observe`; no method resolution, because a schema
        read is a metadata census — exact by construction — so `effective_method` is untouched. A
        failure is typed coverage: `complete=False` matters more here than for a profile, since a
        consumer that read a failed result as "these columns are all the table has" would record
        absences that are actually engine errors.
        """
        physical_id = plan.binding.identity.table_id
        statement = self._dialect.render_schema_observation(plan)
        transaction = getattr(self._conn, "transaction", None)
        try:
            if callable(transaction):
                # SAVEPOINT, exactly like observe_relationship: on psycopg a failed read otherwise
                # aborts the CALLER's transaction and the typed-coverage result below would be
                # unstorable. Plain DBAPI connections (Hive) have no transaction(); same fallback.
                with self._conn.transaction():
                    rows = self._run(plan, statement, fetch_all=True)
            else:
                rows = self._run(plan, statement, fetch_all=True)
        except Exception as exc:                      # noqa: BLE001 — any engine error is coverage
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            logger.info("schema observation failed for %s: %s", physical_id, reason)
            return SchemaObservationResultV1(
                physical_id=physical_id, columns=(), complete=False, failures=(reason,))
        return self._shape_schema(rows, physical_id)

    # ── internals ────────────────────────────────────────────────────────────────────────────────

    def _method(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2
    ) -> str:
        """Prefer the dialect's answer; fall back to the plan for a dialect that predates the
        capability (the PostgresDialect's own fallback is documented in its docstring)."""
        effective = getattr(self._dialect, "effective_method", None)
        if callable(effective):
            return effective(plan)
        return (
            plan.method
            if isinstance(plan, ObservationPlanV1)
            else plan.requested_method
        )

    def _run(
        self,
        plan: ObservationPlanV1 | RelationshipObservationPlanV2 | SchemaObservationPlanV1,
        statement: str,
        *,
        fetch_all: bool = False,
    ):
        """Execute through a CURSOR — the DB-API 2.0 contract every driver implements.

        An earlier version called `conn.execute(...)`, which is a psycopg3 convenience the standard
        does not define, so a PyHive or impyla connection would have failed with AttributeError
        before running anything. The connection is BORROWED, so the cursor is closed and the
        connection is not. `fetch_all` is for the statements whose ANSWER is rows (a schema read)
        rather than one aggregate row.
        """
        cursor = self._conn.cursor()
        try:
            for bound in self._timeouts(plan):
                cursor.execute(bound)
            cursor.execute(statement)
            return cursor.fetchall() if fetch_all else cursor.fetchone()
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _timeouts(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2 | SchemaObservationPlanV1
    ) -> tuple[str, ...]:
        statements = getattr(self._dialect, "timeout_statements", None)
        return tuple(statements(plan)) if callable(statements) else ()

    def _shape_schema(self, rows, physical_id: str) -> SchemaObservationResultV1:
        """Positional again — `(name, type, ...)` is the first-two-columns shape of both engines'
        schema statements, and reading by position keeps the executor engine-agnostic.

        Hive's plain `DESCRIBE` on a partitioned table appends a marker section — a blank
        separator row, `# Partition Information`, `# col_name` — and then REPEATS the partition
        columns. Marker rows (blank or `#`-prefixed name) are not columns, and a repeated name is
        one physical column, so the first occurrence wins. Harmless for information_schema, which
        emits neither."""
        columns: list[ColumnTypeObservationV1] = []
        seen: set[str] = set()
        for row in rows or ():
            name = ("" if row[0] is None else str(row[0])).strip()
            if not name or name.startswith("#"):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            engine_type = ("" if len(row) < 2 or row[1] is None else str(row[1])).strip()
            columns.append(ColumnTypeObservationV1(column=name, engine_type=engine_type))
        return SchemaObservationResultV1(
            physical_id=physical_id, columns=tuple(columns), complete=True, failures=())

    def _shape(self, plan: ObservationPlanV1, row, physical_id: str,
               partitions: tuple[str, ...]) -> DataObservationResultV1:
        """Positional, because the dialect built the projection in this exact order. Reading by
        position keeps the executor engine-agnostic — column LABELS differ between engines in ways
        the plan should not have to know about."""
        values = list(row)
        row_count = int(values[0] or 0)
        cursor = 1
        bounds = {c.strip().lower() for c in plan.bounds_columns}
        columns: list[ColumnObservationV1] = []
        for column in plan.columns:
            non_null = int(values[cursor] or 0)
            cursor += 1
            distinct = int(values[cursor] or 0)
            cursor += 1
            minimum = maximum = None
            if column.strip().lower() in bounds:
                minimum = None if values[cursor] is None else str(values[cursor])
                cursor += 1
                maximum = None if values[cursor] is None else str(values[cursor])
                cursor += 1
            columns.append(ColumnObservationV1(
                column=column, non_null_count=non_null, distinct_count=distinct,
                minimum=minimum, maximum=maximum, observed_rows=row_count))

        return DataObservationResultV1(
            physical_id=physical_id, row_count=row_count, columns=tuple(columns),
            partitions_read=partitions, method=self._method(plan), complete=True,
            failures=())

    def _shape_relationship(
        self,
        plan: RelationshipObservationPlanV2,
        row,
        *,
        method: str,
        observed_at: datetime,
    ) -> RelationshipObservationV2:
        values = [int(value or 0) for value in row]
        if len(values) != 18:
            raise ValueError(
                f"relationship probe returned {len(values)} metrics, expected 18")
        (
            left_rows,
            left_non_null,
            left_distinct,
            left_duplicate_tuples,
            left_duplicates,
            left_max,
            right_rows,
            right_non_null,
            right_distinct,
            right_duplicate_tuples,
            right_duplicates,
            right_max,
            matched,
            left_orphan_rows,
            right_orphan_rows,
            joined_rows,
            max_right_matches,
            max_left_matches,
        ) = values

        def endpoint(binding, columns, selector, metrics):
            rows, non_null, distinct, duplicate_tuples, duplicates, maximum = metrics
            return EndpointTupleObservationV2(
                physical_id=binding.identity.table_id,
                binding_revision_id=binding.binding_revision_id,
                binding_content_hash=binding.content_hash,
                columns=columns,
                row_count=rows,
                non_null_row_count=non_null,
                distinct_tuple_count=distinct,
                duplicate_tuple_count=duplicate_tuples,
                duplicate_row_count=duplicates,
                max_rows_per_tuple=maximum,
                partitions_read=tuple(selector.values) if selector else (),
            )

        left = endpoint(
            plan.left_binding,
            tuple(pair.left_column for pair in plan.column_pairs),
            plan.scope.left_partitions,
            (
                left_rows,
                left_non_null,
                left_distinct,
                left_duplicate_tuples,
                left_duplicates,
                left_max,
            ),
        )
        right = endpoint(
            plan.right_binding,
            tuple(pair.right_column for pair in plan.column_pairs),
            plan.scope.right_partitions,
            (
                right_rows,
                right_non_null,
                right_distinct,
                right_duplicate_tuples,
                right_duplicates,
                right_max,
            ),
        )
        return RelationshipObservationV2(
            realization_revision_id=plan.realization_revision_id,
            plan_hash=plan.plan_hash,
            scope_id=plan.scope.scope_id,
            left=left,
            right=right,
            matched_left_distinct=matched,
            unmatched_left_distinct=max(0, left_distinct - matched),
            matched_right_distinct=matched,
            unmatched_right_distinct=max(0, right_distinct - matched),
            left_orphan_rows=left_orphan_rows,
            right_orphan_rows=right_orphan_rows,
            joined_row_count=joined_rows,
            max_right_matches_per_left_row=max_right_matches,
            max_left_matches_per_right_row=max_left_matches,
            normalization_ids=plan.scope.normalization_ids,
            predicate_ids=tuple(
                predicate.predicate_id for predicate in plan.scope.predicates),
            left_source_snapshot_id=plan.left_source_snapshot_id,
            right_source_snapshot_id=plan.right_source_snapshot_id,
            snapshot_or_as_of=plan.scope.snapshot_or_as_of,
            execution_principal=plan.scope.execution_principal,
            method=method,
            row_coverage=plan.scope.row_coverage,
            complete=True,
            observed_at=observed_at,
        )
