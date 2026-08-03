"""`AnalysisExecutionIRV2` — the V1 → V2 delta, pinned field by field (Release-B Task 9).

The plan review's finding 9 is the whole reason this contract exists: `AnalysisExecutionIRV1` has
NO version discriminator and its `plan_hash` is a raw `"|"`-join of stringified fields. So these
tests care about exactly three things:

  * V1 DID NOT MOVE. Every V1 hash is byte-identical to what it was, because nothing may re-key.
  * V2's identity is JCS over STRUCTURE, carries the version token, and covers the decisions —
    a plan whose computation is unchanged but whose source moved is a different plan.
  * the join's two known weaknesses (no version, no escaping) are gone in V2.

No database: these are contract tests over hand-built objects.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.plan import SelectionPreviewV1
from featuregen.analysis.sealed_plan import (
    ANALYSIS_PLAN_CONTRACT,
    ANALYSIS_PLAN_CONTRACT_VERSION,
    AnalysisExecutionIRV2,
    SealedDecisionRefsV1,
    SealedPlanError,
    SealedRowDecisionV1,
    SealedSourceDecisionV1,
    build_execution_ir_v2,
    seal_refs_from_selections,
)
from featuregen.data_agent.analysis import (
    AnalysisExecutionIRV1,
    Comparison,
    Period,
    PopulationSpine,
)
from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    AuthorityBasis,
    CandidateDecisionV1,
    CandidateDisposition,
    DatasetNeedRole,
    DatasetNeedV1,
    DatasetServingPolicyRevisionV1,
    DatasetSourceSelectionV1,
    SelectionBasis,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetRowSelectionV1,
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)

_SRC = "ftr"
_PBR_A = "pbr_" + "a" * 64
_PBR_B = "pbr_" + "b" * 64
_PROFILE_A = "dph_" + "1" * 16
_PROFILE_B = "dph_" + "2" * 16
CUST = f"{_SRC}::dpl_eib.customer_master"
TRAN = f"{_SRC}::dpl_eib.tran_repos"
SEG = f"{_SRC}::dpl_eib.customer_segment_history"


def _binding(table: str) -> PhysicalDatasetBindingV1:
    return PhysicalDatasetBindingV1(
        binding_id=f"b-{table}", catalog_logical_ref=f"{_SRC}::dpl_eib.{table}",
        connection_id="local-pg",
        identity=PhysicalObjectIdentityV1(catalog_source=_SRC, database="featuregen_test",
                                          schema="dpl_eib", table=table, object_kind="table"))


def _eligibility(**over) -> TransactionEligibilityPolicyV1:
    kw = {"status_column": "tran_status", "included_status_values": ("POSTED",),
          "reversal_mode": ReversalMode.BOOLEAN_OR_CODE_COLUMN,
          "reversal_column": "reversal_flag",
          "non_reversed_values": ("N",), "null_behavior": NullBehavior.EXCLUDE}
    kw.update(over)
    return TransactionEligibilityPolicyV1(**kw)


def _ir(**over) -> AnalysisExecutionIRV1:
    kw = {
        "question": "which customers had fewer transactions this month",
        "spine": PopulationSpine(binding=_binding("customer_master"), key_column="cif_id"),
        "event_binding": _binding("tran_repos"),
        "event_key_column": "cif_id",
        "period_column": "tran_month",
        "current": Period(label="current", values=("2026-06",)),
        "previous": Period(label="previous", values=("2026-05",)),
        "comparison": Comparison.DECREASED,
        "eligibility": _eligibility(),
    }
    kw.update(over)
    return AnalysisExecutionIRV1(**kw)


def _need(role: DatasetNeedRole, *, explicit: str | None = None) -> DatasetNeedV1:
    return DatasetNeedV1(entity_id="customer", need_role=role,
                         serving_purpose=ServingPurpose.ANALYTICAL,
                         execution_tier=ExecutionTier.PRODUCTION, explicit_dataset_ref=explicit)


def _source(role: DatasetNeedRole, ref: str, *, profile: str = _PROFILE_A,
            binding: str = _PBR_A, policy: str | None = None,
            considered: tuple[CandidateDecisionV1, ...] = ()) -> DatasetSourceSelectionV1:
    return DatasetSourceSelectionV1(
        need=_need(role, explicit=ref if role is DatasetNeedRole.POPULATION else None),
        selected_dataset_ref=ref, selected_dataset_profile_hash=profile,
        selected_binding_revision_id=binding, serving_policy_revision_id=policy,
        authority_role="master", authority_basis=AuthorityBasis.LOAD_BEARING_PROFILE,
        selection_basis=(SelectionBasis.EXPLICIT_REQUEST if role is DatasetNeedRole.POPULATION
                         else SelectionBasis.SINGLE_ELIGIBLE_CANDIDATE),
        considered_candidates=considered)


def _policy_revision() -> DatasetServingPolicyRevisionV1:
    return DatasetServingPolicyRevisionV1(
        entity_id="customer", need_role=DatasetNeedRole.EVENT_SOURCE,
        serving_purpose=ServingPurpose.ANALYTICAL,
        eligible_dataset_refs=(TRAN,), preferred_dataset_refs=(TRAN,),
        provenance=human_declaration_provenance(producer_ref="user:priya"))


def _temporal_revision() -> DatasetTemporalPolicyRevisionV1:
    return DatasetTemporalPolicyRevisionV1(
        dataset_logical_ref=SEG, temporal_storage_model=TemporalStorageModel.SCD2,
        current_selection=TemporalSelectionKind.CURRENT_RECORD,
        historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
        effective_from_ref=f"{SEG}.effective_from", effective_to_ref=f"{SEG}.effective_to",
        current_flag_ref=f"{SEG}.is_current",
        provenance=human_declaration_provenance(producer_ref="user:priya"))


def _row(**over) -> DatasetRowSelectionV1:
    kw = {"dataset_logical_ref": SEG, "dataset_profile_hash": _PROFILE_A,
          "temporal_policy_revision_id": _temporal_revision().revision_id,
          "selection_kind": TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
          "cutoff_value_ref": "report_cutoff_param"}
    kw.update(over)
    return DatasetRowSelectionV1(**kw)


def _preview(**over) -> SelectionPreviewV1:
    sources = over.pop("source_selections", (
        _source(DatasetNeedRole.POPULATION, CUST),
        _source(DatasetNeedRole.EVENT_SOURCE, TRAN, policy=_policy_revision().revision_id),
        _source(DatasetNeedRole.DIMENSION_SOURCE, SEG)))
    rows = over.pop("row_selections", (_row(),))
    kw = {"source_selections": sources, "row_selections": rows,
          "needs_considered": len(sources), "row_decisions_expected": len(rows)}
    kw.update(over)
    return SelectionPreviewV1(**kw)


def _v2(**over) -> AnalysisExecutionIRV2:
    ir = over.pop("execution_ir", _ir())
    return build_execution_ir_v2(ir, over.pop("selections", _preview()), **over)


# ── V1 did not move ──────────────────────────────────────────────────────────────────────────────


def test_V1_plan_hash_is_still_the_delimiter_join_and_is_UNCHANGED():
    """The one thing that may not move. A literal, so a refactor that quietly re-keys every stored
    plan hash fails here rather than in production replay."""
    import hashlib

    ir = _ir()
    e = ir.eligibility
    expected = hashlib.sha256("|".join([
        ir.spine.binding.identity.table_id, "cif_id",
        ir.event_binding.identity.table_id, "cif_id", "tran_month",
        "2026-06", "2026-05", "count", str(Comparison.DECREASED), "",
        e.status_column, "POSTED", str(e.reversal_mode), e.reversal_column, "N",
        str(e.null_behavior), "",
    ]).encode()).hexdigest()[:32]
    assert ir.plan_hash == expected
    assert len(ir.plan_hash) == 32


def test_identity_payload_enumerates_the_SAME_facts_the_join_does():
    """The drift guard between the two enumerations: every field that moves one moves the other.

    Without this the structural restatement is a second hand-maintained list, and the failure mode
    is silent — a field added to `plan_hash` and forgotten here would leave two IRs with different
    answers sharing one V2 identity.
    """
    base = _ir()
    mutations = {
        "spine": PopulationSpine(binding=_binding("kyc_customers"), key_column="cif_id"),
        "event_binding": _binding("tran_archive"),
        "event_key_column": "customer_id",
        "period_column": "posting_month",
        "current": Period(label="current", values=("2026-07",)),
        "previous": Period(label="previous", values=("2026-04",)),
        "comparison": Comparison.INCREASED,
        "eligibility": _eligibility(included_status_values=("POSTED", "SETTLED")),
    }
    for field_name, value in mutations.items():
        mutated = _ir(**{field_name: value})
        assert mutated.plan_hash != base.plan_hash, field_name
        assert mutated.identity_payload() != base.identity_payload(), field_name


def test_identity_payload_excludes_the_question_exactly_as_the_join_does():
    reworded = _ir(question="show me customers with declining transaction volume")
    assert reworded.plan_hash == _ir().plan_hash
    assert reworded.identity_payload() == _ir().identity_payload()


# ── what V2 adds ─────────────────────────────────────────────────────────────────────────────────


def test_V2_carries_the_version_token_INSIDE_the_hashed_content():
    v2 = _v2()
    payload = v2.content_payload()
    assert payload["contract"] == ANALYSIS_PLAN_CONTRACT
    assert payload["contract_version"] == ANALYSIS_PLAN_CONTRACT_VERSION == 2
    assert v2.plan_hash == materialize_hash(payload)


def test_V2_identity_is_JCS_over_structure_not_the_delimiter_join():
    v2 = _v2()
    assert v2.plan_hash != v2.v1_plan_hash
    # 64 hex, not V1's truncated 32 — a different function, not the same one relabelled.
    assert len(v2.plan_hash) == 64
    assert "computation" in v2.content_payload()


def test_V2_keeps_the_V1_identity_REACHABLE_and_unchanged():
    """Nothing re-keys: the flag-off hash the preview has always shown is still exactly that."""
    ir = _ir()
    assert _v2(execution_ir=ir).v1_plan_hash == ir.plan_hash


def test_a_source_that_MOVED_is_a_different_plan_even_when_the_computation_is_identical():
    """The whole point of pinning. Same SQL, different copy of the data, different plan."""
    base = _v2()
    moved = _v2(selections=_preview(source_selections=(
        _source(DatasetNeedRole.POPULATION, CUST),
        _source(DatasetNeedRole.EVENT_SOURCE, TRAN, policy=_policy_revision().revision_id,
                binding=_PBR_B),
        _source(DatasetNeedRole.DIMENSION_SOURCE, SEG))))
    assert moved.execution_ir.plan_hash == base.execution_ir.plan_hash
    assert moved.plan_hash != base.plan_hash


@pytest.mark.parametrize("changed", ["profile", "policy", "binding", "temporal", "row"])
def test_every_one_of_the_SIX_PINS_re_keys_the_plan(changed):
    """One case per pin (D6): dataset_profile, serving_policy, source_selection (composite,
    covered by the moved-source test above), physical_binding, temporal_policy, row_selection."""
    base = _v2()
    if changed == "profile":
        selections = _preview(source_selections=(
            _source(DatasetNeedRole.POPULATION, CUST, profile=_PROFILE_B),
            _source(DatasetNeedRole.EVENT_SOURCE, TRAN),
            _source(DatasetNeedRole.DIMENSION_SOURCE, SEG)))
    elif changed == "policy":
        selections = _preview(source_selections=(
            _source(DatasetNeedRole.POPULATION, CUST),
            _source(DatasetNeedRole.EVENT_SOURCE, TRAN),      # policy dropped
            _source(DatasetNeedRole.DIMENSION_SOURCE, SEG)))
    elif changed == "binding":
        selections = _preview(source_selections=(
            _source(DatasetNeedRole.POPULATION, CUST, binding=_PBR_B),
            _source(DatasetNeedRole.EVENT_SOURCE, TRAN, policy=_policy_revision().revision_id),
            _source(DatasetNeedRole.DIMENSION_SOURCE, SEG)))
    elif changed == "temporal":
        other = DatasetTemporalPolicyRevisionV1(
            dataset_logical_ref=SEG, temporal_storage_model=TemporalStorageModel.SCD2,
            current_selection=TemporalSelectionKind.CURRENT_RECORD,
            historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
            effective_from_ref=f"{SEG}.valid_from", effective_to_ref=f"{SEG}.valid_to",
            current_flag_ref=f"{SEG}.is_current",
            provenance=human_declaration_provenance(producer_ref="user:priya"))
        selections = _preview(row_selections=(
            _row(temporal_policy_revision_id=other.revision_id),))
    else:
        selections = _preview(row_selections=(
            _row(selection_kind=TemporalSelectionKind.CURRENT_RECORD, cutoff_value_ref=None),))
    assert _v2(selections=selections).plan_hash != base.plan_hash


def test_the_SEVENTH_pin_re_keys_the_plan_ON_ITS_OWN():
    """PIN 7 — which rows COUNT. Isolated from the computation deliberately: changing the policy
    itself would move the plan hash through the computation half anyway, and what has to be proved
    is that the DECISION REF carries identity of its own. Without that the pin is a field nobody
    would notice going missing."""
    from dataclasses import replace

    from featuregen.analysis.sealed_plan import SealedEligibilityDecisionV1

    base = _v2()
    moved = AnalysisExecutionIRV2(
        execution_ir=base.execution_ir,
        decisions=replace(base.decisions, eligibility=SealedEligibilityDecisionV1(
            dataset_ref=TRAN, policy_hash="0" * 64)))
    assert moved.execution_ir.plan_hash == base.execution_ir.plan_hash
    assert moved.plan_hash != base.plan_hash


def test_the_eligibility_pin_names_the_EVENT_source_and_not_the_population():
    """The policy is keyed by the table it governs. Pinned against the population table it would
    revalidate the wrong fact — and pass, because customer_master has no eligibility policy at
    all."""
    pin = _v2().decisions.eligibility
    assert pin is not None
    assert pin.dataset_ref == TRAN


def test_the_seventh_pin_round_trips_through_its_payload():
    refs = _v2().decisions
    assert SealedDecisionRefsV1.from_payload(refs.payload()) == refs
    assert refs.payload()["eligibility"]["dataset_ref"] == TRAN


def test_the_candidate_SET_order_does_not_re_key_the_plan():
    """Alternatives-considered is a SET. The order the resolver walked its needs in is an
    implementation detail and must not produce two identities for one decision."""
    losing = CandidateDecisionV1(dataset_ref=f"{_SRC}::dpl_eib.tran_archive",
                                 dataset_profile_hash=_PROFILE_B, binding_revision_id=None,
                                 disposition=CandidateDisposition.ELIGIBLE,
                                 reason_codes=("policy_eligible",))
    winning = CandidateDecisionV1(dataset_ref=TRAN, dataset_profile_hash=_PROFILE_A,
                                  binding_revision_id=_PBR_A,
                                  disposition=CandidateDisposition.SELECTED,
                                  reason_codes=("policy_preferred",))
    forward = _source(DatasetNeedRole.EVENT_SOURCE, TRAN,
                      policy=_policy_revision().revision_id, considered=(winning, losing))
    reverse = _source(DatasetNeedRole.EVENT_SOURCE, TRAN,
                      policy=_policy_revision().revision_id, considered=(losing, winning))
    a = _v2(selections=_preview(source_selections=(
        _source(DatasetNeedRole.POPULATION, CUST), forward,
        _source(DatasetNeedRole.DIMENSION_SOURCE, SEG))))
    b = _v2(selections=_preview(source_selections=(
        _source(DatasetNeedRole.DIMENSION_SOURCE, SEG),
        _source(DatasetNeedRole.POPULATION, CUST), reverse)))
    assert a.plan_hash == b.plan_hash


def test_the_sealed_refs_round_trip_through_their_payload():
    refs = seal_refs_from_selections(_preview())
    assert SealedDecisionRefsV1.from_payload(refs.payload()) == refs


def test_the_warnings_a_decision_rode_are_SEALED_with_it():
    """`PROPOSED_AUTHORITY_USED` is part of what the decision WAS. Dropping it at seal time makes
    an unconfirmed classification look confirmed the moment the plan is recorded."""
    refs = seal_refs_from_selections(_preview(warnings=("PROPOSED_AUTHORITY_USED",)))
    assert refs.warnings == ("PROPOSED_AUTHORITY_USED",)
    assert "PROPOSED_AUTHORITY_USED" in str(refs.payload())


# ── what V2 refuses ──────────────────────────────────────────────────────────────────────────────


def test_an_UNRESOLVED_selection_seals_nothing():
    """A refused plan seals nothing: replaying it later would read as a decision nobody made."""
    from featuregen.overlay.upload.source_selection import SELECTION_POPULATION_UNDECLARED
    from featuregen.overlay.upload.source_selector import SelectionRefusalV1

    refused = _preview(refusals=(SelectionRefusalV1(
        code=SELECTION_POPULATION_UNDECLARED, subject_refs=(CUST,), detail="undeclared"),))
    assert refused.resolved is False
    with pytest.raises(SealedPlanError, match="did not resolve"):
        seal_refs_from_selections(refused)


def test_a_FLAG_OFF_plan_has_nothing_to_seal():
    with pytest.raises(SealedPlanError, match="flag is off"):
        seal_refs_from_selections(None)


def test_the_version_token_cannot_be_set_to_anything():
    with pytest.raises(SealedPlanError, match="discriminates nothing"):
        AnalysisExecutionIRV2(execution_ir=_ir(),
                              decisions=seal_refs_from_selections(_preview()),
                              contract_version=3)


def test_a_V2_with_no_source_decisions_is_refused():
    with pytest.raises(SealedPlanError, match="pins nothing"):
        AnalysisExecutionIRV2(execution_ir=_ir(), decisions=SealedDecisionRefsV1())


def test_no_freshness_notion_can_enter_a_sealed_plan_identity():
    """The §6.4 mechanical rule, enforced on this contract too: a decision identity that moved
    because data merely arrived later would re-key every replay."""
    v2 = _v2()
    payload = v2.content_payload()
    assert "loaded_at" not in str(payload)
    assert "watermark" not in str(payload)


def test_a_row_decision_seals_the_cutoff_REF_and_never_its_value():
    sealed = SealedRowDecisionV1.from_selection(_row())
    assert sealed.cutoff_value_ref == "report_cutoff_param"
    assert "2026" not in str(sealed.payload())


def test_a_source_decision_seals_what_revalidation_needs_to_re_read_the_policy():
    """The serving-policy pointer is keyed by entity + need_role + serving_purpose. Without all
    three sealed, revalidation compares the sealed id against nothing and reads as 'unchanged'."""
    sealed = SealedSourceDecisionV1.from_selection(
        _source(DatasetNeedRole.EVENT_SOURCE, TRAN, policy=_policy_revision().revision_id))
    assert (sealed.entity_id, sealed.need_role, sealed.serving_purpose) == (
        "customer", "event_source", "analytical")
    assert sealed.serving_policy_revision_id == _policy_revision().revision_id
