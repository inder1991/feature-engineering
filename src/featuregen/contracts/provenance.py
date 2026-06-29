from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProvenanceEnvelope:
    """Reproducibility envelope on every event/document (§8)."""

    artifact_type: str  # matches the §3.7 stage/artifact enum casing
    schema_version: int
    producing_component: str  # "sp2-intake@1.4.0"
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: str | None = None
    source_snapshots: tuple[str, ...] = ()
    event_registry_snapshot: str | None = None  # pinned snapshot id (replay determinism)
    doc_registry_snapshot: str | None = None
    evaluation_dataset_ref: str | None = None
    holdout_partition_spec: str | None = None
    random_seed: int | None = None
    candidates_explored_count: int | None = None
    external_refs: tuple[str, ...] = ()
