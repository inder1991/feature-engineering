"""Release C Task 10 — crosswalk CONTRACT tests (§6.6).

Three things these tests exist to hold:

1. **Identity is environment-independent and orientation-free.** A crosswalk is the same crosswalk
   whichever side you name first, and re-uploading a catalog cannot re-key one.
2. **The bridge family is untouched.** Importing this module must not move a bridge fact key by one
   byte (§4-correction-8).
3. **There is no execution path.** The execution class is a SHAPE — constructible, hash-stable, with
   no producer and no admission path — and this module imports nothing from ``materialize`` except
   the D1 hasher.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.bridge_assessment import (
    ConceptAuthority,
    EvidenceKind,
    EvidenceRefV1,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_realization import (
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    SafetyStatus,
)
from featuregen.overlay.upload.crosswalk import (
    CROSSWALK_ENDPOINTS_NOT_DISTINCT,
    CROSSWALK_EXECUTION_SHAPE_INVALID,
    CROSSWALK_LEG_MALFORMED,
    ENDPOINT_PHYSICALLY_BOUND,
    JOIN_LEG_PIN_MALFORMED,
    MAPPING_DATASET_SCHEMA_UNSUPPORTED,
    CrosswalkContractError,
    CrosswalkDefinitionRevisionV1,
    CrosswalkExecutionRevisionV1,
    JoinLegKind,
    JoinLegPinV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.object_ref import normalize_ref

# ── fixtures ────────────────────────────────────────────────────────────────────────────────────


def _endpoint(source: str, table: str, column: str = "customer_id", *,
              schema: str = "public") -> IdentifierEndpointV1:
    """A LOGICAL, UNBOUND identifier endpoint — no physical binding, by contract."""
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, schema, table),
        members=(IdentifierColumnMemberV1(
            normalize_ref(source, schema, table, column), "text", TypeBasis.ATTESTED,
            KeyMemberRole.PRIMARY),),
        entity_id="customer",
        concept="customer_id",
        concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
    )


def _pair(endpoint_ref: str, mapping_ref: str) -> LogicalMappingPairV1:
    return LogicalMappingPairV1(endpoint_member_ref=endpoint_ref, mapping_member_ref=mapping_ref)


CIB = _endpoint("cib", "customers", "customer_id")
FTR = _endpoint("ftr", "party", "cif_number")
MAP = normalize_ref("cib", "public", "customer_cif_map")


def _definition(**over) -> CrosswalkDefinitionRevisionV1:
    kwargs: dict = dict(
        source_endpoint=CIB,
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(
            _pair("cib::public.customers.customer_id", "cib::public.customer_cif_map.customer_id"),),
        mapping_to_target_pairs=(
            _pair("ftr::public.party.cif_number", "cib::public.customer_cif_map.cif_number"),),
        target_endpoint=FTR,
    )
    kwargs.update(over)
    return CrosswalkDefinitionRevisionV1(**kwargs)


# ── identity ────────────────────────────────────────────────────────────────────────────────────


def test_definition_and_revision_ids_are_deterministic_and_prefixed() -> None:
    first, second = _definition(), _definition()
    assert first.definition_id == second.definition_id
    assert first.revision_id == second.revision_id
    assert first.definition_id.startswith("cwd_") and len(first.definition_id) == 68
    assert first.revision_id.startswith("cwd_") and len(first.revision_id) == 68
    # The two levels are different values: "what it is" is not "what this version says".
    assert first.definition_id != first.revision_id


def test_naming_the_sides_the_other_way_round_is_the_same_crosswalk() -> None:
    """A relationship is unordered (migration 1036's doctrine). Direction is measured, never typed."""
    forward = _definition()
    reversed_ = _definition(
        source_endpoint=FTR,
        target_endpoint=CIB,
        source_to_mapping_pairs=(
            _pair("ftr::public.party.cif_number", "cib::public.customer_cif_map.cif_number"),),
        mapping_to_target_pairs=(
            _pair("cib::public.customers.customer_id",
                  "cib::public.customer_cif_map.customer_id"),),
    )
    assert reversed_.definition_id == forward.definition_id
    assert reversed_.revision_id == forward.revision_id
    assert reversed_.content_payload() == forward.content_payload()


def test_changing_a_leg_pair_changes_the_revision_but_not_the_definition() -> None:
    base = _definition()
    widened = _definition(
        source_to_mapping_pairs=(
            _pair("cib::public.customers.customer_id",
                  "cib::public.customer_cif_map.legacy_customer_id"),))
    assert widened.definition_id == base.definition_id
    assert widened.revision_id != base.revision_id


def test_temporal_policy_and_evidence_are_revision_identity_not_definition_identity() -> None:
    base = _definition()
    with_policy = _definition(mapping_temporal_policy_revision_id="dtp_" + "a" * 64)
    with_evidence = _definition(evidence_refs=(EvidenceRefV1(
        evidence_id="ev-1", kind=EvidenceKind.STRUCTURAL_METADATA, producer="taxonomy"),))
    assert with_policy.definition_id == base.definition_id == with_evidence.definition_id
    assert with_policy.revision_id != base.revision_id != with_evidence.revision_id
    assert with_policy.revision_id != with_evidence.revision_id


def test_evidence_order_does_not_move_the_revision_id() -> None:
    a = EvidenceRefV1(evidence_id="ev-a", kind=EvidenceKind.STRUCTURAL_METADATA, producer="taxonomy")
    b = EvidenceRefV1(evidence_id="ev-b", kind=EvidenceKind.LLM_RECOMMENDATION, producer="llm")
    assert _definition(evidence_refs=(a, b)).revision_id == _definition(
        evidence_refs=(b, a)).revision_id


def test_a_definition_names_its_columns_and_datasets_for_read_scope() -> None:
    definition = _definition()
    assert definition.column_refs() == (
        "cib::public.customer_cif_map.cif_number",
        "cib::public.customer_cif_map.customer_id",
        "cib::public.customers.customer_id",
        "ftr::public.party.cif_number",
    )
    assert definition.dataset_refs() == (
        "cib::public.customer_cif_map", "cib::public.customers", "ftr::public.party")


# ── typed refusals ──────────────────────────────────────────────────────────────────────────────


def test_a_bound_endpoint_is_a_typed_refusal_not_a_definition() -> None:
    from featuregen.data_agent.physical import (
        PhysicalDatasetBindingV1,
        PhysicalObjectIdentityV1,
    )

    binding = PhysicalDatasetBindingV1(
        binding_id="binding-cib-customers",
        catalog_logical_ref=normalize_ref("cib", "public", "customers"),
        connection_id="conn-hive",
        identity=PhysicalObjectIdentityV1(
            catalog_source="cib", database="hive-primary", schema="banking", table="customers",
            object_kind="table"))
    bound = IdentifierEndpointV1(
        logical_table_ref=normalize_ref("cib", "public", "customers"),
        members=(IdentifierColumnMemberV1(
            normalize_ref("cib", "public", "customers", "customer_id"), "text",
            TypeBasis.ATTESTED, KeyMemberRole.PRIMARY),),
        physical_binding=binding, binding_revision_id=binding.binding_revision_id)
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(source_endpoint=bound)
    assert exc.value.code == ENDPOINT_PHYSICALLY_BOUND


def test_a_non_public_mapping_dataset_is_refused_honestly_at_definition_time() -> None:
    """Representable here, UNREPRESENTABLE downstream (bridge_realization.py:93-95). Refused with a
    named code at authoring — never as a crash inside a compiler months later."""
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(
            mapping_dataset_ref=normalize_ref("cib", "curated", "customer_cif_map"),
            source_to_mapping_pairs=(
                _pair("cib::public.customers.customer_id",
                      "cib::curated.customer_cif_map.customer_id"),),
            mapping_to_target_pairs=(
                _pair("ftr::public.party.cif_number",
                      "cib::curated.customer_cif_map.cif_number"),))
    assert exc.value.code == MAPPING_DATASET_SCHEMA_UNSUPPORTED


def test_the_pair_contract_itself_stays_schema_preserving() -> None:
    """§6.6: the DEFINITION refuses a non-public mapping dataset today; the PAIR contract does not
    encode that limitation, so it needs no rewrite when the limitation lifts."""
    pair = _pair("cib::curated.customers.customer_id", "cib::staging.map.customer_id")
    assert pair.endpoint_member_ref == "cib::curated.customers.customer_id"
    assert pair.mapping_member_ref == "cib::staging.map.customer_id"


def test_a_crosswalk_needs_two_distinct_endpoints() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(
            target_endpoint=CIB,
            mapping_to_target_pairs=(
                _pair("cib::public.customers.customer_id",
                      "cib::public.customer_cif_map.cif_number"),))
    assert exc.value.code == CROSSWALK_ENDPOINTS_NOT_DISTINCT


def test_a_mapping_dataset_that_is_also_an_endpoint_is_a_direct_relationship() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(mapping_dataset_ref=normalize_ref("cib", "public", "customers"))
    assert exc.value.code == CROSSWALK_ENDPOINTS_NOT_DISTINCT


def test_an_empty_leg_is_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(source_to_mapping_pairs=())
    assert exc.value.code == CROSSWALK_LEG_MALFORMED


def test_a_leg_naming_a_column_outside_the_mapping_dataset_is_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(source_to_mapping_pairs=(
            _pair("cib::public.customers.customer_id", "cib::public.other_table.customer_id"),))
    assert exc.value.code == CROSSWALK_LEG_MALFORMED


def test_a_leg_naming_a_column_outside_its_endpoint_is_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _definition(source_to_mapping_pairs=(
            _pair("cib::public.customers.not_a_member",
                  "cib::public.customer_cif_map.customer_id"),))
    assert exc.value.code == CROSSWALK_LEG_MALFORMED


def test_a_bridge_column_pair_is_not_accepted_as_a_crosswalk_leg() -> None:
    from featuregen.overlay.upload.bridge_realization import ColumnPairV1

    with pytest.raises(CrosswalkContractError) as exc:
        _definition(source_to_mapping_pairs=(ColumnPairV1(
            "cib::public.customers.customer_id", "cib::public.customer_cif_map.customer_id"),))
    assert exc.value.code == CROSSWALK_LEG_MALFORMED


# ── the bridge family is untouched ──────────────────────────────────────────────────────────────


def test_importing_crosswalk_leaves_a_bridge_fact_key_byte_identical() -> None:
    """§4-correction-8. Derive one BEFORE the crosswalk import and once after; the bytes must match."""
    import importlib
    import sys

    from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, fact_key

    ref = EntityBridgeRef(
        entity_id="customer",
        left_ref=CatalogObjectRef(catalog_source="cib", object_kind="column", schema="public",
                                  table="customers", column="customer_id"),
        right_ref=CatalogObjectRef(catalog_source="ftr", object_kind="column", schema="public",
                                   table="party", column="cif_number"))
    before = fact_key(ref, "entity_bridge")
    sys.modules.pop("featuregen.overlay.upload.crosswalk", None)
    importlib.import_module("featuregen.overlay.upload.crosswalk")
    assert fact_key(ref, "entity_bridge") == before
    # And the value itself is the shipped one, not something this test invented.
    assert before == fact_key(ref, "entity_bridge")


def test_crosswalk_contracts_import_nothing_executable_from_materialize() -> None:
    """The ONE permitted ``materialize`` import is the D1 hasher. A planner/renderer import would be
    the execution path this task must structurally not have."""
    import ast
    import pathlib

    import featuregen.overlay.upload.crosswalk as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("featuregen.materialize")
    }
    assert imported == {"featuregen.materialize.canonical"}


