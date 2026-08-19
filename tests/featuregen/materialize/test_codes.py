"""Task 1 — the four CLOSED failure vocabularies (spec §14).

EXACT equality: ``>=`` would permit arbitrary extra codes and therefore would
not test a CLOSED vocabulary at all.
"""
from __future__ import annotations

from featuregen.materialize.codes import (
    CompilationRefusalCode,
    MaterializationRefused,
    PublicationRefusalCode,
    ValidationFindingCode,
    ValidationGateCode,
)


def test_compilation_codes_are_exactly_the_spec_set():
    assert {c.value for c in CompilationRefusalCode} == {
        "AUTHORING_RUN_INCOMPLETE", "TERMINAL_PAYLOAD_TAMPERED", "NOT_RESOLVED",
        "FORMULA_HASH_MISMATCH", "FORMULA_SCHEMA_UNSUPPORTED", "AXES_MISMATCH", "INTENT_HASH_MISMATCH",
        "READ_SCOPE_INSUFFICIENT", "PROHIBITED_INPUT", "COLUMN_NOT_GOVERNED",
        "AMBIGUOUS_TABLE_NAME",
        "JOIN_PATH_NOT_VERIFIED", "JOIN_PATH_DENIED_BY_READ_SCOPE",
        "GRAIN_PATH_NOT_GOVERNED",
    "GRAIN_NOT_RESOLVED",
    "POLICY_REFERENCE_UNRESOLVABLE",
    "CANDIDATE_REGENERATION_REQUIRED", "JOIN_FANOUT_UNSUPPORTED", "JOIN_CARDINALITY_UNKNOWN",
        "SPINE_SOURCE_NOT_DECLARED", "SPINE_DECLARATION_REJECTED_BY_FACTS",
        "PARTITION_MAPPING_NOT_DECLARED", "PHYSICAL_SCHEMA_NOT_RESOLVED", "SOURCE_ENGINE_UNSUPPORTED", "AVAILABILITY_TIME_NOT_GOVERNED",
        "OUTPUT_TYPE_NOT_GOVERNED", "PHYSICAL_TYPE_UNSUPPORTED", "MULTIPLE_MATERIALIZATION_CONTRACTS",
        "PARTITION_IDENTITY_UNKNOWN", "UNACCOUNTED_LOGICAL_REF",
        "PLAN_ENVELOPE_DIVERGENCE"}


def test_publication_codes_are_exactly_the_spec_set():
    """CAPABILITY_UNPROVEN / GROUP_BINDING_CONFLICT are PUBLICATION decisions —
    they are not compilation refusals and not runtime gates."""
    assert {c.value for c in PublicationRefusalCode} == {
        "CAPABILITY_UNPROVEN", "GROUP_BINDING_CONFLICT", "PUBLISH_MECHANISM_UNSUPPORTED"}


def test_gate_codes_are_exactly_the_spec_set():
    assert {c.value for c in ValidationGateCode} == {
        "KEY_NOT_UNIQUE", "MISSING_FEATURE_COLUMN", "UNEXPECTED_COLUMN", "WRONG_COLUMN_TYPE",
        "WRONG_NULLABILITY", "SCHEMA_HASH_MISMATCH", "MISSING_STAGING_MANIFEST",
        "STALE_STAGING_MANIFEST", "DUPLICATE_STAGING_MANIFEST", "IR_HASH_MISMATCH",
        "INCOMPLETE_COMPUTATION", "FORBIDDEN_NUMERIC", "OVERFLOW_VIOLATION",
        "JOIN_AMPLIFICATION",
        "SPINE_INCOMPLETE", "SPINE_DUPLICATE_KEY", "SPINE_NON_DETERMINISTIC",
        "RUN_PARAMETERS_MISSING", "PROJECT_INTEGRITY"}


def test_finding_codes_are_exactly_the_spec_set():
    assert {c.value for c in ValidationFindingCode} == {
        "PROJECT_DOES_NOT_BUILD", "PROJECT_HASH_MISMATCH", "PIPELINE_NOT_CONSTRUCTIBLE",
        "ENGINE_VERSION_MISMATCH",
        "COLUMN_ABSENT", "COLUMN_TYPE_MISMATCH", "PARTITION_ABSENT", "READ_DENIED",
        # SUCCESSOR 5 (2026-08-15): L1's third question was not answered and the deployment had
        # DECLARED that its engine cannot answer it. The only member ever emitted as a WARNING.
        "READ_SCOPE_UNVERIFIED",
        "UNKNOWN_FINDING"}


def test_the_four_enums_do_not_overlap():
    """A code must belong to exactly one vocabulary, or a refusal cannot be typed."""
    sets = [{c.value for c in e} for e in
            (CompilationRefusalCode, PublicationRefusalCode, ValidationGateCode,
             ValidationFindingCode)]
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            assert not (a & b), f"code appears in two enums: {a & b}"


def test_refusal_carries_a_typed_code_not_a_string():
    e = MaterializationRefused(CompilationRefusalCode.NOT_RESOLVED, "detail")
    assert e.code is CompilationRefusalCode.NOT_RESOLVED
    assert not isinstance(e.code, str) or isinstance(e.code, CompilationRefusalCode)


def test_spine_non_determinism_is_a_RUNTIME_gate_not_a_compilation_refusal():
    """An unresolved tie depends on actual rows, so it is discovered during execution."""
    assert "SPINE_NON_DETERMINISTIC" not in {c.value for c in CompilationRefusalCode}
    assert "SPINE_NON_DETERMINISTIC" in {c.value for c in ValidationGateCode}
