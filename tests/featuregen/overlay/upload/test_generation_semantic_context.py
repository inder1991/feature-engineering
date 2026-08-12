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


def test_layer_a_is_exactly_three_queries_regardless_of_width(db):
    """The rebased SE-2 budget gate: O(fact families), never O(columns). Three families now:
    columns, the watermark, and the table-level dataset axis (deeper SE-8)."""
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
    assert len(calls) == 3, calls


# ── SE-2 part 2: the durable seal + kind-dispatched freshness ──────────────────────────────────

def _rr(conn) -> None:
    import psycopg

    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def test_the_context_seals_into_the_snapshot_and_drift_is_caught(conn):
    """The context item joins the C0 sealed set, verifies CURRENT while the catalog holds, and
    catches drift the per-ref column pins cannot see — a change to a column that is IN the
    frozen universe but NOT among the snapshot's candidate refs."""
    from featuregen.overlay.upload.feature_metadata_snapshot import (
        build_metadata_snapshot,
        compare_snapshot_to_current,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        GENERATION_CONTEXT_ITEM_KIND,
        context_snapshot_item,
    )

    _rr(conn)
    _seed(conn)
    context = build_generation_semantic_context(conn, catalog_source=SOURCE)
    ctx = build_metadata_snapshot(
        conn, generation_run_id="genrun_se2", refs=[(SOURCE, "public.accounts.account_id")],
        read_scope_hash="sha256:scope", fields=["additivity"],
        extra_items=[context_snapshot_item(context)])

    stored_kinds = [r[0] for r in conn.execute(
        "SELECT item_kind FROM catalog_metadata_snapshot_item WHERE snapshot_id = %s",
        (ctx.snapshot_id,)).fetchall()]
    assert GENERATION_CONTEXT_ITEM_KIND in stored_kinds

    fresh = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert (fresh.status, fresh.reason) == ("current", None)

    # Drift a column OUTSIDE the candidate refs: only the context pin can see it.
    conn.execute("UPDATE graph_node SET additivity = 'non_additive' "
                 "WHERE object_ref = 'public.transactions.amount'")
    drifted = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert (drifted.status, drifted.reason) == ("drifted", "SNAPSHOT_ITEM_DRIFT")
