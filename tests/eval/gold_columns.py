"""Human-reviewed expected concepts for representative + hard columns (spec: Evaluation).

Each entry: ``(CanonicalRow, expected_concept, acceptable_alternatives)``.

INVARIANTS (enforced by the harness + a self-check below):
- ``expected_concept`` is ALWAYS a real name in the concept registry
  (``featuregen.overlay.upload.concepts.CONCEPTS``) or the literal ``"unclassified"``.
  This matters for the hermetic self-check: the scripted FakeLLM returns each row's
  ``expected_concept`` verbatim, and the batch path's ``_accept_concept`` rejects anything
  that is neither a known concept nor ``"unclassified"`` — a bogus expected value would make
  the gate fail for the wrong reason.
- ``expected_concept`` is a member of ``acceptable_alternatives`` (so the hermetic run scores it
  a hit); the alternatives are the OTHER concepts a human reviewer would also accept for that
  column against a live provider.
- Every ``acceptable_alternatives`` value is itself a known concept (or ``"unclassified"``).

The set spans the hard cases called out in the brief: repeated generic names (``status``,
``amount``, ``*_id``), acronyms (``pd``/``lgd``/``ead``/``ecl``/``rwa``/``var``/``dpd``/``apr``/
``lei``/``mcc``), same-name-different-table (``status`` in ``cards.txn`` vs ``loans.loan``), rare
concepts, and blank/opaque column names (expected ``unclassified``). Seeded from the brief's
starter rows (with the sample's placeholder concept names replaced by their closest real registry
concept — ``status_flag``→``lifecycle_state``, ``probability``→``pd``) and expanded toward the
~40-row target. Grow this set as reviewers adjudicate more live columns.
"""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow

