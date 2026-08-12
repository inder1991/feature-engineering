"""FIX 4 — an identifier whose own wording says BANK cannot be a party's identifier.

THE LIVE DEFECT (measured 2026-08-09). Seven FTR columns carried the concept `counterparty_id`
while their own source-attested definitions opened "Correspondent Receiver Bank Code", "Third
Reimbursement Institution Code", "Sender Bank Identifier Code". Because `counterparty_id`
canonicalizes to `customer_id` and declares the `cif` namespace, every one of them became a
cross-catalog bridge CANDIDATE against `cib.bo_cib_customer.cust_num` — eight proposals asserting
that a bank's SWIFT code identifies the same entity as a customer number.

WHY THIS IS AN ENTITY RULE, NOT A NAMESPACE ONE. `_claimed_namespaces` deliberately covers only
shapes with one unambiguous namespace (a BIC, a UUID/UETR); its comment records that scheme codes,
CIFs and serials are "shape-anonymous" and stay the critic's question. That reasoning is correct and
this rule does not touch it: a "Bank Code" genuinely does NOT reveal its scheme — national,
correspondent, or in-house. What it does reveal, unambiguously, is WHO it identifies. An identifier
of a BANK cannot be an identifier of a customer or counterparty whatever scheme it belongs to. So
the refutation is made on the weaker axis that IS decidable, which is also the axis the live defect
crossed.

The namespace rule alone caught 4 of the 7 (those whose name carries the token "bic"); the three
wording variants — "Bank Code", "Institution Code" — passed clean. With this rule the deterministic
gate covers all seven, measured against the real catalog with zero false positives across all 237
columns.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.attest.representation import SHAPE_CONFLICT_CODES, shape_conflicts

_CODE = "identifier_entity_mismatch"

#: The seven live columns, with their real source-attested definitions (opening clause).
_MISCLASSIFIED = [
    ("corres_bank_receiver_code", "Correspondent Receiver Bank Code supports payment-message and "
                                  "interbank traceability for the financial transaction record."),
    ("corres_bank_sender_code", "Correspondent Sender Bank Code supports payment-message and "
                                "interbank traceability for the financial transaction record."),
    ("third_reimb_inst_code", "Third Reimbursement Institution Code supports payment-message and "
                              "interbank traceability for the financial transaction record."),
    ("sender_bic", "Sender Bank Identifier Code supports payment-message and interbank "
                   "traceability for the financial transaction record."),
    ("receiver_bic", "Receiver Bank Identifier Code supports payment-message and interbank "
                     "traceability."),
    ("counter_party_bic", "Counterparty Bank Identifier Code describes an attribute of the "
                          "counterparty side of the financial transaction."),
    ("corres_bank_intermediary_bic", "Correspondent Intermediary Bank BIC supports payment-message "
                                     "and interbank traceability."),
]


def test_the_code_is_in_the_closed_set() -> None:
    """`shape_conflicts` raises on any code outside the set, so an unregistered code is a landmine
    that only fires on the input that produces it."""
    assert _CODE in SHAPE_CONFLICT_CODES


@pytest.mark.parametrize(("column", "definition"), _MISCLASSIFIED,
                         ids=[c for c, _ in _MISCLASSIFIED])
def test_a_bank_identifier_labelled_as_a_party_identifier_is_refuted(column, definition) -> None:
    assert _CODE in shape_conflicts(column, "string", definition, "counterparty_id")


@pytest.mark.parametrize("column", ["corres_bank_receiver_code", "corres_bank_sender_code",
                                    "third_reimb_inst_code"])
def test_the_three_the_namespace_rule_could_not_reach(column) -> None:
    """These carry no "bic" token, so `identifier_namespace_mismatch` cannot fire — they are the
    reason this rule exists rather than a widening of the namespace tokens.

    Uses each column's REAL definition. An earlier draft synthesised one from the column name
    (`"Corres Bank Receiver Code"`), which the adjacency rule correctly declined — the real term is
    "Correspondent Receiver Bank Code", where the institution word DOES modify the identifier noun.
    A fixture invented from the identifier under test proves nothing about the identifier."""
    definition = dict(_MISCLASSIFIED)[column]
    conflicts = shape_conflicts(column, "string", definition, "counterparty_id")
    assert _CODE in conflicts
    assert "identifier_namespace_mismatch" not in conflicts


# ── the half that matters more: what must NOT be refuted ────────────────────────────────────────

def test_a_correctly_bank_linked_concept_is_untouched() -> None:
    """The rule refutes only a PARTY-linked concept. A column that says bank AND is classified as a
    bank identifier is a clean assignment — the commonest shape in a payments catalog."""
    for concept_name in ("bank_bic", "clearing_member_code"):
        assert _CODE not in shape_conflicts(
            "sender_bic", "string", "Sender Bank Identifier Code.", concept_name)


def test_a_genuine_customer_identifier_is_untouched() -> None:
    """`cif_id` and `counter_party_cif_id` are the two columns in the live pair that were CORRECT;
    a rule that refuted them would be worse than the defect."""
    assert _CODE not in shape_conflicts(
        "cif_id", "string", "Customer Information File Identifier is the customer information file "
                            "identifier that connects the customer to the transaction.", "customer_id")
    assert _CODE not in shape_conflicts(
        "counter_party_cif_id", "string", "Counterparty Customer Information File Identifier "
                                          "describes an attribute of the counterparty side.",
        "counterparty_id")


def test_merely_mentioning_a_bank_does_not_fire() -> None:
    """Both an institution word AND an identifier word are required, so ordinary prose that happens
    to name a bank cannot refute a correct party identifier."""
    assert _CODE not in shape_conflicts(
        "cust_num", "string", "The customer number for the account held at the bank.", "customer_id")


def test_a_non_identifier_concept_is_out_of_scope() -> None:
    """The rule lives in the identifier branch; a bank NAME is a label, and labels have their own
    rules. Asserted so a later refactor cannot quietly widen the blast radius."""
    assert _CODE not in shape_conflicts(
        "counter_party_bank_name", "string", "Counterparty Bank Name.", "beneficiary_bank")


def test_a_bank_that_ISSUES_the_identifier_does_not_fire() -> None:
    """THE REGRESSION. The first draft asked whether an institution word and an identifier word both
    appeared ANYWHERE, and refuted this CORRECT assignment — the exact wording of the live
    `counter_party_cif_id` glossary term, caught by the Pass-A golden payload:

        "The bank's own customer information file identifier for the counterparty."

    "the bank's own" is POSSESSIVE: the bank ISSUES the identifier, the customer is what it
    identifies. Only adjacency tells that apart from "Bank Code", where the institution word
    modifies the identifier noun. A rule that refutes correct assignments is worse than the defect
    it was written for."""
    assert _CODE not in shape_conflicts(
        "counter_party_cif_id", "varchar(20)",
        "The bank's own customer information file identifier for the counterparty.", "customer_id")


def test_interbank_traceability_prose_does_not_fire_on_its_own() -> None:
    """All seven live definitions end "...supports payment-message and interbank traceability...".
    If that clause were the trigger, the rule would fire on every payments column in the catalog
    rather than on the seven whose SUBJECT is a bank identifier."""
    assert _CODE not in shape_conflicts(
        "pstd_date", "date",
        "Posted date supports payment-message and interbank traceability for the record.",
        "customer_id")
