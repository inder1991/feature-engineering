"""Task A2 — the Formula-v2 expectation blueprint, DERIVED from the recipe definition.

Design decision D-1, implemented: a ``RecipeDefinitionV2`` already declares operands with roles,
concepts and operand classes; a ``TemporalSpecV2`` with basis/unit/parameter/inclusivity; an
``OutputSpecV2``; ``EligibilitySpecV2.policy_refs``; and ``output_grain`` / ``source_grain``.
Those are precisely the fields ``RecipeFormulaExpectationBlueprintV1`` carries by hand for two
recipes. So the v2 blueprint is DERIVED by a pure function from work already reviewed under BR-2
— and re-keying a grain (the ``merchant_mcc_diversity`` problem, D-7) becomes structurally
impossible, because the grain comes from the definition that also produced the candidate.

**The law of this module: it refuses, it never guesses.** Where the definition does not
STRUCTURALLY determine a field, the answer is a named :class:`BlueprintDerivationRefusal`, which
leaves the recipe ``FORMULA_BLOCKED`` with a blocker a human can act on. Every code below names a
concrete piece of registry work, not a mystery. Three fields have no structural source anywhere
in the registry and are therefore **declared constants of this derivation** rather than reads
(each stated once, below): the window timezone, the decimal policy, and the window's inclusivity
convention, which is taken from ``compile_temporal``'s own compiled PIT text.

Nothing here reads a database, a catalog, or a candidate — the signature takes a definition and
nothing else, and a test asserts that.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.formula.schema import (
    EmptyWindowResult,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowUnit,
)
from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    AuthorityRefsV2,
    FinalOperationV2,
    WindowBasisV2,
)
from featuregen.overlay.upload.recipe_contract_v2 import RecipeDefinitionV2
from featuregen.overlay.upload.recipe_formula_contracts import (
    DecimalPolicyExpectationV1,
    SemanticParameterProjectionKind,
    SemanticParameterProjectionV1,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    ExpressionRoleExpectationV2,
    GrainExpectationV2,
    RecipeFormulaExpectationBlueprintV2,
    WindowPolicyExpectationV2,
    validate_blueprint_v2,
)

# ── the closed refusal vocabulary ──────────────────────────────────────────────────────────
#: The recipe computes nothing deterministic — a conceptual pattern or a governed model output.
NOT_A_DETERMINISTIC_FORMULA = "NOT_A_DETERMINISTIC_FORMULA"
#: ``compile_temporal`` refused the definition's own temporal contract.
TEMPORAL_BLOCKED = "TEMPORAL_BLOCKED"
#: The temporal compiles, but not to a bounded EVENT window — an as-of snapshot, an effective
#: interval or a contractual future has no ``WindowPolicyV2`` shape in this grammar yet.
WINDOW_NOT_EVENT_ANCHORED = "WINDOW_NOT_EVENT_ANCHORED"
#: The declared window unit has no ``WindowUnit`` member (the registry's ``minutes`` recipes).
WINDOW_UNIT_UNSUPPORTED = "WINDOW_UNIT_UNSUPPORTED"
#: The result class does not name exactly one v2 aggregate (``extremum`` is min OR max; ``flag``
#: needs a filter the definition states only in prose).
AGGREGATION_UNDECLARED = "AGGREGATION_UNDECLARED"
#: The body shape is not derivable from a single aggregated operand — a ratio or a share needs
#: two expressions the definition never separates, or the recipe declares several candidates.
MULTIPLE_MEASURE_OPERANDS_UNRESOLVED = "MULTIPLE_MEASURE_OPERANDS_UNRESOLVED"
#: The aggregation consumes an operand and the definition declares none of the required class.
NO_MEASURE_OPERAND = "NO_MEASURE_OPERAND"
#: Not exactly one ``entity_key`` operand — the grain key cannot be chosen without guessing.
GRAIN_KEY_UNRESOLVED = "GRAIN_KEY_UNRESOLVED"
#: ``empty_population_policy`` / ``null_input_policy`` are authored PROSE, and this recipe's
#: sentences do not resolve to exactly one closed enum member.
OUTPUT_POLICY_UNDERIVABLE = "OUTPUT_POLICY_UNDERIVABLE"
#: A declared parameter has no reviewed projection into the formula AST.
PARAMETER_PROJECTION_UNDERIVABLE = "PARAMETER_PROJECTION_UNDERIVABLE"
#: Two governed policy refs of the same kind — which one the expression computes under is a
#: governance answer, not a derivation.
AUTHORITY_REFS_AMBIGUOUS = "AUTHORITY_REFS_AMBIGUOUS"

DERIVATION_REFUSAL_CODES: tuple[str, ...] = (
    NOT_A_DETERMINISTIC_FORMULA, TEMPORAL_BLOCKED, WINDOW_NOT_EVENT_ANCHORED,
    WINDOW_UNIT_UNSUPPORTED, AGGREGATION_UNDECLARED, MULTIPLE_MEASURE_OPERANDS_UNRESOLVED,
    NO_MEASURE_OPERAND, GRAIN_KEY_UNRESOLVED, OUTPUT_POLICY_UNDERIVABLE,
    PARAMETER_PROJECTION_UNDERIVABLE, AUTHORITY_REFS_AMBIGUOUS)

# ── the three declared constants (no structural source exists in the registry) ─────────────
#: `TemporalSpecV2.timezone_policy` is EMPTY for all 317 recipes, so there is nothing to read.
#: UTC is what the reviewed v1 blueprints declare, and it is the reviewer's to change.
DERIVED_TIMEZONE = "UTC"
#: `OutputSpecV2.scale_policy` is EMPTY for all 317 recipes. Counts are exact; everything else
#: carries the exemplar's monetary scale. Precision/rounding/overflow follow the v1 blueprints.
DERIVED_PRECISION = 38
DERIVED_COUNT_SCALE = 0
DERIVED_VALUE_SCALE = 6

#: result class -> (aggregate, the operand class the aggregate consumes | None for count_rows).
#: Only the UNAMBIGUOUS classes appear; a class absent from this table is AGGREGATION_UNDECLARED.
_AGGREGATION_BY_RESULT_CLASS: dict[str, tuple[AggregateFunctionV2, str | None]] = {
    "sum": (AggregateFunctionV2.SUM, "measure"),
    "count": (AggregateFunctionV2.COUNT_ROWS, None),
    "distinct_count": (AggregateFunctionV2.COUNT_DISTINCT, "dimension"),
    "dispersion": (AggregateFunctionV2.STDDEV, "measure"),
    "slope": (AggregateFunctionV2.SLOPE, "measure"),
    "recency": (AggregateFunctionV2.RECENCY, "event_timestamp"),
}
#: Result classes whose body needs TWO expressions the definition never separates.
_MULTI_EXPRESSION_RESULT_CLASSES = frozenset({"ratio", "share"})

#: eligibility policy-ref prefix -> the ``AuthorityRefsV2`` field it declares.
_AUTHORITY_FIELD_BY_PREFIX = {
    "eligible_status": "status_policy_ref",
    "direction_sign": "direction_policy_ref",
    "reversal_correction": "reversal_policy_ref",
    "currency_conversion": "currency_conversion_ref",
}
_ALLOCATION_PREFIX = "allocation"

#: The prose readings. Each maps a sentence to at most one enum member; a sentence matching two
#: readings, or none, is OUTPUT_POLICY_UNDERIVABLE.
_EMPTY_WINDOW_READINGS: tuple[tuple[str, EmptyWindowResult], ...] = (
    ("zero", EmptyWindowResult.ZERO),
    ("null", EmptyWindowResult.NULL),
)
_NULL_INPUT_READINGS: tuple[tuple[str, NullInput], ...] = (
    ("exclud", NullInput.IGNORE),
    ("ignor", NullInput.IGNORE),
    ("treated as zero", NullInput.ZERO),
    ("propagat", NullInput.PROPAGATE),
)

_WINDOW_UNIT_BY_DECLARATION = {
    "days": WindowUnit.DAY,
    "weeks": WindowUnit.WEEK,
    "months": WindowUnit.MONTH,
    "quarters": WindowUnit.QUARTER,
    "years": WindowUnit.YEAR,
}


@dataclass(frozen=True, slots=True)
class BlueprintDerivationRefusal:
    """A named reason this definition does not structurally determine a v2 blueprint."""

    recipe_id: str
    code: str
    detail: str


def derive_blueprint_v2(
    definition: RecipeDefinitionV2,
) -> RecipeFormulaExpectationBlueprintV2 | BlueprintDerivationRefusal:
    """Derive one recipe's v2 expectation blueprint, or refuse by name. Pure: definition only."""
    from featuregen.overlay.upload.recipe_temporal_v2 import compile_temporal

    def refuse(code: str, detail: str) -> BlueprintDerivationRefusal:
        return BlueprintDerivationRefusal(definition.recipe_id, code, detail)

    if definition.computation_kind != "deterministic_formula" or definition.formula is None:
        return refuse(NOT_A_DETERMINISTIC_FORMULA,
                      f"computation kind {definition.computation_kind!r} declares no formula")

    compiled = compile_temporal(definition)
    if compiled.status != "compiled":
        return refuse(TEMPORAL_BLOCKED, ", ".join(compiled.blockers) or "temporal did not compile")

    temporal = definition.temporal
    if (compiled.anchor_kind != "event" or not temporal.event_time_role
            or not temporal.window_parameter or temporal.window_basis != "trailing"):
        return refuse(
            WINDOW_NOT_EVENT_ANCHORED,
            f"anchor {compiled.anchor_kind!r} / basis {temporal.window_basis!r} carries no "
            "bounded event window")
    unit = _WINDOW_UNIT_BY_DECLARATION.get(temporal.window_unit)
    if unit is None:
        return refuse(WINDOW_UNIT_UNSUPPORTED, f"window unit {temporal.window_unit!r}")

    result_class = definition.formula.result_class
    if result_class in _MULTI_EXPRESSION_RESULT_CLASSES:
        return refuse(
            MULTIPLE_MEASURE_OPERANDS_UNRESOLVED,
            f"a {result_class} body needs two expressions the definition does not separate")
    declared = _AGGREGATION_BY_RESULT_CLASS.get(result_class)
    if declared is None:
        return refuse(AGGREGATION_UNDECLARED,
                      f"result class {result_class!r} names no single v2 aggregate")
    aggregation, operand_class = declared

    operand_role: str | None = None
    if operand_class is not None:
        candidates = [operand.role for operand in definition.operands
                      if operand.operand_class == operand_class and operand.required]
        if not candidates:
            return refuse(NO_MEASURE_OPERAND,
                          f"{aggregation.value} consumes a {operand_class} operand and none is "
                          "declared")
        if len(candidates) > 1:
            return refuse(MULTIPLE_MEASURE_OPERANDS_UNRESOLVED,
                          f"{len(candidates)} required {operand_class} operands: {candidates}")
        operand_role = candidates[0]

    entity_keys = [operand.role for operand in definition.operands
                   if operand.operand_class == "entity_key" and operand.required]
    if len(entity_keys) != 1:
        return refuse(GRAIN_KEY_UNRESOLVED,
                      f"{len(entity_keys)} required entity_key operands: {entity_keys}")

    empty_window = _read_policy(definition.output.empty_population_policy,
                                _EMPTY_WINDOW_READINGS)
    null_input = _read_policy(definition.output.null_input_policy, _NULL_INPUT_READINGS)
    if empty_window is None or null_input is None:
        return refuse(
            OUTPUT_POLICY_UNDERIVABLE,
            f"empty={definition.output.empty_population_policy!r} "
            f"null={definition.output.null_input_policy!r}")

    authority = _derive_authority_refs(definition)
    if authority is _AMBIGUOUS:
        return refuse(AUTHORITY_REFS_AMBIGUOUS,
                      f"policy refs {list(definition.eligibility.policy_refs)}")

    projections = _derive_projections(definition)
    if projections is None:
        return refuse(
            PARAMETER_PROJECTION_UNDERIVABLE,
            "parameters without a reviewed projection: "
            f"{[p.name for p in definition.parameters if p.name != temporal.window_parameter]}")

    # The compiled PIT text is the convention: `(cutoff − Ld, cutoff]` for an inclusive cutoff.
    end_inclusive = (Inclusivity.INCLUSIVE if temporal.cutoff_inclusivity == "inclusive"
                     else Inclusivity.EXCLUSIVE)
    window = WindowPolicyExpectationV2(
        event_time_role=temporal.event_time_role, basis=WindowBasisV2.TRAILING,
        length_parameter=temporal.window_parameter, unit=unit,
        start_inclusive=Inclusivity.EXCLUSIVE, end_inclusive=end_inclusive,
        timezone=DERIVED_TIMEZONE, empty_window=empty_window, null_input=null_input)
    scale = (DERIVED_COUNT_SCALE if result_class in ("count", "distinct_count")
             else DERIVED_VALUE_SCALE)
    blueprint = RecipeFormulaExpectationBlueprintV2(
        recipe_id=definition.recipe_id,
        expectation_ref=definition.formula.expectation_ref,
        final_operation=FinalOperationV2.IDENTITY,
        expressions=(ExpressionRoleExpectationV2(
            expression_path="body.expr", aggregation=aggregation, operand_role=operand_role,
            # The relation an expression reads is the relation its own roles resolve to; the
            # binder proves they agree. The operand names it when there is one, the event clock
            # otherwise (COUNT_ROWS binds no operand but always binds a time).
            source_relation_role=operand_role or temporal.event_time_role,
            window=window, authority_refs=authority,
            # C-A3b: carried from the recipe's DECLARATION. Nothing here reads the recipe id or
            # its prose — which is the whole point: `posted_debit_amount` and
            # `posted_credit_amount` are otherwise identical, so a derivation that inferred
            # direction would be inferring it from the name.
            row_selections=definition.row_selections),),
        grain=GrainExpectationV2(entity=definition.output_grain,
                                 key_roles=(entity_keys[0],)),
        semantic_parameter_projections=projections,
        decimal=DecimalPolicyExpectationV1(precision=DERIVED_PRECISION, scale=scale,
                                           rounding=RoundingMode.HALF_EVEN,
                                           overflow=OverflowBehavior.ERROR),
        allocation_policy_ref=_single_ref(definition, _ALLOCATION_PREFIX) or "")
    validate_blueprint_v2(blueprint)
    return blueprint


