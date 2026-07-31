from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload.conftest import _drain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.catalog import FixtureCatalog, current_catalog_adapter
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.upload.attest.bridge_grounding import ground_bridge_endpoint
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_cardinality import (
    CompleteKeyV1,
    EndpointKeyness,
    KeyCompletionRequirementKind,
    assess_endpoint_keyness,
    infer_metadata_cardinality,
    resolve_complete_key,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import _assert_fact
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.multisource_contracts import GrainAuthorityProvenance
from featuregen.overlay.upload.planner.multisource_endpoints import governed_endpoint
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality
from featuregen.overlay.upload.upload_catalog import (
    ensure_upload_catalog_adapter,
    table_ref,
)

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _endpoint(source: str, table: str, *columns: str) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=tuple(
            IdentifierColumnMemberV1(
                normalize_ref(source, "public", table, column),
                "string",
                TypeBasis.DECLARED,
            )
            for column in columns
        ),
    )


def _key(
    source: str,
    table: str,
    *columns: str,
    unique: bool = True,
    authority: GrainAuthorityProvenance = GrainAuthorityProvenance.source_declared,
) -> CompleteKeyV1:
    return CompleteKeyV1(
        catalog=source,
        logical_table_ref=normalize_ref(source, "public", table),
        ordered_column_refs=tuple(
            normalize_ref(source, "public", table, column) for column in columns
        ),
        is_unique=unique,
        authority_provenance=authority,
        fact_key=f"grain:{source}:{table}",
        fact_revision=f"revision:{source}:{table}",
        dependency_identity=f"dependency:{source}:{table}",
    )


def _graph(db, source: str, table: str, columns: tuple[str, ...]) -> None:
    build_graph(
        db,
        source,
        [
            CanonicalRow(source, table, column, "string", is_grain=True)
            for column in columns
        ],
        concepts={},
    )


def test_cib_ftr_scalar_link_does_not_invent_a_complete_composite_key() -> None:
    ftr = _endpoint("ftr", "transactions", "cif_id")
    cib = _endpoint("cib", "customers", "cust_num")

    result = infer_metadata_cardinality(
        ftr,
        cib,
        from_complete_key=_key("ftr", "transactions", "tran_id"),
        to_complete_key=_key("cib", "customers", "business_dt", "cust_num"),
    )

    assert not result.cardinality.known
    assert result.to_key.keyness is EndpointKeyness.COMPOSITE_KEY_MEMBER
    assert result.requirements == (
        result.requirements[0],
    )
    requirement = result.requirements[0]
    assert requirement.kind is (
        KeyCompletionRequirementKind.ADDITIONAL_KEY_OR_SNAPSHOT_PREDICATE
    )
    assert requirement.endpoint_side == "to"
    assert requirement.missing_key_refs == (
        normalize_ref("cib", "public", "customers", "business_dt"),
    )


def test_separately_supplied_context_mapping_can_complete_target_key() -> None:
    ftr = _endpoint("ftr", "transactions", "cif_id", "business_dt")
    cib = _endpoint("cib", "customers", "cust_num", "business_dt")
    result = infer_metadata_cardinality(
        ftr,
        cib,
        from_complete_key=_key("ftr", "transactions", "tran_id"),
        to_complete_key=_key("cib", "customers", "cust_num", "business_dt"),
    )
    assert result.cardinality.value is Cardinality.MANY_TO_ONE
    assert result.requirements == ()


def test_same_named_context_column_is_not_added_to_a_scalar_mapping() -> None:
    ftr = _endpoint("ftr", "transactions", "cif_id")
    cib = _endpoint("cib", "customers", "cust_num")
    result = infer_metadata_cardinality(
        ftr,
        cib,
        from_complete_key=_key("ftr", "transactions", "tran_id"),
        to_complete_key=_key("cib", "customers", "cust_num", "business_dt"),
    )
    assert not result.cardinality.known
    assert result.to_key.missing_key_refs == (
        normalize_ref("cib", "public", "customers", "business_dt"),
    )


def test_complete_non_unique_target_cannot_yield_many_to_one() -> None:
    source = _endpoint("ftr", "transactions", "cif_id")
    target = _endpoint("cib", "customers", "cust_num")
    result = infer_metadata_cardinality(
        source,
        target,
        from_complete_key=_key("ftr", "transactions", "tran_id"),
        to_complete_key=_key("cib", "customers", "cust_num", unique=False),
    )
    assert result.to_key.keyness is EndpointKeyness.COMPLETE_NON_UNIQUE_GRAIN
    assert result.cardinality.value is Cardinality.MANY_TO_MANY
    assert result.risk_codes == ("many_to_many_fanout_risk",)


