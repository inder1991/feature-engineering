"""Durable storage for data observations.

Immutable versions plus a **derived** current pointer (migration 1033). A new observation never
overwrites an older one, and `latest` is decided by ordering rather than by mutation — so a later
partial run cannot erase what an earlier complete one proved, and a backfilled older profile cannot
become current merely because it was written second.

Provenance is stored alongside the numbers because it bounds what they may later support: `method`
(a sampled profile that finds no duplicate proves nothing about uniqueness), `complete` plus
`failures` (a partial observation must never read as a whole one), `partitions_read` (empty means
unpartitioned, never "everything"), and `execution_principal` (it decides what the read could see).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from featuregen.data_agent.results import ColumnObservationV1, DataObservationResultV1


@dataclass(frozen=True, slots=True)
class StoredObservationV1(DataObservationResultV1):
    """A persisted observation — the result plus the ids the store assigned."""

    observation_id: str = ""
    execution_principal: str = ""


def record_observation(conn, result: DataObservationResultV1, *, catalog_source: str,
                       connection_id: str, execution_principal: str, dialect: str,
                       now: datetime) -> str:
    """Append one observation. Returns its id. Never updates an existing row."""
    observation_id = f"obs-{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO data_observation (observation_id, physical_id, catalog_source, connection_id, "
        "  execution_principal, dialect, row_count, method, complete, partitions_read, failures, "
        "  observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (observation_id, result.physical_id, catalog_source, connection_id, execution_principal,
         dialect, result.row_count, result.method, result.complete,
         list(result.partitions_read), list(result.failures), now))
    for column in result.columns:
        conn.execute(
            "INSERT INTO data_observation_column (observation_id, column_name, non_null_count, "
            "  distinct_count, observed_rows, minimum, maximum) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (observation_id, column.column, column.non_null_count, column.distinct_count,
             column.observed_rows, column.minimum, column.maximum))
    return observation_id


def _hydrate(conn, row) -> StoredObservationV1:
    (observation_id, physical_id, principal, row_count, method, complete, partitions,
     failures) = row
    columns = tuple(
        ColumnObservationV1(column=c, non_null_count=n, distinct_count=d, observed_rows=o,
                            minimum=mn, maximum=mx)
        for c, n, d, o, mn, mx in conn.execute(
            "SELECT column_name, non_null_count, distinct_count, observed_rows, minimum, maximum "
            "FROM data_observation_column WHERE observation_id = %s ORDER BY column_name",
            (observation_id,)).fetchall())
    return StoredObservationV1(
        physical_id=physical_id, row_count=row_count, columns=columns,
        partitions_read=tuple(partitions or ()), method=method, complete=complete,
        failures=tuple(failures or ()), observation_id=observation_id,
        execution_principal=principal)


_SELECT = (
    "SELECT observation_id, physical_id, execution_principal, row_count, method, complete, "
    "       partitions_read, failures FROM data_observation WHERE physical_id = %s "
    "ORDER BY observed_at DESC, observation_id DESC")


def latest_observation(conn, physical_id: str) -> "StoredObservationV1 | None":
    """The current profile for one physical object, or None. Newest by OBSERVATION time — a
    backfilled older profile must not become current because it was inserted later."""
    row = conn.execute(_SELECT + " LIMIT 1", (physical_id,)).fetchone()
    return _hydrate(conn, row) if row else None


def observation_history(conn, physical_id: str) -> tuple["StoredObservationV1", ...]:
    """Every observation for one object, newest first. The audit answer to "what did we know when
    that fact was accepted?"."""
    return tuple(_hydrate(conn, row)
                 for row in conn.execute(_SELECT, (physical_id,)).fetchall())
