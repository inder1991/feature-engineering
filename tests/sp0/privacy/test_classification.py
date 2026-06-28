import pytest

from sp0.privacy.classification import (
    BODY_CLASSIFICATIONS,
    GOVERNANCE_RETAINED,
    PII_ERASABLE,
    InlinePIIError,
    assert_references_only,
    validate_classification,
)


def test_classification_values_match_ddl():
    assert PII_ERASABLE == "pii-erasable"
    assert GOVERNANCE_RETAINED == "governance-retained"
    assert BODY_CLASSIFICATIONS == ("pii-erasable", "governance-retained")
    validate_classification("pii-erasable")
    with pytest.raises(ValueError):
        validate_classification("public")


def test_references_only_accepts_blob_and_doc_refs():
    assert_references_only(
        {"raw_input_ref": "blob_abc", "confirmed_contract_ref": "doc_xyz"},
        sensitive_fields=("raw_input_ref", "confirmed_contract_ref"),
    )


def test_references_only_rejects_inline_bodies_and_skips_absent_fields():
    with pytest.raises(InlinePIIError):
        assert_references_only(
            {"raw_input_ref": {"text": "SSN 123-45-6789"}}, sensitive_fields=("raw_input_ref",)
        )
    with pytest.raises(InlinePIIError):
        assert_references_only(
            {"raw_input_ref": "the customer's salary is ..."}, sensitive_fields=("raw_input_ref",)
        )
    assert_references_only({}, sensitive_fields=("raw_input_ref",))  # absent => no raise
