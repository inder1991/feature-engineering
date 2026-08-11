"""BR-10 — the canonical banking event/lifecycle vocabulary, and its refusals.

The additions are grounded in the recipe audit's missing-concept admissions: every concept a
recipe had to apologize for ("no dedicated chargeback concept", "no promise_to_pay concept",
"no product_holding concept", ...) now exists as governed vocabulary with classifier-grade
NEGATIVES in its description — the sentences that keep an authorization feed, a core-ledger
posting table and a settlement feed from being read as interchangeable transaction tables.
"""
from __future__ import annotations

from featuregen.overlay.upload.concepts import (
    _LEGACY_ALIASES,
    CONCEPT_REGISTRY,
    classification_vocabulary,
    concept,
    concept_path,
)

#: Every BR-10 addition, as authored. The test below holds this list against the registry AND
#: against the alias hard rule, so a future edit cannot quietly revive a retired name.
BR10_CONCEPTS = (
    "original_transaction_id", "beneficiary_id", "original_amount",
    "authorization_status", "authorization_timestamp", "booking_status",
    "clearing_status", "clearing_timestamp", "reversal_indicator",
    "payment_return_status", "return_reason_code", "chargeback_status", "dispute_reason_code",
    "account_status", "product_holding", "notice_period", "available_limit", "drawn_principal",
    "due_date", "minimum_due_amount", "payment_allocation",
    "promise_amount", "promise_due_date", "promise_outcome",
    "contact_attempt_event", "contact_outcome", "right_party_contact_flag",
    "matching_status", "instruction_execution_outcome", "lc_guarantee_event",
    "claim_status", "claim_paid_amount", "invoice_status",
    "policy_loan_balance", "customer_income",
)


def test_every_admitted_gap_concept_now_exists_and_no_alias_was_revived():
    """The audit's admissions close, and the BR-plan hard rule holds: none of the additions
    carries a `_LEGACY_ALIASES` name — `counterparty_id` stays retired (counterparty is a party
    ROLE, never a revived identifier)."""
    for name in BR10_CONCEPTS:
        assert name in CONCEPT_REGISTRY, name
        assert name not in _LEGACY_ALIASES, name
    assert "counterparty_id" in _LEGACY_ALIASES        # still retired, untouched


def test_the_acceptance_named_concepts_are_present():
    """The BR-10 acceptance names its own proof: chargeback, right-party contact, promise
    outcome, invoice lifecycle and the installment schedule no longer need disclaimers."""
    for name in ("chargeback_status", "right_party_contact_flag", "promise_outcome",
                 "invoice_status", "due_date", "minimum_due_amount", "payment_allocation",
                 "product_holding"):
        assert name in CONCEPT_REGISTRY


def test_authorization_is_never_settlement_or_posting():
    """The refusal the plan orders: an authorization column must not be classified from — or
    into — a settlement or posting feed. Three DISTINCT concepts exist, and each status
    description names the neighbours it is not, which is the mechanism (the bank_bic precedent)
    that keeps the classifier from landing on a near-neighbour."""
    auth = concept("authorization_status")
    assert concept("booking_status") is not None and concept("settlement_status") is not None
    assert "not booking_status" in auth.description or "never booking_status" in auth.description
    assert "settlement_status" in auth.description
    ts = concept("authorization_timestamp")
    assert "not a core-ledger posting table" in ts.description
    assert "never interchangeable" in ts.description


def test_a_payee_is_never_the_beneficiary_bank():
    """The payee-from-beneficiary-bank refusal: beneficiary_id is an identifier in its OWN
    payee-registry namespace — never the destination bank (a categorical), never a CIF."""
    payee = concept("beneficiary_id")
    assert payee.group == "identifier" and payee.namespace == "payee_registry"
    assert payee.entity_link == "beneficiary"
    assert "Not beneficiary_bank" in payee.description
    bank = concept("beneficiary_bank")
    assert bank.group == "categorical" and bank.namespace is None    # a bank is not a payee id


def test_a_contact_attempt_is_never_its_cost():
    """The contact-from-cost refusal: an attempt is ACTIVITY (behavioural event), the cost is
    money (monetary flow), and the RPC flag is a quality of the outcome — none substitutes."""
    attempt = concept("contact_attempt_event")
    assert attempt.group == "behavioural"
    assert "cost_to_collect" in attempt.description
    cost = concept("cost_to_collect")
    assert cost.group == "monetary"
    rpc = concept("right_party_contact_flag")
    assert rpc.group == "flag" and "never derivable from cost" in rpc.description


def test_lifecycle_stages_are_not_collapsed():
    """"Define aliases without collapsing distinct lifecycle stages": return, chargeback and
    reversal are three concepts with three descriptions that name each other apart, and matching
    is a stage BEFORE settlement, not a synonym for it."""
    assert concept("payment_return_status") is not concept("chargeback_status")
    assert "chargeback" in concept("payment_return_status").description
    assert "return" in concept("chargeback_status").description.lower()
    assert "reversal_indicator" in concept("payment_return_status").description
    assert "Not settlement_status" in concept("matching_status").description


def test_behavioural_metadata_is_declared_not_defaulted():
    """Units, additivity, PIT roles and grains carry the banking behaviour downstream gates
    consume — each declared, none left on a wrong default."""
    assert concept("due_date").pit_role == "maturity"          # contractual-future, like maturity
    assert concept("promise_due_date").pit_role == "maturity"
    assert concept("authorization_timestamp").pit_role == "event"
    assert concept("drawn_principal").additivity == "semi_additive"
    assert concept("available_limit").additivity == "semi_additive"
    assert concept("claim_paid_amount").additivity == "additive"
    assert concept("original_amount").additivity == "additive"
    # is_a edges resolve into the governed hierarchy (concept_path walks to the root):
    assert concept_path("minimum_due_amount") == ("minimum_due_amount", "scheduled_amount",
                                                  "monetary_flow")
    assert concept_path("policy_loan_balance") == ("policy_loan_balance", "monetary_stock")
    assert concept_path("available_limit") == ("available_limit", "limit")


def test_the_registry_distinguishes_every_required_grain():
    """The acceptance's grain list: customer, account, facility, merchant, beneficiary and legal
    group are distinct entity links; counterparty is deliberately NOT an entity — it is the party
    ROLE the three-axis decision made it, carried by party_vocab over a cif identifier."""
    links = {c.entity_link for c in CONCEPT_REGISTRY.values() if c.entity_link}
    assert {"customer", "account", "facility", "merchant", "beneficiary",
            "customer_group"} <= links
    from featuregen.overlay.upload.party_vocab import PartyRole, normalize_party_role
    assert normalize_party_role("counter_party_cif_id") is PartyRole.COUNTERPARTY


def test_lineage_key_shares_the_transaction_namespace():
    """original_transaction_id holds transaction ids, so it declares the SAME namespace — equal
    values denote the same transaction, which is exactly what makes the lineage join real."""
    assert concept("original_transaction_id").namespace == concept("transaction_id").namespace
    assert concept("original_transaction_id").entity_link == "transaction"


def test_every_new_concept_reaches_the_enrichment_vocabulary():
    """Prompt and projection coverage is DERIVED from the registry, so presence in
    `classification_vocabulary()` (with the full description as the hint) is the coverage."""
    vocab = {entry["name"]: entry for entry in classification_vocabulary()}
    for name in BR10_CONCEPTS:
        assert name in vocab, name
        assert vocab[name]["hint"], name
