"""Inference / proposal heuristics for the overlay profiler (SP-1).

The policy layer behind the profiler: the `Proposal` contract and the per-fact-type proposal builders
that decide WHAT to propose from collected metrics (and the grain uniqueness thresholds). These call
the metric collectors in `profiler_metrics`; they construct no SQL of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

from featuregen.overlay.facts import validate_fact_value
from featuregen.overlay.profiler_metrics import _combination_distinct, _evidence

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from psycopg import sql

    from featuregen.contracts import DbConn
    from featuregen.overlay.catalog import CatalogObject
    from featuregen.overlay.identity import CatalogObjectRef
    from featuregen.overlay.profiler import ProfilerLimits

GRAIN = "grain"
AVAILABILITY_TIME = "availability_time"
SCD_EFFECTIVE_DATING = "scd_effective_dating"


@dataclass(frozen=True, slots=True)
class Proposal:
    ref: CatalogObjectRef
    fact_type: str
    proposed_value: Mapping[str, object]
    evidence_metrics: Mapping[str, object]
    use_case: str | None = None


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
    """Build a GRAIN proposal for a (near-)unique column set.

    Contract: a grain candidate is proposed only once its sampled uniqueness_ratio
    (distinct_count / scanned) clears the profiler threshold
    (``ProfilerLimits.uniqueness_threshold``, default 0.99) — the callers gate on that before
    reaching here. ``is_unique`` asserts STRICT uniqueness: it is True ONLY when the sampled
    ratio is exactly 1.0 (no duplicates observed in the sample). A near-unique candidate
    (threshold <= ratio < 1.0) is still proposed, but with ``is_unique=False`` so a human
    confirms whether the residual duplicates are real or a sampling artifact. The observed
    ratio is carried in the evidence metrics (``uniqueness_ratio``) for that review.
    """
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


def _combination_grain(
    conn: DbConn,
    ref: CatalogObjectRef,
    columns: Sequence[CatalogObject],
    *,
    row_count: int,
    sample_size: int,
    table_snapshot_at: datetime,
    limits: ProfilerLimits,
    sample: sql.Composable,
) -> list[Proposal]:
    proposals: list[Proposal] = []
    probed = 0
    names = [c.column for c in columns]
    for pair in combinations(names, 2):
        if probed >= limits.max_column_combinations:
            break
        probed += 1
        n, distinct_count = _combination_distinct(conn, ref, pair, sample=sample)
        ratio = distinct_count / n if n else 0.0
        if ratio >= limits.uniqueness_threshold:
            proposals.append(
                _grain_proposal(
                    ref,
                    list(pair),
                    distinct_count=distinct_count,
                    null_count=0,
                    scanned=n,
                    row_count=row_count,
                    sample_size=sample_size,
                    table_snapshot_at=table_snapshot_at,
                    limits=limits,
                )
            )
    return proposals
