from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.attest.bridge_grounding import (
    BridgeConceptGroundingV1,
    BridgeEndpointGroundingV1,
    EvidencePresence,
    MetadataFacetV1,
    RepresentationRole,
)
from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    KeyMemberRole,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_candidates import (
    MAX_BRIDGE_DERIVATION_EXAMPLES,
    MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR,
    BridgeCandidateDerivationV1,
    BridgeDerivationExampleV1,
    _derive_from_identifier_columns,
    _IdCol,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.ingestion_run import get_run
from featuregen.overlay.upload.stage_report import StageRecorder

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _tokens(name: str) -> tuple[str, ...]:
    return tuple(part for part in name.lower().split("_") if part)


def _fake_col(
    source: str,
    index: int,
    *,
    name: str | None = None,
    role: RepresentationRole = RepresentationRole.IDENTIFIER_VALUE,
) -> _IdCol:
    column = name or f"customer_{index:04d}_id"
    logical_ref = f"{source}::public.customers.{column}"
    facets = (
        MetadataFacetV1("definition", EvidencePresence.ABSENT),
        MetadataFacetV1("names", EvidencePresence.PRESENT, _tokens(column)),
        MetadataFacetV1("terms", EvidencePresence.ABSENT),
        MetadataFacetV1("synonyms", EvidencePresence.ABSENT),
        MetadataFacetV1("domain", EvidencePresence.ABSENT),
        MetadataFacetV1("taxonomy", EvidencePresence.ABSENT),
    )
    grounding = BridgeEndpointGroundingV1(
        logical_column_ref=logical_ref,
        logical_table_ref=f"{source}::public.customers",
        exists=True,
        column_name=column,
        concept=BridgeConceptGroundingV1(
            "customer_id",
            ConceptAuthority.UNKNOWN,
            "unknown",
            (),
            False,
        ),
        governed_entity_id=None,
        advisory_entity_id="customer",
        explicit_namespace=None,
        governed_population=None,
        representation_role=role,
        data_type_family="text",
        type_basis=TypeBasis.ATTESTED,
        is_grain=False,
        key_member_role=KeyMemberRole.UNKNOWN,
        tuple_key_role=TupleKeyRole.UNKNOWN,
        observed_format="identifier",
        metadata_facets=facets,
        evidence_refs=(),
    )
    return _IdCol(
        catalog_source=source,
        table_name="customers",
        column_name=column,
        entity="customer",
        type_family="text",
        is_grain=False,
        type_basis="attested",
        concept_authority="unknown",
        grounding=grounding,
    )


def _candidate_ids(report: BridgeCandidateDerivationV1) -> tuple[str, ...]:
    return tuple(candidate.candidate_id for candidate in report.candidates)


def test_stable_input_order_produces_the_same_bounded_candidates():
    columns = [
        *(_fake_col("left", index) for index in range(35)),
        *(_fake_col("right", index) for index in range(35)),
    ]

    forward = _derive_from_identifier_columns(columns)
    reverse = _derive_from_identifier_columns(reversed(columns))

    assert _candidate_ids(forward) == _candidate_ids(reverse)
    assert forward.block_matched_count == reverse.block_matched_count
    assert forward.considered_count == reverse.considered_count
    assert forward.truncated_pair_count == reverse.truncated_pair_count


def test_one_thousand_column_hub_scores_bounded_work_and_caps_both_endpoints():
    columns = [
        *(_fake_col("left", index) for index in range(500)),
        *(_fake_col("right", index) for index in range(500)),
    ]

    report = _derive_from_identifier_columns(columns)

    assert report.block_matched_count == 250_000
    assert report.considered_count <= (
        len(columns) * MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR // 2
    )
    assert report.considered_count < report.block_matched_count
    assert report.truncated is True
    degree: Counter[str] = Counter()
    for candidate in report.candidates:
        degree[
            f"{candidate.left_ref.catalog_source}:{candidate.left_ref.column}"
        ] += 1
        degree[
            f"{candidate.right_ref.catalog_source}:{candidate.right_ref.column}"
        ] += 1
    assert degree
    assert max(degree.values()) <= MAX_CANDIDATES_PER_ENDPOINT_PER_SOURCE_PAIR


def test_metadata_corroborated_identifier_survives_decoy_descriptions():
    columns = [
        *(_fake_col("left", index) for index in range(30)),
        *(
            _fake_col(
                "right",
                index,
                role=(
                    RepresentationRole.IDENTIFIER_VALUE
                    if index == 29
                    else RepresentationRole.DESCRIPTION_TEXT
                ),
            )
            for index in range(30)
        ),
    ]

    report = _derive_from_identifier_columns(columns)

    retained_pairs = {
        (candidate.left_ref.column, candidate.right_ref.column)
        for candidate in report.candidates
    }
    assert ("customer_0029_id", "customer_0029_id") in retained_pairs
    assert report.suppressed_count > 0
    assert len(report.examples) <= MAX_BRIDGE_DERIVATION_EXAMPLES


def test_truncation_is_visible_in_result_stage_and_run_api(db, monkeypatch):
    report = BridgeCandidateDerivationV1(
        candidates=(),
        block_matched_count=100,
        considered_count=20,
        retained_count=0,
        suppressed_count=5,
        truncated_pair_count=95,
        reason_counts=(
            ("hard_conflict:incompatible_representation_role", 5),
            ("per_endpoint_source_pair_limit", 95),
        ),
        examples=(
            BridgeDerivationExampleV1(
                "left::public.customers.name",
                "right::public.customers.customer_id",
                ("incompatible_representation_role",),
            ),
        ),
    )
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    monkeypatch.setattr(
        "featuregen.overlay.upload.ingest.derive_bridge_candidates_with_report",
        lambda _conn: report,
    )
    run_id = "ingrun_bridge_bound"
    db.execute(
        "INSERT INTO ingestion_run "
        "(id, origin_type, catalog_source, actor_subject, status, started_at, heartbeat_at) "
        "VALUES (%s, 'upload', 'pilot', 'owner', 'in_progress', %s, %s)",
        (run_id, _NOW, _NOW),
    )
    recorder = StageRecorder()
    result = ingest_upload(
        db,
        "pilot",
        [CanonicalRow("pilot", "customers", "customer_id", "text")],
        actor=IdentityEnvelope(
            subject="owner",
            actor_kind="human",
            authenticated=True,
            auth_method="oidc",
            role_claims=("data_owner",),
        ),
        now=_NOW,
        ingestion_run_id=run_id,
        stage_recorder=recorder,
    )

    assert result.join_candidates == 0
    assert result.entity_bridge_candidates_considered == 20
    assert result.entity_bridge_candidates_retained == 0
    assert result.entity_bridge_candidates_suppressed == 5
    assert result.entity_bridge_candidates_truncated == 95
    stage = next(item for item in recorder.reports if item.stage == "entity_bridges")
    assert stage.state == "partial"
    assert stage.reason_code == "candidate_bound"
    assert stage.detail is not None
    assert stage.detail["truncated"] is True
    assert stage.detail["considered_count"] == 20
    assert len(stage.detail["examples"]) == 1

    recorder.flush(db, run_id, now=_NOW)
    run = get_run(db, run_id)
    assert run is not None
    api_stage = next(item for item in run["stages"] if item["stage"] == "entity_bridges")
    assert api_stage["detail"]["truncated_pair_count"] == 95
    assert api_stage["detail"]["truncation_reason"] == "per_endpoint_source_pair_limit"
