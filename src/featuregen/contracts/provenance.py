from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True, slots=True)
class ProvenanceEnvelope:
    """Reproducibility envelope on every event/document (§8)."""

    artifact_type: str  # matches the §3.7 stage/artifact enum casing
    schema_version: int
    producing_component: str  # "sp2-intake@1.4.0"
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: Optional[str] = None
    source_snapshots: tuple[str, ...] = ()
    event_registry_snapshot: Optional[str] = None  # pinned snapshot id (replay determinism)
    doc_registry_snapshot: Optional[str] = None
    evaluation_dataset_ref: Optional[str] = None
    holdout_partition_spec: Optional[str] = None
    random_seed: Optional[int] = None
    candidates_explored_count: Optional[int] = None
    external_refs: tuple[str, ...] = ()
