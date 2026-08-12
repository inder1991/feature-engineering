"""Composed crosswalk observation persistence (Release C Task 11; migration 1057).

Immutable ``crosswalk_observation_revision`` rows + a CAS ``crosswalk_observation_current`` pointer
keyed on the ``current_scope_key`` — the same shape as the V2 relationship store (``data_agent.store``)
and the same read-back-and-re-derive discipline as the crosswalk definition store: every write is
confirmed by reading the row back and re-hashing its content, because an ``ON CONFLICT DO NOTHING``
insert reports success for a row it did not write.

**The 1038 store is untouched.** Per-LEG observations are ordinary two-endpoint rows and are written
by ``data_agent.store.record_relationship_observation`` exactly as shipped. This module refuses to
advance a composition whose leg observations are not persisted — the foreign keys make that
structural rather than conventional.

**Quality ranking mirrors the V2 store's**, deliberately: an incomplete measurement ranks 0, a
non-full coverage 10, an approximate 20, a full exact 30, and a weaker or older measurement never
displaces a stronger one. A crosswalk whose composition was once proved exact does not lose that
proof because somebody later ran a sample.

**There is no safety verdict here.** A row says what was seen. ``crosswalk_admission`` turns seeing
into a verdict, and its output pins these observation ids rather than being stored inside them.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.data_agent.relationship_observation import RelationshipMethod, RowCoverage
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.crosswalk_observation import (
    CrosswalkExecutionObservationV1,
    crosswalk_observation_from_json,
    crosswalk_observation_to_json,
)


class CrosswalkObservationStoreConflict(Exception):
    """A CAS miss, a missing row, or a content-addressed id naming different immutable bytes."""


@dataclass(frozen=True, slots=True)
class CrosswalkObservationCurrentV1:
    """The outcome of one write: what is current now, and why this measurement did or did not win."""

    current_scope_key: str
    observation_revision_id: str | None
    pointer_version: int
    became_current: bool
    reason: str | None = None


def observation_quality(observation: CrosswalkExecutionObservationV1) -> int:
    """The V2 store's rank, verbatim (``data_agent.store._relationship_quality``)."""
    if not observation.complete:
        return 0
    if observation.row_coverage is not RowCoverage.FULL:
        return 10
    return 30 if observation.method == RelationshipMethod.EXACT.value else 20


def _assert_legs_agree_with_the_two_endpoint_store(
    conn: DbConn, observation: CrosswalkExecutionObservationV1,
) -> None:
    """Where a leg names a shipped V2 row, the two must be the SAME numbers.

    The composition embeds every leg's full measurement (the two-endpoint store cannot hold a
    same-catalog leg — see the contract module), so a bridge-backed leg exists in two places. Two
    places holding one truth is two places that can disagree; this is the check that makes it one.
    """
    from featuregen.data_agent.store import RelationshipObservationStoreCorruption

    for leg in (observation.source_leg, observation.target_leg):
        if leg.v2_observation_revision_id is None:
            continue
        row = conn.execute(
            "SELECT observation_json FROM relationship_observation_revision "
            "WHERE observation_revision_id = %s", (leg.v2_observation_revision_id,)).fetchone()
        if row is None:
            raise CrosswalkObservationStoreConflict(
                f"leg {leg.leg!r} names two-endpoint observation "
                f"{leg.v2_observation_revision_id} which is not persisted; a composition that "
                "cannot be traced to its legs cannot be replayed or explained")
        stored = row[0]
        left, right = stored["left"], stored["right"]
        endpoint = (left if left["binding_revision_id"] == leg.endpoint_binding_revision_id
                    else right)
        if (endpoint["binding_revision_id"] != leg.endpoint_binding_revision_id
                or tuple(endpoint["columns"]) != leg.endpoint_columns
                or int(endpoint["row_count"]) != leg.endpoint_row_count
                or int(endpoint["distinct_tuple_count"]) != leg.endpoint_distinct_tuple_count
                or int(stored["joined_row_count"]) != leg.joined_row_count):
            raise RelationshipObservationStoreCorruption(
                f"leg {leg.leg!r} disagrees with the two-endpoint observation it names "
                f"({leg.v2_observation_revision_id}); one of the two is not what was measured")


