"""Attested data types — `graph_node.data_type` upgraded ONLY from a real engine read (Task 7).

The live defect this closes: every column on the deployed catalogs holds `data_type='unknown'`
with only a glossary-declared type, so every downstream `type_basis` derivation
(`bridge_candidates._resolve_family`: `data_type` first, `declared_type` fallback) reports the
weaker `declared` basis. This module is the ONE honest way up:

* :func:`attest_types_from_observation` takes a :class:`SchemaObservationResultV1` — the typed
  result of an engine schema read (`DESCRIBE` / information_schema, via the data-agent executor)
  — and fills `data_type` where it is `unknown`. A glossary `declared_type` NEVER travels into
  the attested slot: it is not an input to any write here, so a declared-only column cannot
  launder its own answer into looking engine-attested.
* Provenance per the module conventions: one `field_evidence` row per engine claim
  (`field_name='data_type'`, producer `structural_connector`, strength `attested`,
  `producer_ref` = the content-addressed observation ref), with producer-scoped staleness and
  input-hash reuse (`_write_producer_field` shape) so an unchanged re-run re-writes nothing.
* A re-run whose engine type CHANGED does not overwrite — the stored value stands and the
  divergence is a typed :class:`TypeAttestConflict` in the report, drift handling's input. The
  same applies to an upload-attested operational type the engine disagrees with: correction is
  never mutation of an uploader's declaration.
* A catalog column absent from the physical table stays `unknown`, the absence recorded
  (`report.absent`); a physical column the catalog does not know is reported
  (`report.physical_only`), never invented as a node.
* An incomplete observation attests NOTHING — a failed read cannot distinguish "absent" from
  "unread", so acting on it would record absences that are actually engine errors.

The upgrade is guarded in SQL (fill-only-unknown, the `axis_projection._fill` discipline), so a
re-run — or a concurrent writer — cannot double-apply, and `data_type` feeds no `search_doc`
slot, so no rebuild rides along.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from featuregen.data_agent.results import SchemaObservationResultV1
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
    stale_source_evidence,
)
from featuregen.overlay.upload.object_ref import normalize_ref

logger = logging.getLogger(__name__)

#: The `data_type` spellings that mean "nothing attested yet" — the ONLY values an engine
#: observation may replace. Everything else is somebody's attestation and is conflict-guarded.
_UNKNOWN_VALUES = ("", "unknown")


def _is_unknown(value: object) -> bool:
    return value is None or str(value).strip().lower() in _UNKNOWN_VALUES


def _normalized(value: object) -> str:
    """Case/whitespace-insensitive comparison key. Deliberately EXACT beyond that — `varchar` and
    `varchar(150)` are different engine claims, and papering over a parameter change here would
    hide exactly the drift the conflict path exists to surface (family-level tolerance belongs to
    `resolve_type_family`, the read side)."""
    return str(value or "").strip().lower()


def observation_ref(observation: SchemaObservationResultV1) -> str:
    """The content-addressed ref provenance cites for one schema observation.

    Deterministic over WHAT was observed (physical id + the name->type mapping), so re-recording
    an identical observation cites the same ref — which is what lets the evidence layer's
    input-hash reuse keep an unchanged re-run write-free — while any engine change mints a new
    one."""
    payload = {
        "physical_id": observation.physical_id,
        "columns": sorted(
            (column, engine_type)
            for column, engine_type in observation.types_by_column().items()
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"schema-obs:{observation.physical_id}@{digest[:16]}"


@dataclass(frozen=True, slots=True)
class TypeAttestConflict:
    """One engine/stored divergence — drift handling's input, never resolved by overwriting."""

    object_ref: str
    column: str
    stored_type: str
    observed_type: str
    observation_ref: str


@dataclass(frozen=True, slots=True)
class TypeAttestReport:
    """What one attestation run did, per column, as object_refs — the provenance record the stage
    detail summarizes. `absent` is the honesty list: catalog columns the physical table does not
    carry, which therefore STAY `unknown` (their declared value is not a fallback here)."""

    observation_ref: str
    upgraded: tuple[str, ...] = ()
    corroborated: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    physical_only: tuple[str, ...] = ()
    conflicts: tuple[TypeAttestConflict, ...] = ()
    skipped_reason: str | None = None

    def stage_detail(self) -> dict:
        """A SMALL stage-detail dict (#22 house rule: counts, bounded lists, never row data)."""
        detail: dict = {
            "observation_ref": self.observation_ref,
            "upgraded": len(self.upgraded),
            "corroborated": len(self.corroborated),
        }
        if self.absent:
            detail["absent_count"] = len(self.absent)
            detail["absent"] = list(self.absent[:50])
        if self.physical_only:
            detail["physical_only_count"] = len(self.physical_only)
            detail["physical_only"] = list(self.physical_only[:50])
        if self.conflicts:
            detail["conflict_count"] = len(self.conflicts)
            detail["conflicts"] = [
                {"object_ref": c.object_ref, "stored_type": c.stored_type,
                 "observed_type": c.observed_type}
                for c in self.conflicts[:50]
            ]
        if self.skipped_reason:
            detail["skipped_reason"] = self.skipped_reason
        return detail


