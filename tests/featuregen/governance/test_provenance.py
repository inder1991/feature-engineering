from dataclasses import FrozenInstanceError

import pytest

from featuregen.contracts.provenance import ProvenanceEnvelope


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
    prov = ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="featuregen@1"
    )
    assert prov.tool_versions == {}
    assert prov.source_snapshots == ()
    assert prov.event_registry_snapshot is None


def test_provenance_envelope_resolves_to_one_class_across_import_paths():
    # The overview's clearLayers rule: a shared symbol must be ONE class, not duplicated per phase.
    from featuregen.contracts import ProvenanceEnvelope as P_pkg
    from featuregen.contracts.envelopes import ProvenanceEnvelope as P_env
    from featuregen.contracts.provenance import ProvenanceEnvelope as P_mod

    assert P_pkg is P_env is P_mod


from featuregen.governance.provenance import ProvenanceError, build_provenance, validate_provenance


def test_build_provenance_folds_named_tool_versions():
    prov = build_provenance(
        artifact_type="EVALUATION_REPORT",
        schema_version=2,
        producing_component="sp6-eval@2.0.0",
        llm_model="m@1",
        prompt_version="p@3",
        validator="iv@1",
        compiler="dsl@9",
        event_registry_snapshot="events@v37",
        doc_registry_snapshot="docs@v11",
        random_seed=7,
        candidates_explored_count=5,
        external_refs=("sandbox_run:job_9",),
    )
    assert prov.tool_versions == {
        "llm_model": "m@1",
        "prompt_version": "p@3",
        "validator": "iv@1",
        "compiler": "dsl@9",
    }
    assert prov.candidates_explored_count == 5
    validate_provenance(prov)  # well-formed => no raise


def test_validate_provenance_requires_component_and_positive_schema_version():
    with pytest.raises(ProvenanceError):
        validate_provenance(
            ProvenanceEnvelope(artifact_type="X", schema_version=1, producing_component="")
        )
    with pytest.raises(ProvenanceError):
        validate_provenance(
            ProvenanceEnvelope(artifact_type="X", schema_version=0, producing_component="c")
        )


def test_validate_provenance_rejects_inline_external_refs_and_missing_replay_pins():
    inline = ProvenanceEnvelope(
        artifact_type="X",
        schema_version=1,
        producing_component="c",
        external_refs=("this is a raw inline body, not a ref",),
    )
    with pytest.raises(ProvenanceError):
        validate_provenance(inline)
    no_pins = ProvenanceEnvelope(artifact_type="X", schema_version=1, producing_component="c")
    with pytest.raises(ProvenanceError):
        validate_provenance(no_pins, require_replay_pins=True)
