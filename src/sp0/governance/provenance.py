from __future__ import annotations

import re
from typing import Mapping, Optional

from sp0.contracts.provenance import ProvenanceEnvelope

_REF_RE = re.compile(r"^[^\s:]+:[^\s]+$")  # a reference id token "kind:id" — never an inline body (§9)


class ProvenanceError(Exception):
    """Raised when a ProvenanceEnvelope is not well-formed for audit/replay (§8/§9)."""


def build_provenance(
    *,
    artifact_type: str,
    schema_version: int,
    producing_component: str,
    llm_model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    validator: Optional[str] = None,
    compiler: Optional[str] = None,
    tool_versions: Optional[Mapping[str, str]] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    source_snapshots: tuple[str, ...] = (),
    event_registry_snapshot: Optional[str] = None,
    doc_registry_snapshot: Optional[str] = None,
    evaluation_dataset_ref: Optional[str] = None,
    holdout_partition_spec: Optional[str] = None,
    random_seed: Optional[int] = None,
    candidates_explored_count: Optional[int] = None,
    external_refs: tuple[str, ...] = (),
) -> ProvenanceEnvelope:
    merged: dict[str, str] = dict(tool_versions or {})
    for key, value in (
        ("llm_model", llm_model), ("prompt_version", prompt_version),
        ("validator", validator), ("compiler", compiler),
    ):
        if value is not None:
            merged[key] = value
    return ProvenanceEnvelope(
        artifact_type=artifact_type,
        schema_version=schema_version,
        producing_component=producing_component,
        tool_versions=merged,
        dsl_operation_catalog_version=dsl_operation_catalog_version,
        source_snapshots=source_snapshots,
        event_registry_snapshot=event_registry_snapshot,
        doc_registry_snapshot=doc_registry_snapshot,
        evaluation_dataset_ref=evaluation_dataset_ref,
        holdout_partition_spec=holdout_partition_spec,
        random_seed=random_seed,
        candidates_explored_count=candidates_explored_count,
        external_refs=external_refs,
    )


def validate_provenance(prov: ProvenanceEnvelope, *, require_replay_pins: bool = False) -> None:
    if not prov.artifact_type:
        raise ProvenanceError("artifact_type is required")
    if not prov.producing_component:
        raise ProvenanceError("producing_component is required")
    if prov.schema_version <= 0:
        raise ProvenanceError("schema_version must be > 0")
    for ref in prov.external_refs:
        if not _REF_RE.match(ref):
            raise ProvenanceError(f"external_ref {ref!r} must be a 'kind:id' reference, not inline content (§9)")
    if require_replay_pins and not (prov.event_registry_snapshot and prov.doc_registry_snapshot):
        raise ProvenanceError("replay determinism requires event_registry_snapshot and doc_registry_snapshot (§8)")
