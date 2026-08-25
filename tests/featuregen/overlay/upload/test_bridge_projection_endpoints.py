"""A2 — the governed bridge projection carries FULL ordered endpoint tuples + the evidence split.

What is pinned here, row by row of the plan's Identity & staleness law:

* composite-key round-trip — a two-member endpoint link surfaces BOTH members through
  ``active_bridges``, in DECLARED order (not sorted), so ``source_system + customer_number``-class
  keys survive the projection instead of collapsing to ``members[0]``;
* the legacy thin fields are UNCHANGED for single-member links (the old behavior, regression-pinned)
  and documented as the first-member compat surface for composite ones;
* display-evidence / wording changes NEVER rekey ``link_semantic_revision`` — they move only the
  currentness dependency (``candidate_revision_id``);
* proposed→confirmed NEVER rekeys — it moves only the annotation (``status``) and the currentness
  stream head;
* a REJECTED or withdrawn link DISAPPEARS from the projection (the lifecycle allow-list, kept);
* the projection reads NO raw candidate ledger (grep-pin on the module source);
* the step-4 handoff: ``ordered_member_pairs`` supplies ``produce_provisional_realization``'s
  ordered directional mapping straight from the projection, direction stays a per-call INPUT.
"""
from __future__ import annotations

import inspect

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.overlay import facts, store
from featuregen.overlay.upload import bridge_projection
from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_projection import (
    ActiveBridgeV1,
    BridgeCurrentnessV1,
    active_bridges,
    ordered_member_pairs,
)
from featuregen.overlay.upload.bridge_store import record_candidate_assessment
from featuregen.overlay.upload.object_ref import normalize_ref

FACT = "bridge-fact-a2"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
def _endpoint(source: str, table: str, columns: tuple[str, ...]) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED)
            for column in columns
        ),
        entity_id="customer",
    )


def _assessment(
    *,
    columns: tuple[str, ...] = ("customer_id",),
    evidence: tuple[EvidenceRefV1, ...] = (),
    hypothesis: str | None = None,
) -> IdentifierLinkAssessmentV1:
    return IdentifierLinkAssessmentV1(
        left_endpoint=_endpoint("cib", "customers", columns),
        right_endpoint=_endpoint("ftr", "transactions", columns),
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1",
        bridge_fact_key=FACT,
        population_hypothesis=hypothesis,
        evidence_refs=evidence,
    )


def _seed(
    db,
    *,
    columns: tuple[str, ...] = ("customer_id",),
    status: str = "DRAFT",
    evidence: tuple[EvidenceRefV1, ...] = (),
    hypothesis: str | None = None,
) -> IdentifierLinkAssessmentV1:
    govern_bridge_fact(
        db, FACT, entity="customer",
        left_source="cib", left_ref=f"public.customers.{columns[0]}",
        right_source="ftr", right_ref=f"public.transactions.{columns[0]}",
        status=status)
    assessment = _assessment(columns=columns, evidence=evidence, hypothesis=hypothesis)
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    return assessment


# ── full ordered endpoint tuples ─────────────────────────────────────────────────────────────────
def test_composite_key_link_surfaces_both_members_ordered(db) -> None:
    """The round-trip: a two-member endpoint link surfaces BOTH members through the projection, in
    DECLARED composite order — deliberately NON-lexical here ('source_system' before
    'customer_number'), so a sort would be caught."""
    _seed(db, columns=("source_system", "customer_number"))
    (bridge,) = active_bridges(db)
    assert bridge.fact_key == FACT
    assert bridge.entity_id == "customer"
    assert bridge.left_member_refs == (
        "cib::public.customers.source_system",
        "cib::public.customers.customer_number",
    )
    assert bridge.right_member_refs == (
        "ftr::public.transactions.source_system",
        "ftr::public.transactions.customer_number",
    )
    # The legacy thin fields remain the FIRST-member compat flattening — documented, not a data
    # loss: the complete tuple now rides the type beside them.
    assert bridge.left_object_ref == "public.customers.source_system"
    assert bridge.right_object_ref == "public.transactions.source_system"


def test_single_member_link_thin_fields_unchanged(db) -> None:
    """The members[0] regression pin: for a single-member link the projection's original eight
    fields carry exactly what they always carried."""
    _seed(db, columns=("customer_id",))
    (bridge,) = active_bridges(db)
    assert bridge.fact_key == FACT
    assert bridge.entity_id == "customer"
    assert bridge.left_catalog_source == "cib"
    assert bridge.left_object_ref == "public.customers.customer_id"
    assert bridge.right_catalog_source == "ftr"
    assert bridge.right_object_ref == "public.transactions.customer_id"
    assert bridge.status == "proposed"
    assert bridge.strength == 0
    # and the full-tuple surface agrees with the thin one for the single-member shape
    assert bridge.left_member_refs == ("cib::public.customers.customer_id",)
    assert bridge.right_member_refs == ("ftr::public.transactions.customer_id",)