def record_crosswalk_observation(
    conn: DbConn,
    observation: CrosswalkExecutionObservationV1,
    *,
    expected_pointer_version: int | None = None,
) -> CrosswalkObservationCurrentV1:
    """Append the immutable measurement and conditionally advance its same-scope current pointer.

    The append ALWAYS happens (a measurement that lost the pointer race is still evidence, and the
    conflict a weaker measurement reports is often the interesting one). What is conditional is
    which measurement the pointer names.
    """
    revision_id = observation.observation_revision_id
    scope_key = observation.current_scope_key
    payload = crosswalk_observation_to_json(observation)
    content_hash = materialize_hash(payload)
    quality = observation_quality(observation)

    _assert_legs_agree_with_the_two_endpoint_store(conn, observation)

    conn.execute(
        "INSERT INTO crosswalk_observation_revision ("
        "  observation_revision_id, current_scope_key, crosswalk_definition_revision_id,"
        "  source_leg_observation_revision_id, target_leg_observation_revision_id,"
        "  mapping_binding_revision_id, scope_id, as_of, mapping_temporal_policy_revision_id,"
        "  execution_principal, method, row_coverage, complete, source_to_target_max_matches,"
        "  target_to_source_max_matches, composed_row_count, quality_rank, producer, strength,"
        "  observation_json, content_hash, observed_at"
        ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (observation_revision_id) DO NOTHING",
        (revision_id, scope_key, observation.crosswalk_definition_revision_id,
         observation.source_leg.v2_observation_revision_id,
         observation.target_leg.v2_observation_revision_id,
         observation.mapping_binding_revision_id, observation.scope_id, observation.as_of,
         observation.mapping_temporal_policy_revision_id, observation.execution_principal,
         observation.method, observation.row_coverage.value, observation.complete,
         observation.source_to_target_max_matches, observation.target_to_source_max_matches,
         observation.composed_row_count, quality, observation.producer, observation.strength,
         Jsonb(payload), content_hash, observation.observed_at))
    stored = conn.execute(
        "SELECT observation_json, content_hash FROM crosswalk_observation_revision "
        "WHERE observation_revision_id = %s", (revision_id,)).fetchone()
    if stored is None or stored[0] != payload or stored[1] != content_hash:
        # Content-addressed: the same id naming different bytes is corruption, not a race.
        raise CrosswalkObservationStoreConflict(
            f"crosswalk observation collision for {revision_id}")

    current = conn.execute(
        "SELECT observation_revision_id, quality_rank, observed_at, pointer_version "
        "FROM crosswalk_observation_current WHERE current_scope_key = %s",
        (scope_key,)).fetchone()
    current_version = 0 if current is None else int(current[3])
    expected = current_version if expected_pointer_version is None else expected_pointer_version
    if expected != current_version:
        return CrosswalkObservationCurrentV1(
            scope_key, None if current is None else current[0], current_version, False,
            "stale_current_pointer")

    if current is not None:
        current_revision, current_quality, current_observed_at, _version = current
        if current_revision == revision_id:
            return CrosswalkObservationCurrentV1(
                scope_key, revision_id, current_version, False, "already_current")
        if quality < int(current_quality) or (
            quality == int(current_quality)
            and observation.observed_at <= current_observed_at
        ):
            return CrosswalkObservationCurrentV1(
                scope_key, current_revision, current_version, False,
                "weaker_or_older_observation")

    if current is None:
        changed = conn.execute(
            "INSERT INTO crosswalk_observation_current ("
            "  current_scope_key, observation_revision_id, crosswalk_definition_revision_id,"
            "  quality_rank, observed_at, pointer_version) VALUES (%s,%s,%s,%s,%s,1) "
            "ON CONFLICT (current_scope_key) DO NOTHING",
            (scope_key, revision_id, observation.crosswalk_definition_revision_id, quality,
             observation.observed_at)).rowcount
        new_version = 1
    else:
        changed = conn.execute(
            "UPDATE crosswalk_observation_current SET observation_revision_id = %s, "
            "  crosswalk_definition_revision_id = %s, quality_rank = %s, observed_at = %s, "
            "  pointer_version = pointer_version + 1, updated_at = now() "
            "WHERE current_scope_key = %s AND pointer_version = %s",
            (revision_id, observation.crosswalk_definition_revision_id, quality,
             observation.observed_at, scope_key, expected)).rowcount
        new_version = expected + 1
    if changed != 1:
        row = conn.execute(
            "SELECT observation_revision_id, pointer_version FROM crosswalk_observation_current "
            "WHERE current_scope_key = %s", (scope_key,)).fetchone()
        return CrosswalkObservationCurrentV1(
            scope_key, None if row is None else row[0], 0 if row is None else int(row[1]),
            False, "stale_current_pointer")
    return CrosswalkObservationCurrentV1(scope_key, revision_id, new_version, True)