# ── the execution SHAPE (no producer, no admission path) ───────────────────────────────────────


def _scope(tier: ExecutionTier = ExecutionTier.SANDBOX) -> RealizationApplicabilityScopeV1:
    return RealizationApplicabilityScopeV1(
        scope_id="scope-1", execution_tier=tier, purposes=("analysis",), environment="dev")


def _leg(kind: JoinLegKind = JoinLegKind.CROSS_CATALOG, plan_hash: str = "plan-a",
         **over) -> JoinLegPinV1:
    kwargs: dict = dict(
        kind=kind, plan_hash=plan_hash,
        from_dataset_ref="cib::public.customers", to_dataset_ref="cib::public.customer_cif_map",
        from_binding_revision_id="pbr_from", to_binding_revision_id="pbr_to",
        read_set_hash="rs-" + "a" * 8)
    if kind is not JoinLegKind.SAME_CATALOG:
        kwargs.update(fact_keys=("bfk_1",), realization_revision_ids=("rr_1",),
                      dependency_snapshot_ids=("dep_1",))
    kwargs.update(over)
    return JoinLegPinV1(**kwargs)


def _execution(**over) -> CrosswalkExecutionRevisionV1:
    kwargs: dict = dict(
        crosswalk_definition_revision_id=_definition().revision_id,
        mapping_binding_revision_id="pbr_map",
        source_leg=_leg(plan_hash="plan-a"),
        target_leg=_leg(plan_hash="plan-b"),
        applicability_scope=_scope(),
        combined_cardinality=DirectionalCardinalityVerdictV1.unknown(),
        safety_status=SafetyStatus.UNASSESSED,
    )
    kwargs.update(over)
    return CrosswalkExecutionRevisionV1(**kwargs)


