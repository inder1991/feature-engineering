from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import sql

from featuregen.contracts import DbConn
from featuregen.overlay.catalog import CatalogAdapter, CatalogObject
from featuregen.overlay.facts import validate_fact_value
from featuregen.overlay.identity import CatalogObjectRef

PROFILE_VERSION = "overlay-profiler@1"

GRAIN = "grain"
AVAILABILITY_TIME = "availability_time"
SCD_EFFECTIVE_DATING = "scd_effective_dating"

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
_FROM_TOKENS = ("valid_from", "effective_from", "eff_from", "start_date", "valid_start")
_TO_TOKENS = ("valid_to", "effective_to", "eff_to", "end_date", "valid_end")


class SchemaNotAllowedError(Exception):
    """The profiler refused a target whose schema is not on the allowlist (§5.2)."""


@dataclass(frozen=True, slots=True)
class ProfilerLimits:
    allowed_schemas: frozenset[str]
    uniqueness_threshold: float = 0.99
    max_columns: int = 64
    max_column_combinations: int = 16
    statement_timeout_ms: int = 5000
    sample_threshold_rows: int = 1_000_000
    sample_size: int = 100_000


@dataclass(frozen=True, slots=True)
class Proposal:
    ref: CatalogObjectRef
    fact_type: str
    proposed_value: Mapping[str, object]
    evidence_metrics: Mapping[str, object]
    use_case: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _columns_for(adapter: CatalogAdapter, ref: CatalogObjectRef) -> list[CatalogObject]:
    return [
        o
        for o in adapter.list_objects()
        if o.object_kind == "column" and o.schema == ref.schema and o.table == ref.table
    ]


def _availability_basis(column: str) -> str:
    lowered = column.lower()
    if "post" in lowered:
        return "posted_at"
    return "ingested_at"


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


def _grain_proposal(
    ref: CatalogObjectRef,
    columns: Sequence[str],
    *,
    distinct_count: int,
    null_count: int,
    scanned: int,
    row_count: int,
    sample_size: int,
    table_snapshot_at: datetime,
    limits: ProfilerLimits,
) -> Proposal:
    ratio = round(distinct_count / scanned, 6) if scanned else 0.0
    proposed_value = {"columns": list(columns), "is_unique": ratio == 1.0}
    validate_fact_value(GRAIN, proposed_value)
    metric_values = {
        "distinct_count": distinct_count,
        "null_count": null_count,
        "uniqueness_ratio": ratio,
        "column_count": len(columns),
    }
    return Proposal(
        ref=ref,
        fact_type=GRAIN,
        proposed_value=proposed_value,
        evidence_metrics=_evidence(
            row_count=row_count,
            sample_size=sample_size,
            table_snapshot_at=table_snapshot_at,
            limits=limits,
            metric_values=metric_values,
        ),
    )


def run_profiler_scan(
    conn: DbConn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef,
    *,
    limits: ProfilerLimits,
) -> list[Proposal]:
    columns = _columns_for(adapter, ref)[: limits.max_columns]
    table_snapshot_at = _now()
    sample = sql.SQL("")
    row_count, _ignored, _n = _profile_single(
        conn, ref, columns[0].column, sample=sample
    ) if columns else (
        int(conn.execute(
            sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier(ref.schema, ref.table))
        ).fetchone()[0]),
        0,
        0,
    )
    sample_size = row_count
    proposals: list[Proposal] = []

    unique_singletons: list[str] = []
    for col in columns:
        n, distinct_count, null_count = _profile_single(conn, ref, col.column, sample=sample)
        ratio = distinct_count / n if n else 0.0
        if ratio >= limits.uniqueness_threshold:
            unique_singletons.append(col.column)
            proposals.append(
                _grain_proposal(
                    ref,
                    [col.column],
                    distinct_count=distinct_count,
                    null_count=null_count,
                    scanned=n,
                    row_count=row_count,
                    sample_size=sample_size,
                    table_snapshot_at=table_snapshot_at,
                    limits=limits,
                )
            )

    for col in columns:
        if (col.data_type or "").lower() in _TIMESTAMP_TYPES:
            n, distinct_count, null_count = _profile_single(conn, ref, col.column, sample=sample)
            proposed_value = {"column": col.column, "basis": _availability_basis(col.column)}
            validate_fact_value(AVAILABILITY_TIME, proposed_value)
            proposals.append(
                Proposal(
                    ref=ref,
                    fact_type=AVAILABILITY_TIME,
                    proposed_value=proposed_value,
                    evidence_metrics=_evidence(
                        row_count=row_count,
                        sample_size=sample_size,
                        table_snapshot_at=table_snapshot_at,
                        limits=limits,
                        metric_values={"distinct_count": distinct_count, "null_count": null_count},
                    ),
                )
            )

    from_col = next(
        (c.column for c in columns if any(t in c.column.lower() for t in _FROM_TOKENS)), None
    )
    to_col = next(
        (c.column for c in columns if any(t in c.column.lower() for t in _TO_TOKENS)), None
    )
    if from_col is not None and to_col is not None:
        proposed_value = {"valid_from": from_col, "valid_to": to_col}
        validate_fact_value(SCD_EFFECTIVE_DATING, proposed_value)
        _n, _d, from_nulls = _profile_single(conn, ref, from_col, sample=sample)
        proposals.append(
            Proposal(
                ref=ref,
                fact_type=SCD_EFFECTIVE_DATING,
                proposed_value=proposed_value,
                evidence_metrics=_evidence(
                    row_count=row_count,
                    sample_size=sample_size,
                    table_snapshot_at=table_snapshot_at,
                    limits=limits,
                    metric_values={"valid_from_null_count": from_nulls},
                ),
            )
        )

    return proposals