def load_crosswalk_observation(
    conn: DbConn, observation_revision_id: str,
) -> CrosswalkExecutionObservationV1 | None:
    """Load and CONTENT-VERIFY one measurement; corruption raises rather than being served."""
    row = conn.execute(
        "SELECT observation_json, content_hash FROM crosswalk_observation_revision "
        "WHERE observation_revision_id = %s", (observation_revision_id,)).fetchone()
    if row is None:
        return None
    try:
        observation = crosswalk_observation_from_json(row[0])
    except Exception as exc:            # noqa: BLE001 — a stored payload that no longer satisfies
        raise CrosswalkObservationStoreConflict(   # the contract is corruption, not a caller error
            f"crosswalk observation {observation_revision_id} is not reconstructible: {exc}") from exc
    if (materialize_hash(observation.content_payload()) != row[1]
            or observation.observation_revision_id != observation_revision_id):
        raise CrosswalkObservationStoreConflict(
            f"crosswalk observation {observation_revision_id} fails content verification")
    return observation


def current_crosswalk_observation(
    conn: DbConn, current_scope_key: str,
) -> CrosswalkExecutionObservationV1 | None:
    """The measurement the pointer names for one scope — or ``None`` when nothing measured it."""
    row = conn.execute(
        "SELECT observation_revision_id FROM crosswalk_observation_current "
        "WHERE current_scope_key = %s", (current_scope_key,)).fetchone()
    return None if row is None else load_crosswalk_observation(conn, row[0])


def current_crosswalk_observations_for_revision(
    conn: DbConn, crosswalk_definition_revision_id: str,
) -> tuple[CrosswalkExecutionObservationV1, ...]:
    """Every CURRENT measurement of one definition revision — one per measured scope.

    A crosswalk can be measured under more than one applicability scope (a partition-restricted
    sandbox probe and an unrestricted one are different questions), so this is a tuple and not a
    single value. Admission consumes them per scope; it never merges them.
    """
    rows = conn.execute(
        "SELECT c.observation_revision_id FROM crosswalk_observation_current c "
        "WHERE c.crosswalk_definition_revision_id = %s "
        "ORDER BY c.observed_at DESC, c.observation_revision_id",
        (crosswalk_definition_revision_id,)).fetchall()
    loaded = [load_crosswalk_observation(conn, row[0]) for row in rows]
    return tuple(item for item in loaded if item is not None)


def crosswalk_observation_history(
    conn: DbConn, crosswalk_definition_revision_id: str,
) -> tuple[CrosswalkExecutionObservationV1, ...]:
    """Every measurement of one definition revision, newest first. The audit answer to "what did we
    know when that verdict was taken?"."""
    rows = conn.execute(
        "SELECT observation_revision_id FROM crosswalk_observation_revision "
        "WHERE crosswalk_definition_revision_id = %s "
        "ORDER BY observed_at DESC, observation_revision_id",
        (crosswalk_definition_revision_id,)).fetchall()
    loaded = [load_crosswalk_observation(conn, row[0]) for row in rows]
    return tuple(item for item in loaded if item is not None)
