from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import sql

from featuregen.contracts import DbConn
from featuregen.overlay.catalog import CatalogAdapter, CatalogObject
from featuregen.overlay.facts import validate_fact_value
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.profiler_heuristics import (
    GRAIN,
    Proposal,
    _combination_grain,
    _grain_proposal,
)
from featuregen.overlay.profiler_metrics import (
    _TIMESTAMP_TYPES,
    PROFILE_VERSION,
    _apply_statement_timeout,
    _evidence,
    _profile_single,
    _sampling,
)

__all__ = [
    "run_profiler_scan",
    "ProfilerLimits",
    "SchemaNotAllowedError",
    "Proposal",
    "PROFILE_VERSION",
    "GRAIN",
    "AVAILABILITY_TIME",
    "SCD_EFFECTIVE_DATING",
]

AVAILABILITY_TIME = "availability_time"
SCD_EFFECTIVE_DATING = "scd_effective_dating"

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


def run_profiler_scan(
    conn: DbConn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef,
    *,
    limits: ProfilerLimits,
) -> list[Proposal]:
    if ref.schema not in limits.allowed_schemas:
        raise SchemaNotAllowedError(
            f"schema {ref.schema!r} is not on the profiler allowlist {sorted(limits.allowed_schemas)}"
        )
    _apply_statement_timeout(conn, limits)
    columns = _columns_for(adapter, ref)[: limits.max_columns]
    table_snapshot_at = _now()
    row_count = int(
        conn.execute(
            sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=sql.Identifier(ref.schema, ref.table))
        ).fetchone()[0]
    )
    sample_size, sample = _sampling(row_count, limits)
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

    if not unique_singletons:
        proposals.extend(
            _combination_grain(
                conn,
                ref,
                columns,
                row_count=row_count,
                sample_size=sample_size,
                table_snapshot_at=table_snapshot_at,
                limits=limits,
                sample=sample,
            )
        )

    for col in columns:
        if (col.data_type or "").lower() in _TIMESTAMP_TYPES:
            n, distinct_count, null_count = _profile_single(conn, ref, col.column, sample=sample)
            proposed_value = {"column": col.column, "basis": _availability_basis(col.column)}
            validate_fact_value(AVAILABILITY_TIME, proposed_value)
            month_buckets = int(
                conn.execute(
                    sql.SQL(
                        "SELECT count(DISTINCT date_trunc('month', {col})) FROM {tbl} {sample}"
                    ).format(
                        col=sql.Identifier(col.column),
                        tbl=sql.Identifier(ref.schema, ref.table),
                        sample=sample,
                    )
                ).fetchone()[0]
            )
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
                        metric_values={
                            "distinct_count": distinct_count,
                            "null_count": null_count,
                            "month_buckets": month_buckets,
                        },
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
