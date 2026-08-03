"""Temporal-policy persistence (profile plan Task 7; migration 1049).

Immutable ``dataset_temporal_policy_revision`` rows + a CAS ``dataset_temporal_policy_current``
pointer, keyed on ``dataset_logical_ref``. Same discipline as
:mod:`featuregen.overlay.upload.serving_policy_store` — read that module's docstring for the CAS
template, the read-back verification and the body-carried ``expected_pointer_version`` convention;
this module adds ONE thing.

**The agreement rule, enforced at authoring (Task 7 item / D12.7).**
:func:`publish_temporal_policy` resolves the dataset's CURRENT effective profile and:

* refuses when a LOAD-BEARING ``temporal_storage_model`` exists and CONTRADICTS the policy — a
  policy may not overrule a governed classification, because that classification is what the
  execution gates read;
* ACCEPTS when the profile value is absent or merely PROPOSED, in which case the POLICY ITSELF is
  the operational declaration. This is the case that keeps an upload-only catalog working: nothing
  ever attested its temporal model, and the alternative (promoting the LLM proposal to
  load-bearing) is exactly what the authority engine exists to prevent. The revision's provenance
  records WHO declared it; the platform-admin route is what gives it operational effect.

Every logical column ref the policy names is validated to exist and be readable by the author
before any write.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    PolicyStoreConflict,
    SelectionError,
    assert_column_refs_readable,
    assert_dataset_refs_readable,
    provenance_from_payload,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
    contradicts_load_bearing_model,
)


@dataclass(frozen=True, slots=True)
class TemporalPolicyCurrentV1:
    """The CAS current pointer for one dataset's temporal policy."""

    dataset_logical_ref: str
    revision_id: str
    pointer_version: int
    declared_by: str


def record_temporal_policy_revision(conn: DbConn, revision: DatasetTemporalPolicyRevisionV1, *,
                                    authored_by: str) -> str:
    """Append ``revision`` if absent (idempotent on identical content) and return its id."""
    if not authored_by or not authored_by.strip():
        raise PolicyStoreConflict("a policy revision must record who authored it")
    conn.execute(
        "INSERT INTO dataset_temporal_policy_revision ("
        "  revision_id, dataset_logical_ref, temporal_storage_model, current_selection,"
        "  historical_selection, effective_from_ref, effective_to_ref, snapshot_ref,"
        "  current_flag_ref, availability_ref, tie_break_refs, provenance, content_hash,"
        "  authored_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.dataset_logical_ref,
         revision.temporal_storage_model.value, revision.current_selection.value,
         revision.historical_selection.value, revision.effective_from_ref,
         revision.effective_to_ref, revision.snapshot_ref, revision.current_flag_ref,
         revision.availability_ref, Jsonb(list(revision.tie_break_refs)),
         Jsonb(revision.provenance.payload()), revision.content_hash, authored_by.strip()))
    if load_temporal_policy_revision(conn, revision.revision_id) is None:
        raise PolicyStoreConflict(
            f"temporal policy revision {revision.revision_id} did not persist")
    return revision.revision_id


def set_current_temporal_policy(conn: DbConn, *, dataset_logical_ref: str, revision_id: str,
                                expected_pointer_version: int, declared_by: str) -> int:
    """Advance the CAS current pointer and return the NEW pointer version."""
    if expected_pointer_version < 0:
        raise PolicyStoreConflict("expected_pointer_version cannot be negative")
    if not declared_by or not declared_by.strip():
        raise PolicyStoreConflict("advancing a policy pointer must record who declared it")
    if expected_pointer_version == 0:
        changed = conn.execute(
            "INSERT INTO dataset_temporal_policy_current "
            "  (dataset_logical_ref, revision_id, pointer_version, declared_by) "
            "VALUES (%s,%s,1,%s) ON CONFLICT (dataset_logical_ref) DO NOTHING",
            (dataset_logical_ref, revision_id, declared_by.strip())).rowcount
    else:
        changed = conn.execute(
            "UPDATE dataset_temporal_policy_current SET revision_id = %s, "
            "  pointer_version = pointer_version + 1, declared_by = %s, updated_at = now() "
            "WHERE dataset_logical_ref = %s AND pointer_version = %s",
            (revision_id, declared_by.strip(), dataset_logical_ref,
             expected_pointer_version)).rowcount
    if changed != 1:
        raise PolicyStoreConflict(
            f"temporal policy pointer for {dataset_logical_ref!r} advanced past version "
            f"{expected_pointer_version}")
    return expected_pointer_version + 1


