"""Entity Map v0 (ingestion-richness Task 3D): one availability truth, read-scoped counts.

The load-bearing test is byte-identity: the map's link population IS
``available_identifier_links()`` — same fixture, same filters — so governance, the planner and the
map can never disagree about which links exist. The must-die mutation the plan names ("the map
re-reads the candidate ledger or folds lifecycles itself") is guarded statically as well: the
module's only link source is the reader.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.overlay import facts, store
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierLinkAssessmentV1,
    LinkReviewStatus,
    NamespaceVerdict,
    PopulationRelation,
    available_identifier_links,
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
from featuregen.overlay.upload.entity_map import (
    STATUS_CONFIRMED,
    STATUS_PROPOSED,
    build_entity_map,
)

_ROOT = Path(__file__).parents[4]

_LEFT_TABLE = "public.bo_cib_customer"
_RIGHT_TABLE = "public.comp_financial_tran_repos_dly"

#: Every visibility class grantable today — the all-access caller the audit script's unscoped
#: numbers are compared against.
_ALL_ACCESS = ("pii_reader", "restricted_reader", "confidential_reader")


def _column(db, source, table, column, *, entity=None, sensitivity=None, is_grain=False):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, entity, sensitivity, is_grain) VALUES (%s,%s,'column',%s,%s,'text',%s,%s,%s)",
        (source, f"public.{table}.{column}", table, column, entity, sensitivity, is_grain))


def _candidate(db, entity, left_col, right_col, *, basis="declared", status="DRAFT",
               left_grain=False, right_grain=False):
    """A ledger candidate WITH the governed stream production always writes behind it."""
    key = f"fk-{left_col}"
    ev = {"entity_id": entity, "type_basis": basis, "candidate_id": f"{left_col}-{right_col}",
          "left_is_grain": left_grain, "right_is_grain": right_grain,
          "data_type_family": "text", "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES (%s,'cib',%s,'ftr',%s,%s,%s,'text',%s,'1.0.0')",
        (entity, f"{_LEFT_TABLE}.{left_col}", f"{_RIGHT_TABLE}.{right_col}",
         ev["candidate_id"], key, json.dumps(ev)))
    govern_bridge_fact(
        db, key, entity=entity, left_source="cib", left_ref=f"{_LEFT_TABLE}.{left_col}",
        right_source="ftr", right_ref=f"{_RIGHT_TABLE}.{right_col}", status=status)
    return key


def _reject(db, fact_key):
    """Reject an existing governed fact the way governance does: against its proposed event."""
    stream = tuple(store.load_fact(db, fact_key))
    proposed = next(e for e in stream if e.type == facts.OVERLAY_FACT_PROPOSED)
    from tests.featuregen.overlay.upload._bridge_fixtures import _ACTOR
    store.append_overlay_event(
        db, fact_key=fact_key, type=facts.OVERLAY_FACT_REJECTED, actor=_ACTOR,
        payload={"rejected_by": "user:one", "target_event_id": proposed.event_id})


def _modern_link(db, *, fact_key="bridge-fact-1"):
    """A MODERN current assessment (concept customer_id -> namespace cif) with a governed stream."""
    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left,
        right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1",
        bridge_fact_key=fact_key,
    )
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, fact_key, entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    return assessment, left, right


# ── the load-bearing identity: map links == reader links ─────────────────────────────────────────


def _identity(links):
    return [(link.candidate_id, link.candidate_revision_id, link.bridge_fact_key, link.strength)
            for link in links]


def test_map_links_are_byte_identical_to_available_identifier_links(db) -> None:
    _candidate(db, "customer", "cust_num", "cif_id", basis="attested", status="DRAFT",
               left_grain=True)
    _candidate(db, "branch", "branch_cd", "branch_desc", status="PARTIALLY_CONFIRMED")
    _candidate(db, "account", "acct_no", "acct_ref", status="VERIFIED")
    _candidate(db, "customer", "cust_rejected", "cif_rejected", status="REJECTED")  # unavailable
    _modern_link(db)

    expected = [
        (link.assessment.candidate_id, link.assessment.candidate_revision_id,
         link.availability.bridge_fact_key, link.ranking_strength)
        for link in available_identifier_links(db)
    ]
    assert expected, "fixture must produce available links or this test proves nothing"
    result = build_entity_map(db)
    assert _identity(result.links) == expected
    # The rejected decoy is in NEITHER — same allow-list, applied once, by the reader.
    assert "fk-cust_rejected" not in {link.bridge_fact_key for link in result.links}


def test_the_map_never_reinterprets_availability_itself() -> None:
    """The must-die mutation, statically: the module's ONLY link source is the reader. It never
    touches the candidate ledger, never folds a lifecycle, never re-implements the allow-list."""
    source = (_ROOT / "src/featuregen/overlay/upload/entity_map.py").read_text()
    assert "available_identifier_links" in source
    for banned in (
        "entity_bridge_candidate_evidence",
        "fold_overlay_state",
        "load_fact",
        "AVAILABLE_STATUSES",
        "cross_catalog_links",
        "read_overlay_identifier_link_state",
        "entity_bridge_edge",
    ):
        assert banned not in source, f"entity_map.py must not re-interpret availability: {banned}"


# ── statuses: proposed is usable output, confirmed is an annotation ──────────────────────────────


def test_proposed_only_map_renders_proposed_never_anything_else(db) -> None:
    _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    _candidate(db, "branch", "branch_cd", "branch_desc", status="PARTIALLY_CONFIRMED")
    result = build_entity_map(db)
    assert {link.status for link in result.links} == {STATUS_PROPOSED}


def test_human_verified_link_reports_confirmed(db) -> None:
    _candidate(db, "customer", "cust_num", "cif_id", status="VERIFIED")
    (link,) = build_entity_map(db).links
    assert link.status == STATUS_CONFIRMED
    assert link.folded_status == "VERIFIED"


def test_empty_map_is_empty_tuples_not_an_error(db) -> None:
    result = build_entity_map(db)
    assert result.links == ()
    assert result.entities == ()


# ── after a decoy is rejected the edge disappears, with NO rebuild ───────────────────────────────


def test_rejecting_a_decoy_removes_the_edge_without_a_rebuild(db) -> None:
    keep = _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    decoy = _candidate(db, "branch", "branch_cd", "branch_desc", status="DRAFT")
    before = {link.bridge_fact_key for link in build_entity_map(db).links}
    assert before == {keep, decoy}
    _reject(db, decoy)
    after = {link.bridge_fact_key for link in build_entity_map(db).links}
    assert after == {keep}


# ── entity nodes: read-scoped counts + samples, reconciled with the audit script ─────────────────


def test_entity_counts_group_by_catalog_and_read_scope_hides_restricted(db) -> None:
    _column(db, "cib", "bo_cib_customer", "cust_num", entity="customer", is_grain=True)
    _column(db, "cib", "bo_cib_customer", "cust_name", entity="customer", sensitivity="pii")
    _column(db, "ftr", "comp_financial_tran_repos_dly", "cif_id", entity="customer")
    _column(db, "ftr", "comp_financial_tran_repos_dly", "tran_id", entity="transaction")
    _column(db, "ftr", "comp_financial_tran_repos_dly", "tran_amt")  # no entity -> no node

    scoped = build_entity_map(db, roles=())
    by_id = {node.entity_id: node for node in scoped.entities}
    assert set(by_id) == {"customer", "transaction"}
    customer = by_id["customer"]
    assert customer.registered is True
    assert customer.column_count == 2          # the pii column is NOT counted for this caller
    counts = {group.catalog_source: group.column_count for group in customer.catalogs}
    assert counts == {"cib": 1, "ftr": 1}
    all_samples = [ref for group in customer.catalogs for ref in group.sample_refs]
    assert "public.bo_cib_customer.cust_name" not in all_samples

    lifted = build_entity_map(db, roles=("pii_reader",))
    customer_lifted = next(n for n in lifted.entities if n.entity_id == "customer")
    assert customer_lifted.column_count == 3   # visible now, so counted now
    cib = next(g for g in customer_lifted.catalogs if g.catalog_source == "cib")
    assert "public.bo_cib_customer.cust_name" in cib.sample_refs


def test_map_counts_reconcile_with_the_audit_script(db) -> None:
    """The same fixture, the script's own SQL: per-catalog entity coverage must equal the map's
    per-catalog sums for an all-access caller. If the map ever grows a private filter (freshness,
    a vocabulary allow-list), this is the test that says so."""
    _column(db, "cib", "bo_cib_customer", "cust_num", entity="customer")
    _column(db, "cib", "bo_cib_customer", "cust_name", entity="customer", sensitivity="pii")
    _column(db, "cib", "bo_cib_customer", "seg_cd", entity="segment")   # NOT in known_entities()
    _column(db, "ftr", "comp_financial_tran_repos_dly", "cif_id", entity="customer")
    _column(db, "ftr", "comp_financial_tran_repos_dly", "tran_amt")     # entity-less

    script = (_ROOT / "scripts/verify_catalog_richness.sql").read_text()
    audited: dict[str, int] = {}
    for (row,) in db.execute(script).fetchall():
        metric, _, rest = row.partition("|")
        if metric == "coverage_entity_display":
            catalog, _, count = rest.partition("|")
            audited[catalog] = int(count)
    assert audited == {"cib": 3, "ftr": 1}

    result = build_entity_map(db, roles=_ALL_ACCESS)
    summed: dict[str, int] = {}
    for node in result.entities:
        for group in node.catalogs:
            summed[group.catalog_source] = summed.get(group.catalog_source, 0) + group.column_count
    assert summed == audited
    # The unregistered entity is SHOWN and flagged, never silently dropped — dropping it is exactly
    # how the map's numbers would drift from the audit's.
    segment = next(n for n in result.entities if n.entity_id == "segment")
    assert segment.registered is False


def test_link_endpoint_entity_gets_a_node_even_with_no_visible_columns(db) -> None:
    _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    result = build_entity_map(db)
    (node,) = [n for n in result.entities if n.entity_id == "customer"]
    assert node.column_count == 0
    assert node.catalogs == ()


# ── namespace + direction-specific eligibility come from the existing readers ────────────────────


def test_modern_link_carries_registry_namespace_and_realization_eligibility(db) -> None:
    _assessment, left, right = _modern_link(db)
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id, revision.realization_revision_id,
        SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE, 1)
    record_realization_revision(
        db, revision, current,
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))

    (link,) = build_entity_map(db).links
    assert link.left.concept == "customer_id"
    assert link.left.namespace == "cif"        # read from the concept registry, never guessed
    assert link.left.entity_id == "customer"
    (realized,) = link.realizations
    assert (realized.from_catalog_source, realized.to_catalog_source) == ("cib", "ftr")
    assert realized.sandbox_eligible is True
    assert realized.production_eligible is True
    assert realized.lifecycle == "active"


def test_legacy_link_without_concept_has_no_namespace_and_no_realizations(db) -> None:
    _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    (link,) = build_entity_map(db).links
    assert link.left.concept is None
    assert link.left.namespace is None
    assert link.realizations == ()
    assert link.left.entity_id == "customer"   # the ledger's entity conclusion, via the reader


def test_demoted_realization_reports_ineligible_not_hidden(db) -> None:
    _assessment, left, right = _modern_link(db)
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id, revision.realization_revision_id,
        SafetyStatus.UNSAFE, LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE, 1)
    record_realization_revision(
        db, revision, current,
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))
    (link,) = build_entity_map(db).links
    (realized,) = link.realizations
    assert realized.sandbox_eligible is False
    assert realized.production_eligible is False
    assert realized.safety_status == "unsafe"


def test_replaced_fact_key_on_modern_assessment_still_matches_stream(db) -> None:
    """`replace()` on the frozen assessment recomputes identities — guard the fixture itself."""
    left, right = _executable_pair()
    base = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-a")
    renamed = replace(base, bridge_fact_key="bridge-fact-b")
    assert renamed.bridge_fact_key == "bridge-fact-b"
    assert renamed.candidate_id == base.candidate_id
