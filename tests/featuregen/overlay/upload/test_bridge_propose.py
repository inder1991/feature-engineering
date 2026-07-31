from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload.conftest import _confirm_grain
from tests.featuregen.overlay.upload.test_bridge_candidates import (
    _load,
    _two_catalog_customer,
)

from featuregen.contracts.envelopes import Command
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.evidence import read_evidence
from featuregen.overlay.identity import proposal_fingerprint
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    LinkAvailability,
    TupleKeyRole,
    available_identifier_links,
)
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.bridge_store import load_current_candidate_assessments
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_readiness import column_readiness
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.table_fact_governance import project_verified_table_fact
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref
from featuregen.projections.runner import run_projection

_T0 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _propose(db) -> str:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    cand = derive_bridge_candidates(db)[0]
    return propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)


def test_propose_opens_a_draft_bridge_fact(db):
    key = _propose(db)
    state = fold_overlay_state(load_fact(db, key))
    assert state.status == "DRAFT"
    links = available_identifier_links(db)
    assert len(links) == 1
    assert links[0].assessment.bridge_fact_key == key
    assert links[0].availability.availability is LinkAvailability.AVAILABLE
    assert links[0].assessment.explanation_codes != ("legacy_candidate_union",)


def test_propose_stamps_the_candidate_ledger(db):
    key = _propose(db)
    row = db.execute(
        "SELECT entity_id, fact_key, proposed_event_id, data_type_family "
        "FROM entity_bridge_candidate_evidence "
        "WHERE left_catalog_source = 'core' AND right_catalog_source = 'crm'").fetchone()
    assert row is not None
    assert row[0] == "customer" and row[1] == key and row[2] is not None and row[3] == "integer"


def test_available_cross_catalog_candidate_earns_join_connectivity_preview(db):
    _propose(db)
    run_projection(db, OverlayProjection())

    readiness = column_readiness(
        db,
        source="core",
        object_ref="public.customer_master.customer_id",
    )

    assert readiness.as_join_key.operational_status == "ready"
    assert any(
        requirement.requirement_id == "external:JOIN_CONNECTIVITY"
        and requirement.external_preview
        for requirement in readiness.as_join_key.requirements
    )


def test_propose_opens_one_governance_gate_task(db):
    _propose(db)
    # single-confirmer -> exactly one open human task (platform-admin governance), not two
    n = db.execute("SELECT count(*) FROM human_tasks WHERE status = 'open'").fetchone()[0]
    assert n == 1


def test_reproposing_same_candidate_is_idempotent(db):
    key1 = _propose(db)
    # a second propose of the SAME candidate must NOT raise, must return the same fact_key,
    # and must not open a second gate task or a second ledger row.
    cand = derive_bridge_candidates(db)[0]
    key2 = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    assert key2 == key1
    assert db.execute("SELECT count(*) FROM human_tasks WHERE status = 'open'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM overlay_evidence").fetchone()[0] == 1


