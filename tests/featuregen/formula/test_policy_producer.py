"""C-C11 — the policy producer's seam: registered schema, bounded input, closed taxonomy.

The plan's gate is *"the schema is registered and an unknown field fails closed"*, so the first
section resolves the schema through the SAME function the dispatch path uses rather than reading
the dict, and checks `additionalProperties` at every object level.
"""
from __future__ import annotations

import pytest

from featuregen.formula.policy_occurrences import PolicyOccurrenceV1
from featuregen.formula.policy_producer import (
    MAX_SAMPLED_VALUES,
    POLICY_REALIZATION_SCHEMA_ID,
    POLICY_REALIZATION_SCHEMA_VERSION,
    PolicyProducerInputV1,
    PolicyProducerRefused,
    SampledValueV1,
    logical_call_ref_for,
    realization_from_payload,
    sampled_values_from,
    semantic_tokens_for,
    validate_policy_realization,
)
from featuregen.formula.policy_realization import RealizationProvenanceV1
from featuregen.overlay.upload.enrich_llm import (
    SchemaUnregisteredError,
    canonical_output_schema,
)

DATASET = "hdfc::public.transactions"


def _occurrence(role: str = "direction", kind: str = "direction_sign") -> PolicyOccurrenceV1:
    return PolicyOccurrenceV1(
        expr_path="body.expr", policy_ref_field=f"{role}_policy_ref", policy_kind=kind,
        policy_ref=f"{kind}:foundation", semantic_role=role, bound_dataset=DATASET,
        bound_column=f"{DATASET}.txn_amt", environment_id="hdfc-local")


def _payload(**overrides):
    payload = {
        "policy_column_ref": f"{DATASET}.dr_cr_flag",
        "value_map": [{"semantic_value": "debit", "physical_value": "D"},
                      {"semantic_value": "credit", "physical_value": "C"}],
        "evidence_refs": ["profile:dr_cr_flag/distinct"],
    }
    payload.update(overrides)
    return payload


# ══ THE GATE — registered, and unknown fields fail closed ════════════════════════════════════════
def test_the_schema_is_REGISTERED_and_resolvable_through_the_dispatch_path():
    """Resolved through `canonical_output_schema`, the same function dispatch uses — reading
    `_SCHEMAS` directly would prove the dict has a key, not that a call can resolve it."""
    schema = canonical_output_schema(POLICY_REALIZATION_SCHEMA_ID,
                                     POLICY_REALIZATION_SCHEMA_VERSION)
    assert set(schema["required"]) == {"policy_column_ref", "value_map", "evidence_refs"}


def test_an_unregistered_version_still_fails_LOUD():
    with pytest.raises(SchemaUnregisteredError):
        canonical_output_schema(POLICY_REALIZATION_SCHEMA_ID, 99)


def test_UNKNOWN_FIELDS_FAIL_CLOSED_AT_EVERY_LEVEL():
    """A model that invents `confidence` or `notes` is refused rather than having the extra
    silently dropped — a field nobody declared is a field nobody governs."""
    schema = canonical_output_schema(POLICY_REALIZATION_SCHEMA_ID,
                                     POLICY_REALIZATION_SCHEMA_VERSION)

    def _objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from _objects(value)
        elif isinstance(node, list):
            for item in node:
                yield from _objects(item)

    objects = list(_objects(schema))
    assert len(objects) == 2, "the top level and each value_map entry"
    for obj in objects:
        assert obj["additionalProperties"] is False


def test_the_schema_projects_to_an_anthropic_compatible_wire_schema():
    """`register_enrichment_schemas` asserts this over EVERY schema at bootstrap, so a
    projection-hostile node here would fail every live structured call closed, not just this one."""
    from featuregen.intake.schema_projection import (
        assert_schemas_provider_compatible,
        project_for_anthropic,
    )

    schema = canonical_output_schema(POLICY_REALIZATION_SCHEMA_ID,
                                     POLICY_REALIZATION_SCHEMA_VERSION)
    assert_schemas_provider_compatible([(POLICY_REALIZATION_SCHEMA_ID,
                                         project_for_anthropic(schema))])


# ══ the bounded input contract ═══════════════════════════════════════════════════════════════════
def test_only_the_declared_fields_egress():
    """A whitelist, not a redaction: widening it is a diff someone reviews."""
    payload = PolicyProducerInputV1(
        occurrence=_occurrence(), column_logical_type="string",
        sampled_values=(SampledValueV1("D", 4_200_000), SampledValueV1("C", 3_900_000)),
    ).egress_payload()

    assert set(payload) == {"policy_kind", "semantic_role", "bound_column", "column_logical_type",
                            "legal_semantic_values", "sampled_values"}
    assert payload["legal_semantic_values"] == ["credit", "debit"]
    assert payload["sampled_values"] == [{"value": "D", "row_count": 4_200_000},
                                         {"value": "C", "row_count": 3_900_000}]


def test_a_sample_larger_than_a_VOCABULARY_is_refused():
    """The line between a vocabulary and a data export, drawn as a number."""
    too_many = tuple(SampledValueV1(f"v{i}", 1) for i in range(MAX_SAMPLED_VALUES + 1))
    with pytest.raises(ValueError, match="starts being a data export"):
        PolicyProducerInputV1(occurrence=_occurrence(), column_logical_type="string",
                              sampled_values=too_many)


def test_repeated_sample_values_are_refused():
    with pytest.raises(ValueError, match="repeats a value"):
        PolicyProducerInputV1(
            occurrence=_occurrence(), column_logical_type="string",
            sampled_values=(SampledValueV1("D", 1), SampledValueV1("D", 2)))


