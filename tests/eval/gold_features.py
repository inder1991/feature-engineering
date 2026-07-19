"""Curated gold set for the Slice-3 feature-gen quality gate (spec §9): objective -> expert-expected
feature. A VERSIONED artifact — grow it as reviewers adjudicate more objectives; keep >= 40 cases.

INVARIANTS (enforced by test_feature_eval.py):
- len(GOLD) >= 40
- every `expected_operations` is a non-empty subset of OPERATION_VOCAB
- every `expected_disposition` is in DISPOSITIONS
- every `relevance_terms` and `expected_columns` is non-empty
- objectives are unique (no duplicate case)

`expected_columns` are object_refs an expert feature would derive from; `relevance_terms` are lowercased
objective anchors the scorer credits a feature name against; `expected_disposition` is the honest state
an expert expects for that feature given a governed catalog (numeric ops on FTR-style unknown-operational
measures land in NEEDS_EXTERNAL_VALIDATION; confirmed-grain counts land in DESIGN_CHECKED)."""
from __future__ import annotations

from dataclasses import dataclass

OPERATION_VOCAB = frozenset({
    "sum", "count", "count_distinct", "avg", "ratio", "recency", "min", "max", "stddev", "trend",
})
DISPOSITIONS = frozenset({"DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION"})


@dataclass(frozen=True, slots=True)
class GoldFeature:
    objective: str
    entity: str | None
    catalog_source: str | None
    expected_columns: frozenset[str]
    expected_operations: frozenset[str]
    expected_disposition: str
    relevance_terms: frozenset[str]


