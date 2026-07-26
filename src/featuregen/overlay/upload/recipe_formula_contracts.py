"""Closed recipe-to-TypedFormula expectation contracts.

These contracts describe authored semantics by recipe role. They contain no physical column,
provider output, or authority claim. A later capture step binds the roles to an immutable
``RecipeGroundingContextV1`` and verifies operational authority independently.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum

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
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.templates import (
    BindingResolution,
    SourceEntityRoleResolution,
)

RECIPE_FORMULA_EXPECTATION_POLICY_VERSION = 1


class SemanticParameterProjectionKind(StrEnum):
    AST_PATH = "ast_path"
    FORMULA_PARAMETER = "formula_parameter"
    OPERATIONAL_ONLY = "operational_only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class WindowExpectationV1:
    event_time_role: str
    basis: WindowBasis
    length_parameter: str
    unit: WindowUnit
    start_inclusive: Inclusivity
    end_inclusive: Inclusivity
    timezone: str
    empty_window: EmptyWindowResult
    null_input: NullInput


@dataclass(frozen=True, slots=True)
class ExpressionRoleExpectationV1:
    expression_path: str
    aggregation: AggregateFunction
    operand_role: str | None
    source_relation_role: str
    window: WindowExpectationV1


@dataclass(frozen=True, slots=True)
class GrainExpectationV1:
    entity: str
    key_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticParameterProjectionV1:
    recipe_parameter: str
    projection_kind: SemanticParameterProjectionKind
    canonical_formula_paths: tuple[str, ...] = ()
    formula_parameter_name: str | None = None
    operational_contract_id: str | None = None


@dataclass(frozen=True, slots=True)
class DecimalPolicyExpectationV1:
    precision: int
    scale: int
    rounding: RoundingMode
    overflow: OverflowBehavior


@dataclass(frozen=True, slots=True)
class RecipeFormulaExpectationBlueprintV1:
    recipe_id: str
    final_operation: FinalOperation
    expressions: tuple[ExpressionRoleExpectationV1, ...]
    grain: GrainExpectationV1
    semantic_parameter_projections: tuple[SemanticParameterProjectionV1, ...]
    decimal: DecimalPolicyExpectationV1
    policy_version: int = RECIPE_FORMULA_EXPECTATION_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class BoundExpressionExpectationV1:
    expression_path: str
    aggregation: AggregateFunction
    operand_ref: str | None
    source_relation_ref: str
    event_time_ref: str
    window_length: int
    window: WindowExpectationV1


@dataclass(frozen=True, slots=True)
class BoundRecipeFormulaExpectationV1:
    recipe_candidate_key: str
    recipe_id: str
    semantic_parameter_binding_hash: str
    final_operation: FinalOperation
    expressions: tuple[BoundExpressionExpectationV1, ...]
    grain_entity: str
    grain_key_refs: tuple[str, ...]
    decimal: DecimalPolicyExpectationV1
    blueprint_content_hash: str
    policy_version: int


class RecipeFormulaPreflightError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _plain(value):
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in sorted(value.items())}
    return value


def expectation_content_hash(expectation: RecipeFormulaExpectationBlueprintV1) -> str:
    material = _plain(asdict(expectation))
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_blueprint(expectation: RecipeFormulaExpectationBlueprintV1) -> None:
    if expectation.final_operation is not FinalOperation.IDENTITY:
        raise ValueError("Delivery B v1 anchor expectations support unary identity formulas only")
    if len(expectation.expressions) != 1:
        raise ValueError("an identity formula must declare exactly one expression")
    expression = expectation.expressions[0]
    if expression.expression_path != "body.expr":
        raise ValueError("the unary expression path must be body.expr")
    if expression.aggregation is AggregateFunction.COUNT_ROWS:
        if expression.operand_role is not None:
            raise ValueError("COUNT_ROWS must not declare an operand role")
    elif expression.operand_role is None:
        raise ValueError("a non-COUNT_ROWS expression requires an operand role")
    roles = {
        role
        for role in (
            expression.operand_role,
            expression.source_relation_role,
            expression.window.event_time_role,
            *expectation.grain.key_roles,
        )
        if role is not None
    }
    if not roles or any(not role for role in roles):
        raise ValueError("formula role names must be non-empty")
    projections = expectation.semantic_parameter_projections
    names = [projection.recipe_parameter for projection in projections]
    if len(names) != len(set(names)):
        raise ValueError("each recipe parameter must have exactly one projection")
    for projection in projections:
        if projection.projection_kind is SemanticParameterProjectionKind.AST_PATH:
            if not projection.canonical_formula_paths:
                raise ValueError("AST_PATH projections require canonical formula paths")
            if projection.formula_parameter_name or projection.operational_contract_id:
                raise ValueError("AST_PATH projections cannot carry another destination")
        elif projection.projection_kind is SemanticParameterProjectionKind.FORMULA_PARAMETER:
            if not projection.formula_parameter_name or projection.canonical_formula_paths:
                raise ValueError("FORMULA_PARAMETER requires only a parameter name")
        elif projection.projection_kind is SemanticParameterProjectionKind.OPERATIONAL_ONLY:
            if not projection.operational_contract_id or projection.canonical_formula_paths:
                raise ValueError("OPERATIONAL_ONLY requires only a reviewed contract id")
        elif (
            projection.canonical_formula_paths
            or projection.formula_parameter_name
            or projection.operational_contract_id
        ):
            raise ValueError("UNSUPPORTED projections cannot carry a destination")


def bind_formula_expectation(
    context: RecipeGroundingContextV1,
    blueprint: RecipeFormulaExpectationBlueprintV1,
) -> BoundRecipeFormulaExpectationV1:
    """Bind authored roles to exact refs, without resolving or claiming field authority."""
    if context.recipe_id != blueprint.recipe_id:
        raise RecipeFormulaPreflightError("RECIPE_EXPECTATION_MISMATCH")
    if grounding_content_hash(context.template_definition) != context.template_content_hash:
        raise RecipeFormulaPreflightError("RECIPE_DEFINITION_HASH_MISMATCH")
    if (
        semantic_parameter_hash(context.recipe_id, context.semantic_parameters)
        != context.semantic_parameter_binding_hash
    ):
        raise RecipeFormulaPreflightError("SEMANTIC_PARAMETER_HASH_MISMATCH")
    if (
        context.source_entity_role_resolution
        not in {
            SourceEntityRoleResolution.EXPLICIT,
            SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        }
        or context.source_entity_need_role is None
        or context.source_entity_need_role not in blueprint.grain.key_roles
    ):
        raise RecipeFormulaPreflightError("FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED")
    bindings = {binding.role: binding for binding in context.need_bindings}
    if len(bindings) != len(context.need_bindings):
        raise RecipeFormulaPreflightError("FORMULA_BINDING_AMBIGUOUS")
    required_roles = {
        role
        for expression in blueprint.expressions
        for role in (
            expression.operand_role,
            expression.source_relation_role,
            expression.window.event_time_role,
        )
        if role is not None
    } | set(blueprint.grain.key_roles)
    if not required_roles <= set(bindings):
        raise RecipeFormulaPreflightError("FORMULA_BINDING_MISSING")
    for role in required_roles:
        if bindings[role].binding_resolution is not BindingResolution.UNIQUE:
            raise RecipeFormulaPreflightError("FORMULA_BINDING_AMBIGUOUS")

    selected_parameters = dict(context.semantic_parameters)
    projection_names = {
        projection.recipe_parameter
        for projection in blueprint.semantic_parameter_projections
    }
    if projection_names != set(selected_parameters):
        raise RecipeFormulaPreflightError("SEMANTIC_PARAMETER_PROJECTION_INCOMPLETE")

    bound_expressions: list[BoundExpressionExpectationV1] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for expression in blueprint.expressions:
        involved_roles = {
            expression.source_relation_role,
            expression.window.event_time_role,
            *(() if expression.operand_role is None else (expression.operand_role,)),
            *blueprint.grain.key_roles,
        }
        parsed = {
            role: parse_ref(bindings[role].logical_ref)
            for role in involved_roles
        }
        if any(
            source != bindings[role].catalog_source
            for role, (source, _schema, _table, _column) in parsed.items()
        ):
            raise RecipeFormulaPreflightError("FORMULA_BINDING_SOURCE_MISMATCH")
        if any(column is None for _source, _schema, _table, column in parsed.values()):
            raise RecipeFormulaPreflightError("FORMULA_BINDING_SHAPE_INVALID")
        expression_relations = {
            (source, schema, table)
            for source, schema, table, _column in parsed.values()
        }
        if len(expression_relations) != 1:
            raise RecipeFormulaPreflightError("FORMULA_AUTHORING_UNSUPPORTED")
        source, schema, table = next(iter(expression_relations))
        relation_keys.add((source, schema, table))
        length = selected_parameters.get(expression.window.length_parameter)
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise RecipeFormulaPreflightError("SEMANTIC_WINDOW_INVALID")
        bound_expressions.append(BoundExpressionExpectationV1(
            expression_path=expression.expression_path,
            aggregation=expression.aggregation,
            operand_ref=(
                None
                if expression.operand_role is None
                else bindings[expression.operand_role].logical_ref
            ),
            source_relation_ref=normalize_ref(source, schema, table),
            event_time_ref=bindings[expression.window.event_time_role].logical_ref,
            window_length=length,
            window=expression.window,
        ))
    if len(relation_keys) != 1:
        raise RecipeFormulaPreflightError("FORMULA_AUTHORING_UNSUPPORTED")
    return BoundRecipeFormulaExpectationV1(
        recipe_candidate_key=context.recipe_candidate_key,
        recipe_id=context.recipe_id,
        semantic_parameter_binding_hash=context.semantic_parameter_binding_hash,
        final_operation=blueprint.final_operation,
        expressions=tuple(bound_expressions),
        grain_entity=blueprint.grain.entity,
        grain_key_refs=tuple(
            bindings[role].logical_ref for role in blueprint.grain.key_roles),
        decimal=blueprint.decimal,
        blueprint_content_hash=expectation_content_hash(blueprint),
        policy_version=blueprint.policy_version,
    )
