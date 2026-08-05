"""The hand-reconciliation procedure's checks, proved EXECUTABLE at fixture level.

The plan's activation step — *hand-reconcile one direct bridge and one real mapping-table crosswalk
before activation* — is a Gate-B activity against live data. This suite is the pre-gate half: it
drives every check the procedure names (docs/architecture/2026-08-05-crosswalk-hand-reconciliation-
procedure.md) against fixtures, so the procedure is a runnable thing rather than a paragraph.

NO CLUSTER, NO CATALOG READ, NO LLM. The reconciliation compares numbers a person brings back with
artifacts the platform already holds, which is exactly why it is testable here.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_TABLE,
    FTR_TABLE,
    MAP_TABLE,
    binding,
    definition,
    leg,
    mapping_observation,
)

from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    CardinalityBasis,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
)
from featuregen.overlay.upload.crosswalk import JoinLegKind, JoinLegPinV1
from featuregen.overlay.upload.crosswalk_admission import (
    AdmittedCrosswalkV1,
    CrosswalkAdmissionPolicyV1,
    admitted_crosswalk_execution,
    evaluate_crosswalk_admission,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    compose_crosswalk_observation,
)
from featuregen.overlay.upload.crosswalk_reconciliation import (
    BridgeHandCountsV1,
    HandCountsV1,
    ReconciliationFinding,
    reconcile_crosswalk,
    reconcile_direct_bridge,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

NOW = datetime(2026, 8, 5, 9, tzinfo=UTC)
POLICY_REVISION = "dtp_" + "7" * 64
DEFINITION = definition()

CIB_BINDING = binding("cib", CIB_TABLE)
FTR_BINDING = binding("ftr", FTR_TABLE, schema="dpl_ftr")
MAP_BINDING = binding("cib", MAP_TABLE)

SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-production-scope", execution_tier=ExecutionTier.PRODUCTION,
    purposes=("feature_materialization",), environment="prod")

#: What an operator would type into their query, if they typed it right.
PINNED_ADDRESSES = (
    f"cib::{CIB_BINDING.identity.schema}.{CIB_TABLE}",
    f"cib::{MAP_BINDING.identity.schema}.{MAP_TABLE}",
    f"ftr::{FTR_BINDING.identity.schema}.{FTR_TABLE}",
)


def _observation(**over):
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        source_leg=leg(which=SOURCE_TO_MAPPING, endpoint_binding=CIB_BINDING,
                       mapping_binding=MAP_BINDING, endpoint_column="acct_no",
                       mapping_column="acct_no"),
        target_leg=leg(which=MAPPING_TO_TARGET, endpoint_binding=FTR_BINDING,
                       mapping_binding=MAP_BINDING, endpoint_column="counter_party_acct_no",
                       mapping_column="ext_acct_ref", cross_catalog=True),
        mapping=mapping_observation(MAP_BINDING),
        scope_id=SCOPE.scope_id,
        matched_source_distinct=3, unmatched_source_distinct=1,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        observed_at=NOW,
        mapping_temporal_policy_revision_id=POLICY_REVISION,
        mapping_row_selection_hash="e" * 64)
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


def _pins():
    source = JoinLegPinV1(
        kind=JoinLegKind.SAME_CATALOG, plan_hash="plan-" + "1" * 16,
        from_dataset_ref=DEFINITION.source_endpoint.logical_table_ref,
        to_dataset_ref=DEFINITION.mapping_dataset_ref,
        from_binding_revision_id=CIB_BINDING.binding_revision_id,
        to_binding_revision_id=MAP_BINDING.binding_revision_id,
        read_set_hash="read-" + "1" * 16)
    target = JoinLegPinV1(
        kind=JoinLegKind.CROSS_CATALOG, plan_hash="plan-" + "2" * 16,
        from_dataset_ref=DEFINITION.mapping_dataset_ref,
        to_dataset_ref=DEFINITION.target_endpoint.logical_table_ref,
        from_binding_revision_id=MAP_BINDING.binding_revision_id,
        to_binding_revision_id=FTR_BINDING.binding_revision_id,
        read_set_hash="read-" + "2" * 16,
        fact_keys=("ebf-recon-1",), realization_revision_ids=("bjr_recon_1",),
        dependency_snapshot_ids=("dps_recon_1",))
    return source, target


def admitted(*, observation=None, measured=True) -> AdmittedCrosswalkV1:
    obs = observation if observation is not None else (_observation() if measured else None)
    decision = evaluate_crosswalk_admission(
        crosswalk_definition_revision_id=DEFINITION.revision_id, scope=SCOPE,
        policy=CrosswalkAdmissionPolicyV1(), now=NOW, observation=obs,
        mapping_binding_revision_id=MAP_BINDING.binding_revision_id)
    source_pin, target_pin = _pins()
    execution = admitted_crosswalk_execution(
        decision, source_leg=source_pin, target_leg=target_pin,
        mapping_binding_revision_id=MAP_BINDING.binding_revision_id,
        scope=SCOPE, observation=obs,
        mapping_temporal_policy_revision_id=POLICY_REVISION)
    return AdmittedCrosswalkV1(
        definition=DEFINITION, execution=execution, decision=decision,
        source_binding=CIB_BINDING, target_binding=FTR_BINDING, mapping_binding=MAP_BINDING,
        observation=obs)


def counts(**over) -> HandCountsV1:
    """What an operator who counted CORRECTLY brings back."""
    kwargs = dict(
        composed_row_count=3, mapping_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        read_addresses=PINNED_ADDRESSES, row_rule_applied=POLICY_REVISION,
        counted_by="user:asha")
    kwargs.update(over)
    return HandCountsV1(**kwargs)


# ── the crosswalk half ──────────────────────────────────────────────────────────────────────────

def test_a_correct_hand_count_agrees_and_does_not_block_activation() -> None:
    report = reconcile_crosswalk(admitted(), counts())

    assert report.agrees is True
    assert report.findings == ()
    assert report.blocks_activation is False
    assert report.counted_by == "user:asha"
    assert report.subject == DEFINITION.definition_id


@pytest.mark.parametrize("field_name,value,expected", [
    ("composed_row_count", 4, ReconciliationFinding.COMPOSED_ROW_COUNT_DISAGREES),
    ("mapping_row_count", 5, ReconciliationFinding.MAPPING_ROW_COUNT_DISAGREES),
    ("source_to_target_max_matches", 2, ReconciliationFinding.FORWARD_FANOUT_DISAGREES),
    ("target_to_source_max_matches", 2, ReconciliationFinding.REVERSE_FANOUT_DISAGREES),
])
def test_every_counted_number_is_actually_compared(field_name, value, expected) -> None:
    """One test per field, so a comparison silently dropped from the loop fails HERE.

    The failure this guards is the one a reconciliation is least able to survive: a check that runs,
    reports agreement, and never looked at the number in question.
    """
    report = reconcile_crosswalk(admitted(), counts(**{field_name: value}))

    assert report.agrees is False
    assert expected in report.findings
    assert report.blocks_activation is True


def test_a_divergence_names_BOTH_numbers_so_the_finding_is_actionable() -> None:
    """"They disagree" is not a finding anybody can act on."""
    report = reconcile_crosswalk(admitted(), counts(composed_row_count=41882))

    joined = " | ".join(report.detail)
    assert "41882" in joined and "3" in joined
    assert "composed rows" in joined


def test_there_is_no_tolerance_and_an_off_by_one_is_a_finding() -> None:
    """A tolerance is a policy, and a policy does not belong inside the independent check."""
    report = reconcile_crosswalk(admitted(), counts(composed_row_count=4))

    assert report.blocks_activation is True


def test_counting_the_wrong_TABLE_is_caught_before_any_number_is_believed() -> None:
    """The commonest reconciliation error: a right count of the wrong thing."""
    wrong_schema = (PINNED_ADDRESSES[0], "cib::dpl_eib_v2.acct_xref", PINNED_ADDRESSES[2])

    report = reconcile_crosswalk(admitted(), counts(read_addresses=wrong_schema))

    assert ReconciliationFinding.ADDRESS_DISAGREES in report.findings
    assert "dpl_eib_v2" in " | ".join(report.detail)
    assert report.blocks_activation is True


def test_counting_under_the_wrong_ROW_RULE_is_its_own_finding() -> None:
    """Uniqueness over one row set says nothing about another — including "no filter at all"."""
    unfiltered = reconcile_crosswalk(admitted(), counts(row_rule_applied=None))
    other_rule = reconcile_crosswalk(admitted(), counts(row_rule_applied="dtp_" + "0" * 64))

    for report in (unfiltered, other_rule):
        assert ReconciliationFinding.ROW_RULE_DISAGREES in report.findings
        assert report.blocks_activation is True


def test_an_unmeasured_crosswalk_reports_ABSENCE_and_not_a_disagreement() -> None:
    """Nothing to reconcile against is a different sentence from "your numbers are wrong"."""
    report = reconcile_crosswalk(admitted(measured=False), counts())

    assert ReconciliationFinding.NOT_MEASURED in report.findings
    assert ReconciliationFinding.COMPOSED_ROW_COUNT_DISAGREES not in report.findings
    assert "profile it first" in " | ".join(report.detail)


def test_the_row_rule_compared_against_is_the_MEASUREMENTS_and_not_a_derived_pin() -> None:
    """The independent check asks the measurement, because the pin is derived from it.

    `crosswalk_assembly.pinned_mapping_temporal_policy` fills the execution's pin FROM the composed
    measurement, so on anything this repository assembles the two agree. What must not happen is
    the reconciliation quietly resting on the derived value: this check exists to catch a wrong
    MEASUREMENT, and an operator who counted under the rule the measurement records has counted the
    right rows however the pin was computed.
    """
    other_rule = "dtp_" + "0" * 64
    bundle = admitted()
    drifted_pin = dataclasses.replace(
        bundle.execution, mapping_temporal_policy_revision_id=other_rule)
    bundle = dataclasses.replace(bundle, execution=drifted_pin)

    report = reconcile_crosswalk(bundle, counts(row_rule_applied=POLICY_REVISION))

    assert ReconciliationFinding.ROW_RULE_DISAGREES not in report.findings, (
        "the operator counted under the rule the MEASUREMENT was taken under and the report "
        "compared them against a pin instead")
    assert reconcile_crosswalk(
        bundle, counts(row_rule_applied=other_rule)).findings, (
        "counting under the pin rather than the measurement must still be a finding")


def test_the_report_never_frames_a_divergence_as_the_crosswalks_fault() -> None:
    """A finding is about the data or about how the query was run — never a verdict on the link."""
    report = reconcile_crosswalk(admitted(), counts(composed_row_count=9))

    text = " | ".join((*report.detail, *(f.value for f in report.findings))).lower()
    for word in ("invalid", "rejected", "unsafe", "failed", "blocked crosswalk"):
        assert word not in text


# ── the direct-bridge half ──────────────────────────────────────────────────────────────────────

def _endpoint(source: str, table: str, column: str, bind):
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=(IdentifierColumnMemberV1(
            normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED,
            KeyMemberRole.PRIMARY),),
        entity_id="account", concept="account_id",
        concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
        physical_binding=bind, binding_revision_id=bind.binding_revision_id)


def realization(*, cardinality=Cardinality.MANY_TO_ONE) -> BridgeJoinRealizationRevisionV1:
    return BridgeJoinRealizationRevisionV1(
        bridge_fact_key="ebf-recon-1",
        from_endpoint=_endpoint("cib", CIB_TABLE, "acct_no", CIB_BINDING),
        to_endpoint=_endpoint("ftr", FTR_TABLE, "counter_party_acct_no", FTR_BINDING),
        column_pairs=(ColumnPairV1(
            normalize_ref("cib", "public", CIB_TABLE, "acct_no"),
            normalize_ref("ftr", "public", FTR_TABLE, "counter_party_acct_no")),),
        predicates=(),
        applicability_scope=SCOPE,
        cardinality=DirectionalCardinalityVerdictV1(cardinality),
        cardinality_basis=CardinalityBasis.GOVERNED_KEY,
        evidence_refs=(),
        dependency_snapshot_id="dps_recon_1",
        derivation_version="derivation-v1",
        admission_policy_version="bridge-admission-v1")


BRIDGE_ADDRESSES = (PINNED_ADDRESSES[0], PINNED_ADDRESSES[2])


def test_a_direct_bridge_reconciles_by_the_SAME_mechanism() -> None:
    """Both subjects through one comparison, so a divergence on both points at the METHOD.

    That distinction is the whole reason the plan asks for two subjects: reconciling only the
    crosswalk leaves "does hand-counting this platform's joins agree at all?" unanswered.
    """
    report = reconcile_direct_bridge(
        realization(),
        BridgeHandCountsV1(joined_row_count=3, max_matches_per_source_row=1,
                           read_addresses=BRIDGE_ADDRESSES, counted_by="user:asha"))

    assert report.agrees is True
    assert report.blocks_activation is False


def test_a_hand_counted_fanout_contradicts_a_recorded_fan_in_verdict() -> None:
    """`N:1` claims at most one match per source row; two is the fan-out the family refuses."""
    report = reconcile_direct_bridge(
        realization(),
        BridgeHandCountsV1(joined_row_count=6, max_matches_per_source_row=2,
                           read_addresses=BRIDGE_ADDRESSES))

    assert ReconciliationFinding.CARDINALITY_DISAGREES in report.findings
    assert "many_to_one" in " | ".join(report.detail)
    assert report.blocks_activation is True


def test_a_ONE_TO_MANY_realization_claims_nothing_a_count_could_contradict() -> None:
    """So reconciling one proves nothing, and the procedure says to pick a fan-in shape instead."""
    report = reconcile_direct_bridge(
        realization(cardinality=Cardinality.ONE_TO_MANY),
        BridgeHandCountsV1(joined_row_count=6, max_matches_per_source_row=2,
                           read_addresses=BRIDGE_ADDRESSES))

    assert report.agrees is True


def test_an_UNKNOWN_cardinality_is_an_absent_claim_not_a_free_pass() -> None:
    report = reconcile_direct_bridge(
        realization(),
        BridgeHandCountsV1(joined_row_count=6, max_matches_per_source_row=2,
                           read_addresses=BRIDGE_ADDRESSES))
    assert report.blocks_activation is True

    unknown = reconcile_direct_bridge(
        BridgeJoinRealizationRevisionV1(
            **{**{f: getattr(realization(), f) for f in (
                "bridge_fact_key", "from_endpoint", "to_endpoint", "column_pairs", "predicates",
                "applicability_scope", "cardinality_basis", "evidence_refs",
                "dependency_snapshot_id", "derivation_version", "admission_policy_version")},
               "cardinality": DirectionalCardinalityVerdictV1.unknown()}),
        BridgeHandCountsV1(joined_row_count=6, max_matches_per_source_row=2,
                           read_addresses=BRIDGE_ADDRESSES))
    # No claim, so nothing to contradict — and the report says so by finding nothing, not by
    # asserting the bridge is fine.
    assert unknown.agrees is True
    assert unknown.findings == ()


def test_the_bridge_half_catches_the_wrong_tables_too() -> None:
    report = reconcile_direct_bridge(
        realization(),
        BridgeHandCountsV1(joined_row_count=3, max_matches_per_source_row=1,
                           read_addresses=("cib::elsewhere.accounts", BRIDGE_ADDRESSES[1])))

    assert ReconciliationFinding.ADDRESS_DISAGREES in report.findings


# ── the procedure document and the code stay in step ────────────────────────────────────────────

def test_the_procedure_document_names_every_finding_the_code_can_return() -> None:
    """A procedure that cannot explain an outcome the code produces is an incomplete procedure."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[4].joinpath(
        "docs/architecture/2026-08-05-crosswalk-hand-reconciliation-procedure.md")
    text = doc.read_text()
    assert "reconcile_crosswalk" in text and "reconcile_direct_bridge" in text
    for finding in (ReconciliationFinding.ADDRESS_DISAGREES,
                    ReconciliationFinding.ROW_RULE_DISAGREES,
                    ReconciliationFinding.NOT_MEASURED):
        assert finding.name in text, f"the procedure never tells a reader what {finding.name} means"
    # And it states the rule that makes the whole exercise worth running.
    assert "blocks_activation" in text
    assert "no tolerance" in text.lower()
