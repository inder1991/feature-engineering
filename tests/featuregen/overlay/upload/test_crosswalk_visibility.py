"""Release C Task 10 — a crosswalk is VISIBLE, distinctly, and never looks executable.

The rules this suite holds:

* a direct bridge and a crosswalk between the SAME two endpoints stay distinct records and BOTH
  appear — they answer different questions ("these ids are equal" / "these ids are related through
  this table") and collapsing them loses the difference;
* nothing on a crosswalk payload implies executability: no fabricated safety_status, no borrowed
  realization, `executable_now` always False;
* availability keeps the two-word `LinkAvailability` vocabulary; a crosswalk the caller may not see
  is withheld WHOLE, never listed as "unavailable";
* a human confirmation moves review status and nothing else.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierLinkAssessmentV1,
    LinkReviewStatus,
    NamespaceVerdict,
    PopulationRelation,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    record_candidate_assessment,
    record_realization_revision,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.context_graph import build_context_section
from featuregen.overlay.upload.crosswalk import (
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.crosswalk_store import (
    publish_crosswalk_definition,
    set_crosswalk_review_status,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.semantic_context import RelationshipKind
from featuregen.projections.runner import run_projection

ACTOR = "user:priya"
ANCHOR = "public.customers.customer_id"
MAP_TABLE = "cust_xref"
MAP = normalize_ref("cib", "public", MAP_TABLE)
CIB_KEY = normalize_ref("cib", "public", "customers", "customer_id")
FTR_KEY = normalize_ref("ftr", "public", "transactions", "customer_id")


def _unbound(endpoint):
    """A crosswalk definition accepts LOGICAL, UNBOUND endpoints only — the same two endpoints the
    bridge realization pins physically, with the environment stripped out."""
    return dataclasses.replace(endpoint, physical_binding=None, binding_revision_id=None)


def _crosswalk_between(left, right, *, sensitivity: str = "") -> CrosswalkDefinitionRevisionV1:
    return CrosswalkDefinitionRevisionV1(
        source_endpoint=_unbound(left),
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(LogicalMappingPairV1(
            CIB_KEY, normalize_ref("cib", "public", MAP_TABLE, "cust_num")),),
        mapping_to_target_pairs=(LogicalMappingPairV1(
            FTR_KEY, normalize_ref("cib", "public", MAP_TABLE, "tran_cust_id")),),
        target_endpoint=_unbound(right),
    )


def _bank(db, *, map_sensitivity: str = "") -> tuple:
    """One governed direct bridge AND one crosswalk between the SAME two endpoints."""
    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)
    record_realization_revision(
        db, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))
    # One build_graph per SOURCE rebuilds that source's nodes, so the anchor table and the mapping
    # table go in together.
    build_graph(db, "cib", [
        CanonicalRow("cib", "customers", "customer_id", "text", is_grain=True),
        CanonicalRow("cib", MAP_TABLE, "cust_num", "text", sensitivity=map_sensitivity),
        CanonicalRow("cib", MAP_TABLE, "tran_cust_id", "text", sensitivity=map_sensitivity),
    ])
    build_graph(db, "ftr", [CanonicalRow("ftr", "transactions", "customer_id", "text")])
    run_projection(db, OverlayProjection())
    crosswalk = _crosswalk_between(left, right)
    publish_crosswalk_definition(
        db, crosswalk, expected_pointer_version=0, actor=ACTOR,
        roles=("restricted_reader",) if map_sensitivity else ())
    return assessment, revision, crosswalk


# ── coexistence ─────────────────────────────────────────────────────────────────────────────────

def test_a_direct_bridge_and_a_crosswalk_between_the_same_endpoints_are_BOTH_visible(db) -> None:
    """The pinned rule. Two records, two refs, two kinds, both shown — because "these ids are
    equal" and "these ids are related through this table" are different claims and only one of
    them may turn out to be true."""
    _assessment, _revision, crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    by_kind = {link.kind: link for link in bundle.relationship_context}
    assert set(by_kind) == {RelationshipKind.DIRECT_EQUALITY.value,
                            RelationshipKind.CROSSWALK.value}
    direct, cross = by_kind["direct_equality"], by_kind["crosswalk"]
    assert direct.relationship_ref == "bridge-fact-1"
    assert cross.relationship_ref == crosswalk.definition_id
    assert direct.relationship_ref != cross.relationship_ref
    assert {direct.left_ref, direct.right_ref} == {cross.left_ref, cross.right_ref}


def test_the_crosswalk_carries_its_mapping_dataset_and_both_legs(db) -> None:
    _assessment, _revision, crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    cross = next(link for link in bundle.relationship_context if link.kind == "crosswalk")
    assert cross.crosswalk is not None
    assert cross.crosswalk.mapping_dataset_ref == MAP
    assert cross.crosswalk.definition_revision_id == crosswalk.revision_id
    assert cross.crosswalk.source_to_mapping_refs and cross.crosswalk.mapping_to_target_refs
    # EMPTY by contract: a leg is pinned by RESOLVING it, and nothing in this release resolves.
    assert cross.crosswalk.leg_pins == ()


def test_the_direct_link_keeps_its_realizations_and_the_crosswalk_has_none(db) -> None:
    _assessment, revision, _crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    by_kind = {link.kind: link for link in bundle.relationship_context}
    assert [r.realization_revision_id for r in by_kind["direct_equality"].realizations] == [
        revision.realization_revision_id]
    assert by_kind["crosswalk"].realizations == ()


def _section(db, *, roles=()) -> dict:
    return build_context_section(
        db, source="cib", object_ref=ANCHOR, kind="column", logical_ref=CIB_KEY, roles=roles,
        now=datetime.now(UTC))


# ── nothing implies executability ───────────────────────────────────────────────────────────────

def test_the_context_graph_never_reports_a_crosswalk_as_executable(db) -> None:
    _bank(db)
    section = _section(db)
    cross = next(r for r in section["relationships"] if r["kind"] == "crosswalk")
    assert cross["executable_now"] is False
    assert cross["realizations"] == []
    assert cross["crosswalk"]["mapping_dataset_ref"] == MAP
    assert cross["crosswalk"]["leg_pins"] == []


def test_no_safety_verdict_is_fabricated_anywhere_on_a_crosswalk_payload(db) -> None:
    """A crosswalk has no measured safety on this tree. A `safety_status` key here would be a
    borrowed verdict, and a reader cannot tell a borrowed verdict from a real one."""
    _bank(db)
    section = _section(db)
    cross = next(r for r in section["relationships"] if r["kind"] == "crosswalk")
    assert "safety_status" not in cross
    assert "safety_status" not in cross["crosswalk"]
    assert not any(key in cross["crosswalk"]
                   for key in ("sandbox_eligible", "production_eligible", "execution_tier"))


def test_the_crosswalk_edge_is_drawn_as_its_own_kind(db) -> None:
    """A graph that draws a crosswalk with the identifier-link edge kind renders two different
    relationships identically."""
    _bank(db)
    section = _section(db)
    kinds = {edge["kind"] for edge in section["edges"]}
    assert {"identifier_link", "crosswalk_link"} <= kinds
    node = next(n for n in section["nodes"]
                if n["kind"] == "relationship" and n["detail"]["kind"] == "crosswalk")
    assert node["detail"]["mapping_dataset_ref"] == MAP


def test_the_why_string_names_the_mapping_table_and_claims_nothing(db) -> None:
    _bank(db)
    section = _section(db)
    why = next(e["why"] for e in section["edges"] if e["kind"] == "crosswalk_link")
    assert MAP in why
    assert "executable" not in why


# ── read scope and review ───────────────────────────────────────────────────────────────────────

def test_a_hidden_mapping_dataset_withholds_the_crosswalk_WHOLE(db) -> None:
    """Not "unavailable" — ABSENT. A row saying "there is something here you cannot see" is itself
    the disclosure the scope exists to prevent."""
    _bank(db, map_sensitivity="restricted")
    unprivileged = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    assert [link.kind for link in unprivileged.relationship_context] == ["direct_equality"]
    privileged = sc.bundle_from_store(db, "cib", ANCHOR, roles=("restricted_reader",))
    assert {link.kind for link in privileged.relationship_context} == {
        "direct_equality", "crosswalk"}


def test_confirming_a_crosswalk_changes_review_status_and_not_availability(db) -> None:
    _assessment, _revision, crosswalk = _bank(db)
    before = next(link for link in sc.bundle_from_store(db, "cib", ANCHOR, roles=()
                                                        ).relationship_context
                  if link.kind == "crosswalk")
    assert (before.review_status, before.availability, before.strength) == (
        "unreviewed", "available", "proposed")
    set_crosswalk_review_status(
        db, definition_id=crosswalk.definition_id,
        review_status=LinkReviewStatus.HUMAN_VERIFIED, expected_pointer_version=1,
        declared_by="user:sam")
    after = next(link for link in sc.bundle_from_store(db, "cib", ANCHOR, roles=()
                                                       ).relationship_context
                 if link.kind == "crosswalk")
    assert after.review_status == "human_verified"
    assert after.availability == before.availability == "available"
    assert after.strength == "confirmed"     # somebody is accountable; nothing else moved
    assert after.crosswalk == before.crosswalk


# ── contract guards ─────────────────────────────────────────────────────────────────────────────

def _relationship(**over) -> sc.RelationshipContextV1:
    base: dict = dict(
        relationship_ref="cwd_" + "a" * 64, kind=RelationshipKind.CROSSWALK.value,
        left_ref=CIB_KEY, right_ref=FTR_KEY, availability="available",
        review_status="unreviewed", assessment_revision_id=None, realizations=(),
        producer="taxonomy", strength="proposed", lifecycle="active", current=True,
        evidence_ids=(),
        crosswalk=sc.CrosswalkContextV1(
            definition_id="cwd_" + "a" * 64, definition_revision_id="cwd_" + "b" * 64,
            mapping_dataset_ref=MAP, source_to_mapping_refs=("cib::public.cust_xref.cust_num",),
            mapping_to_target_refs=("cib::public.cust_xref.tran_cust_id",)))
    base.update(over)
    return sc.RelationshipContextV1(**base)


def test_a_direct_link_may_not_carry_a_crosswalk_extension() -> None:
    with pytest.raises(sc.SemanticContextError):
        _relationship(kind=RelationshipKind.DIRECT_EQUALITY.value)


def test_a_crosswalk_without_its_extension_is_indistinguishable_from_a_direct_link() -> None:
    with pytest.raises(sc.SemanticContextError):
        _relationship(crosswalk=None)


def test_a_crosswalk_may_not_borrow_a_bridge_realization() -> None:
    """MUTATION: presenting a directional realization on a crosswalk would import the bridge
    family's safety verdicts for a relationship nothing has measured."""
    realization = sc.DirectionalRealizationContextV1(
        realization_revision_id="rr_1", from_ref="cib::public.customers",
        to_ref="ftr::public.transactions", lifecycle="active",
        safety_status="deterministically_validated", cardinality="N:1", scope_id="scope-1",
        sandbox_eligible=True, production_eligible=True)
    with pytest.raises(sc.SemanticContextError):
        _relationship(realizations=(realization,))


def test_a_crosswalk_context_must_name_both_legs() -> None:
    with pytest.raises(sc.SemanticContextError):
        sc.CrosswalkContextV1(
            definition_id="cwd_a", definition_revision_id="cwd_b", mapping_dataset_ref=MAP,
            source_to_mapping_refs=("cib::public.cust_xref.cust_num",),
            mapping_to_target_refs=())
