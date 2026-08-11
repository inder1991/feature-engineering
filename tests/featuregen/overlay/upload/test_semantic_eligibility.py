"""SE-4 — the eligibility fold: every known bad binding class refuses, honestly and actionably."""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.column_capabilities import ColumnCapabilityV1
from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.semantic_eligibility import (
    AUTHORITY_MATRIX,
    authority_matrix_hash,
    evaluate_operand,
)


def capability(**over) -> ColumnCapabilityV1:
    base = dict(
        object_ref="public.transactions.amount", table="transactions", column="amount",
        declared_type="numeric", type_family="numeric", is_grain=False, is_as_of=False,
        concept="monetary_flow", concept_authority="human/confirmed",
        identifier_namespace=None, identifier_like=False,
        possible_operand_classes=("measure",),
        operand_class_map_version=OPERAND_CLASS_MAP_VERSION,
        entity=None, entity_authority="absent",
        additivity="additive", additivity_authority="graph_hint",
        currency="USD", currency_authority="graph_hint",
        economic_role=None, economic_role_authority="absent",
        missing_context=("dataset_profile_absent", "relationship_state_absent",
                         "use_policy_absent"),
        retrieval_text="signed transaction amount")
    base.update(over)
    return ColumnCapabilityV1(**base)


def operand(**over) -> RequiredOperandV1:
    base = dict(role="amount", concept="monetary_flow", operand_class="measure")
    base.update(over)
    return RequiredOperandV1(**base)


def test_confirmed_authority_clears_the_declared_floor():
    verdict = evaluate_operand(operand(), capability())
    assert verdict.status == "eligible"
    assert verdict.reason_codes == ()
    assert verdict.authority_observed == "human/confirmed"
    assert verdict.policy_content_hash == authority_matrix_hash()


def test_a_proposal_retrieves_but_never_clears_and_names_the_funnel():
    verdict = evaluate_operand(operand(), capability(concept_authority="llm/proposed"))
    assert verdict.status == "provisional"
    assert verdict.primary_reason_code == R.PROPOSED_METADATA_ONLY
    assert verdict.primary_family == "undecided"
    assert "concept-confirmation queue" in verdict.resolution


def test_a_display_only_graph_value_clears_nothing():
    verdict = evaluate_operand(operand(), capability(concept_authority="graph_hint"))
    assert verdict.status == "provisional"
    assert verdict.primary_reason_code == R.SEMANTIC_AUTHORITY_INSUFFICIENT


def test_an_unknown_authority_class_fails_closed():
    verdict = evaluate_operand(operand(), capability(concept_authority="oracle/divined"))
    assert verdict.status == "provisional"
    assert AUTHORITY_MATRIX.get("oracle/divined") is None


def test_a_different_meaning_is_not_applicable_never_a_lesser_match():
    verdict = evaluate_operand(
        operand(), capability(concept="consent_status",
                              possible_operand_classes=("policy_input",)))
    assert verdict.status == "not_applicable"
    assert verdict.reason_codes == (R.CONCEPT_MISMATCH,)
    assert verdict.primary_family == "structurally_unsuitable"


def test_an_identifier_never_serves_a_measure_even_confirmed():
    verdict = evaluate_operand(
        operand(concept="customer_id"),
        capability(concept="customer_id", concept_authority="human/confirmed",
                   identifier_namespace="cif", identifier_like=True,
                   possible_operand_classes=("dimension", "entity_key")))
    assert verdict.status == "blocked"
    assert R.IDENTIFIER_NOT_A_MEASURE in verdict.reason_codes
    assert "never a quantity" in verdict.resolution


def test_all_applicable_codes_are_collected_with_a_precedence_primary():
    """A varchar identifier offered as a measure with an unmet economic role: THREE truths at
    once, all named, structural first."""
    verdict = evaluate_operand(
        operand(concept="customer_id", economic_role="drawn_credit_exposure"),
        capability(concept="customer_id", concept_authority="llm/proposed",
                   declared_type="varchar(30)", type_family="text",
                   identifier_namespace="cif", identifier_like=True,
                   possible_operand_classes=("dimension", "entity_key")))
    assert verdict.status == "blocked"
    assert set(verdict.reason_codes) == {
        R.IDENTIFIER_NOT_A_MEASURE, R.TYPE_INCOMPATIBLE, R.ECONOMIC_ROLE_UNPROVEN}
    assert verdict.primary_reason_code == R.IDENTIFIER_NOT_A_MEASURE


def test_missing_evidence_is_never_blocked_and_checks_are_split_out():
    """Invariant 6: missing and contradictory are different conditions — a currency-expecting
    operand over a currency-less column is SETUP work, not a contradiction."""
    verdict = evaluate_operand(
        operand(currency_expectation="per_row", relationship_requirement="verified_join"),
        capability(currency=None, currency_authority="absent"))
    assert verdict.status == "provisional"
    assert R.CURRENCY_POLICY_MISSING in verdict.missing_checks
    assert R.RELATIONSHIP_REQUIRED in verdict.missing_checks
    assert R.CURRENCY_POLICY_MISSING in verdict.reason_codes


def test_economic_role_binds_only_over_governed_evidence():
    demanding = operand(economic_role="drawn_credit_exposure")
    unproven = evaluate_operand(demanding, capability())
    assert unproven.status == "blocked"
    assert R.ECONOMIC_ROLE_UNPROVEN in unproven.reason_codes
    proven = evaluate_operand(demanding, capability(
        economic_role="drawn_credit_exposure",
        economic_role_authority="human/confirmed"))
    assert proven.status == "eligible"


def test_the_policy_identity_moves_with_the_matrix():
    baseline = authority_matrix_hash()
    assert baseline == authority_matrix_hash()                 # deterministic
    try:
        AUTHORITY_MATRIX["llm/proposed"]["suggestion_at_declared"] = True
        assert authority_matrix_hash() != baseline             # any policy edit is a new policy
    finally:
        AUTHORITY_MATRIX["llm/proposed"]["suggestion_at_declared"] = False
    assert authority_matrix_hash() == baseline


def test_every_reason_code_has_a_family():
    codes = [getattr(R, name) for name in dir(R)
             if name.isupper() and isinstance(getattr(R, name), str)
             and not name.startswith("REASON")]
    for code in codes:
        assert R.reason_family(code), code
