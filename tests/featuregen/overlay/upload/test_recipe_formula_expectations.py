from dataclasses import replace

import pytest

from featuregen.formula.schema import FinalOperation
from featuregen.overlay.upload.recipe_formula_contracts import (
    RecipeFormulaPreflightError,
    bind_formula_expectation,
    expectation_content_hash,
    validate_blueprint,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
    validate_expectation_registry,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import semantic_parameter_hash


def test_registry_exactly_covers_the_two_authorable_anchors() -> None:
    assert set(RECIPE_FORMULA_EXPECTATIONS) == {
        "merchant_mcc_diversity",
        "obligor_facility_count",
    }
    validate_expectation_registry()


@pytest.mark.parametrize(
    ("recipe_id", "grain_role", "operand_role"),
    (
        ("merchant_mcc_diversity", "merchant", "mcc"),
        ("obligor_facility_count", "obligor", "facility"),
    ),
)
def test_count_distinct_blueprints_are_complete(
    recipe_id: str,
    grain_role: str,
    operand_role: str,
) -> None:
    blueprint = RECIPE_FORMULA_EXPECTATIONS[recipe_id]
    expression = blueprint.expressions[0]
    assert blueprint.grain.key_roles == (grain_role,)
    assert expression.operand_role == operand_role
    assert expression.window.event_time_role == "event_ts"
    assert expression.window.length_parameter == "window"
    assert blueprint.semantic_parameter_projections[0].canonical_formula_paths == (
        "body.expr.window.length",
    )
    assert len(expectation_content_hash(blueprint)) == 64


def test_v1_registry_rejects_an_unreviewed_final_operation() -> None:
    blueprint = RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"]
    with pytest.raises(ValueError, match="unary identity"):
        validate_blueprint(replace(blueprint, final_operation=FinalOperation.RATIO))


def test_preflight_binds_roles_and_rejects_cross_table_context() -> None:
    from featuregen.overlay.upload.recipe_grounding_context import RecipeGroundingContextV1
    from featuregen.overlay.upload.templates import (
        BindingResolution,
        GroundedNeedBinding,
        SourceEntityRoleResolution,
    )

    def _binding(role: str, ref: str, concept: str) -> GroundedNeedBinding:
        return GroundedNeedBinding(
            role=role,
            catalog_source="bank",
            logical_ref=ref,
            graph_object_ref=ref.replace("bank::", "public."),
            expected_concept=concept,
            optional=False,
            join_role=None,
            temporal_role=None,
            distinct_binding_group=None,
            binding_resolution=BindingResolution.UNIQUE,
            tied_candidate_logical_refs=(ref,),
            tied_candidate_set_hash="set-hash",
        )

    template_definition = {"version": "test"}
    semantic_parameters = (("window", 90),)
    context = RecipeGroundingContextV1(
        recipe_candidate_key="candidate",
        recipe_id="merchant_mcc_diversity",
        source_entity_need_role="merchant",
        source_entity_role_resolution=SourceEntityRoleResolution.EXPLICIT,
        need_bindings=(
            _binding("merchant", "bank::public.txn.merchant_id", "merchant_id"),
            _binding("mcc", "bank::public.txn.mcc", "mcc"),
            _binding("event_ts", "bank::public.txn.event_ts", "event_timestamp"),
        ),
        semantic_parameters=semantic_parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(
            "merchant_mcc_diversity", semantic_parameters),
        template_definition=template_definition,
        template_content_hash=grounding_content_hash(template_definition),
    )
    bound = bind_formula_expectation(
        context, RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"])
    assert bound.expressions[0].source_relation_ref == "bank::public.txn"
    assert bound.expressions[0].operand_ref == "bank::public.txn.mcc"
    assert bound.expressions[0].window_length == 90

    cross_table = replace(
        context,
        need_bindings=(
            context.need_bindings[0],
            replace(context.need_bindings[1], logical_ref="bank::public.other.mcc"),
            context.need_bindings[2],
        ),
    )
    with pytest.raises(RecipeFormulaPreflightError, match="FORMULA_AUTHORING_UNSUPPORTED"):
        bind_formula_expectation(
            cross_table, RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"])

    with pytest.raises(RecipeFormulaPreflightError, match="RECIPE_DEFINITION_HASH_MISMATCH"):
        bind_formula_expectation(
            replace(context, template_definition={"version": "tampered"}),
            RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"],
        )
