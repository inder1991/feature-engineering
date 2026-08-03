"""Release-B Task 7 — the §6.4/§6.5 selection contracts.

Proves the properties the plan and the verified-interface doc bind these contracts to, WITHOUT a
database: canonical (JCS) identity that is insertion-order- and enumeration-order-insensitive,
provenance excluded from identity, `preferred ⊆ eligible`, the D5 `dataset_profile_hash` naming,
`ExecutionTier` reuse, the absence of every freshness/SLA notion, the closed predicate shape
("no free SQL"), the refusal of a literal cutoff VALUE where a ref belongs, the temporal-policy
agreement rule including the D12.7 escape hatch, and the fail-closed flag dependency (D8).
"""
from __future__ import annotations

import pytest

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.evidence import AssertionStrength, EvidenceLifecycle, EvidenceProducer
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.semantic_context import EvidenceAuthorityV1
from featuregen.overlay.upload.source_selection import (
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_REFUSAL_CODES,
    SERVING_POLICY_ID_PREFIX,
    SOURCE_TEMPORAL_SELECTION_FLAG,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    AuthorityBasis,
    CandidateDecisionV1,
    CandidateDisposition,
    DatasetNeedRole,
    DatasetNeedV1,
    DatasetServingPolicyRevisionV1,
    DatasetSourceSelectionV1,
    PolicyProvenanceV1,
    SelectionBasis,
    SelectionError,
    ServingPurpose,
    human_declaration_provenance,
    provenance_from_payload,
    source_temporal_selection_enabled,
    source_temporal_selection_status,
)
from featuregen.overlay.upload.temporal_policy import (
    TEMPORAL_POLICY_ID_PREFIX,
    DatasetRowSelectionV1,
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
    contradicts_load_bearing_model,
    temporal_policy_agreement,
)

_CUST = "bank::public.customer_master"
_CUST_ODS = "bank::public.customer_ods"
_SEG = "bank::public.customer_segment"
_PBR = "pbr_" + "a" * 64
_PBR2 = "pbr_" + "b" * 64
_PROFILE_HASH = "c" * 64


def _provenance() -> PolicyProvenanceV1:
    return PolicyProvenanceV1(
        evidence=(EvidenceAuthorityV1(
            producer=EvidenceProducer.HUMAN.value,
            strength=AssertionStrength.CONFIRMED.value,
            lifecycle=EvidenceLifecycle.ACTIVE.value,
            producer_ref="user:priya", evidence_id="ev-1"),),
        decision_refs=("dec-1",))


def _policy(**over) -> DatasetServingPolicyRevisionV1:
    kw = dict(entity_id="customer", need_role=DatasetNeedRole.POPULATION,
              serving_purpose=ServingPurpose.ANALYTICAL,
              eligible_dataset_refs=(_CUST, _CUST_ODS), preferred_dataset_refs=(_CUST,),
              provenance=_provenance())
    kw.update(over)
    return DatasetServingPolicyRevisionV1(**kw)


def _need(**over) -> DatasetNeedV1:
    kw = dict(entity_id="customer", need_role=DatasetNeedRole.POPULATION,
              serving_purpose=ServingPurpose.ANALYTICAL,
              execution_tier=ExecutionTier.SANDBOX)
    kw.update(over)
    return DatasetNeedV1(**kw)


def _selection(**over) -> DatasetSourceSelectionV1:
    kw = dict(need=_need(), selected_dataset_ref=_CUST,
              selected_dataset_profile_hash=_PROFILE_HASH,
              selected_binding_revision_id=_PBR, serving_policy_revision_id=None,
              authority_role="system_of_record",
              authority_basis=AuthorityBasis.LOAD_BEARING_PROFILE,
              selection_basis=SelectionBasis.SERVING_POLICY)
    kw.update(over)
    return DatasetSourceSelectionV1(**kw)


def _temporal(**over) -> DatasetTemporalPolicyRevisionV1:
    kw = dict(dataset_logical_ref=_SEG, temporal_storage_model=TemporalStorageModel.SCD2,
              current_selection=TemporalSelectionKind.CURRENT_RECORD,
              historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
              effective_from_ref=f"{_SEG}.effective_from",
              effective_to_ref=f"{_SEG}.effective_to",
              current_flag_ref=f"{_SEG}.is_current",
              provenance=_provenance())
    kw.update(over)
    return DatasetTemporalPolicyRevisionV1(**kw)


