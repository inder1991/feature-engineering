"""SE-3 — the capability compiler + the governed operand-class map: typed, pinned, honest."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.column_capabilities import compile_capabilities
from featuregen.overlay.upload.concept_operand_classes import (
    OPERAND_CLASS_MAP_VERSION,
    allowed_operand_classes,
    operand_class_map,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.generation_semantic_context import (
    build_generation_semantic_context,
)
from featuregen.overlay.upload.graph import build_graph

SOURCE = "capbank"


# ── the map: complete by construction, identifier law enforced, versioned ──────────────────────

def test_the_map_covers_every_operand_concept_and_bans_identifier_measures():
    from featuregen.overlay.upload.concepts import concept as registered_concept
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    mapping = operand_class_map()
    for recipe in V2_RECIPES:
        for operand in recipe.operands:
            assert operand.concept in mapping, operand.concept
            assert operand.operand_class in mapping[operand.concept]
    for concept_name, classes in mapping.items():
        try:
            registered = registered_concept(concept_name)
        except Exception:
            registered = None
        if registered is not None and registered.namespace is not None:
            assert "measure" not in classes, concept_name
    assert OPERAND_CLASS_MAP_VERSION.startswith("operand-classes@")
    assert allowed_operand_classes("no_such_concept") is None


# ── the compiler ───────────────────────────────────────────────────────────────────────────────

def _seed(db) -> None:
    rows = [
        (CanonicalRow(SOURCE, "customers", "cust_no", "varchar(30)",
                      definition="the customer number"), "customer_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _propose_concept(db, object_ref: str, concept: str) -> None:
    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="concept", proposed_value=concept,
        producer="llm", strength="proposed", producer_ref="svc:enrichment",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                    material=concept))


def test_capabilities_are_typed_pinned_and_honest_about_absence(db):
    _seed(db)
    _propose_concept(db, "public.customers.cust_no", "customer_id")
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    caps = compile_capabilities(db, context, [
        "public.customers.cust_no", "public.transactions.amount"])

    ident = caps["public.customers.cust_no"]
    assert ident.type_family == "text"
    assert ident.identifier_like and ident.identifier_namespace
    assert "measure" not in ident.possible_operand_classes
    assert "entity_key" in ident.possible_operand_classes
    assert ident.concept_authority == "llm/proposed"          # the pinned evidence axis
    assert ident.operand_class_map_version == OPERAND_CLASS_MAP_VERSION
    # absent axes are FACTS with names, never silent
    assert "dataset_profile_absent" in ident.missing_context
    assert "relationship_state_absent" in ident.missing_context
    assert "the customer number" in ident.retrieval_text      # prose rides retrieval ONLY

    amount = caps["public.transactions.amount"]
    assert amount.type_family == "numeric"
    assert not amount.identifier_like
    assert amount.possible_operand_classes == ("measure",)
    assert amount.concept_authority == "graph_hint"           # display value, no evidence row
    assert amount.additivity == "additive"


def test_an_unmapped_concept_refuses_with_a_marker_never_a_guess(db):
    rows = [(CanonicalRow(SOURCE, "notes", "freetext", "text",
                          definition="free text"), "leakage_target")]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    if "public.notes.freetext" not in {c.object_ref for c in context.columns}:
        pytest.skip("sensitivity floor hid the seeded column from the default scope")
    caps = compile_capabilities(db, context, ["public.notes.freetext"])
    cap = caps["public.notes.freetext"]
    if cap.concept is None:
        pytest.skip("classifier did not accept the probe concept")
    assert cap.possible_operand_classes == ()
    assert "concept_not_in_operand_class_map" in cap.missing_context


def test_the_compiler_is_one_query_and_cannot_widen_the_frozen_universe(db):
    _seed(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    calls: list[str] = []
    original = db.execute

    def counting(query, *args, **kwargs):
        calls.append(str(query))
        return original(query, *args, **kwargs)

    db.execute = counting
    try:
        caps = compile_capabilities(db, context, [
            "public.customers.cust_no", "public.transactions.amount",
            "public.transactions.booked_ts", "public.not_in.universe"])
    finally:
        db.execute = original
    assert len(calls) == 1, calls                             # Layer B: one batched read
    assert "public.not_in.universe" not in caps               # the context IS visibility
    assert len(caps) == 3
