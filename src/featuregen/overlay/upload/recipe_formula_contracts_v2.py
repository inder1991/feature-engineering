"""Task A1 — the Formula-v2 expectation contract, BESIDE the frozen v1 one.

The same law ``schema_v2`` states for the grammar holds here for the expectation: **v1 stays
frozen**. ``RecipeFormulaExpectationBlueprintV1`` and its binder are untouched — two reviewed
anchors and a large corpus depend on their exact bytes — so the v2 blueprint is its OWN type
even where it mirrors v1 shape-for-shape. The version-neutral LEAVES (the preflight error, the
semantic-parameter projections, the decimal policy) are imported from v1 verbatim: they carry no
grammar vocabulary, and forking them would fork validation the two generations must share.

What v2 adds over v1's unary-identity-only blueprint: every ``AggregateFunctionV2`` (not just
COUNT_DISTINCT), the four body shapes (unary / ratio / diff / composite) with their canonical
expression paths, ``offset_periods`` and ``FUTURE_HORIZON`` on the window, the aggregate's own
argument, the second operand of a row-level binary operation, and the governed
``AuthorityRefsV2`` block the expression computes under.

What it deliberately does NOT change: the closed preflight vocabulary. The binder raises exactly
the codes ``bind_formula_expectation`` raises, so the formula shadow's ``technical_axis`` needs
no new words for a v2 expectation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from featuregen.formula.schema import (
    EmptyWindowResult,
    Inclusivity,
    NullInput,
    WindowUnit,
)
from featuregen.formula.schema_v2 import (
    MAX_WINDOW_OFFSET_PERIODS,
    AggregateFunctionV2,
    AuthorityRefsV2,
    FinalOperationV2,
    WindowBasisV2,
)
from featuregen.formula.schema_v3 import SemanticRowSelectionV1
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.recipe_formula_contracts import (
    DecimalPolicyExpectationV1,
    RecipeFormulaPreflightError,
    SemanticParameterProjectionKind,
    SemanticParameterProjectionV1,
    _plain,
)
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

RECIPE_FORMULA_V2_EXPECTATION_POLICY_VERSION = 1

#: The body shape each final operation IS — one vocabulary, so a blueprint can never declare a
#: shape that disagrees with its combiner.
BODY_SHAPE_BY_FINAL_OPERATION: dict[FinalOperationV2, str] = {
    FinalOperationV2.IDENTITY: "unary",
    FinalOperationV2.RATIO: "ratio",
    FinalOperationV2.DIFFERENCE: "diff",
    FinalOperationV2.SIGNED_SUM: "composite",
}

#: The canonical expression paths per shape — the same paths ``canonical_v2`` writes, so a
#: blueprint's projection targets (``body.expr.window.length``) address the real AST.
EXPRESSION_PATHS_BY_FINAL_OPERATION: dict[FinalOperationV2, tuple[str, ...]] = {
    FinalOperationV2.IDENTITY: ("body.expr",),
    FinalOperationV2.RATIO: ("body.numerator", "body.denominator"),
    FinalOperationV2.DIFFERENCE: ("body.minuend", "body.subtrahend"),
}


def composite_expression_path(index: int) -> str:
    return f"body.terms[{index}].expr"


@dataclass(frozen=True, slots=True)
class WindowPolicyExpectationV2:
    """v1's window expectation plus the two v2 forks: ``offset_periods`` (lag/delta as a
    composition rather than a new aggregate) and ``FUTURE_HORIZON`` as a third basis."""

    event_time_role: str
    basis: WindowBasisV2
    length_parameter: str
    unit: WindowUnit
    start_inclusive: Inclusivity
    end_inclusive: Inclusivity
    timezone: str
    empty_window: EmptyWindowResult
    null_input: NullInput
    offset_periods: int = 0


@dataclass(frozen=True, slots=True)
class ExpressionRoleExpectationV2:
    """One authored expression, by ROLE. No physical column, no authority claim — the binder
    resolves roles to refs and the output-authority layer answers authority separately."""

    expression_path: str
    aggregation: AggregateFunctionV2
    operand_role: str | None
    source_relation_role: str
    window: WindowPolicyExpectationV2
    second_operand_role: str | None = None
    aggregation_argument: float | None = None
    #: The governed policy REFS this expression computes under (increment 8). These are refs,
    #: not roles: ``AuthorityRefsV2`` carries policy identifiers, they bind to nothing physical,
    #: and they are identity-bearing exactly as authored.
    authority_refs: AuthorityRefsV2 | None = None
    #: C-A3b — the STRUCTURAL row selections carried through from the recipe. Not inferred: the
    #: recipe declares them, this expectation transports them, and the binder never invents one.
    row_selections: tuple[SemanticRowSelectionV1, ...] = ()
    #: Composite bodies only — the term's authored name and its ±1 sign.
    term_name: str = ""
    term_sign: int = 0


@dataclass(frozen=True, slots=True)
class GrainExpectationV2:
    entity: str
    key_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecipeFormulaExpectationBlueprintV2:
    """The reviewed v2 expectation for one recipe, keyed by its EXPECTATION REF (which is not
    the recipe id for 295 of the 317 registry recipes — see task A0)."""

    recipe_id: str
    expectation_ref: str
    final_operation: FinalOperationV2
    expressions: tuple[ExpressionRoleExpectationV2, ...]
    grain: GrainExpectationV2
    semantic_parameter_projections: tuple[SemanticParameterProjectionV1, ...]
    decimal: DecimalPolicyExpectationV1
    allocation_policy_ref: str = ""
    policy_version: int = RECIPE_FORMULA_V2_EXPECTATION_POLICY_VERSION

    @property
    def body_shape(self) -> str:
        """Derived, never declared — a second field could disagree with the combiner."""
        return BODY_SHAPE_BY_FINAL_OPERATION[self.final_operation]


@dataclass(frozen=True, slots=True)
class BoundExpressionExpectationV2:
    expression_path: str
    aggregation: AggregateFunctionV2
    operand_ref: str | None
    second_operand_ref: str | None
    source_relation_ref: str
    event_time_ref: str
    window_length: int
    window: WindowPolicyExpectationV2
    aggregation_argument: float | None = None
    authority_refs: AuthorityRefsV2 | None = None
    #: C-A3b — carried through binding unchanged; a bound expectation selects what the recipe said.
    row_selections: tuple[SemanticRowSelectionV1, ...] = ()
    term_name: str = ""
    term_sign: int = 0


@dataclass(frozen=True, slots=True)
class BoundRecipeFormulaExpectationV2:
    recipe_candidate_key: str
    recipe_id: str
    expectation_ref: str
    semantic_parameter_binding_hash: str
    final_operation: FinalOperationV2
    expressions: tuple[BoundExpressionExpectationV2, ...]
    grain_entity: str
    grain_key_refs: tuple[str, ...]
    decimal: DecimalPolicyExpectationV1
    blueprint_content_hash: str
    policy_version: int
    allocation_policy_ref: str = ""


def expectation_content_hash_v2(blueprint: RecipeFormulaExpectationBlueprintV2) -> str:
    material = _plain(asdict(blueprint))
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _expected_paths(blueprint: RecipeFormulaExpectationBlueprintV2) -> tuple[str, ...]:
    if blueprint.final_operation is FinalOperationV2.SIGNED_SUM:
        return tuple(composite_expression_path(i) for i in range(len(blueprint.expressions)))
    return EXPRESSION_PATHS_BY_FINAL_OPERATION[blueprint.final_operation]


def validate_blueprint_v2(blueprint: RecipeFormulaExpectationBlueprintV2) -> None:
    """Construction-time law, mirroring ``validate_blueprint`` over the v2 vocabulary.

    Everything checkable WITHOUT a catalog is checked here, so a malformed reviewed expectation
    fails at import rather than at capture time on a customer's request.
    """
    if not blueprint.recipe_id.strip() or not blueprint.expectation_ref.strip():
        raise ValueError("a v2 blueprint needs both its recipe id and its expectation ref")
    if not isinstance(blueprint.final_operation, FinalOperationV2):
        raise ValueError(f"not a v2 final operation: {blueprint.final_operation!r}")
    if blueprint.final_operation is FinalOperationV2.SIGNED_SUM:
        if len(blueprint.expressions) < 2:
            raise ValueError("a signed sum needs at least two terms — one term is identity")
        names = [expression.term_name for expression in blueprint.expressions]
        if len(names) != len(set(names)) or not all(name.strip() for name in names):
            raise ValueError("signed-sum terms need unique, non-blank names")
        if not all(expression.term_sign in (1, -1) for expression in blueprint.expressions):
            raise ValueError("a signed-sum term's sign is +1 or -1 — weights are not a thing "
                             "this combiner does")
    elif any(expression.term_name or expression.term_sign
             for expression in blueprint.expressions):
        raise ValueError("only a signed sum names and signs its terms")
    expected_paths = _expected_paths(blueprint)
    if tuple(e.expression_path for e in blueprint.expressions) != expected_paths:
        raise ValueError(
            f"{blueprint.body_shape} expressions must be exactly {list(expected_paths)}")

    for expression in blueprint.expressions:
        _validate_expression_v2(expression)

    roles = {
        role
        for expression in blueprint.expressions
        for role in (expression.operand_role, expression.second_operand_role,
                     expression.source_relation_role, expression.window.event_time_role)
        if role is not None
    } | set(blueprint.grain.key_roles)
    if not roles or any(not role for role in roles):
        raise ValueError("formula role names must be non-empty")
    if not blueprint.grain.entity.strip() or not blueprint.grain.key_roles:
        raise ValueError("a v2 blueprint declares its grain entity and at least one key role")
    if len(set(blueprint.grain.key_roles)) != len(blueprint.grain.key_roles):
        raise ValueError("grain key roles must be distinct")

    projections = blueprint.semantic_parameter_projections
    names = [projection.recipe_parameter for projection in projections]
    if len(names) != len(set(names)):
        raise ValueError("each recipe parameter must have exactly one projection")
    for projection in projections:
        _validate_projection(projection)


def _validate_expression_v2(expression: ExpressionRoleExpectationV2) -> None:
    from featuregen.formula.operations_v2 import operation_rule

    if not isinstance(expression.aggregation, AggregateFunctionV2):
        raise ValueError(f"not a v2 aggregate: {expression.aggregation!r}")
    rule = operation_rule(expression.aggregation)
    if rule.operand_required and expression.operand_role is None:
        raise ValueError(f"{expression.aggregation.value} requires an operand role")
    if not rule.operand_required and expression.operand_role is not None:
        raise ValueError(f"{expression.aggregation.value} carries no operand")
    if rule.second_operand == "required" and expression.second_operand_role is None:
        raise ValueError(f"{expression.aggregation.value} requires its second operand role")
    if rule.second_operand == "forbidden" and expression.second_operand_role is not None:
        raise ValueError(f"{expression.aggregation.value} takes no second operand")
    if rule.argument == "percentile":
        argument = expression.aggregation_argument
        if not isinstance(argument, (int, float)) or isinstance(argument, bool) \
                or not 0 < argument < 100:
            raise ValueError("percentile requires p strictly between 0 and 100")
    elif expression.aggregation_argument is not None:
        raise ValueError(f"{expression.aggregation.value} takes no argument")
    window = expression.window
    if window.basis is WindowBasisV2.FUTURE_HORIZON and rule.order_sensitive:
        raise ValueError(
            f"{expression.aggregation.value} is order-sensitive — it reads OBSERVED history, "
            "and a future horizon has none")
    if not isinstance(window.offset_periods, int) or isinstance(window.offset_periods, bool) \
            or not 0 <= window.offset_periods <= MAX_WINDOW_OFFSET_PERIODS:
        raise ValueError(
            f"window.offset_periods must be an int in [0, {MAX_WINDOW_OFFSET_PERIODS}]")
    if not window.length_parameter.strip():
        raise ValueError("a window expectation names the recipe parameter carrying its length")


def _validate_projection(projection: SemanticParameterProjectionV1) -> None:
    kind = projection.projection_kind
    if kind is SemanticParameterProjectionKind.AST_PATH:
        if not projection.canonical_formula_paths:
            raise ValueError("AST_PATH projections require canonical formula paths")
        if projection.formula_parameter_name or projection.operational_contract_id:
            raise ValueError("AST_PATH projections cannot carry another destination")
    elif kind is SemanticParameterProjectionKind.FORMULA_PARAMETER:
        if not projection.formula_parameter_name or projection.canonical_formula_paths:
            raise ValueError("FORMULA_PARAMETER requires only a parameter name")
    elif kind is SemanticParameterProjectionKind.OPERATIONAL_ONLY:
        if not projection.operational_contract_id or projection.canonical_formula_paths:
            raise ValueError("OPERATIONAL_ONLY requires only a reviewed contract id")
    elif (projection.canonical_formula_paths or projection.formula_parameter_name
          or projection.operational_contract_id):
        raise ValueError("UNSUPPORTED projections cannot carry a destination")


def bind_formula_expectation_v2(
    context: RecipeGroundingContextV1,
    blueprint: RecipeFormulaExpectationBlueprintV2,
) -> BoundRecipeFormulaExpectationV2:
    """Bind authored v2 roles to exact refs, without resolving or claiming field authority.

    Every refusal is a :class:`RecipeFormulaPreflightError` carrying a code from the SAME closed
    set the v1 binder raises — the shadow classifies both generations with one vocabulary.
    """
    if context.recipe_id != blueprint.recipe_id:
        raise RecipeFormulaPreflightError("RECIPE_EXPECTATION_MISMATCH")
    if grounding_content_hash(context.template_definition) != context.template_content_hash:
        raise RecipeFormulaPreflightError("RECIPE_DEFINITION_HASH_MISMATCH")
    if (semantic_parameter_hash(context.recipe_id, context.semantic_parameters)
            != context.semantic_parameter_binding_hash):
        raise RecipeFormulaPreflightError("SEMANTIC_PARAMETER_HASH_MISMATCH")
    if (context.source_entity_role_resolution not in {
            SourceEntityRoleResolution.EXPLICIT,
            SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS}
            or context.source_entity_need_role is None
            or context.source_entity_need_role not in blueprint.grain.key_roles):
        raise RecipeFormulaPreflightError("FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED")

    bindings = {binding.role: binding for binding in context.need_bindings}
    if len(bindings) != len(context.need_bindings):
        raise RecipeFormulaPreflightError("FORMULA_BINDING_AMBIGUOUS")
    required_roles = {
        role
        for expression in blueprint.expressions
        for role in (expression.operand_role, expression.second_operand_role,
                     expression.source_relation_role, expression.window.event_time_role)
        if role is not None
    } | set(blueprint.grain.key_roles)
    if not required_roles <= set(bindings):
        raise RecipeFormulaPreflightError("FORMULA_BINDING_MISSING")
    for role in required_roles:
        if bindings[role].binding_resolution is not BindingResolution.UNIQUE:
            raise RecipeFormulaPreflightError("FORMULA_BINDING_AMBIGUOUS")

    selected_parameters = dict(context.semantic_parameters)
    projection_names = {projection.recipe_parameter
                        for projection in blueprint.semantic_parameter_projections}
    if projection_names != set(selected_parameters):
        raise RecipeFormulaPreflightError("SEMANTIC_PARAMETER_PROJECTION_INCOMPLETE")

    bound_expressions: list[BoundExpressionExpectationV2] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for expression in blueprint.expressions:
        involved_roles = {
            expression.source_relation_role,
            expression.window.event_time_role,
            *(() if expression.operand_role is None else (expression.operand_role,)),
            *(() if expression.second_operand_role is None
              else (expression.second_operand_role,)),
            *blueprint.grain.key_roles,
        }
        parsed = {role: parse_ref(bindings[role].logical_ref) for role in involved_roles}
        if any(source != bindings[role].catalog_source
               for role, (source, _schema, _table, _column) in parsed.items()):
            raise RecipeFormulaPreflightError("FORMULA_BINDING_SOURCE_MISMATCH")
        if any(column is None for _source, _schema, _table, column in parsed.values()):
            raise RecipeFormulaPreflightError("FORMULA_BINDING_SHAPE_INVALID")
        expression_relations = {(source, schema, table)
                                for source, schema, table, _column in parsed.values()}
        if len(expression_relations) != 1:
            raise RecipeFormulaPreflightError("FORMULA_AUTHORING_UNSUPPORTED")
        source, schema, table = next(iter(expression_relations))
        relation_keys.add((source, schema, table))
        length = selected_parameters.get(expression.window.length_parameter)
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise RecipeFormulaPreflightError("SEMANTIC_WINDOW_INVALID")
        bound_expressions.append(BoundExpressionExpectationV2(
            expression_path=expression.expression_path,
            aggregation=expression.aggregation,
            operand_ref=(None if expression.operand_role is None
                         else bindings[expression.operand_role].logical_ref),
            second_operand_ref=(None if expression.second_operand_role is None
                                else bindings[expression.second_operand_role].logical_ref),
            source_relation_ref=normalize_ref(source, schema, table),
            event_time_ref=bindings[expression.window.event_time_role].logical_ref,
            window_length=length,
            window=expression.window,
            aggregation_argument=expression.aggregation_argument,
            authority_refs=expression.authority_refs,
            term_name=expression.term_name,
            term_sign=expression.term_sign))
    if len(relation_keys) != 1:
        raise RecipeFormulaPreflightError("FORMULA_AUTHORING_UNSUPPORTED")
    return BoundRecipeFormulaExpectationV2(
        recipe_candidate_key=context.recipe_candidate_key,
        recipe_id=context.recipe_id,
        expectation_ref=blueprint.expectation_ref,
        semantic_parameter_binding_hash=context.semantic_parameter_binding_hash,
        final_operation=blueprint.final_operation,
        expressions=tuple(bound_expressions),
        grain_entity=blueprint.grain.entity,
        grain_key_refs=tuple(bindings[role].logical_ref
                             for role in blueprint.grain.key_roles),
        decimal=blueprint.decimal,
        allocation_policy_ref=blueprint.allocation_policy_ref,
        blueprint_content_hash=expectation_content_hash_v2(blueprint),
        policy_version=blueprint.policy_version)


__all__ = [
    "BODY_SHAPE_BY_FINAL_OPERATION",
    "EXPRESSION_PATHS_BY_FINAL_OPERATION",
    "RECIPE_FORMULA_V2_EXPECTATION_POLICY_VERSION",
    "BoundExpressionExpectationV2",
    "BoundRecipeFormulaExpectationV2",
    "ExpressionRoleExpectationV2",
    "GrainExpectationV2",
    "RecipeFormulaExpectationBlueprintV2",
    "WindowPolicyExpectationV2",
    "bind_formula_expectation_v2",
    "composite_expression_path",
    "expectation_content_hash_v2",
    "validate_blueprint_v2",
]