# ── canonical identity (D1) ──────────────────────────────────────────────────────────────────────

def test_policy_identity_is_deterministic_and_order_insensitive():
    """JCS sorts keys, and the contract sorts the ref SETS — so nothing about how a caller happened
    to spell or order the same declaration can produce a second revision."""
    a = _policy(eligible_dataset_refs=(_CUST, _CUST_ODS))
    b = _policy(eligible_dataset_refs=(_CUST_ODS, _CUST, _CUST.upper()))
    assert a.revision_id == b.revision_id
    assert a.revision_id == f"{SERVING_POLICY_ID_PREFIX}{a.content_hash}"
    assert a.content_hash == materialize_hash(a.content_payload())
    assert a.eligible_dataset_refs == (_CUST, _CUST_ODS)   # sorted, deduped


def test_provenance_identity_carries_the_axes_but_not_who_filed_it():
    """D2/§6.2 split: the AUTHORITY CLASS is identity, the subject and the decision refs are
    provenance. A second admin re-declaring identical content reuses the revision."""
    a = _policy()
    b = _policy(provenance=PolicyProvenanceV1(
        evidence=(EvidenceAuthorityV1(
            producer=EvidenceProducer.HUMAN.value,
            strength=AssertionStrength.CONFIRMED.value,
            lifecycle=EvidenceLifecycle.ACTIVE.value,
            producer_ref="user:sam", evidence_id="ev-9"),),
        decision_refs=("dec-2", "dec-3")))
    assert a.revision_id == b.revision_id


def test_a_different_authority_CLASS_is_a_different_policy():
    """The escape hatch cannot be laundered: a human-confirmed declaration and an llm-proposed one
    are not the same policy even when they name the same datasets."""
    human = _policy()
    llm = _policy(provenance=PolicyProvenanceV1(evidence=(EvidenceAuthorityV1(
        producer=EvidenceProducer.LLM.value, strength=AssertionStrength.PROPOSED.value,
        lifecycle=EvidenceLifecycle.ACTIVE.value),)))
    assert human.revision_id != llm.revision_id


def test_provenance_round_trips_through_its_stored_payload():
    original = _provenance()
    assert provenance_from_payload(original.payload()) == original


def test_the_declaration_provenance_is_human_confirmed_not_an_invented_source_attestation():
    """D12.7: for an upload-only catalog the POLICY is the operational declaration — declared by a
    person, never relabelled as if a source had attested it."""
    prov = human_declaration_provenance(producer_ref="user:priya")
    (evidence,) = prov.evidence
    assert (evidence.producer, evidence.strength) == ("human", "confirmed")


# ── §6.4 invariants ─────────────────────────────────────────────────────────────────────────────

def test_preferred_must_be_a_subset_of_eligible():
    with pytest.raises(SelectionError, match="subset of eligible"):
        _policy(preferred_dataset_refs=(_SEG,))


def test_a_policy_with_no_eligible_dataset_is_refused():
    with pytest.raises(SelectionError, match="declares nothing"):
        _policy(eligible_dataset_refs=(), preferred_dataset_refs=())


def test_ambiguity_is_explicit_not_resolved_by_order():
    """"Empty or multiple equally preferred candidates remain an explicit ambiguity, never an
    ordering accident" — the contract SAYS so rather than letting a first element win."""
    assert _policy().ambiguous is False
    assert _policy(preferred_dataset_refs=(_CUST, _CUST_ODS)).ambiguous is True
    assert _policy(preferred_dataset_refs=()).ambiguous is True
    # One eligible and no preference is not ambiguous: there is nothing to choose between.
    assert _policy(eligible_dataset_refs=(_CUST,), preferred_dataset_refs=()).ambiguous is False


def test_a_column_ref_is_not_a_dataset():
    with pytest.raises(SelectionError, match="must address a TABLE"):
        _policy(eligible_dataset_refs=(f"{_CUST}.cif_id",))


def test_the_policy_key_is_entity_need_role_and_serving_purpose():
    assert _policy().policy_key == ("customer", "population", "analytical")
    assert _need().policy_key() == ("customer", "population", "analytical")