GOLD: list[GoldFeature] = [
    GoldFeature("predict account churn from spending drop", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"sum", "avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"spending", "spend", "churn", "amount"})),
    GoldFeature("count transactions per account in the last 90 days", "Account", "bank",
                frozenset({"public.transactions.txn_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"transactions", "count", "account"})),
    GoldFeature("recency of last transaction per account", "Account", "bank",
                frozenset({"public.transactions.txn_date"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "last", "transaction"})),
    GoldFeature("distinct merchants a customer transacted with", "Customer", "bank",
                frozenset({"public.transactions.merchant_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "merchants", "customer"})),
    GoldFeature("ratio of debit to credit volume per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"ratio"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"ratio", "debit", "credit", "volume"})),
    GoldFeature("average balance held per customer", "Customer", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"average", "balance", "customer"})),
    GoldFeature("total loan exposure per customer", "Customer", "bank",
                frozenset({"public.loans.principal"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"loan", "exposure", "total"})),
    GoldFeature("days past due trend per loan", "Loan", "bank",
                frozenset({"public.loans.dpd"}), frozenset({"trend", "max"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"days", "past", "due", "trend", "loan"})),
    GoldFeature("count of declined card authorizations per account", "Account", "bank",
                frozenset({"public.card_auth.auth_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"declined", "authorizations", "card"})),
    GoldFeature("standard deviation of transaction amount per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"stddev"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"deviation", "transaction", "amount"})),
    GoldFeature("recency of last login per customer", "Customer", "bank",
                frozenset({"public.sessions.login_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "last", "login"})),
    GoldFeature("count of distinct product holdings per customer", "Customer", "bank",
                frozenset({"public.holdings.product_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "product", "holdings"})),
    GoldFeature("maximum single transaction amount per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"max"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"maximum", "transaction", "amount"})),
    GoldFeature("minimum balance over the period per account", "Account", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"min"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"minimum", "balance", "period"})),
    GoldFeature("count of active accounts per customer", "Customer", "bank",
                frozenset({"public.accounts.account_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"active", "accounts", "customer"})),
    GoldFeature("total fees charged per account", "Account", "bank",
                frozenset({"public.transactions.fee"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"total", "fees", "charged"})),
    # --- ratio / utilization ---------------------------------------------------------------------
    GoldFeature("credit utilization ratio per card", "Card", "bank",
                frozenset({"public.cards.balance", "public.cards.credit_limit"}),
                frozenset({"ratio"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"utilization", "credit", "ratio", "card"})),
    GoldFeature("ratio of fees to total spend per account", "Account", "bank",
                frozenset({"public.transactions.fee", "public.transactions.amount"}),
                frozenset({"ratio", "sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"ratio", "fees", "spend"})),
    GoldFeature("share of cash withdrawals in transaction count per account", "Account", "bank",
                frozenset({"public.transactions.txn_type", "public.transactions.txn_id"}),
                frozenset({"ratio", "count"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"share", "cash", "withdrawals"})),
    GoldFeature("share of international transactions per account", "Account", "bank",
                frozenset({"public.transactions.country", "public.transactions.txn_id"}),
                frozenset({"ratio", "count"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"share", "international", "transactions"})),
    GoldFeature("loan payment to income ratio per customer", "Customer", "bank",
                frozenset({"public.payments.amount", "public.customers.declared_income"}),
                frozenset({"ratio", "sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"payment", "income", "ratio", "loan"})),
    # --- temporal / recency ----------------------------------------------------------------------
    GoldFeature("days since account opening", "Account", "bank",
                frozenset({"public.accounts.opened_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"days", "since", "opening", "account"})),
    GoldFeature("recency of last loan payment per loan", "Loan", "bank",
                frozenset({"public.payments.payment_date"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "last", "payment", "loan"})),
    GoldFeature("recency of most recent card authorization per card", "Card", "bank",
                frozenset({"public.card_auth.auth_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "recent", "authorization"})),
    GoldFeature("days since last dispute was opened per customer", "Customer", "bank",
                frozenset({"public.disputes.opened_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"days", "since", "dispute", "opened"})),
    GoldFeature("trend of monthly spend per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"trend", "sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"trend", "monthly", "spend"})),
    GoldFeature("month over month change in balance per account", "Account", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"trend"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"month", "change", "balance"})),
    GoldFeature("trend of credit utilization over six months per card", "Card", "bank",
                frozenset({"public.cards.balance", "public.cards.credit_limit"}),
                frozenset({"trend", "ratio"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"trend", "utilization", "months", "card"})),
    # --- distributional (stddev / z-score / percentile-style) ------------------------------------
    GoldFeature("z-score of latest transaction amount against account history", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"stddev", "avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"score", "transaction", "amount",
                                                        "history"})),
    GoldFeature("percentile rank of customer balance across all customers", "Customer", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"stddev", "avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"percentile", "rank", "balance"})),
    GoldFeature("volatility of monthly login counts per customer", "Customer", "bank",
                frozenset({"public.sessions.login_at"}), frozenset({"stddev", "count"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"volatility", "monthly", "login"})),
    GoldFeature("dispersion of payment amounts per loan", "Loan", "bank",
                frozenset({"public.payments.amount"}), frozenset({"stddev"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"dispersion", "payment", "amounts"})),
    # --- unary transforms ------------------------------------------------------------------------
    GoldFeature("log of total transaction volume per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"log", "volume", "transaction"})),
    GoldFeature("flag whether customer holds a mortgage product", "Customer", "bank",
                frozenset({"public.holdings.product_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"flag", "mortgage", "holds", "product"})),
    GoldFeature("absolute change in days past due since last statement per loan", "Loan", "bank",
                frozenset({"public.loans.dpd"}), frozenset({"trend"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"absolute", "change", "past", "due"})),
    # --- cross-entity / cross-catalog (aggregation over children) --------------------------------
    GoldFeature("total credit limit across cards per customer", "Customer", "bank",
                frozenset({"public.cards.credit_limit"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"total", "credit", "limit", "cards"})),
    GoldFeature("maximum days past due across loans per customer", "Customer", "bank",
                frozenset({"public.loans.dpd"}), frozenset({"max"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"maximum", "past", "due", "loans"})),
    GoldFeature("total payment amount received across loans per customer", "Customer", "bank",
                frozenset({"public.payments.amount"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"total", "payment", "received", "loans"})),
    GoldFeature("blend bureau credit score with internal card utilization per customer",
                "Customer", "risk",
                frozenset({"risk.scores.credit_score", "public.cards.credit_limit"}),
                frozenset({"ratio", "avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"bureau", "credit", "score",
                                                        "utilization"})),
    GoldFeature("recency of last bureau score refresh per customer", "Customer", "risk",
                frozenset({"risk.scores.scored_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "bureau", "score", "refresh"})),
    # --- counts over confirmed-grain identifiers -------------------------------------------------
    GoldFeature("count of loan payments in the last 12 months per loan", "Loan", "bank",
                frozenset({"public.payments.payment_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"count", "payments", "loan", "months"})),
    GoldFeature("count of distinct branches a customer visited", "Customer", "bank",
                frozenset({"public.branch_visits.branch_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "branches", "visited"})),
    GoldFeature("count of open disputes per customer", "Customer", "bank",
                frozenset({"public.disputes.dispute_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"count", "open", "disputes"})),
    GoldFeature("count of distinct devices used to log in per customer", "Customer", "bank",
                frozenset({"public.sessions.device_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "devices", "login"})),
    GoldFeature("count of failed login attempts per customer", "Customer", "bank",
                frozenset({"public.sessions.session_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"count", "failed", "login", "attempts"})),
    # --- additional measure aggregations ---------------------------------------------------------
    GoldFeature("average payment amount per loan", "Loan", "bank",
                frozenset({"public.payments.amount"}), frozenset({"avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"average", "payment", "amount", "loan"})),
    GoldFeature("average days to resolve a dispute per customer", "Customer", "bank",
                frozenset({"public.disputes.opened_at", "public.disputes.resolved_at"}),
                frozenset({"avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"average", "days", "resolve", "dispute"})),
    GoldFeature("minimum payment amount per loan", "Loan", "bank",
                frozenset({"public.payments.amount"}), frozenset({"min"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"minimum", "payment", "amount", "loan"})),
    GoldFeature("average session duration per customer", "Customer", "bank",
                frozenset({"public.sessions.duration_sec"}), frozenset({"avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"average", "session", "duration"})),
]
