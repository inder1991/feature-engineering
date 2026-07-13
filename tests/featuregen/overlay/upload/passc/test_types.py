from featuregen.overlay.upload.passc.types import (
    NamespaceCompatibility, CardinalityInferenceStatus, SignalEvidence,
    JoinCandidateEvidenceV1, DEFAULT_CONFIG, CONFIG_VERSION, ALGORITHM_VERSION)
def test_enums_and_config_defaults():
    assert NamespaceCompatibility.COMPATIBLE == "compatible"
    assert set(NamespaceCompatibility) >= {NamespaceCompatibility.COMPATIBLE, NamespaceCompatibility.POSSIBLE,
        NamespaceCompatibility.AMBIGUOUS, NamespaceCompatibility.INCOMPATIBLE}
    assert DEFAULT_CONFIG.strong_threshold == 80 and DEFAULT_CONFIG.weak_threshold == 50
    assert "amount" in DEFAULT_CONFIG.negative_concepts and "date" in DEFAULT_CONFIG.negative_concepts
    assert DEFAULT_CONFIG.weights["same_identifier_concept"] == 40
    assert "namespace_ambiguous" not in DEFAULT_CONFIG.weights   # AMBIGUOUS/INCOMPATIBLE are gated out, not scored
    assert CONFIG_VERSION and ALGORITHM_VERSION
def test_evidence_asdict_round_trip():
    import dataclasses
    ev = JoinCandidateEvidenceV1(candidate_id="c1", from_ref="a", to_ref="b", column_pairs=(("cif_id","cif_id"),),
        proposed_direction="N:1", proposed_cardinality="N:1",
        cardinality_status=CardinalityInferenceStatus.INFERRED_FROM_CONFIRMED_GRAIN, bucket="strong", score=95,
        positive_signals=(), negative_signals=(), namespace_compatibility=NamespaceCompatibility.COMPATIBLE,
        namespace_reason_codes=("same_column_entity",), grain_evidence=(), missing_requirements=(),
        llm_annotations=(), explanation="…", producer="deterministic_pass_c", config_version=CONFIG_VERSION,
        candidate_algorithm_version=ALGORITHM_VERSION, source_snapshot_id="snap")
    assert dataclasses.asdict(ev)["score"] == 95
