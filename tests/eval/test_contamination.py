"""CI coverage for the cross-item contamination metric (MF-8b).

Deliberately UNMARKED (no ``eval`` marker) and SDK-free/DB-free: the metric logic must be exercised
by default CI even though the sweep harness that consumes it only runs with a live provider key.
"""
from __future__ import annotations

from tests.eval.contamination import (
    Item,
    contaminated_refs,
    contamination_rate,
    generic_terms,
    item_from_text,
    per_item_echoes,
    sibling_distinctive_terms,
    tokenize,
)


def test_tokenize_drops_short_and_stopwords():
    toks = tokenize("The balance of the Account id")
    assert "balance" in toks and "account" in toks
    assert "the" not in toks   # stopword
    assert "of" not in toks    # too short
    assert "id" not in toks    # too short


def test_clean_answers_have_zero_contamination():
    # Each answer talks only about its own column — no sibling leakage.
    items = [
        item_from_text("a", "cards txn merchant_id text", "The merchant that acquired the card."),
        item_from_text("b", "deposits accounts balance numeric", "The balance held on the account."),
    ]
    assert contamination_rate(items) == 0.0
    assert contaminated_refs(items) == set()


def test_sibling_token_echo_is_flagged():
    # Item 'a' is a merchant column; item 'b' is a balance column but its answer drags in the
    # sibling-distinctive token "merchant" — that is contamination.
    items = [
        item_from_text("a", "cards txn merchant_id text", "The merchant identifier."),
        item_from_text("b", "deposits accounts balance numeric",
                       "The balance, similar to a merchant total."),
    ]
    echoes = per_item_echoes(items)
    assert "merchant" in echoes["b"]
    assert echoes["a"] == set()          # 'a' legitimately uses its own token
    assert contaminated_refs(items) == {"b"}
    assert contamination_rate(items) == 0.5


def test_generic_shared_tokens_are_not_contamination():
    # "account" appears in BOTH items' identities -> generic baseline -> echoing it is fine even
    # though it is technically also a sibling's token.
    items = [
        item_from_text("a", "deposits account balance numeric", "The account balance."),
        item_from_text("b", "deposits account status text", "The account status flag."),
    ]
    assert "account" in generic_terms(items)
    assert "account" not in sibling_distinctive_terms(items)["a"]
    assert contamination_rate(items) == 0.0


def test_single_item_run_is_the_zero_baseline():
    # No siblings -> nothing to contaminate from -> 0.0 (the sweep's per-task baseline).
    items = [item_from_text("solo", "cards txn amount numeric", "A merchant fee amount somewhere.")]
    assert contamination_rate(items) == 0.0
    assert sibling_distinctive_terms(items)["solo"] == set()


def test_empty_run_is_zero_not_error():
    assert contamination_rate([]) == 0.0
    assert generic_terms([]) == set()


def test_item_dataclass_is_frozen():
    it = Item(ref="x", own_terms=frozenset({"a"}), answer="a")
    try:
        it.ref = "y"  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Item must be frozen")