def test_existing_positional_construction_still_builds() -> None:
    """The type's own docstring convention: every NEW field is defaulted so the planner's existing
    positional constructions keep building."""
    thin = ActiveBridgeV1(
        "k", "customer", "cib", "public.customers.customer_id",
        "ftr", "public.transactions.customer_id", "proposed", 3)
    assert thin.left_member_refs == ()
    assert thin.right_member_refs == ()
    assert thin.link_semantic_revision == ""
    assert thin.currentness is None


# ── the evidence split (R9 / the staleness law) ──────────────────────────────────────────────────
def test_display_evidence_change_does_not_rekey_the_semantic_revision(db) -> None:
    """Display/ranking material may change freely; the link's SEMANTIC revision must not move.
    The change lands where it belongs — in the currentness dependency."""
    first = _seed(
        db,
        evidence=(EvidenceRefV1(
            evidence_id="llm-proposal-1", kind=EvidenceKind.LLM_RECOMMENDATION,
            producer="link-proposer-llm"),),
        hypothesis="original wording")
    (before,) = active_bridges(db)
    assert before.link_semantic_revision == first.candidate_id
    assert isinstance(before.currentness, BridgeCurrentnessV1)
    assert before.currentness.candidate_revision_id == first.candidate_revision_id
    assert before.currentness.overlay_head_event_id is not None

    reworded = _assessment(
        evidence=(EvidenceRefV1(
            evidence_id="llm-proposal-2", kind=EvidenceKind.LLM_RECOMMENDATION,
            producer="link-proposer-llm-v2"),),
        hypothesis="entirely different display wording")
    assert reworded.candidate_revision_id != first.candidate_revision_id  # the evidence DID change
    record_candidate_assessment(db, reworded, expected_pointer_version=1)

    (after,) = active_bridges(db)
    assert after.link_semantic_revision == before.link_semantic_revision  # NO rekey
    assert after.fact_key == before.fact_key
    assert after.left_member_refs == before.left_member_refs
    assert after.right_member_refs == before.right_member_refs
    # the movement is visible EXACTLY on the currentness surface
    assert after.currentness.candidate_revision_id == reworded.candidate_revision_id
    assert after.currentness.candidate_revision_id != before.currentness.candidate_revision_id


def test_join_column_change_rekeys_the_semantic_revision() -> None:
    """The other half of the law: join columns ARE semantic material — changing them is a
    DIFFERENT link, never the same link re-described."""
    one = _assessment(columns=("customer_id",))
    other = _assessment(columns=("customer_number",))
    assert one.candidate_id != other.candidate_id


def test_proposed_to_confirmed_does_not_rekey(db) -> None:
    """The law's first row: confirmation is an ANNOTATION. It flips ``status`` and advances the
    currentness stream head; identity and the endpoint tuples stand still."""
    _seed(db, status="DRAFT")
    (before,) = active_bridges(db)
    assert before.status == "proposed"

    events = store.load_fact(db, FACT)
    proposed = events[0]
    value = {
        "entity_id": "customer",
        "left_ref": {"catalog_source": "cib", "object_kind": "column", "schema": "public",
                     "table": "customers", "column": "customer_id"},
        "right_ref": {"catalog_source": "ftr", "object_kind": "column", "schema": "public",
                      "table": "transactions", "column": "customer_id"},
    }
    store.append_overlay_event(
        db, fact_key=FACT, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=mint_test_identity(subject="user:reviewer", role_claims=("platform_admin",)),
        payload={"value": value, "confirms_event_id": proposed.event_id,
                 "confirmers": [{"subject": "user:reviewer", "role": "platform_admin"}]})

    (after,) = active_bridges(db)
    assert after.status == "confirmed"                                     # the annotation moved
    assert after.link_semantic_revision == before.link_semantic_revision   # identity did not
    assert after.fact_key == before.fact_key
    assert after.left_member_refs == before.left_member_refs
    # the lifecycle movement is visible on the currentness surface, nowhere else
    assert (after.currentness.overlay_head_event_id
            != before.currentness.overlay_head_event_id)
    assert after.currentness.candidate_revision_id == before.currentness.candidate_revision_id


# ── lifecycle allow-list, kept ───────────────────────────────────────────────────────────────────
def test_rejected_link_disappears_from_the_projection(db) -> None:
    _seed(db, status="REJECTED")
    assert active_bridges(db) == ()


