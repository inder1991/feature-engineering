"""Reviewed Formula-v1 blueprints for authorable recipe anchors."""
from __future__ import annotations

from featuregen.formula.schema import (
    AggregateFunction,
    EmptyWindowResult,
    FinalOperation,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowBasis,
    WindowUnit,
)
from featuregen.overlay.upload.recipe_formula_contracts import (
    DecimalPolicyExpectationV1,
    ExpressionRoleExpectationV1,
    GrainExpectationV1,
    RecipeFormulaExpectationBlueprintV1,
    SemanticParameterProjectionKind,
    SemanticParameterProjectionV1,
    WindowExpectationV1,
    validate_blueprint,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES, FormulaAuthoringClass


def _count_distinct_blueprint(
    *,
    recipe_id: str,
    entity: str,
    grain_role: str,
    operand_role: str,
) -> RecipeFormulaExpectationBlueprintV1:
    return RecipeFormulaExpectationBlueprintV1(
        recipe_id=recipe_id,
        final_operation=FinalOperation.IDENTITY,
        expressions=(
            ExpressionRoleExpectationV1(
                expression_path="body.expr",
                aggregation=AggregateFunction.COUNT_DISTINCT,
                operand_role=operand_role,
                source_relation_role=operand_role,
                window=WindowExpectationV1(
                    event_time_role="event_ts",
                    basis=WindowBasis.TRAILING,
                    length_parameter="window",
                    unit=WindowUnit.DAY,
                    start_inclusive=Inclusivity.EXCLUSIVE,
                    end_inclusive=Inclusivity.INCLUSIVE,
                    timezone="UTC",
                    empty_window=EmptyWindowResult.ZERO,
                    null_input=NullInput.IGNORE,
                ),
            ),
        ),
        grain=GrainExpectationV1(entity=entity, key_roles=(grain_role,)),
        semantic_parameter_projections=(
            SemanticParameterProjectionV1(
                recipe_parameter="window",
                projection_kind=SemanticParameterProjectionKind.AST_PATH,
                canonical_formula_paths=("body.expr.window.length",),
            ),
        ),
        decimal=DecimalPolicyExpectationV1(
            precision=38,
            scale=0,
            rounding=RoundingMode.HALF_EVEN,
            overflow=OverflowBehavior.ERROR,
        ),
    )


RECIPE_FORMULA_EXPECTATIONS = {
    "merchant_mcc_diversity": _count_distinct_blueprint(
        recipe_id="merchant_mcc_diversity",
        entity="merchant",
        grain_role="merchant",
        operand_role="mcc",
    ),
    "obligor_facility_count": _count_distinct_blueprint(
        recipe_id="obligor_facility_count",
        entity="obligor",
        grain_role="obligor",
        operand_role="facility",
    ),
}


def validate_expectation_registry() -> None:
    templates = {template.id: template for template in ALL_TEMPLATES}
    authorable = {
        template.id
        for template in ALL_TEMPLATES
        if template.formula_authoring_class is FormulaAuthoringClass.FORMULA_V1_AUTHORABLE
    }
    if set(RECIPE_FORMULA_EXPECTATIONS) != authorable:
        raise ValueError(
            "formula expectation registry must exactly cover Formula-v1-authorable recipes")
    for recipe_id, blueprint in RECIPE_FORMULA_EXPECTATIONS.items():
        if blueprint.recipe_id != recipe_id:
            raise ValueError("formula expectation key and recipe id differ")
        validate_blueprint(blueprint)
        template = templates[recipe_id]
        role_names = {need.role for need in template.needs}
        referenced = {
            role
            for expression in blueprint.expressions
            for role in (
                expression.operand_role,
                expression.source_relation_role,
                expression.window.event_time_role,
            )
            if role is not None
        } | set(blueprint.grain.key_roles)
        if not referenced <= role_names:
            raise ValueError(
                f"formula expectation for {recipe_id!r} references unknown roles "
                f"{sorted(referenced - role_names)}")
        projection_names = {
            projection.recipe_parameter
            for projection in blueprint.semantic_parameter_projections
        }
        if projection_names != set(template.params):
            raise ValueError(
                f"formula expectation for {recipe_id!r} does not totally project recipe parameters")


validate_expectation_registry()
