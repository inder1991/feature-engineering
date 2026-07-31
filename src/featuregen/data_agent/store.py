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

from psycopg.types.json import Jsonb

from featuregen.data_agent.relationship_observation import (
    RelationshipObservationV2,
    RowCoverage,
    observation_from_json,
    observation_to_json,
)
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


def latest_observation(conn, physical_id: str) -> StoredObservationV1 | None:
    """The current profile for one physical object, or None. Newest by OBSERVATION time — a
    backfilled older profile must not become current because it was inserted later."""
    row = conn.execute(_SELECT + " LIMIT 1", (physical_id,)).fetchone()
    return _hydrate(conn, row) if row else None


def observation_history(conn, physical_id: str) -> tuple[StoredObservationV1, ...]:
    """Every observation for one object, newest first. The audit answer to "what did we know when
    that fact was accepted?"."""
    return tuple(_hydrate(conn, row)
                 for row in conn.execute(_SELECT, (physical_id,)).fetchall())


@dataclass(frozen=True, slots=True)
class RelationshipObservationCurrentV2:
    current_scope_key: str
    observation_revision_id: str | None
    pointer_version: int
    became_current: bool
    reason: str | None = None


class RelationshipObservationStoreCorruption(RuntimeError):
    """A content-addressed observation id already names different immutable bytes."""


def _relationship_quality(observation: RelationshipObservationV2) -> int:
    if not observation.complete:
        return 0
    if observation.row_coverage is not RowCoverage.FULL:
        return 10
    return 30 if observation.method == "exact" else 20


def _relationship_conflict(observation: RelationshipObservationV2) -> bool:
    """Directional production conflict: the target tuple is not globally key-like."""
    return (
        observation.right.duplicate_row_count > 0
        or observation.right.null_row_count > 0
        or observation.max_right_matches_per_left_row > 1
    )


def _observation_matches_stored_realization(
    conn,
    observation: RelationshipObservationV2,
) -> bool:
    """Verify the evidence tuple/scope against immutable realization bytes before using metrics."""
    row = conn.execute(
        "SELECT realization_json FROM bridge_join_realization_revision "
        "WHERE realization_revision_id=%s",
        (observation.realization_revision_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], dict):
        return False
    payload = row[0]
    try:
        pairs = payload["column_pairs"]
        left_columns = tuple(
            str(pair["from_logical_column_ref"]).rpartition(".")[2]
            for pair in pairs
        )
        right_columns = tuple(
            str(pair["to_logical_column_ref"]).rpartition(".")[2]
            for pair in pairs
        )
        return (
            observation.scope_id == payload["applicability_scope"]["scope_id"]
            and observation.left.binding_revision_id
            == payload["from_endpoint"]["binding_revision_id"]
            and observation.right.binding_revision_id
            == payload["to_endpoint"]["binding_revision_id"]
            and observation.left.columns == left_columns
            and observation.right.columns == right_columns
        )
    except (KeyError, TypeError):
        return False


def _realization_revision_is_current(conn, realization_revision_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bridge_join_realization_current "
        "WHERE realization_revision_id=%s AND lifecycle='active'",
        (realization_revision_id,),
    ).fetchone()
    return row is not None


