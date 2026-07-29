"""Payments vocabulary — the gaps the transaction catalog still shows.

Grounded in what FTR's 126 columns actually landed on, after the customer-master and label work:

  free_text   tran_narration_1, tran_narration_2, sender_to_recvr_info, inter_bank_information
  pii         counter_party_address, counter_party_mob_num, counter_party_phone_no,
              hsmi_customer_address, initiating_party, ultimate_debtor
  unclassified  unique_hash_key
  boolean_flag  acct_stmt_flg, exclude_from_statement

Three SME observations behind the additions:

* PAYMENT NARRATIVE is the single richest signal in transaction data — it drives categorisation,
  merchant identification and AML screening — and it was landing on `free_text`, which says only
  "prose". It also routinely CONTAINS names and account numbers, so it needs a pii floor of its own
  rather than inheriting one by luck.
* `pii` is a CLASSIFICATION, not a concept. Collapsing an address, a phone number and a payment
  party role into one word loses the handling difference: a phone can be tokenised, an address can
  be generalised to a region feature, a party role is a structural field of the message.
* `initiating_party` and `ultimate_debtor` are ISO 20022 fields with specific AML meaning — the
  ultimate debtor is who the money is REALLY from, which is exactly the question a screening model
  asks and exactly what `pii` erases.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.concepts import concept

_NEW = ["payment_narrative", "initiating_party", "ultimate_debtor", "ultimate_creditor",
        "postal_address", "phone_number", "email_address", "row_hash",
        "statement_visibility_flag"]


@pytest.mark.parametrize("name", _NEW)
def test_the_concept_exists_and_is_described(name):
    c = concept(name)
    assert c is not None, f"{name} is missing"
    assert len(c.description) > 40


# ── the personal ones carry a floor ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["payment_narrative", "postal_address", "phone_number",
                                  "email_address", "initiating_party", "ultimate_debtor",
                                  "ultimate_creditor"])
def test_personal_data_is_not_public_by_default(name):
    """These replace a bare `pii` classification; losing the floor while gaining the meaning would
    be a straight downgrade."""
    assert concept(name).sensitivity != "public"


# ── THE trap this vocabulary must not repeat ─────────────────────────────────────────────────────

def test_a_row_hash_can_never_bridge_catalogs(name="row_hash"):
    """`unique_hash_key` is a dedupe/surrogate key, not a business entity. If it were an identifier
    with an entity_link, `derive_bridge_candidates` would pair EVERY hash column in EVERY catalog —
    the same mechanism that turned six `branch_id` columns into eight false joins, at far greater
    scale and with far more plausible-looking results."""
    c = concept(name)
    assert c.group != "identifier"
    assert c.entity_link is None


@pytest.mark.parametrize("name", ["postal_address", "phone_number", "email_address",
                                  "payment_narrative"])
def test_contact_details_are_not_join_keys_either(name):
    """A shared phone number is a fraud SIGNAL, not a join key — linking two catalogs on it would
    silently merge unrelated parties."""
    assert concept(name).entity_link is None


# ── aggregation honesty ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", _NEW)
def test_none_of_these_are_additive(name):
    assert concept(name).additivity in {"n/a", "non_additive"}


# ── the ISO 20022 pair is distinguishable ────────────────────────────────────────────────────────

def test_ultimate_and_initiating_parties_are_distinct_concepts():
    """The ultimate debtor is who the money is REALLY from; the initiating party is who pushed the
    button. Conflating them is precisely what makes a screening model useless."""
    assert concept("ultimate_debtor") is not concept("initiating_party")
    assert "ultimate" in concept("ultimate_debtor").description.lower()


def test_the_existing_pii_concept_is_untouched():
    """The broad classification stays for a column that is genuinely just "personal data"."""
    assert concept("pii").sensitivity == "pii"