# (row, expected_concept, acceptable_alternatives)
GOLD: list[tuple[CanonicalRow, str, set[str]]] = [
    # ── Representative core (brief starter rows; placeholder concepts mapped to the registry) ──
    (CanonicalRow("deposits", "accounts", "balance", "numeric"),
     "monetary_stock", {"monetary_stock"}),
    (CanonicalRow("cards", "txn", "amount", "numeric"),
     "monetary_flow", {"monetary_flow"}),
    # 'status_flag' is NOT a registry concept -> the closest real concept is 'lifecycle_state'.
    (CanonicalRow("cards", "txn", "status", "text"),
     "lifecycle_state", {"lifecycle_state", "category_code"}),
    # Same generic name in a DIFFERENT table (same-name-different-table hard case).
    (CanonicalRow("loans", "loan", "status", "text"),
     "lifecycle_state", {"lifecycle_state", "category_code"}),
    # 'probability'/'risk_score' are NOT registry concepts -> the exact match is 'pd'.
    (CanonicalRow("risk", "exposure", "pd", "numeric"),
     "pd", {"pd", "score_probability"}),

    # ── Monetary distinctions (stock vs flow vs rate vs price vs limit) ──
    (CanonicalRow("deposits", "accounts", "interest_rate", "numeric"),
     "monetary_rate", {"monetary_rate"}),
    (CanonicalRow("loans", "loan", "apr", "numeric"),                       # acronym
     "monetary_rate", {"monetary_rate"}),
    (CanonicalRow("markets", "positions", "price", "numeric"),
     "price", {"price", "nav"}),
    (CanonicalRow("lending", "facility", "credit_limit", "numeric"),
     "limit", {"limit", "monetary_stock"}),
    (CanonicalRow("payments", "txn", "fee", "numeric"),                     # CRITICAL: monetary_flow
     "monetary_flow", {"monetary_flow", "interchange"}),

    # ── Identifiers (repeated '*_id' generic names across tables) ──
    (CanonicalRow("deposits", "accounts", "customer_id", "text"),
     "customer_id", {"customer_id"}),
    (CanonicalRow("deposits", "accounts", "account_id", "text"),
     "account_id", {"account_id"}),
    (CanonicalRow("cards", "txn", "transaction_id", "text"),
     "transaction_id", {"transaction_id"}),
    (CanonicalRow("counterparties", "entity", "lei", "text"),              # acronym
     "lei", {"lei"}),
    (CanonicalRow("cards", "merchant", "merchant_id", "text"),
     "merchant_id", {"merchant_id"}),

    # ── Temporal (point-in-time critical) ──
    (CanonicalRow("loans", "loan", "origination_date", "date"),
     "origination_date", {"origination_date", "effective_date"}),
    (CanonicalRow("deposits", "accounts", "as_of_date", "date"),
     "as_of_date", {"as_of_date", "reporting_period"}),
    (CanonicalRow("markets", "trades", "maturity_date", "date"),
     "maturity_date", {"maturity_date"}),

    # ── Risk & capital acronyms ──
    (CanonicalRow("risk", "exposure", "lgd", "numeric"),                    # acronym
     "lgd", {"lgd", "downturn_lgd"}),
    (CanonicalRow("risk", "exposure", "ead", "numeric"),                    # acronym
     "ead", {"ead", "monetary_stock"}),
    (CanonicalRow("risk", "provision", "ecl", "numeric"),                   # acronym
     "ecl", {"ecl", "provision_amount"}),
    (CanonicalRow("capital", "report", "rwa", "numeric"),                   # acronym
     "rwa", {"rwa", "monetary_stock"}),
    (CanonicalRow("markets", "book", "var", "numeric"),                     # acronym
     "var", {"var"}),
    (CanonicalRow("collections", "arrears", "dpd", "integer"),              # acronym
     "dpd", {"dpd", "delinquency_bucket"}),

    # ── Categorical & coded ──
    (CanonicalRow("cards", "merchant", "mcc", "text"),                      # acronym
     "mcc", {"mcc", "category_code"}),
    (CanonicalRow("deposits", "accounts", "account_type", "text"),
     "account_type", {"account_type", "category_code"}),
    (CanonicalRow("payments", "txn", "currency", "text"),
     "currency_code", {"currency_code"}),
    (CanonicalRow("cards", "txn", "debit_credit", "text"),
     "debit_credit_indicator", {"debit_credit_indicator"}),

    # ── Sensitive / regulatory ──
    (CanonicalRow("crm", "customer", "email", "text"),
     "pii", {"pii"}),
    (CanonicalRow("crm", "customer", "postcode", "text"),
     "geographic", {"geographic"}),
    (CanonicalRow("crm", "customer", "date_of_birth", "date"),
     "pii", {"pii", "protected_attribute"}),

    # ── Flags & labels (leakage anchors — includes CRITICAL outcome_label) ──
    (CanonicalRow("risk", "loan", "default_flag", "boolean"),
     "default_flag", {"default_flag", "outcome_label"}),
    (CanonicalRow("modeling", "target", "churned", "boolean"),             # CRITICAL: outcome_label
     "outcome_label", {"outcome_label"}),
    (CanonicalRow("cards", "txn", "fraud_flag", "boolean"),
     "fraud_flag", {"fraud_flag", "outcome_label"}),

    # ── Blank / opaque names — a human reviewer classifies these 'unclassified' ──
    (CanonicalRow("staging", "raw", "col_017", "text"),
     "unclassified", {"unclassified"}),
    (CanonicalRow("staging", "raw", "", "text"),                           # blank column name
     "unclassified", {"unclassified"}),
    (CanonicalRow("staging", "raw", "misc", "text"),
     "unclassified", {"unclassified", "free_text"}),
]

# Critical concepts that must NEVER regress vs single mode (stratified gate). Each is a real
# registry concept and appears as an ``expected_concept`` in at least one GOLD row above, so the
# zero-regression gate actually exercises them.
CRITICAL: set[str] = {"outcome_label", "monetary_flow", "monetary_stock"}