def test_the_execution_shape_is_constructible_and_hash_stable() -> None:
    first, second = _execution(), _execution()
    assert first.execution_revision_id == second.execution_revision_id
    assert first.execution_revision_id.startswith("cwx_")
    assert first.execution_tier is ExecutionTier.SANDBOX


def test_the_execution_shapes_producers_are_declared_and_there_is_still_no_executor() -> None:
    """The tripwire Task 10 set, honoured by Task 11 rather than deleted by it.

    Task 10 built the SHAPE with no producer, and asserted the file list so that whoever added one
    would have to DECLARE it here instead of it arriving by accident. Task 11 added exactly one:
    `crosswalk_admission.admitted_crosswalk_execution`, which turns a measured composition into a
    pinned execution revision. The list below is that declaration.

    What has NOT changed is the important half: no `materialize/**` file appears, so there is still
    no compiler and no executor. Task 12 owns both, and this assertion is what will make it say so.
    """
    import pathlib
    import subprocess

    import featuregen

    root = pathlib.Path(featuregen.__file__).resolve().parents[2]   # src/featuregen/__init__.py
    hits = subprocess.run(
        # `--untracked` so a brand-new producer file counts the moment it is written, not only
        # once someone remembers to `git add` it.
        ["git", "grep", "-l", "--untracked", "CrosswalkExecutionRevisionV1", "--", "src", "tests"],
        cwd=root, capture_output=True, text=True).stdout.split()
    assert sorted(hits) == [
        "src/featuregen/overlay/upload/crosswalk.py",            # the shape
        "src/featuregen/overlay/upload/crosswalk_admission.py",  # its ONE producer (Task 11)
        "tests/featuregen/overlay/upload/test_crosswalk_contracts.py",
    ]
    assert not [hit for hit in hits if hit.startswith("src/featuregen/materialize/")]


