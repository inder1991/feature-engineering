from __future__ import annotations

from typing import Any, Mapping

from featuregen.contracts import EventEnvelope, IdentityEnvelope, ProvenanceEnvelope


def identity_to_jsonb(idv: IdentityEnvelope) -> dict[str, Any]:
    return {
        "subject": idv.subject,
        "actor_kind": idv.actor_kind,
        "authenticated": idv.authenticated,
        "auth_method": idv.auth_method,
        "role_claims": list(idv.role_claims),
        "groups": list(idv.groups),
        "tenant": idv.tenant,
        "on_behalf_of": idv.on_behalf_of,
        "impersonation": idv.impersonation,
        "break_glass": idv.break_glass,
        "source_of_authority": idv.source_of_authority,
        "attestation": idv.attestation,
    }


def identity_from_jsonb(d: Mapping[str, Any]) -> IdentityEnvelope:
    return IdentityEnvelope(
        subject=d["subject"],
        actor_kind=d["actor_kind"],
        authenticated=d["authenticated"],
        auth_method=d["auth_method"],
        role_claims=tuple(d.get("role_claims", ())),
        groups=tuple(d.get("groups", ())),
        tenant=d.get("tenant"),
        on_behalf_of=d.get("on_behalf_of"),
        impersonation=d.get("impersonation"),
        break_glass=d.get("break_glass", False),
        source_of_authority=d.get("source_of_authority"),
        attestation=d.get("attestation"),
    )


def provenance_to_jsonb(p: ProvenanceEnvelope) -> dict[str, Any]:
    return {
        "artifact_type": p.artifact_type,
        "schema_version": p.schema_version,
        "producing_component": p.producing_component,
        "tool_versions": dict(p.tool_versions),
        "dsl_operation_catalog_version": p.dsl_operation_catalog_version,
        "source_snapshots": list(p.source_snapshots),
        "event_registry_snapshot": p.event_registry_snapshot,
        "doc_registry_snapshot": p.doc_registry_snapshot,
        "evaluation_dataset_ref": p.evaluation_dataset_ref,
        "holdout_partition_spec": p.holdout_partition_spec,
        "random_seed": p.random_seed,
        "candidates_explored_count": p.candidates_explored_count,
        "external_refs": list(p.external_refs),
    }


def provenance_from_jsonb(d: Mapping[str, Any]) -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type=d["artifact_type"],
        schema_version=d["schema_version"],
        producing_component=d["producing_component"],
        tool_versions=dict(d.get("tool_versions", {})),
        dsl_operation_catalog_version=d.get("dsl_operation_catalog_version"),
        source_snapshots=tuple(d.get("source_snapshots", ())),
        event_registry_snapshot=d.get("event_registry_snapshot"),
        doc_registry_snapshot=d.get("doc_registry_snapshot"),
        evaluation_dataset_ref=d.get("evaluation_dataset_ref"),
        holdout_partition_spec=d.get("holdout_partition_spec"),
        random_seed=d.get("random_seed"),
        candidates_explored_count=d.get("candidates_explored_count"),
        external_refs=tuple(d.get("external_refs", ())),
    )


def row_to_event(row: Mapping[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=row["event_id"],
        global_seq=row["global_seq"],
        aggregate=row["aggregate"],
        aggregate_id=row["aggregate_id"],
        stream_version=row["stream_version"],
        type=row["type"],
        schema_version=row["schema_version"],
        table_version=row["table_version"],
        actor=identity_from_jsonb(row["actor"]),
        payload=row["payload"],
        provenance=provenance_from_jsonb(row["provenance"]),
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        request_id=row["request_id"],
        feature_id=row["feature_id"],
        run_id=row["run_id"],
        caused_by=row["caused_by"],
    )
