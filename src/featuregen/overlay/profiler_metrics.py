"""Metric-collection mechanics for the overlay profiler (SP-1).

The SQL/measurement layer behind the profiler: sampling math, identifier-safe SQL construction,
statement-timeout application, the per-column / per-combination metric queries, and evidence-metric
assembly. These collectors carry no proposal/inference policy — `profiler_heuristics` calls them and
`profiler` orchestrates them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from featuregen.contracts import DbConn
    from featuregen.overlay.identity import CatalogObjectRef
    from featuregen.overlay.profiler import ProfilerLimits

PROFILE_VERSION = "overlay-profiler@1"

# Aggregate-only, derived (bucketed) profiling never reads raw values; timestamp candidacy is by type.
_TIMESTAMP_TYPES = frozenset(
    {
        "timestamp without time zone",
        "timestamp with time zone",
        "timestamptz",
        "timestamp",
        "date",
    }
)


def _evidence(
    *,
    row_count: int,
    sample_size: int,
    table_snapshot_at: datetime,
    limits: ProfilerLimits,
    metric_values: dict,
) -> dict:
    return {
        "profile_version": PROFILE_VERSION,
        "row_count": row_count,
        "sample_size": sample_size,
        "table_snapshot_at": table_snapshot_at,
        "thresholds": {"uniqueness_threshold": limits.uniqueness_threshold},
        "metric_values": metric_values,
    }


def _profile_single(
    conn: DbConn, ref: CatalogObjectRef, column: str, *, sample: sql.Composable
) -> tuple[int, int, int]:
    query = sql.SQL(
        "SELECT count(*) AS n, count(DISTINCT {col}) AS distinct_count, "
        "count(*) FILTER (WHERE {col} IS NULL) AS null_count FROM {tbl} {sample}"
    ).format(
        col=sql.Identifier(column),
        tbl=sql.Identifier(ref.schema, ref.table),
        sample=sample,
    )
    n, distinct_count, null_count = conn.execute(query).fetchone()
    return int(n), int(distinct_count), int(null_count)


def _apply_statement_timeout(conn: DbConn, limits: ProfilerLimits) -> None:
    # SET LOCAL is transaction-scoped, so the bound is dropped when the scan's txn ends.
    conn.execute(
        sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(limits.statement_timeout_ms))
    )


def _sampling(row_count: int, limits: ProfilerLimits) -> tuple[int, sql.Composable]:
    if row_count > limits.sample_threshold_rows:
        # Allow sub-1% so the cap is REAL: flooring at 1.0 would scan ~1% of a huge table while
        # reporting the nominal sample_size. BERNOULLI accepts a sub-1 percentage.
        pct = min(100.0, 100.0 * limits.sample_size / row_count)
        return min(row_count, limits.sample_size), sql.SQL("TABLESAMPLE BERNOULLI ({})").format(
            sql.Literal(pct)
        )
    return row_count, sql.SQL("")


def _combination_distinct(
    conn: DbConn, ref: CatalogObjectRef, columns: Sequence[str], *, sample: sql.Composable
) -> tuple[int, int]:
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    # ONE materialized sampled CTE so count(*) and the grouped-distinct count are computed from the
    # SAME Bernoulli sample — applying {sample} twice would draw two different samples and skew the
    # uniqueness ratio.
    query = sql.SQL(
        "WITH s AS MATERIALIZED (SELECT {cols} FROM {tbl} {sample}) "
        "SELECT (SELECT count(*) FROM s) AS n, "
        "(SELECT count(*) FROM (SELECT 1 FROM s GROUP BY {cols}) g) AS distinct_count"
    ).format(tbl=sql.Identifier(ref.schema, ref.table), sample=sample, cols=cols)
    n, distinct_count = conn.execute(query).fetchone()
    return int(n), int(distinct_count)