def _record_engine_claim(conn, *, logical_ref: str, engine_type: str, obs_ref: str,
                         column: str) -> None:
    """One engine claim into `field_evidence`, with producer-scoped staleness + input-hash reuse
    (the `_write_producer_field` shape). Staling is scoped to the STRUCTURAL_CONNECTOR producer's
    own `data_type` rows, so a changed engine claim retires only its predecessor — never a
    source/human row. Advisory: a failed evidence write never aborts the attestation."""
    input_hash = field_input_hash(
        logical_ref=logical_ref, field_name="data_type",
        material={"engine_type": _normalized(engine_type)})
    try:
        with conn.transaction():   # savepoint: contain a failed write without poisoning the txn
            stale_source_evidence(
                conn, logical_ref=logical_ref, field_name="data_type",
                producer=EvidenceProducer.STRUCTURAL_CONNECTOR, keep_input_hash=input_hash)
            reused = any(
                e.producer == EvidenceProducer.STRUCTURAL_CONNECTOR.value
                and e.input_hash == input_hash
                for e in read_active_field_evidence(conn, logical_ref, "data_type"))
            if not reused:
                record_field_evidence(
                    conn, logical_ref=logical_ref, field_name="data_type",
                    proposed_value=engine_type,
                    producer=EvidenceProducer.STRUCTURAL_CONNECTOR,
                    strength=AssertionStrength.ATTESTED,
                    producer_ref=obs_ref, producer_item_ref=column,
                    source_snapshot_id=obs_ref, input_hash=input_hash)
    except Exception:  # noqa: BLE001 — advisory: provenance must not veto the honest upgrade
        logger.warning("advisory data_type field_evidence write failed for %s", logical_ref,
                       exc_info=True)


def attest_types_from_observation(
    conn, *, source: str, table: str, observation: SchemaObservationResultV1,
) -> TypeAttestReport:
    """Upgrade one table's `graph_node.data_type` from ONE engine schema observation (module
    docstring has the rules). Idempotent: the upgrade is a guarded fill-only-unknown UPDATE and
    the evidence write reuses on an unchanged input, so re-running with the same observation
    changes nothing and reports `corroborated`."""
    obs_ref = observation_ref(observation)
    if not observation.complete:
        return TypeAttestReport(
            observation_ref=obs_ref, skipped_reason="observation_incomplete")

    engine_types = observation.types_by_column()
    rows = conn.execute(
        "SELECT object_ref, column_name, data_type FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s "
        "ORDER BY object_ref",
        (source, table)).fetchall()

    upgraded: list[str] = []
    corroborated: list[str] = []
    absent: list[str] = []
    conflicts: list[TypeAttestConflict] = []
    catalog_columns: set[str] = set()

    for object_ref, column_name, data_type in rows:
        key = _normalized(column_name)
        catalog_columns.add(key)
        engine_type = engine_types.get(key)
        if not engine_type:
            # Physically absent (or reported typeless, which attests nothing): the column STAYS
            # `unknown` — its declared value is a display fallback elsewhere, never a source here.
            absent.append(object_ref)
            continue
        logical_ref = normalize_ref(source, "public", table, column_name)
        if _is_unknown(data_type):
            updated = conn.execute(
                "UPDATE graph_node SET data_type = %s "
                "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column' "
                "AND (data_type IS NULL OR lower(btrim(data_type)) IN ('', 'unknown')) "
                "RETURNING object_ref",
                (engine_type, source, object_ref)).fetchone()
            if updated is not None:
                upgraded.append(object_ref)
            else:
                # A concurrent writer filled it first; the guard held. Reported honestly.
                corroborated.append(object_ref)
            _record_engine_claim(conn, logical_ref=logical_ref, engine_type=engine_type,
                                 obs_ref=obs_ref, column=key)
        elif _normalized(data_type) == _normalized(engine_type):
            corroborated.append(object_ref)
            _record_engine_claim(conn, logical_ref=logical_ref, engine_type=engine_type,
                                 obs_ref=obs_ref, column=key)
        else:
            # A CHANGED engine type (or an engine/upload disagreement) is drift, not a write. The
            # divergent claim still lands in evidence — append-only history is how drift handling
            # later sees when the engine's answer moved.
            conflicts.append(TypeAttestConflict(
                object_ref=object_ref, column=str(column_name),
                stored_type=str(data_type), observed_type=engine_type,
                observation_ref=obs_ref))
            _record_engine_claim(conn, logical_ref=logical_ref, engine_type=engine_type,
                                 obs_ref=obs_ref, column=key)

    physical_only = tuple(sorted(k for k in engine_types if k not in catalog_columns))
    return TypeAttestReport(
        observation_ref=obs_ref,
        upgraded=tuple(upgraded),
        corroborated=tuple(corroborated),
        absent=tuple(absent),
        physical_only=physical_only,
        conflicts=tuple(conflicts))