def _read_policy(prose: str, readings):
    """One closed reading of an authored sentence, or ``None`` when it names none or several."""
    lowered = prose.casefold()
    matched = {member for token, member in readings if token in lowered}
    return matched.pop() if len(matched) == 1 else None


class _Ambiguous:
    __slots__ = ()


_AMBIGUOUS = _Ambiguous()


def _refs_by_prefix(definition: RecipeDefinitionV2) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for ref in definition.eligibility.policy_refs:
        grouped.setdefault(ref.split(":", 1)[0], []).append(ref)
    return grouped


def _single_ref(definition: RecipeDefinitionV2, prefix: str) -> str | None:
    refs = _refs_by_prefix(definition).get(prefix, [])
    return refs[0] if len(refs) == 1 else None


def _derive_authority_refs(definition: RecipeDefinitionV2):
    grouped = _refs_by_prefix(definition)
    fields: dict[str, str] = {}
    for prefix, field in _AUTHORITY_FIELD_BY_PREFIX.items():
        refs = grouped.get(prefix, [])
        if len(refs) > 1:
            return _AMBIGUOUS
        if refs:
            fields[field] = refs[0]
    if not fields:
        # No governed row policies is a REAL answer for many features; a vacuous block is a lie
        # the schema refuses to construct.
        return None
    return AuthorityRefsV2(**fields)


