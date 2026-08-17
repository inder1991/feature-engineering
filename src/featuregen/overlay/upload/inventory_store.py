"""S3 — persisting the generation inventory observation and the bound input set (1074).

**Binding is unreachable without an inventory**, and that is a foreign key rather than a rule this
module remembers to apply: :func:`record_bound_input_set` cannot be called without an observation
id, and the database refuses one that names no observation. ``compile_feature_group`` and
``compile_ir`` both already require an inventory, so a binding recorded without one would describe a
resolution that could not have happened — and would be indistinguishable from one that did.

**The bound set stays addressable independently of any policy.** Nothing here reads, writes or
joins a policy. C-C7's occurrence derivation consumes a bound set; making the bound set depend on
policies in turn would leave neither constructible first.

**An identical re-capture is free.** The observation is stored under its content hash as well as its
id, so :func:`record_inventory_observation` on an unchanged environment returns the existing
identity rather than minting a second one that means the same thing. The id and capture time still
differ — they are provenance — which is exactly the shape C-B7's two gates require.
"""
from __future__ import annotations

import json

from featuregen.overlay.upload.inventory_revisions import (
    BoundInputSetRevisionV2,
    BoundInputV2,
    GenerationInventoryObservationV1,
)

__all__ = [
    "BindingWithoutInventory",
    "read_bound_input_set",
    "record_bound_input_set",
    "record_inventory_observation",
    "same_identity_observations",
]


class BindingWithoutInventory(ValueError):
    """A bound input set was offered with no inventory observation.

    A distinct exception because the fix is distinct: this is not a malformed binding, it is a
    binding attempted before the environment it binds against was ever looked at.
    """


def record_inventory_observation(
    conn, observation: GenerationInventoryObservationV1, *, captured_at: str,
) -> str:
    """Append an observation and return its id. Idempotent on the OBSERVATION id.

    ``captured_at`` is taken rather than read off the inventory so the caller states when the
    environment was looked at; it is provenance and never enters the content hash.
    """
    inventory = observation.inventory
    conn.execute(
        "INSERT INTO generation_inventory_observation (observation_id, environment_id, "
        "inventory_json, used_schema_refs, read_set, content_hash, captured_at) "
        "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s) "
        "ON CONFLICT (observation_id) DO NOTHING",
        (observation.observation_id, observation.environment_id,
         json.dumps({
             "environment_id": inventory.environment_id,
             "engine_versions": inventory.engine_versions.identity_payload(),
             "logical_schema_map": dict(inventory.logical_schema_map),
             "tables": {key: layout.semantic_payload()
                        for key, layout in inventory.tables.items()},
         }),
         json.dumps(sorted(set(observation.used_logical_schema_refs))),
         json.dumps(sorted(set(observation.read_set))),
         observation.content_hash, captured_at))
    return observation.observation_id


def same_identity_observations(conn, content_hash: str) -> tuple[str, ...]:
    """Every observation that a compilation would treat as the same environment.

    More than one is the ORDINARY case, not a defect: re-capturing an unchanged environment mints a
    new id and a new capture time, and the whole point of C-B7's narrowed hash is that those two do
    not invalidate anything.
    """
    return tuple(row[0] for row in conn.execute(
        "SELECT observation_id FROM generation_inventory_observation WHERE content_hash = %s "
        "ORDER BY recorded_at, observation_id", (content_hash,)).fetchall())


def record_bound_input_set(
    conn, bound: BoundInputSetRevisionV2, *, inventory_observation_id: str,
) -> str:
    """Append a bound input set against the inventory it resolved under.

    Raises:
        BindingWithoutInventory: no observation id was supplied. The database enforces the same
            rule through a foreign key; this is the same refusal reached earlier and by name.
    """
    if not (inventory_observation_id or "").strip():
        raise BindingWithoutInventory(
            "a bound input set was offered with no inventory observation. Binding resolves logical "
            "refs against an environment somebody looked at, so a binding with no observation "
            "describes a resolution that could not have happened — and would be indistinguishable "
            "from one that did")

    conn.execute(
        "INSERT INTO bound_input_set_revision (revision_id, inventory_observation_id, "
        "environment_id, content_hash) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (bound.revision_id, inventory_observation_id, bound.environment_id, bound.content_hash))

    for item in bound.inputs:
        conn.execute(
            "INSERT INTO bound_input (revision_id, logical_ref, physical_dataset, physical_column) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (revision_id, logical_ref) DO NOTHING",
            (bound.revision_id, item.logical_ref, item.physical_dataset, item.physical_column))
    return bound.revision_id


def read_bound_input_set(conn, revision_id: str) -> BoundInputSetRevisionV2 | None:
    """One bound set, reconstructed. Reads no policy table, because it has no policy to read."""
    row = conn.execute(
        "SELECT environment_id FROM bound_input_set_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    if row is None:
        return None
    inputs = conn.execute(
        "SELECT logical_ref, physical_dataset, physical_column FROM bound_input "
        "WHERE revision_id = %s ORDER BY logical_ref", (revision_id,)).fetchall()
    return BoundInputSetRevisionV2(
        revision_id=revision_id, environment_id=row[0],
        inputs=tuple(BoundInputV2(*item) for item in inputs))
