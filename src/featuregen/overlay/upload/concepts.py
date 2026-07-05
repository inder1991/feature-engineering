from __future__ import annotations

UNCLASSIFIED = "unclassified"

CONCEPTS: frozenset[str] = frozenset({
    "monetary_amount",
    "account_identifier",
    "customer_identifier",
    "as_of_date",
    "effective_date",
    "timestamp",
    "count",
    "rate_or_ratio",
    "category_code",
    "pii",
    "free_text",
})


def is_known_concept(c: str) -> bool:
    return c in CONCEPTS


def humanize(c: str) -> str:
    return c.replace("_", " ")
