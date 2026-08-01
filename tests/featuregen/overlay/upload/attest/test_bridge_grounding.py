from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import record_field_decision
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.upload.attest.bridge_grounding import (
    EvidencePresence,
    MetadataAgreement,
    PopulationRelation,
    RepresentationCompatibility,
    RepresentationRole,
    ground_bridge_endpoint,
    ground_identifier_link,
)
from featuregen.overlay.upload.bridge_assessment import NamespaceVerdict
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref


def _load(db, source: str, rows: list[tuple[CanonicalRow, str]]) -> None:
    physical_rows = [row for row, _concept in rows]
    build_graph(
        db,
        source,
        physical_rows,
        concepts={content_hash(row): concept for row, concept in rows},
    )


def _column(
    source: str,
    table: str,
    column: str,
    concept: str,
    *,
    definition: str = "",
) -> tuple[CanonicalRow, str]:
    return CanonicalRow(
        source,
        table,
        column,
        "varchar(150)",
        definition=definition,
    ), concept


def _record_concept(
    db,
    *,
    source: str,
    table: str,
    column: str,
    producer: EvidenceProducer,
    strength: AssertionStrength,
) -> str:
    logical_ref = normalize_ref(source, "public", table, column)
    evidence_id = record_field_evidence(
        db,
        logical_ref=logical_ref,
        field_name="concept",
        proposed_value="customer_id",
        producer=producer,
        strength=strength,
        producer_ref=f"{producer.value}-test",
        source_snapshot_id="snapshot-1",
        input_hash=field_input_hash(
            logical_ref=logical_ref,
            field_name="concept",
            material={"column": column, "producer": producer.value},
        ),
    )
    decision_id = record_field_decision(
        db,
        logical_ref=logical_ref,
        field_name="concept",
        event_type="resolved",
        selected_evidence_ids=(evidence_id,),
        evidence_set_hash=canonical_hash([evidence_id]),
        display_value_hash=canonical_hash("customer_id"),
        load_bearing_value_hash=(
            canonical_hash("customer_id")
            if producer in {EvidenceProducer.SOURCE, EvidenceProducer.HUMAN}
            else None
        ),
        conflict_status="none",
        reason_codes=(),
        field_policy_version="test",
        resolver_version="test",
        actor_ref=None,
        supersedes_event_id=None,
    )
    db.execute(
        "UPDATE graph_node SET concept_decision_id = %s "
        "WHERE catalog_source = %s AND object_ref = %s",
        (decision_id, source, f"public.{table}.{column}"),
    )
    return logical_ref


def _record_governed_field(
    db,
    logical_ref: str,
    field_name: str,
    value: str,
) -> None:
    record_field_evidence(
        db,
        logical_ref=logical_ref,
        field_name=field_name,
        proposed_value=value,
        producer=EvidenceProducer.SOURCE,
        strength=AssertionStrength.ATTESTED,
        producer_ref="source-test",
        source_snapshot_id="snapshot-1",
        input_hash=field_input_hash(
            logical_ref=logical_ref,
            field_name=field_name,
            material=value,
        ),
    )


def test_customer_number_and_cif_stay_visible_without_claiming_same_namespace(db):
    _load(db, "cib", [_column("cib", "bo_cib_customer", "cust_num", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])

    candidates = derive_bridge_candidates(db)

    assert len(candidates) == 1
    assessment = candidates[0].assessment
    assert assessment is not None
    assert assessment.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert assessment.hard_conflicts == ()
    assert "same_entity_namespace_unproven" in assessment.explanation_codes


def test_branch_name_cannot_become_an_identifier_namespace_proposal(db):
    _load(
        db,
        "cib",
        [_column("cib", "customers", "cust_prim_branch_nm", "branch_id")],
    )
    _load(
        db,
        "ftr",
        [_column("ftr", "transactions", "tran_branch_sol_id", "branch_id")],
    )

    grounding = ground_identifier_link(
        db,
        "cib::public.customers.cust_prim_branch_nm",
        "ftr::public.transactions.tran_branch_sol_id",
    )

    assert grounding.left.representation_role is RepresentationRole.HUMAN_LABEL
    assert grounding.right.representation_role is RepresentationRole.IDENTIFIER_VALUE
    assert grounding.representation_compatibility is RepresentationCompatibility.INCOMPATIBLE
    assert "incompatible_representation_role" in grounding.hard_conflicts
    assert derive_bridge_candidates(db) == ()


def test_branch_code_cannot_equal_a_description(db):
    _load(
        db,
        "cib",
        [_column("cib", "customers", "cust_pref_branch_cd", "branch_id")],
    )
    _load(
        db,
        "ftr",
        [_column("ftr", "transactions", "sol_desc", "branch_id")],
    )

    grounding = ground_identifier_link(
        db,
        "cib::public.customers.cust_pref_branch_cd",
        "ftr::public.transactions.sol_desc",
    )

    assert grounding.left.representation_role is RepresentationRole.IDENTIFIER_VALUE
    assert grounding.right.representation_role is RepresentationRole.DESCRIPTION_TEXT
    assert "incompatible_representation_role" in grounding.hard_conflicts
    assert derive_bridge_candidates(db) == ()


def test_cardholder_customer_id_is_an_advisory_subset_not_a_different_namespace(db):
    _load(
        db,
        "cards",
        [_column("cards", "cardholders", "cardholder_cif_id", "customer_id")],
    )
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])

    grounding = ground_identifier_link(
        db,
        "cards::public.cardholders.cardholder_cif_id",
        "ftr::public.transactions.cif_id",
    )

    assert grounding.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert grounding.governed_population_relation is PopulationRelation.UNKNOWN
    assert grounding.population_hypothesis == PopulationRelation.LEFT_SUBSET.value
    assert "different_governed_identifier_namespace" not in grounding.hard_conflicts