def test_execution_tier_reuses_the_bridge_realization_vocabulary():
    """D5: no second tier vocabulary. The SAME need at two tiers is two different decisions."""
    sandbox = _need(execution_tier=ExecutionTier.SANDBOX)
    production = _need(execution_tier="production")
    assert production.execution_tier is ExecutionTier.PRODUCTION
    assert _selection(need=sandbox).content_hash != _selection(need=production).content_hash


def test_the_canonical_profile_hash_name_is_dataset_profile_hash():
    """D5/E1: one value, one name. `profile_hash`/`selected_profile_hash` may not reappear."""
    selection_fields = set(DatasetSourceSelectionV1.__dataclass_fields__)
    candidate_fields = set(CandidateDecisionV1.__dataclass_fields__)
    row_fields = set(DatasetRowSelectionV1.__dataclass_fields__)
    assert "selected_dataset_profile_hash" in selection_fields
    assert "dataset_profile_hash" in candidate_fields
    assert "dataset_profile_hash" in row_fields
    for fields in (selection_fields, candidate_fields, row_fields):
        assert "profile_hash" not in fields
        assert "selected_profile_hash" not in fields


def test_a_selection_must_reference_a_persisted_binding_revision_shape():
    with pytest.raises(SelectionError, match="physical binding revision id"):
        _selection(selected_binding_revision_id="derived-bank-customer_master")


def test_a_selection_pins_the_profile_it_read():
    with pytest.raises(SelectionError, match="selected_dataset_profile_hash is required"):
        _selection(selected_dataset_profile_hash="")


def test_candidate_enumeration_order_does_not_re_key_the_decision():
    won = CandidateDecisionV1(dataset_ref=_CUST, dataset_profile_hash=_PROFILE_HASH,
                              binding_revision_id=_PBR,
                              disposition=CandidateDisposition.SELECTED,
                              reason_codes=("policy_preferred", "load_bearing_authority"))
    lost = CandidateDecisionV1(dataset_ref=_CUST_ODS, dataset_profile_hash="d" * 64,
                               binding_revision_id=_PBR2,
                               disposition=CandidateDisposition.REJECTED,
                               reason_codes=("authority_insufficient",))
    assert (_selection(considered_candidates=(won, lost)).content_hash
            == _selection(considered_candidates=(lost, won)).content_hash)


def test_reason_codes_are_a_closed_vocabulary():
    with pytest.raises(SelectionError, match="not one of"):
        CandidateDecisionV1(dataset_ref=_CUST, dataset_profile_hash=_PROFILE_HASH,
                            binding_revision_id=_PBR,
                            disposition=CandidateDisposition.ELIGIBLE,
                            reason_codes=("looked_newest",))


def test_the_selected_candidate_must_be_the_selected_dataset():
    wrong = CandidateDecisionV1(dataset_ref=_CUST_ODS, dataset_profile_hash=_PROFILE_HASH,
                                binding_revision_id=_PBR,
                                disposition=CandidateDisposition.SELECTED)
    with pytest.raises(SelectionError, match="is not the selected dataset"):
        _selection(considered_candidates=(wrong,))


def test_a_tie_may_not_decide():
    """§5.9 / rule 9: a tie is RECORDED, but only an explicit request may coexist with one."""
    won = CandidateDecisionV1(dataset_ref=_CUST, dataset_profile_hash=_PROFILE_HASH,
                              binding_revision_id=_PBR,
                              disposition=CandidateDisposition.SELECTED)
    tied = CandidateDecisionV1(dataset_ref=_CUST_ODS, dataset_profile_hash="d" * 64,
                               binding_revision_id=_PBR2,
                               disposition=CandidateDisposition.TIED,
                               reason_codes=("equally_preferred",))
    with pytest.raises(SelectionError, match="EXPLICIT request settled"):
        _selection(considered_candidates=(won, tied))
    settled = _selection(
        need=_need(explicit_dataset_ref=_CUST),
        selection_basis=SelectionBasis.EXPLICIT_REQUEST,
        considered_candidates=(won, tied))
    assert len(settled.considered_candidates) == 2


# ── no freshness, no wall clock (§6.4) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_key", ["freshness_seconds", "delivery_sla", "loaded_at",
                                     "catalog_watermark", "upload_time"])
