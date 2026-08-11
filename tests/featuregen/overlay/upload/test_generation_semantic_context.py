"""SE-2 (part 1) — the frozen Layer-A context: one identity, two queries, scope-honest."""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.generation_semantic_context import (
    build_generation_semantic_context,
)
from featuregen.overlay.upload.graph import build_graph

SOURCE = "ctxbank"


def _seed(db) -> None:
    rows = [
        (CanonicalRow(SOURCE, "accounts", "account_id", "integer", is_grain=True,
                      entity="Account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "pii_note", "text", sensitivity="pii"), None),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows if c})


def test_the_context_is_frozen_indexed_and_identity_bearing(db):
    _seed(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE,
                                                roles=("pii_reader",))
    assert {c.column for c in context.columns} == {"account_id", "amount", "pii_note"}
    assert context.concept_index["monetary_flow"] == ("public.transactions.amount",)
    assert context.concept_registry_version.startswith("concepts@")
    first = context.context_hash()
    assert first == context.context_hash()                     # deterministic
    rebuilt = build_generation_semantic_context(db, catalog_source=SOURCE,
                                                roles=("pii_reader",))
    assert rebuilt.context_hash() == first                     # same catalog, same identity


def test_any_visible_catalog_change_moves_the_identity(db):
    _seed(db)
    before = build_generation_semantic_context(db, catalog_source=SOURCE).context_hash()
    db.execute("UPDATE graph_node SET additivity = 'semi_additive' "
               "WHERE object_ref = 'public.transactions.amount'")
    after = build_generation_semantic_context(db, catalog_source=SOURCE).context_hash()
    assert after != before


def test_read_scope_shapes_both_content_and_identity(db):
    _seed(db)
    narrow = build_generation_semantic_context(db, catalog_source=SOURCE)
    wide = build_generation_semantic_context(db, catalog_source=SOURCE,
                                             roles=("pii_reader",))
    # A hidden column is ABSENT — not redacted — and two scopes are two identities.
    assert {c.column for c in narrow.columns} == {"account_id", "amount"}
    assert {c.column for c in wide.columns} == {"account_id", "amount", "pii_note"}
    assert narrow.context_hash() != wide.context_hash()


def test_layer_a_is_exactly_two_queries_regardless_of_width(db):
    """The rebased SE-2 budget gate: O(fact families), never O(columns)."""
    _seed(db)
    calls: list[str] = []
    original = db.execute

    def counting_execute(query, *args, **kwargs):
        calls.append(str(query))
        return original(query, *args, **kwargs)

    db.execute = counting_execute
    try:
        build_generation_semantic_context(db, catalog_source=SOURCE)
    finally:
        db.execute = original
    assert len(calls) == 2, calls