def test_llm_only_customer_concept_remains_visible_and_labelled(db):
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])
    left_ref = _record_concept(
        db,
        source="core",
        table="customers",
        column="customer_id",
        producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED,
    )

    endpoint = ground_bridge_endpoint(db, left_ref)
    candidates = derive_bridge_candidates(db)

    assert endpoint.concept.provenance_label == "llm_only"
    assert endpoint.concept.authority.value == "llm"
    assert endpoint.concept.authoritative is False
    assert len(candidates) == 1
    assert candidates[0].left_concept_authority == "llm"
    assert candidates[0].assessment is not None
    assert "llm_only" in candidates[0].assessment.explanation_codes


def test_source_attested_concept_uses_the_planner_authority_semantics(db):
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    logical_ref = _record_concept(
        db,
        source="core",
        table="customers",
        column="customer_id",
        producer=EvidenceProducer.SOURCE,
        strength=AssertionStrength.ATTESTED,
    )

    endpoint = ground_bridge_endpoint(db, logical_ref)

    assert endpoint.concept.authority.value == "source"
    assert endpoint.concept.provenance_label == "source_attested"
    assert endpoint.concept.authoritative is True


def test_explicit_different_namespaces_are_a_hard_conflict(db):
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])
    left_ref = "core::public.customers.customer_id"
    right_ref = "ftr::public.transactions.cif_id"
    _record_governed_field(db, left_ref, "identifier_namespace", "retail-customer")
    _record_governed_field(db, right_ref, "identifier_namespace", "merchant-customer")

    grounding = ground_identifier_link(db, left_ref, right_ref)

    assert grounding.namespace_verdict is NamespaceVerdict.DIFFERENT
    assert "different_governed_identifier_namespace" in grounding.hard_conflicts
    assert derive_bridge_candidates(db) == ()


def test_governed_entity_disagreement_within_one_namespace_is_a_note_not_a_conflict(db):
    """Namespace pairing rule: both concepts draw from "cif", so disagreeing GOVERNED entities
    (still read from governed field evidence, never the display concept — the display concepts
    AGREE here, so only the governed rows can disagree) become an ``entity_disagreement``
    explanation and the pair still derives, with the deterministic entity pick."""
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])
    left_ref = "core::public.customers.customer_id"
    right_ref = "ftr::public.transactions.cif_id"
    _record_governed_field(db, left_ref, "entity", "customer")
    _record_governed_field(db, right_ref, "entity", "merchant")

    grounding = ground_identifier_link(db, left_ref, right_ref)

    assert "different_governed_entity" not in grounding.hard_conflicts
    assert "entity_disagreement" in grounding.explanation_codes
    candidates = derive_bridge_candidates(db)
    assert len(candidates) == 1
    # customer_id is the SUBJECT-role name; cif_id resolves no party role -> the subject's entity.
    assert candidates[0].entity_id == "customer"


def test_governed_entity_conflict_survives_across_namespaces(db):
    """Outside a shared identifier namespace the governed-entity comparison still gates: the pair
    could never derive anyway (different namespaces), and a direct proposal keeps hitting the
    hard conflict."""
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "acct_ref", "account_id")])
    left_ref = "core::public.customers.customer_id"
    right_ref = "ftr::public.transactions.acct_ref"
    _record_governed_field(db, left_ref, "entity", "customer")
    _record_governed_field(db, right_ref, "entity", "merchant")

    grounding = ground_identifier_link(db, left_ref, right_ref)

    assert "different_governed_entity" in grounding.hard_conflicts
    assert derive_bridge_candidates(db) == ()


def test_missing_endpoint_is_a_hard_conflict_not_an_unknown_match(db):
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])

    grounding = ground_identifier_link(
        db,
        "core::public.customers.customer_id",
        "missing::public.customers.customer_id",
    )

    assert "endpoint_not_found" in grounding.hard_conflicts


def test_missing_synonyms_and_taxonomy_are_absent_not_disagreement(db):
    _load(db, "core", [_column("core", "customers", "customer_id", "customer_id")])
    _load(db, "ftr", [_column("ftr", "transactions", "cif_id", "customer_id")])

    grounding = ground_identifier_link(
        db,
        "core::public.customers.customer_id",
        "ftr::public.transactions.cif_id",
    )
    comparisons = {
        item.field_name: item.agreement for item in grounding.metadata_comparisons
    }

    assert grounding.left.facet("synonyms").presence is EvidencePresence.ABSENT
    assert grounding.right.facet("taxonomy").presence is EvidencePresence.ABSENT
    assert comparisons["synonyms"] is MetadataAgreement.ABSENT
    assert comparisons["taxonomy"] is MetadataAgreement.ABSENT


def test_cross_domain_is_neither_a_namespace_conflict_nor_a_hard_conflict(db):
    left = _column("payments", "transactions", "customer_id", "customer_id")
    right = _column("customer", "master", "cif_id", "customer_id")
    build_graph(
        db,
        "payments",
        [left[0]],
        concepts={content_hash(left[0]): left[1]},
        domains={"transactions": "payments"},
    )
    build_graph(
        db,
        "customer",
        [right[0]],
        concepts={content_hash(right[0]): right[1]},
        domains={"master": "customer"},
    )

    grounding = ground_identifier_link(
        db,
        "payments::public.transactions.customer_id",
        "customer::public.master.cif_id",
    )

    assert grounding.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert grounding.hard_conflicts == ()