def test_withdrawn_candidate_disappears_from_the_projection(db) -> None:
    """The stream may still fold available while the governed candidate behind it was withdrawn —
    withdrawal fails closed for discovery."""
    assessment = _seed(db, status="DRAFT")
    assert len(active_bridges(db)) == 1
    db.execute(
        "UPDATE governed_candidate_current SET lifecycle='withdrawn' WHERE candidate_id=%s",
        (assessment.candidate_id,))
    assert active_bridges(db) == ()


# ── no raw candidate-ledger reader ───────────────────────────────────────────────────────────────
def test_projection_module_reads_no_raw_candidate_ledger() -> None:
    """Grep-pin: the projection sources endpoint tuples from the governed assessment surface
    (``available_identifier_links``), NEVER from ``entity_bridge_candidate_evidence`` directly —
    the raw ledger stays behind its one merging reader in ``cross_catalog_links``."""
    source = inspect.getsource(bridge_projection)
    assert "entity_bridge_candidate_evidence" not in source
    assert "available_identifier_links" in source


# ── the step-4 handoff ───────────────────────────────────────────────────────────────────────────
def test_ordered_member_pairs_supply_the_step4_producer(db) -> None:
    """The projection SUPPLIES the producer's ordered directional mapping: the pairs derived from
    the projected tuples are exactly what ``produce_provisional_realization`` accepts, composite
    order preserved, direction a per-call input."""
    from tests.featuregen.overlay.upload.test_bridge_realization_proposal import (
        _link,
        _pairs,
        _seed_bindings,
        _seed_graph,
    )

    from featuregen.overlay.upload.bridge_realization_proposal import (
        produce_provisional_realization,
    )

    columns = ("customer_id", "region_code")
    _seed_graph(db, columns=columns)
    _seed_bindings(db)
    link = _link(db, columns=columns)
    record_candidate_assessment(db, link.assessment, expected_pointer_version=0)

    bridge = next(b for b in active_bridges(db) if b.fact_key == link.assessment.bridge_fact_key)
    pairs = ordered_member_pairs(bridge, from_logical_table_ref="ftr::public.transactions")
    assert pairs == _pairs(columns)

    produced = produce_provisional_realization(
        db, link,
        from_logical_table_ref="ftr::public.transactions",
        ordered_member_pairs=pairs,
        environment="pilot")
    assert [pair.from_logical_column_ref for pair in produced.revision.column_pairs] == [
        "ftr::public.transactions.customer_id", "ftr::public.transactions.region_code"]

    # direction is the CALLER'S input — the opposite traversal comes from the same link value
    reverse = ordered_member_pairs(bridge, from_logical_table_ref="cib::public.customers")
    assert reverse == tuple((to_ref, from_ref) for from_ref, to_ref in pairs)


def test_ordered_member_pairs_refuse_rather_than_guess() -> None:
    thin = ActiveBridgeV1(
        "k", "customer", "cib", "public.customers.customer_id",
        "ftr", "public.transactions.customer_id")
    with pytest.raises(BridgeContractError, match="active_bridges"):
        ordered_member_pairs(thin, from_logical_table_ref="cib::public.customers")

    full = ActiveBridgeV1(
        "k", "customer", "cib", "public.customers.a", "ftr", "public.transactions.a",
        left_member_refs=("cib::public.customers.a", "cib::public.customers.b"),
        right_member_refs=("ftr::public.transactions.a", "ftr::public.transactions.b"))
    with pytest.raises(BridgeContractError, match="TABLE"):
        ordered_member_pairs(full, from_logical_table_ref="cib::public.customers.a")
    with pytest.raises(BridgeContractError, match="neither"):
        ordered_member_pairs(full, from_logical_table_ref="crm::public.accounts")

    lopsided = ActiveBridgeV1(
        "k", "customer", "cib", "public.customers.a", "ftr", "public.transactions.a",
        left_member_refs=("cib::public.customers.a", "cib::public.customers.b"),
        right_member_refs=("ftr::public.transactions.a",))
    with pytest.raises(BridgeContractError, match="arity"):
        ordered_member_pairs(lopsided, from_logical_table_ref="cib::public.customers")

    same_table = ActiveBridgeV1(
        "k", "customer", "cib", "public.customers.a", "cib", "public.customers.b",
        left_member_refs=("cib::public.customers.a",),
        right_member_refs=("cib::public.customers.b",))
    with pytest.raises(BridgeContractError, match="same table"):
        ordered_member_pairs(same_table, from_logical_table_ref="cib::public.customers")
