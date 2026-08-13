"""SE-3 — the capability compiler + the governed operand-class map: typed, pinned, honest."""
from __future__ import annotations

import pytest

from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import logical_ref_of
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
from featuregen.overlay.upload.object_ref import normalize_ref

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


def test_the_table_dataset_axis_rides_the_capability_with_its_own_authority(db):
    """Deeper SE-8 end to end: the TABLE's event_or_snapshot classification reaches every
    capability on that table from the frozen context, and its authority comes from the TABLE's
    own evidence pins — a display-only value stays graph_hint, which downstream blocks nothing
    and clears nothing."""
    _seed(db)
    db.execute(
        "UPDATE graph_node SET event_or_snapshot = 'snapshot' "
        "WHERE kind = 'table' AND catalog_source = %s AND table_name = 'transactions'",
        (SOURCE,))
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    caps = compile_capabilities(db, context, [
        "public.transactions.amount", "public.customers.cust_no"])
    amount = caps["public.transactions.amount"]
    assert amount.table_event_or_snapshot == "snapshot"
    assert amount.table_event_or_snapshot_authority == "graph_hint"
    assert caps["public.customers.cust_no"].table_event_or_snapshot is None

    table_ref = normalize_ref(SOURCE, None, "transactions", None)
    record_field_evidence(
        db, logical_ref=table_ref, field_name="event_or_snapshot",
        proposed_value="snapshot", producer="source", strength="attested",
        producer_ref="svc:catalog-connector", source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=table_ref, field_name="event_or_snapshot",
                                    material="snapshot"))
    repinned = compile_capabilities(db, context, ["public.transactions.amount"])
    assert repinned["public.transactions.amount"].table_event_or_snapshot_authority \
        == "source/attested"                                  # NOW it can block an event window


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
    # Layer B: a CONSTANT number of batched reads regardless of fan-out — the active
    # evidence + (C1) the pending-revalidation set the resolver's verdict depends on.
    assert len(calls) == 2, calls
    assert "public.not_in.universe" not in caps               # the context IS visibility
    assert len(caps) == 3


# ── C1: the pin is the RESOLVER'S verdict — weaker-later never displaces stronger ──────────────

def _evidence(db, object_ref: str, concept: str, *, producer: str, strength: str,
              material_salt: str = "") -> None:
    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="concept", proposed_value=concept,
        producer=producer, strength=strength, producer_ref=f"{producer}:test",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                    material=concept + material_salt))


def test_a_later_weak_proposal_never_rides_a_confirmed_values_back(db):
    """The review's exact failure sequence (C1): the display concept is human-confirmed A;
    a LATER active llm proposal B disagrees. The capability must carry A with A's OWN
    authority — pre-C1 the newest-active-wins read pinned B's llm/proposed onto A. And B is
    a LOSING proposal, not a semantic conflict: no conflict marker."""
    _seed(db)
    ref = "public.customers.cust_no"
    _evidence(db, ref, "customer_id", producer="human", strength="confirmed")
    _evidence(db, ref, "party_identifier", producer="llm", strength="proposed")

    caps = compile_capabilities(
        db, build_generation_semantic_context(db, catalog_source=SOURCE), [ref])
    cap = caps[ref]
    assert cap.concept_authority == "human/confirmed", cap.concept_authority
    assert cap.authority_conflicts == (), cap.authority_conflicts


def test_equal_strength_disagreement_is_the_resolvers_conflict_verdict(db):
    """Two human-confirmed values that disagree ARE a semantic conflict — the resolver's own
    verdict, surfaced as the capability's conflict marker."""
    _seed(db)
    ref = "public.customers.cust_no"
    _evidence(db, ref, "customer_id", producer="human", strength="confirmed")
    _evidence(db, ref, "party_identifier", producer="human", strength="confirmed",
              material_salt=":second-reviewer")

    caps = compile_capabilities(
        db, build_generation_semantic_context(db, catalog_source=SOURCE), [ref])
    assert "concept" in caps[ref].authority_conflicts


def test_a_lone_proposal_still_pins_proposed_so_the_floors_ride(db):
    """An unconfirmed llm proposal keeps its honest llm/proposed authority — the suggestion
    floors (PROPOSED_METADATA_ONLY downstream) depend on this staying visible."""
    _seed(db)
    ref = "public.customers.cust_no"
    _propose_concept(db, ref, "customer_id")

    caps = compile_capabilities(
        db, build_generation_semantic_context(db, catalog_source=SOURCE), [ref])
    assert caps[ref].concept_authority == "llm/proposed"
    assert caps[ref].authority_conflicts == ()


# ── C4: the licence compiles onto the capability in ONE bulk read ──────────────────────────────

def test_the_capability_carries_the_personal_data_licence_state(db):
    """A personal-data concept compiles `personal_data_required`; with no active policy it is
    unlicensed; approving the purpose licenses it WITH the exact revision id. The licence
    read is skipped entirely for a shortlist with no personal data (the query pin holds)."""
    from featuregen.overlay.upload.pii_policy_store import approve_pii_use_policy

    rows = [
        (CanonicalRow(SOURCE, "customers", "pep_ind", "text",
                      definition="politically exposed person marker"), "pep_flag"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    ref = "public.customers.pep_ind"

    caps = compile_capabilities(
        db, build_generation_semantic_context(db, catalog_source=SOURCE), [ref])
    assert caps[ref].personal_data_required is True
    assert caps[ref].personal_data_licensed is False
    assert caps[ref].personal_data_policy_revision_ids == ()

    revision_id, _v = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose="AML transaction monitoring",
        expected_pointer_version=0, actor="admin@bank")
    caps = compile_capabilities(
        db, build_generation_semantic_context(db, catalog_source=SOURCE), [ref])
    assert caps[ref].personal_data_licensed is True
    assert caps[ref].personal_data_policy_revision_ids == (revision_id,)