def test_no_freshness_or_wall_clock_notion_may_enter_a_decision_identity(bad_key):
    """Mechanical, not a convention: a well-meant field added later is refused by construction."""
    from featuregen.overlay.upload.source_selection import reject_freshness_keys

    with pytest.raises(SelectionError, match="freshness/SLA key"):
        reject_freshness_keys({"a": [{bad_key: 1}]}, where="a source selection")


def test_no_shipped_contract_carries_a_freshness_field():
    for contract in (DatasetServingPolicyRevisionV1, DatasetSourceSelectionV1,
                     DatasetTemporalPolicyRevisionV1, DatasetRowSelectionV1, DatasetNeedV1,
                     CandidateDecisionV1):
        for name in contract.__dataclass_fields__:
            lowered = name.lower()
            assert not any(token in lowered for token in
                           ("fresh", "sla", "watermark", "staleness", "latency")), (contract, name)


# ── §6.5 temporal policy ────────────────────────────────────────────────────────────────────────

def test_all_four_selection_kinds_are_representable():
    """D12.6: two of the three product kinds are NEW ENGINE WORK (Task 8), but a POLICY must be
    able to DECLARE any of the four today."""
    assert {k.value for k in TemporalSelectionKind} == {
        "current_record", "valid_at_report_cutoff", "latest_snapshot_as_of", "explicit_only"}
    snapshot = _temporal(
        temporal_storage_model=TemporalStorageModel.SNAPSHOT,
        current_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
        historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
        effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
        snapshot_ref=f"{_SEG}.snapshot_date")
    assert snapshot.revision_id.startswith(TEMPORAL_POLICY_ID_PREFIX)


def test_a_selection_kind_must_name_the_columns_it_needs():
    with pytest.raises(SelectionError, match="requires effective_from_ref"):
        _temporal(effective_from_ref=None)
    with pytest.raises(SelectionError, match="requires snapshot_ref"):
        _temporal(temporal_storage_model=TemporalStorageModel.SNAPSHOT,
                  current_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
                  historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
                  effective_from_ref=None, effective_to_ref=None, current_flag_ref=None)


def test_a_history_keeping_dataset_needs_a_current_flag_to_have_a_current_row():
    with pytest.raises(SelectionError, match="current_flag_ref"):
        _temporal(current_flag_ref=None)


def test_a_historical_request_never_falls_back_to_a_current_only_dataset():
    """§5.5 — the refusal exists so the answer is another source or a stated limitation."""
    with pytest.raises(SelectionError, match="keeps no history"):
        _temporal(temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
                  effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
                  historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF)
    ok = _temporal(temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
                   effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
                   historical_selection=TemporalSelectionKind.EXPLICIT_ONLY)
    assert ok.historical_selection is TemporalSelectionKind.EXPLICIT_ONLY


def test_a_policy_names_column_refs_never_a_table_or_a_fragment():
    with pytest.raises(SelectionError, match="must address a COLUMN"):
        _temporal(effective_from_ref=_SEG)


def test_temporal_identity_excludes_provenance_and_is_deterministic():
    a = _temporal()
    b = _temporal(provenance=human_declaration_provenance(producer_ref="user:priya"))
    assert a.revision_id == f"{TEMPORAL_POLICY_ID_PREFIX}{a.content_hash}"
    assert a.revision_id == b.revision_id          # same axes (human/confirmed/active)


def test_the_agreement_rule_and_the_d12_7_escape_hatch():
    scd2 = TemporalStorageModel.SCD2
    # 1. no load-bearing value at all: the POLICY is the operational declaration.
    assert temporal_policy_agreement(policy_model=scd2, load_bearing_model=None) is None
    # 2. a merely DISPLAYED (proposed) value does not gate either — and is not promoted by agreeing.
    assert temporal_policy_agreement(policy_model=scd2, load_bearing_model=None,
                                     displayed_model="snapshot") is None
    # 3. a load-bearing value that AGREES.
    assert temporal_policy_agreement(policy_model=scd2, load_bearing_model="scd2") is None
    # 4. a load-bearing value that CONTRADICTS — a policy may not overrule a governed value.
    assert temporal_policy_agreement(policy_model=scd2,
                                     load_bearing_model="snapshot") == TEMPORAL_MODEL_UNKNOWN
    assert contradicts_load_bearing_model(policy_model=scd2, load_bearing_model="snapshot") is True
    assert contradicts_load_bearing_model(policy_model=scd2, load_bearing_model=None) is False
    # 5. declaring `unknown` declares nothing.
    assert temporal_policy_agreement(policy_model=TemporalStorageModel.UNKNOWN,
                                     load_bearing_model=None) == TEMPORAL_MODEL_UNKNOWN


