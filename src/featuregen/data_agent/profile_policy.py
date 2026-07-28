"""What a profile is allowed to compute and return.

Mirrors the shape of the shipped ``ProfilerLimits`` (server-owned allowlist, caps, timeout,
sampling) rather than inventing a second policy vocabulary — the PostgreSQL profiler's limits are
the proven ones, and only the SQL dialect differs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfilePolicyV1:
    """Bounds and permissions for one observation. Defaults are the cautious answer."""

    max_columns: int = 64
    max_partitions: int = 32
    statement_timeout_ms: int = 30_000
    #: Exact DISTINCT is a shuffle on a large Hive table. Approximate is the default, and the plan
    #: records which was used because it decides what the evidence can later support: a sample that
    #: finds a duplicate DISPROVES uniqueness, while a sample that finds none proves nothing.
    exact_distinct: bool = False
    #: Categorical top values are real VALUES crossing the data-plane boundary — a top value on a
    #: name column IS a customer name. Off unless explicitly granted.
    allow_top_values: bool = False
    #: Minimum group size before a categorical bucket may be returned at all.
    min_cell_size: int = 5
