import pytest

from sp0.governance.attributes import (
    APPROVAL_TYPES,
    VERIFICATION_STAMPS,
    GovernanceAttributes,
    GovernanceAttributeError,
    validate_governance_attributes,
)


def _attrs(**over):
    base = dict(
        feature_version_id="fv_1", feature_id="feat_1", produced_by_run="run_1",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="medium", approval_type="PRODUCTION",
    )
    base.update(over)
    return GovernanceAttributes(**base)


def test_vocabularies_match_ddl_check_constraints():
    assert VERIFICATION_STAMPS == ("DESIGN-CHECKED", "DATA-CHECKED", "USEFULNESS-CHECKED")
    assert APPROVAL_TYPES == ("EXPERIMENTAL", "PRODUCTION")


def test_valid_attributes_pass():
    validate_governance_attributes(_attrs(approved_use_cases=("churn", "fraud"), max_uses=10))


def test_unknown_verification_stamp_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(verification_stamp="USEFULNESS_CHECKED"))  # underscore is wrong


def test_unknown_approval_type_and_nonpositive_max_uses_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(approval_type="MAYBE"))
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(max_uses=0))


def test_empty_required_ids_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(feature_version_id=""))
