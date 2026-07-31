"""The representation ruleset's NEW namespace-shape extension (ingestion-richness Task 2).

``shape_conflicts`` is the deterministic corroboration layer under the concept critic: given ONLY a
column's name, declared type, definition and its proposed concept, it returns the closed conflict
codes that refute an identifier assignment — without any LLM and without any network. The moved
representation machinery (``representation_role``/``type_family``) stays pinned by the unchanged
bridge-grounding suite; these tests cover the extension plus the public seam the critic consumes.
"""
from __future__ import annotations

from featuregen.overlay.upload.attest.representation import (
    RepresentationRole,
    representation_role,
    shape_conflicts,
    type_family,
)


def test_bic_shape_refutes_counterparty_id():
    c = shape_conflicts("counter_party_bic", "string",
                        "SWIFT BIC of the counterparty bank", "counterparty_id")
    assert "identifier_namespace_mismatch" in c    # bic-shaped name+definition vs party concept


def test_desc_suffix_refutes_identifier():
    # delegates to the MOVED representation_role: sol_desc -> DESCRIPTION_TEXT
    c = shape_conflicts("sol_desc", "string", "Branch description", "branch_id")
    assert "name_or_description_not_identifier" in c


def test_amount_refutes_identifier():
    c = shape_conflicts("actual_counter_party_amt", "double",
                        "Actual counterparty amount", "counterparty_id")
    assert "measure_not_identifier" in c


def test_clean_identifier_passes():
    assert shape_conflicts("cif_id", "string", "Customer CIF", "customer_id") == ()


def test_bic_under_the_bic_concept_is_not_a_conflict():
    # The same BIC shape under the concept whose registry namespace IS swift_bic must pass — the
    # code refutes the WRONG namespace, never the shape itself.
    assert shape_conflicts("sender_bic", "string", "SWIFT BIC of the sender bank",
                           "bank_bic") == ()


def test_uetr_shape_refutes_an_internal_transaction_id():
    # A UETR is a UUID payment-trace namespace of its own; classifying it as the core system's
    # transaction_id (core_serial) is exactly the live conflation the audit found.
    c = shape_conflicts("uetr", "string",
                        "SWIFT gpi UETR, a UUID tracing the payment end-to-end", "transaction_id")
    assert "identifier_namespace_mismatch" in c
    assert shape_conflicts("uetr", "string",
                           "SWIFT gpi UETR, a UUID tracing the payment end-to-end",
                           "swift_uetr") == ()


def test_exact_word_tokens_never_substrings():
    # "mandate" contains "date" and "grate" contains "rate" only as SUBSTRINGS — neither may fire
    # a token rule. A genuine `_rate_` word token must.
    assert shape_conflicts("mandate_id", "string", "Direct debit mandate", "customer_id") == ()
    assert "measure_not_identifier" in shape_conflicts(
        "cust_buy_rate", "double", None, "customer_id")


def test_fractional_declared_type_alone_refutes_identifier():
    # A double can never hold an identifier even when the name looks like one; an INTEGER can
    # (customer numbers are routinely integral) so it must NOT fire.
    assert "measure_not_identifier" in shape_conflicts(
        "cust_ref", "double", None, "customer_id")
    assert shape_conflicts("cust_ref", "bigint", None, "customer_id") == ()


def test_non_identifier_concepts_are_out_of_scope():
    # The extension corroborates IDENTIFIER assignments only; measures/labels pass through — the
    # critic's non-identifier path is a different slice.
    assert shape_conflicts("actual_counter_party_amt", "double",
                           "Actual counterparty amount", "monetary_flow") == ()
    assert shape_conflicts("sol_desc", "string", "Branch description", "branch_name") == ()
    assert shape_conflicts("anything", "string", None, "not_a_registry_concept") == ()


def test_moved_public_seam_is_importable_and_unchanged():
    # The critic consumes the PUBLIC names; spot-pin the moved behavior at the new import path.
    assert type_family("varchar(50)") == "text"
    role = representation_role(column_name="sol_desc", definition="Branch description",
                               concept_name="branch_id", observed_format=None,
                               data_type_family="text")
    assert role is RepresentationRole.DESCRIPTION_TEXT