def test_samples_are_ordered_so_the_payload_is_STABLE():
    """Otherwise the request-identity hash moves because two identical queries returned rows in
    different orders — a reason that is not about the data."""
    assert sampled_values_from([("C", 10), ("D", 99), ("X", 10)]) == (
        SampledValueV1("D", 99), SampledValueV1("C", 10), SampledValueV1("X", 10))


def test_a_role_with_no_token_vocabulary_is_REFUSED():
    """`reversal` and `currency_conversion` are not realized as a token map; accepting one would
    invent a taxonomy for a policy that does not have one."""
    with pytest.raises(PolicyProducerRefused, match="not realized as a token map"):
        semantic_tokens_for(_occurrence(role="reversal", kind="reversal_correction"))


# ══ the closed taxonomy, inside the repair loop ══════════════════════════════════════════════════
def test_a_valid_map_passes():
    validate_policy_realization(_occurrence())(_payload())


def test_A_PHYSICAL_LITERAL_ON_THE_LEFT_INVERTS_THE_MAP_and_refuses():
    """The rule JSON Schema cannot state, because the legal set depends on the OCCURRENCE."""
    with pytest.raises(ValueError, match="inverts the map"):
        validate_policy_realization(_occurrence())(_payload(
            value_map=[{"semantic_value": "D", "physical_value": "debit"}]))


def test_a_column_in_ANOTHER_DATASET_refuses():
    """A realization naming another dataset would authorize a read nobody planned."""
    with pytest.raises(ValueError, match="authorize a read nobody planned"):
        validate_policy_realization(_occurrence())(_payload(
            policy_column_ref="adcb::public.postings.dr_cr"))


def test_an_EMPTY_map_refuses():
    with pytest.raises(ValueError, match="cannot execute the selection"):
        validate_policy_realization(_occurrence())(_payload(value_map=[]))


def test_a_semantic_value_mapped_TWICE_refuses():
    with pytest.raises(ValueError, match="mapped twice"):
        validate_policy_realization(_occurrence())(_payload(value_map=[
            {"semantic_value": "debit", "physical_value": "D"},
            {"semantic_value": "debit", "physical_value": "DR"}]))


def test_ONE_PHYSICAL_VALUE_MEANING_TWO_THINGS_refuses():
    """A row would match both selections."""
    with pytest.raises(ValueError, match="cannot mean both"):
        validate_policy_realization(_occurrence())(_payload(value_map=[
            {"semantic_value": "debit", "physical_value": "X"},
            {"semantic_value": "credit", "physical_value": "X"}]))


def test_an_empty_physical_value_refuses():
    with pytest.raises(ValueError, match="selects no rows"):
        validate_policy_realization(_occurrence())(_payload(value_map=[
            {"semantic_value": "debit", "physical_value": "  "}]))


def test_the_eligibility_role_uses_ITS_OWN_token_set():
    occurrence = _occurrence(role="status", kind="eligible_status")
    assert semantic_tokens_for(occurrence) == frozenset({"eligible"})
    with pytest.raises(ValueError, match="not one of"):
        validate_policy_realization(occurrence)(_payload(value_map=[
            {"semantic_value": "debit", "physical_value": "D"}]))


# ══ replay, and provenance that cannot be argued with ════════════════════════════════════════════
def test_the_replay_key_is_the_OCCURRENCE_HASH():
    """The same occurrence in the same run resolves to the same logical call, so a re-run replays
    rather than re-spends."""
    occurrence = _occurrence()
    assert logical_call_ref_for(occurrence).endswith(occurrence.occurrence_hash)
    assert logical_call_ref_for(occurrence) == logical_call_ref_for(_occurrence())


def test_the_producer_can_ONLY_stamp_LLM_PROPOSED():
    """Hardcoded, not a parameter. A producer that could be asked to stamp SOURCE_DERIVED would be
    one call-site away from laundering a proposal into evidence."""
    import inspect

    from featuregen.formula import policy_producer

    revision = realization_from_payload(
        _payload(), _occurrence(), revision_id="rev-llm-1",
        executable_content_hash="sha256:D-means-debit", cas_pointer="cas://blob/1")

    assert revision.provenance is RealizationProvenanceV1.LLM_PROPOSED
    assert not revision.is_evidence_validated
    assert revision.realizes_occurrences == (_occurrence().occurrence_hash,)

    # The real property, not a source grep: there is no `provenance` knob to turn.
    parameters = inspect.signature(policy_producer.realization_from_payload).parameters
    assert "provenance" not in parameters
    assert set(parameters) == {"payload", "occurrence", "revision_id",
                               "executable_content_hash", "cas_pointer"}


def test_building_a_realization_REVALIDATES_the_payload():
    """A caller that skipped the repair loop cannot smuggle an invalid map in through this door."""
    with pytest.raises(ValueError, match="inverts the map"):
        realization_from_payload(
            _payload(value_map=[{"semantic_value": "D", "physical_value": "debit"}]),
            _occurrence(), revision_id="r", executable_content_hash="h", cas_pointer="c")


def test_the_module_reaches_no_provider_client_itself():
    """The seam is `drive_audited_structured_call`'s; this module supplies the contract and the
    rules, so it must not grow a dispatch path of its own."""
    import inspect

    from featuregen.formula import policy_producer

    source = inspect.getsource(policy_producer)
    for forbidden in ("LLMClient", "anthropic", "requests.", "httpx"):
        assert forbidden not in source, forbidden