def record_relationship_observation(
    conn,
    observation: RelationshipObservationV2,
    *,
    expected_pointer_version: int | None = None,
) -> RelationshipObservationCurrentV2:
    """Append immutable evidence and conditionally advance its same-scope current pointer.

    A stale realization revision never becomes current. A newer partial/sample/approximate result
    is retained as history (and remains queryable as a conflict) but cannot displace a complete
    exact observation. Equal-quality backfills advance only when their observation time is newer.
    """
    revision_id = observation.observation_revision_id
    scope_key = observation.current_scope_key
    payload = observation_to_json(observation)
    quality = _relationship_quality(observation)
    applicable = _observation_matches_stored_realization(conn, observation)
    conflict = applicable and _relationship_conflict(observation)
    conn.execute(
        "INSERT INTO relationship_observation_revision ("
        " observation_revision_id, current_scope_key, realization_revision_id,"
        " left_binding_revision_id, right_binding_revision_id,"
        " left_source_snapshot_id, right_source_snapshot_id, scope_id, execution_principal,"
        " method, row_coverage, complete, quality_rank, conflict_observed, producer, strength,"
        " observation_json, observed_at"
        ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (observation_revision_id) DO NOTHING",
        (
            revision_id,
            scope_key,
            observation.realization_revision_id,
            observation.left.binding_revision_id,
            observation.right.binding_revision_id,
            observation.left_source_snapshot_id,
            observation.right_source_snapshot_id,
            observation.scope_id,
            observation.execution_principal,
            observation.method,
            observation.row_coverage.value,
            observation.complete,
            quality,
            conflict,
            observation.producer,
            observation.strength,
            Jsonb(payload),
            observation.observed_at,
        ),
    )
    stored = conn.execute(
        "SELECT observation_json FROM relationship_observation_revision "
        "WHERE observation_revision_id=%s",
        (revision_id,),
    ).fetchone()
    if stored is None or stored[0] != payload:
        raise RelationshipObservationStoreCorruption(
            f"relationship observation collision for {revision_id}")

    current = conn.execute(
        "SELECT observation_revision_id, quality_rank, observed_at, pointer_version "
        "FROM relationship_observation_current WHERE current_scope_key=%s",
        (scope_key,),
    ).fetchone()
    current_version = 0 if current is None else int(current[3])
    expected = current_version if expected_pointer_version is None else expected_pointer_version
    if expected != current_version:
        return RelationshipObservationCurrentV2(
            scope_key,
            None if current is None else current[0],
            current_version,
            False,
            "stale_current_pointer",
        )
    if not applicable:
        return RelationshipObservationCurrentV2(
            scope_key,
            None if current is None else current[0],
            current_version,
            False,
            "observation_not_for_realization",
        )
    if conflict:
        # A sampled/partial observation cannot prove safety, but one concrete target duplicate or
        # join multiplier disproves it. Withdraw the exact directional revision immediately while
        # retaining both the prior proof and this contradiction as immutable history.
        from featuregen.overlay.upload.bridge_store import demote_realization_revision

        demote_realization_revision(conn, observation.realization_revision_id)
        return RelationshipObservationCurrentV2(
            scope_key,
            None if current is None else current[0],
            current_version,
            False,
            "observed_target_duplicate_or_fanout",
        )
    if not _realization_revision_is_current(
        conn, observation.realization_revision_id
    ):
        return RelationshipObservationCurrentV2(
            scope_key,
            None if current is None else current[0],
            current_version,
            False,
            "realization_revision_not_current",
        )
    if current is not None:
        current_revision, current_quality, current_observed_at, _version = current
        if current_revision == revision_id:
            return RelationshipObservationCurrentV2(
                scope_key, revision_id, current_version, False, "already_current")
        if quality < int(current_quality) or (
            quality == int(current_quality)
            and observation.observed_at <= current_observed_at
        ):
            return RelationshipObservationCurrentV2(
                scope_key,
                current_revision,
                current_version,
                False,
                "weaker_or_older_observation",
            )

    if current is None:
        changed = conn.execute(
            "INSERT INTO relationship_observation_current ("
            " current_scope_key, observation_revision_id, quality_rank, observed_at,"
            " pointer_version) VALUES (%s,%s,%s,%s,1) "
            "ON CONFLICT (current_scope_key) DO NOTHING",
            (scope_key, revision_id, quality, observation.observed_at),
        ).rowcount
        new_version = 1
    else:
        changed = conn.execute(
            "UPDATE relationship_observation_current SET "
            "observation_revision_id=%s, quality_rank=%s, observed_at=%s,"
            "pointer_version=pointer_version+1, updated_at=now() "
            "WHERE current_scope_key=%s AND pointer_version=%s",
            (
                revision_id,
                quality,
                observation.observed_at,
                scope_key,
                expected,
            ),
        ).rowcount
        new_version = expected + 1
    if changed != 1:
        row = conn.execute(
            "SELECT observation_revision_id, pointer_version "
            "FROM relationship_observation_current WHERE current_scope_key=%s",
            (scope_key,),
        ).fetchone()
        return RelationshipObservationCurrentV2(
            scope_key,
            None if row is None else row[0],
            0 if row is None else int(row[1]),
            False,
            "stale_current_pointer",
        )
    return RelationshipObservationCurrentV2(
        scope_key, revision_id, new_version, True)


def current_relationship_observation(
    conn, current_scope_key: str
) -> RelationshipObservationV2 | None:
    row = conn.execute(
        "SELECT r.observation_json FROM relationship_observation_current c "
        "JOIN relationship_observation_revision r "
        "  ON r.observation_revision_id=c.observation_revision_id "
        "JOIN bridge_join_realization_current rc "
        "  ON rc.realization_revision_id=r.realization_revision_id "
        "WHERE c.current_scope_key=%s AND rc.lifecycle='active'",
        (current_scope_key,),
    ).fetchone()
    return None if row is None else observation_from_json(row[0])


def latest_relationship_conflict(
    conn, realization_revision_id: str
) -> RelationshipObservationV2 | None:
    """Latest observed target-duplicate/fan-out conflict, including partial evidence."""
    row = conn.execute(
        "SELECT observation_json FROM relationship_observation_revision "
        "WHERE realization_revision_id=%s AND conflict_observed=true "
        "ORDER BY observed_at DESC, observation_revision_id DESC LIMIT 1",
        (realization_revision_id,),
    ).fetchone()
    return None if row is None else observation_from_json(row[0])