def _derive_projections(
    definition: RecipeDefinitionV2,
) -> tuple[SemanticParameterProjectionV1, ...] | None:
    """The window parameter projects onto the window's length; nothing else has a reviewed
    destination, so a recipe carrying another parameter refuses rather than dropping it."""
    projections: list[SemanticParameterProjectionV1] = []
    for parameter in definition.parameters:
        if parameter.name != definition.temporal.window_parameter:
            return None
        projections.append(SemanticParameterProjectionV1(
            recipe_parameter=parameter.name,
            projection_kind=SemanticParameterProjectionKind.AST_PATH,
            canonical_formula_paths=("body.expr.window.length",)))
    return tuple(projections)


def derive_registry_blueprints(recipes=None) -> dict[str, object]:
    """Every registry recipe's derivation outcome, keyed by recipe id — the measurement A2 owes
    (see the acceptance row): how many banking recipes are executable-shaped, and by what name
    each of the rest is not."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    return {definition.recipe_id: derive_blueprint_v2(definition)
            for definition in (V2_RECIPES if recipes is None else recipes)}


__all__ = [
    "AGGREGATION_UNDECLARED",
    "AUTHORITY_REFS_AMBIGUOUS",
    "DERIVATION_REFUSAL_CODES",
    "DERIVED_COUNT_SCALE",
    "DERIVED_PRECISION",
    "DERIVED_TIMEZONE",
    "DERIVED_VALUE_SCALE",
    "GRAIN_KEY_UNRESOLVED",
    "MULTIPLE_MEASURE_OPERANDS_UNRESOLVED",
    "NOT_A_DETERMINISTIC_FORMULA",
    "NO_MEASURE_OPERAND",
    "OUTPUT_POLICY_UNDERIVABLE",
    "PARAMETER_PROJECTION_UNDERIVABLE",
    "TEMPORAL_BLOCKED",
    "WINDOW_NOT_EVENT_ANCHORED",
    "WINDOW_UNIT_UNSUPPORTED",
    "BlueprintDerivationRefusal",
    "derive_blueprint_v2",
    "derive_registry_blueprints",
]