def test_structural_evidence_keeps_producer_strength_and_run_provenance(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    candidate = derive_bridge_candidates(db)[0]
    key = propose_bridge(
        db,
        candidate,
        actor=_ENRICH_ACTOR,
        now=_T0,
        ingestion_run_id="ingestion-run-7",
    )
    proposed = load_fact(db, key)[0]
    evidence = read_evidence(db, proposed.payload["evidence_ref"])
    assert evidence.producer == "structural_connector"
    assert evidence.strength == "proposed"
    assert evidence.lifecycle == "active"
    assert evidence.producer_item_ref == "ingestion-run-7"
    assert evidence.evidence_spans == (
        "entity_id",
        "data_type_family",
        "type_basis",
        "concept_authority",
        "grain_membership",
    )


def test_reassessment_refreshes_current_revision_and_legacy_projection_without_orphan_evidence(db):
    ensure_upload_catalog_adapter()
    # Start glossary-like: no attested type, only a declared integer on both sides.
    db.execute("DELETE FROM graph_node")
    from tests.featuregen.overlay.upload.test_bridge_candidates import _glossary_col

    _glossary_col(
        db, "core", "customer_master", "customer_id", "customer_id", "integer")
    _glossary_col(
        db, "crm", "customers", "customer_id", "customer_id", "integer")
    first = derive_bridge_candidates(db)[0]
    assert first.type_basis == "declared"
    key = propose_bridge(db, first, actor=_ENRICH_ACTOR, now=_T0)
    first_revision = db.execute(
        "SELECT candidate_revision_id FROM governed_candidate_current "
        "WHERE candidate_id = %s",
        (first.candidate_id,),
    ).fetchone()[0]
    assert db.execute("SELECT count(*) FROM overlay_evidence").fetchone()[0] == 1

    # A structural connector now attests the same physical family. Generic propose_fact denies the
    # still-DRAFT duplicate, but the stronger assessment must become current.
    db.execute(
        "UPDATE graph_node SET data_type = 'integer' "
        "WHERE catalog_source IN ('core', 'crm') AND column_name = 'customer_id'"
    )
    stronger = derive_bridge_candidates(db)[0]
    assert stronger.type_basis == "attested"
    assert propose_bridge(db, stronger, actor=_ENRICH_ACTOR, now=_T0) == key

    current_revision = db.execute(
        "SELECT candidate_revision_id FROM governed_candidate_current "
        "WHERE candidate_id = %s",
        (stronger.candidate_id,),
    ).fetchone()[0]
    assert current_revision != first_revision
    assert db.execute("SELECT count(*) FROM overlay_evidence").fetchone()[0] == 1
    legacy = db.execute(
        "SELECT data_type_family, evidence_json, derivation_version "
        "FROM entity_bridge_candidate_evidence WHERE candidate_id = %s",
        (stronger.candidate_id,),
    ).fetchone()
    assert legacy[0] == "integer"
    assert legacy[1]["type_basis"] == "attested"
    assert legacy[2]


def test_confirmed_composite_grain_reassesses_the_touching_link(
    db,
    service_actor,
    human_actor,
) -> None:
    ensure_upload_catalog_adapter()
    _load(
        db,
        "cib",
        [
            (CanonicalRow("cib", "customers", "cust_num", "integer"), "customer_id"),
            (CanonicalRow("cib", "customers", "business_dt", "date"), "categorical"),
        ],
    )
    _load(
        db,
        "ftr",
        [(CanonicalRow("ftr", "transactions", "cif_id", "integer"), "customer_id")],
    )
    candidate = derive_bridge_candidates(db)[0]
    key = propose_bridge(db, candidate, actor=_ENRICH_ACTOR, now=_T0)
    before = load_current_candidate_assessments(db)[0]
    cib_before = next(
        endpoint
        for endpoint in (before.left_endpoint, before.right_endpoint)
        if endpoint.logical_table_ref == "cib::public.customers"
    )
    assert cib_before.tuple_key_role is TupleKeyRole.UNKNOWN

    value = {"columns": ["cust_num", "business_dt"], "is_unique": True}
    ref = table_ref("cib", "customers")
    proposed = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": ref, "fact_type": "grain", "proposed_value": value},
            service_actor,
            proposal_fingerprint(value),
        ),
    )
    assert proposed.accepted, proposed.denied_reason
    _confirm_grain(
        db,
        "cib",
        "customers",
        ["cust_num", "business_dt"],
        actor=human_actor,
    )
    assert project_verified_table_fact(
        db, "cib", ref, "grain", now=datetime.now(UTC)) == "projected"

    after = load_current_candidate_assessments(db)[0]
    cib_after = next(
        endpoint
        for endpoint in (after.left_endpoint, after.right_endpoint)
        if endpoint.logical_table_ref == "cib::public.customers"
    )
    assert after.bridge_fact_key == key
    assert after.candidate_revision_id != before.candidate_revision_id
    assert cib_after.tuple_key_role is TupleKeyRole.COMPOSITE_MEMBER
    assert any(
        evidence.kind is EvidenceKind.GOVERNED_FACT
        for evidence in after.evidence_refs
    )