@pytest.mark.parametrize(
    ("from_unique", "to_unique", "expected"),
    [
        (False, True, Cardinality.MANY_TO_ONE),
        (True, False, Cardinality.ONE_TO_MANY),
        (True, True, Cardinality.ONE_TO_ONE),
        (False, False, Cardinality.MANY_TO_MANY),
    ],
)
def test_directional_truth_table(
    from_unique: bool,
    to_unique: bool,
    expected: Cardinality,
) -> None:
    left = _endpoint("left", "objects", "id")
    right = _endpoint("right", "objects", "id")
    result = infer_metadata_cardinality(
        left,
        right,
        from_complete_key=_key("left", "objects", "id", unique=from_unique),
        to_complete_key=_key("right", "objects", "id", unique=to_unique),
    )
    assert result.cardinality.value is expected


def test_source_declared_complete_key_needs_no_human_confirmer(db, service_actor) -> None:
    ensure_upload_catalog_adapter()
    _graph(db, "cib", "customers", ("cust_num",))
    assert _assert_fact(
        db,
        "cib",
        "customers",
        "grain",
        {"columns": ["cust_num"], "is_unique": True},
        actor=service_actor,
    )
    _drain(db)

    key = resolve_complete_key(
        db,
        current_catalog_adapter(),
        catalog="cib",
        table_ref="public.customers",
        now=_NOW,
    )

    assert key is not None
    assert key.is_unique is True
    assert key.authority_provenance is GrainAuthorityProvenance.source_declared
    assert key.available_without_human_confirmation is True
    assert key.fact_revision
    assert key.dependency_identity


def test_governed_endpoint_cannot_silently_discard_false_uniqueness(
    db,
    service_actor,
) -> None:
    ensure_upload_catalog_adapter()
    _graph(db, "cib", "customer_history", ("cust_num",))
    assert _assert_fact(
        db,
        "cib",
        "customer_history",
        "grain",
        {"columns": ["cust_num"], "is_unique": False},
        actor=service_actor,
    )
    _drain(db)

    endpoint = governed_endpoint(
        db,
        current_catalog_adapter(),
        catalog="cib",
        table_ref="public.customer_history",
        now=_NOW,
    )
    assert endpoint is not None
    assert endpoint.grain_is_unique is False

    key = resolve_complete_key(
        db,
        current_catalog_adapter(),
        catalog="cib",
        table_ref="public.customer_history",
        now=_NOW,
    )
    assert key is not None
    assert key.ordered_column_refs == (
        normalize_ref("cib", "public", "customer_history", "cust_num"),
    )
    assert key.is_unique is False


def test_flat_is_grain_flag_never_claims_complete_unique_key(db) -> None:
    _graph(db, "cib", "customers", ("cust_num",))
    grounded = ground_bridge_endpoint(
        db, normalize_ref("cib", "public", "customers", "cust_num"))
    assert grounded.is_grain is True
    assert grounded.tuple_key_role.value == "unknown"
    assert grounded.key_member_role.value == "unknown"


def test_catalog_authoritative_complete_key_needs_no_human_confirmer(db) -> None:
    _graph(db, "cib", "customers", ("cust_num",))
    adapter = FixtureCatalog("cib")
    adapter.set_fact(
        table_ref("cib", "customers"),
        "grain",
        {"columns": ["cust_num"], "is_unique": True},
        authoritative=True,
    )
    key = resolve_complete_key(
        db,
        adapter,
        catalog="cib",
        table_ref="public.customers",
        now=_NOW,
    )
    assert key is not None
    assert key.authority_provenance is GrainAuthorityProvenance.catalog_authoritative
    assert key.available_without_human_confirmation is True
    assert key.fact_revision.startswith("catalog:")


def test_draft_and_flat_is_grain_flags_cannot_support_metadata_path(
    db,
    service_actor,
) -> None:
    ensure_upload_catalog_adapter()
    _graph(db, "cib", "customers", ("cust_num",))
    result = propose_fact(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {
                "ref": table_ref("cib", "customers"),
                "fact_type": "grain",
                "proposed_value": {"columns": ["cust_num"], "is_unique": True},
            },
            service_actor,
            "draft-grain-cib-customers",
        ),
    )
    assert result.accepted
    _drain(db)

    assert resolve_complete_key(
        db,
        current_catalog_adapter(),
        catalog="cib",
        table_ref="public.customers",
        now=_NOW,
    ) is None


def test_endpoint_assessment_retains_explicit_false_uniqueness() -> None:
    endpoint = _endpoint("cib", "customers", "cust_num")
    assessed = assess_endpoint_keyness(
        endpoint,
        _key("cib", "customers", "cust_num", unique=False),
    )
    assert assessed.keyness is EndpointKeyness.COMPLETE_NON_UNIQUE_GRAIN
    assert assessed.unique_verdict is False
