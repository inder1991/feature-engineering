"""Cross-item contamination metric for the batch-size sweep (MF-8b).

Batched enrichment sends N columns in ONE prompt. The risk a higher batch ceiling introduces is
CROSS-ITEM CONTAMINATION: an item's answer echoing a *sibling* item's distinctive facts — e.g. a
drafted definition for ``balance`` reusing the word ``merchant`` that belongs to a sibling
``merchant_id`` column. This module scores that leakage for one batch run. It is deliberately
SDK-free and DB-free so (a) the sweep harness can call it per batch size and (b) the metric logic
has its own CI coverage (``tests/eval/test_contamination.py``) that does NOT need a live provider.

The metric is a RATE in ``[0, 1]``: the fraction of items whose answer contains a token that is
distinctive to a SIBLING (belongs to another item's identity) and is neither one of the item's own
tokens nor a GENERIC token shared across most of the batch — the baseline that is subtracted out, so
domain-common words (``account``, ``customer`` …) that every definition legitimately reuses are not
counted as leakage. A single-item run has no siblings, so its rate is 0.0 — the natural baseline the
sweep compares larger batches against.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_LEN = 3
# Grammatical filler only — NOT domain words. Domain-common words are removed by the frequency
# baseline (``generic_terms``), not hard-coded here, so the metric stays corpus-agnostic.
_STOPWORDS = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "are", "was", "were", "has", "have",
    "had", "its", "per", "each", "any", "all", "one", "such", "which", "into", "out", "not",
    "value", "values", "field", "column", "table", "record", "records", "row", "rows", "data",
})


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, dropping short tokens and grammatical stopwords. Domain words
    are intentionally KEPT here and filtered later by the frequency baseline."""
    return {t for t in _TOKEN_RE.findall(text.lower())
            if len(t) >= _MIN_LEN and t not in _STOPWORDS}


@dataclass(frozen=True)
class Item:
    """One enrichment item in a batch. ``own_terms`` are the distinctive tokens of THIS item's
    identity (its table/column/type), ``answer`` is the generated text (concept, definition, or
    domain) whose tokens are checked for sibling leakage."""
    ref: str
    own_terms: frozenset[str]
    answer: str


def item_from_text(ref: str, identity: str, answer: str) -> Item:
    """Build an :class:`Item` by tokenizing ``identity`` (e.g. ``"cards txn merchant_id text"``)
    into ``own_terms``. ``answer`` is stored raw and tokenized when the metric runs."""
    return Item(ref=ref, own_terms=frozenset(tokenize(identity)), answer=answer)


def generic_terms(items: list[Item], *, min_fraction: float = 0.5) -> set[str]:
    """Tokens shared across the batch — the BASELINE of common words that are subtracted out
    (echoing them is not contamination). A term is generic when it appears in the ``own_terms`` of
    at least ``min_fraction`` of the items AND in at least two distinct items. The two-item floor
    keeps a term that is distinctive to a SINGLE item from being called generic in a small batch
    (in a 2-item batch, ``min_fraction*n == 1``, which a lone occurrence would otherwise clear)."""
    if not items:
        return set()
    df: Counter[str] = Counter()
    for it in items:
        df.update(set(it.own_terms))
    threshold = max(2.0, min_fraction * len(items))
    return {t for t, c in df.items() if c >= threshold}


def sibling_distinctive_terms(items: list[Item], *,
                              min_fraction: float = 0.5) -> dict[str, set[str]]:
    """For each item, the tokens that are distinctive to a SIBLING: present in some other item's
    ``own_terms``, absent from this item's own terms, and not generic (baseline)."""
    generic = generic_terms(items, min_fraction=min_fraction)
    all_own = [set(it.own_terms) for it in items]
    out: dict[str, set[str]] = {}
    for i, it in enumerate(items):
        siblings: set[str] = set()
        for j, terms in enumerate(all_own):
            if j != i:
                siblings |= terms
        out[it.ref] = (siblings - set(it.own_terms)) - generic
    return out


def per_item_echoes(items: list[Item], *, min_fraction: float = 0.5) -> dict[str, set[str]]:
    """{ref -> the sibling-distinctive tokens that item's answer actually echoed} (report detail)."""
    distinctive = sibling_distinctive_terms(items, min_fraction=min_fraction)
    return {it.ref: tokenize(it.answer) & distinctive[it.ref] for it in items}


def contaminated_refs(items: list[Item], *, min_fraction: float = 0.5) -> set[str]:
    """Refs whose answer echoes at least one sibling-distinctive token."""
    return {ref for ref, echoed in per_item_echoes(items, min_fraction=min_fraction).items()
            if echoed}


def contamination_rate(items: Iterable[Item], *, min_fraction: float = 0.5) -> float:
    """Fraction of items whose answer echoes a sibling's distinctive token (0.0 if no items)."""
    items = list(items)
    if not items:
        return 0.0
    return len(contaminated_refs(items, min_fraction=min_fraction)) / len(items)