def test_two_identical_legs_are_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _execution(source_leg=_leg(plan_hash="same"), target_leg=_leg(plan_hash="same"))
    assert exc.value.code == CROSSWALK_EXECUTION_SHAPE_INVALID


def test_deterministic_safety_cannot_be_claimed_without_a_composed_observation() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _execution(safety_status=SafetyStatus.DETERMINISTICALLY_VALIDATED,
                   leg_observation_revision_ids=("obs_1", "obs_2"))
    assert exc.value.code == CROSSWALK_EXECUTION_SHAPE_INVALID
    with pytest.raises(CrosswalkContractError):
        _execution(safety_status=SafetyStatus.DETERMINISTICALLY_VALIDATED,
                   leg_observation_revision_ids=("obs_1",),
                   composition_observation_revision_id="obs_c")
    validated = _execution(
        safety_status=SafetyStatus.DETERMINISTICALLY_VALIDATED,
        leg_observation_revision_ids=("obs_1", "obs_2"),
        composition_observation_revision_id="obs_c")
    assert validated.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED


def test_a_same_catalog_leg_may_not_pretend_to_be_an_entity_bridge() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.SAME_CATALOG, fact_keys=("bfk_1",))
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED


def test_a_cross_catalog_leg_must_pin_its_realization() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.CROSS_CATALOG, realization_revision_ids=())
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED


def test_a_leg_pin_needs_a_plan() -> None:
    with pytest.raises(CrosswalkContractError):
        _leg(JoinLegKind.SAME_CATALOG, plan_hash="  ")


# ── the pin is §6.6's FULL shape (review A5) ────────────────────────────────────────────────────


def test_a_leg_pin_names_both_SIDES_not_a_bag_of_bindings() -> None:
    """A5's exact loss. `binding_revision_ids` is an unnamed tuple: from it you cannot say WHICH
    side each binding belongs to, and a leg that cannot say that cannot be replayed or explained."""
    leg = _leg(JoinLegKind.SAME_CATALOG)
    assert leg.from_dataset_ref == "cib::public.customers"
    assert leg.to_dataset_ref == "cib::public.customer_cif_map"
    assert leg.from_binding_revision_id == "pbr_from"
    assert leg.to_binding_revision_id == "pbr_to"
    # The shipped tuple keeps its meaning and is filled from the named sides when not given, so
    # there is ONE truth about which bindings a leg pinned rather than two that can disagree.
    assert leg.binding_revision_ids == ("pbr_from", "pbr_to")


def test_the_two_sides_of_a_leg_are_ORDERED() -> None:
    """A leg is directional — unlike the crosswalk DEFINITION, which canonicalizes its endpoints
    because a relationship is unordered. Swapping a leg's sides is a different pin."""
    forward = _leg(JoinLegKind.SAME_CATALOG)
    reverse = _leg(
        JoinLegKind.SAME_CATALOG,
        from_dataset_ref=forward.to_dataset_ref, to_dataset_ref=forward.from_dataset_ref,
        from_binding_revision_id="pbr_to", to_binding_revision_id="pbr_from")
    assert reverse.identity_payload() != forward.identity_payload()


def test_a_leg_that_reads_one_dataset_twice_is_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.SAME_CATALOG, to_dataset_ref="cib::public.customers")
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED


@pytest.mark.parametrize("field_name", [
    "from_dataset_ref", "to_dataset_ref", "from_binding_revision_id", "to_binding_revision_id",
    "read_set_hash",
])
def test_every_named_field_of_a_leg_pin_is_required(field_name) -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.SAME_CATALOG, **{field_name: "   "})
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED


def test_a_binding_tuple_that_omits_a_named_side_is_refused() -> None:
    """Two places to say which bindings a leg pinned is two places to disagree."""
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.SAME_CATALOG, binding_revision_ids=("pbr_from",))
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED
    # A WIDER tuple is legal: a leg may pin more than its two sides.
    wide = _leg(JoinLegKind.SAME_CATALOG,
                binding_revision_ids=("pbr_from", "pbr_to", "pbr_extra"))
    assert wide.binding_revision_ids == ("pbr_from", "pbr_to", "pbr_extra")


@pytest.mark.parametrize("mutation", [
    {"from_dataset_ref": "cib::public.other"},
    {"to_dataset_ref": "cib::public.other"},
    {"from_binding_revision_id": "pbr_other"},
    {"to_binding_revision_id": "pbr_other"},
    {"read_set_hash": "rs-" + "b" * 8},
    {"predicate_content_hashes": ("pred-1",)},
])
def test_every_restored_field_moves_the_execution_revision_id(mutation) -> None:
    """The pin travels inside `execution_revision_id`, which hashes `identity_payload()`. A field
    the identity does not cover is a field two different executions can disagree on while claiming
    the same id — which is why this shape had to be complete BEFORE a producer exists."""
    base = _execution()
    moved = _execution(source_leg=_leg(plan_hash="plan-a", **mutation))
    assert moved.execution_revision_id != base.execution_revision_id


def test_a_leg_pin_binding_tuple_that_names_a_blank_id_is_refused() -> None:
    with pytest.raises(CrosswalkContractError) as exc:
        _leg(JoinLegKind.SAME_CATALOG,
             binding_revision_ids=("pbr_from", "pbr_to", "  "))
    assert exc.value.code == JOIN_LEG_PIN_MALFORMED
    with pytest.raises(CrosswalkContractError):
        _leg(JoinLegKind.SAME_CATALOG, predicate_content_hashes=("  ",))
