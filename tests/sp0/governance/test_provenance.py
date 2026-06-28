from dataclasses import FrozenInstanceError

import pytest

from sp0.contracts.provenance import ProvenanceEnvelope


def test_provenance_envelope_is_frozen_slotted_and_carries_replay_pins():
    prov = ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=2,
        producing_component="sp2-intake@1.4.0",
        tool_versions={"llm_model": "m@1", "prompt_version": "p@3"},
        dsl_operation_catalog_version="ops@v9",
        source_snapshots=("delta:core.transactions@v8821",),
        event_registry_snapshot="events@v37",
        doc_registry_snapshot="docs@v11",
        evaluation_dataset_ref="doc_eval",
        holdout_partition_spec="oot:2025H2",
        random_seed=42,
        candidates_explored_count=3,
        external_refs=("llm_call:idem_1",),
    )
    assert prov.artifact_type == "CONFIRMED_CONTRACT"
    assert prov.tool_versions["llm_model"] == "m@1"
    assert prov.random_seed == 42
    assert not hasattr(prov, "__dict__")  # slots=True
    with pytest.raises(FrozenInstanceError):
        prov.schema_version = 9  # type: ignore[misc]


def test_provenance_envelope_defaults_are_empty():
    prov = ProvenanceEnvelope(artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="sp0@1")
    assert prov.tool_versions == {}
    assert prov.source_snapshots == ()
    assert prov.event_registry_snapshot is None


def test_provenance_envelope_resolves_to_one_class_across_import_paths():
    # The overview's clearLayers rule: a shared symbol must be ONE class, not duplicated per phase.
    from sp0.contracts import ProvenanceEnvelope as P_pkg
    from sp0.contracts.envelopes import ProvenanceEnvelope as P_env
    from sp0.contracts.provenance import ProvenanceEnvelope as P_mod

    assert P_pkg is P_env is P_mod