def load_bearing_temporal_model(conn: DbConn, *, source: str,
                                dataset_logical_ref: str) -> tuple[str | None, str | None]:
    """``(load_bearing, displayed)`` temporal storage model for one dataset, from the effective
    profile. ``(None, None)`` when the dataset has no profile at all.

    Read through ``dataset_profiles.build_dataset_profile`` rather than the ``graph_node``
    projection column: the projection is rebuildable display state, and an authoring gate must read
    the resolver's answer, not a cache of it."""
    from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
    from featuregen.overlay.upload.profile_store import current_catalog_profile_revision_id

    profile = build_dataset_profile(
        conn, source=source, dataset_logical_ref=dataset_logical_ref,
        catalog_profile_revision_id=current_catalog_profile_revision_id(conn, source))
    if profile is None:
        return None, None
    field = profile.temporal_storage_model
    return (field.load_bearing.value if field.load_bearing is not None else None,
            field.display.value if field.display is not None else None)


def publish_temporal_policy(conn: DbConn, revision: DatasetTemporalPolicyRevisionV1, *,
                            expected_pointer_version: int, actor: str,
                            roles: Iterable[str] = ()) -> tuple[str, int]:
    """Validate refs + the agreement rule, append the revision, advance the pointer — one
    transaction, in that order (nothing is written when a validation refuses)."""
    source = revision.dataset_logical_ref.split("::", 1)[0]
    assert_dataset_refs_readable(conn, (revision.dataset_logical_ref,), roles=roles)
    assert_column_refs_readable(conn, revision.column_refs(), roles=roles)

    load_bearing, _displayed = load_bearing_temporal_model(
        conn, source=source, dataset_logical_ref=revision.dataset_logical_ref)
    if contradicts_load_bearing_model(policy_model=revision.temporal_storage_model,
                                      load_bearing_model=load_bearing):
        raise SelectionError(
            f"the governed temporal storage model for {revision.dataset_logical_ref!r} is "
            f"{load_bearing!r}; a policy declaring "
            f"{revision.temporal_storage_model.value!r} would overrule the classification the "
            "execution gates read. Correct the profile field first (four-eyes), or declare a "
            "policy that agrees with it")
    revision_id = record_temporal_policy_revision(conn, revision, authored_by=actor)
    version = set_current_temporal_policy(
        conn, dataset_logical_ref=revision.dataset_logical_ref, revision_id=revision_id,
        expected_pointer_version=expected_pointer_version, declared_by=actor)
    return revision_id, version


def current_temporal_policy(
    conn: DbConn, dataset_logical_ref: str,
) -> tuple[DatasetTemporalPolicyRevisionV1, TemporalPolicyCurrentV1] | None:
    """Resolve the CURRENT temporal policy (revision + pointer) for one dataset, content-verified."""
    row = conn.execute(
        "SELECT revision_id, pointer_version, declared_by FROM dataset_temporal_policy_current "
        "WHERE dataset_logical_ref = %s", (dataset_logical_ref,)).fetchone()
    if row is None:
        return None
    revision = load_temporal_policy_revision(conn, row[0])
    if revision is None:
        raise PolicyStoreConflict(
            f"dataset_temporal_policy_current for {dataset_logical_ref!r} points at missing "
            f"revision {row[0]}")
    return revision, TemporalPolicyCurrentV1(
        dataset_logical_ref=dataset_logical_ref, revision_id=row[0], pointer_version=row[1],
        declared_by=row[2])


def load_temporal_policy_revision(conn: DbConn,
                                  revision_id: str) -> DatasetTemporalPolicyRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision; corruption raises rather than being served."""
    row = conn.execute(
        "SELECT dataset_logical_ref, temporal_storage_model, current_selection, "
        "       historical_selection, effective_from_ref, effective_to_ref, snapshot_ref, "
        "       current_flag_ref, availability_ref, tie_break_refs, provenance, content_hash "
        "FROM dataset_temporal_policy_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    revision = DatasetTemporalPolicyRevisionV1(
        dataset_logical_ref=row[0], temporal_storage_model=TemporalStorageModel(row[1]),
        current_selection=TemporalSelectionKind(row[2]),
        historical_selection=TemporalSelectionKind(row[3]),
        effective_from_ref=row[4], effective_to_ref=row[5], snapshot_ref=row[6],
        current_flag_ref=row[7], availability_ref=row[8], tie_break_refs=tuple(row[9]),
        provenance=provenance_from_payload(row[10]))
    recomputed = materialize_hash(revision.content_payload())
    if recomputed != row[11] or revision.revision_id != revision_id:
        raise PolicyStoreConflict(
            f"temporal policy revision {revision_id} fails content verification")
    return revision