# ── §6.5 row selection: no free SQL, no live cutoff value ───────────────────────────────────────

def _row(**over) -> DatasetRowSelectionV1:
    kw = dict(dataset_logical_ref=_SEG, dataset_profile_hash=_PROFILE_HASH,
              temporal_policy_revision_id=_temporal().revision_id,
              selection_kind=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
              cutoff_value_ref="param:report_cutoff",
              predicate_payloads=({"kind": "effective_time",
                                   "column_ref": f"{_SEG}.effective_from",
                                   "operator": "<=", "parameter_ref": "param:report_cutoff"},))
    kw.update(over)
    return DatasetRowSelectionV1(**kw)


def test_a_row_selection_round_trips_its_identity():
    row = _row()
    assert row.content_hash == materialize_hash(row.content_payload())


def test_a_literal_cutoff_value_is_refused_where_a_ref_belongs():
    """The identity defect this guards: the same governed decision replayed tomorrow would hash
    differently purely because the report ran on another day."""
    with pytest.raises(SelectionError, match="literal date"):
        _row(cutoff_value_ref="2026-06-30")
    with pytest.raises(SelectionError, match="value or a fragment"):
        _row(cutoff_value_ref="date '2026-06-30'")


def test_a_predicate_is_a_typed_shape_never_free_sql():
    with pytest.raises(SelectionError, match="unknown keys"):
        _row(predicate_payloads=({"sql": "effective_from <= '2026-06-30'"},))
    with pytest.raises(SelectionError, match="operator"):
        _row(predicate_payloads=({"kind": "effective_time",
                                  "column_ref": f"{_SEG}.effective_from",
                                  "operator": "between"},))
    with pytest.raises(SelectionError, match="kind"):
        _row(predicate_payloads=({"kind": "raw", "column_ref": f"{_SEG}.effective_from",
                                  "operator": "<="},))


def test_a_row_selection_must_name_a_temporal_policy_revision():
    with pytest.raises(SelectionError, match="dtp_"):
        _row(temporal_policy_revision_id="whatever")


# ── refusal vocabulary + flag matrix ────────────────────────────────────────────────────────────

def test_the_closed_refusal_vocabulary_is_exactly_the_eight_named_conditions():
    assert SELECTION_REFUSAL_CODES == frozenset({
        "SELECTION_POPULATION_UNDECLARED", "SELECTION_SOURCE_AMBIGUOUS",
        "SELECTION_BINDING_MISSING", "SELECTION_AUTHORITY_INSUFFICIENT",
        "TEMPORAL_MODEL_UNKNOWN", "TEMPORAL_HISTORICAL_CURRENT_ONLY",
        "TEMPORAL_SCD_OVERLAP", "TEMPORAL_SNAPSHOT_TIE"})
    assert SELECTION_POPULATION_UNDECLARED in SELECTION_REFUSAL_CODES
    assert TEMPORAL_SCD_OVERLAP in SELECTION_REFUSAL_CODES


@pytest.mark.parametrize("profiles,requested,enabled,valid", [
    (None, None, False, True),        # both off — the default
    ("1", None, False, True),         # profiles only — Release A's shipped state
    ("1", "1", True, True),           # the Release-B gate
    ("1", "yes", True, True),         # widened truthy set (D8)
    (None, "1", False, False),        # FAIL-CLOSED: the dependency is unmet
    (None, "on", False, False),
])
def test_the_flag_depends_on_dataset_profiles_and_fails_closed(monkeypatch, profiles, requested,
                                                               enabled, valid):
    """D8 / §5.16: an invalid combination is a CONFIGURATION failure, not a half-enabled path."""
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    monkeypatch.delenv(SOURCE_TEMPORAL_SELECTION_FLAG, raising=False)
    if profiles is not None:
        monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", profiles)
    if requested is not None:
        monkeypatch.setenv(SOURCE_TEMPORAL_SELECTION_FLAG, requested)
    status = source_temporal_selection_status()
    assert status.enabled is enabled
    assert status.configuration_valid is valid
    assert source_temporal_selection_enabled() is enabled
