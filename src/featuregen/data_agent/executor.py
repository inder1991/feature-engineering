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
from typing import Protocol

from featuregen.data_agent.observation import ObservationPlanV1
from featuregen.data_agent.results import ColumnObservationV1, DataObservationResultV1

logger = logging.getLogger(__name__)


class Dialect(Protocol):
    """What an engine must render. Adding an engine means adding one of these, not a second plan."""

    name: str

    def render_column_profile(self, plan: ObservationPlanV1) -> str: ...

    def timeout_statements(self, plan: ObservationPlanV1) -> tuple[str, ...]:
        """Statements bounding one query's runtime. Engine-specific: PostgreSQL is transaction
        milliseconds, Hive is session seconds. Empty for an engine with no reliable bound."""
        ...

    def effective_method(self, plan: ObservationPlanV1) -> str:
        """What this engine ACTUALLY does — `exact` or `approximate`. Not what the plan requested:
        an engine without an approximate function silently runs a census, and the result must say
        so."""
        ...


class DirectSqlExecutor:
    """Run an observation over an existing read-only connection."""

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

    # ── internals ────────────────────────────────────────────────────────────────────────────────

    def _method(self, plan: ObservationPlanV1) -> str:
        """Prefer the dialect's answer; fall back to the plan for a dialect that predates the
        capability (the PostgresDialect's own fallback is documented in its docstring)."""
        effective = getattr(self._dialect, "effective_method", None)
        return effective(plan) if callable(effective) else plan.method

    def _run(self, plan: ObservationPlanV1, statement: str):
        """Execute through a CURSOR — the DB-API 2.0 contract every driver implements.

        An earlier version called `conn.execute(...)`, which is a psycopg3 convenience the standard
        does not define, so a PyHive or impyla connection would have failed with AttributeError
        before running anything. The connection is BORROWED, so the cursor is closed and the
        connection is not.
        """
        cursor = self._conn.cursor()
        try:
            for bound in self._timeouts(plan):
                cursor.execute(bound)
            cursor.execute(statement)
            return cursor.fetchone()
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _timeouts(self, plan: ObservationPlanV1) -> tuple[str, ...]:
        statements = getattr(self._dialect, "timeout_statements", None)
        return tuple(statements(plan)) if callable(statements) else ()

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
            non_null = int(values[cursor] or 0); cursor += 1
            distinct = int(values[cursor] or 0); cursor += 1
            minimum = maximum = None
            if column.strip().lower() in bounds:
                minimum = None if values[cursor] is None else str(values[cursor]); cursor += 1
                maximum = None if values[cursor] is None else str(values[cursor]); cursor += 1
            columns.append(ColumnObservationV1(
                column=column, non_null_count=non_null, distinct_count=distinct,
                minimum=minimum, maximum=maximum, observed_rows=row_count))

        return DataObservationResultV1(
            physical_id=physical_id, row_count=row_count, columns=tuple(columns),
            partitions_read=partitions, method=self._method(plan), complete=True,
            failures=())
