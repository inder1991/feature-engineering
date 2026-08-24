"""BR-2 — Recipe Contract v2: one recipe, one atomic output, honestly typed (banking-recipe plan).

The schema that makes the audited debt classes UNCONSTRUCTIBLE rather than merely counted:

* ONE OutputSpecV2 per recipe, structurally — and the multi-output ambiguity cannot re-enter
  through the side door: a parameter that swaps the emitted QUANTITY (the legacy ``measure``
  param, 126 recipes) is rejected at construction. A ratio and an amount are separate recipe
  revisions, even when they share operands.
* NO readiness by implication — ``computation_kind`` and ``readiness`` are closed vocabularies,
  cross-validated (a conceptual pattern cannot claim a formula; an executable recipe cannot omit
  one), and ``UNASSESSED`` is FORBIDDEN here entirely: it exists only on the legacy adapter's
  projection type, so the V2 registry cannot hold an unassessed recipe by construction.
* Typed everything the audit found prose-only: temporal contract (BR-4 compiles it), operand
  authority expectations (BR-5 enforces them), currency/unit/null/zero-denominator policies on
  the output, leakage by permitted modelling stage, revision-specific SME review metadata.
* DEEP immutability — tuples only, no mutable dict inside a frozen dataclass; validated at
  construction so an invalid definition cannot exist long enough to serialize.

Compatibility (the plan's non-negotiables): ``Template``/``ALL_TEMPLATES`` are untouched;
canonical-recipe-v1 hashing is untouched; canonical-recipe-v2 lives beside it
(``recipe_grounding_context.canonical_recipe_v2``); a V2 replacement names its legacy ids
EXPLICITLY (``replaces_legacy_ids``) — no heuristic aliasing, ever.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from featuregen.formula.schema_v3 import (
    SELECTION_TOKENS,
    SelectionKind,
    SemanticRowSelectionV1,
)
from featuregen.overlay.upload.concepts import canonical_concept_name, is_classifier_producible

RECIPE_CONTRACT_V2_SCHEMA_VERSION = "recipe-contract-v2"

COMPUTATION_KINDS = ("deterministic_formula", "governed_model_output", "conceptual_pattern")

# BR-7's closed readiness vocabulary, typed here because the definition carries its state.
# UNASSESSED is deliberately ABSENT: it is legal only on the legacy adapter's projection.
RECIPE_READINESS = ("CONCEPTUAL_ONLY", "FORMULA_BLOCKED", "FORMULA_AUTHORABLE",
                    "FORMULA_VALIDATED", "MATERIALIZATION_BLOCKED", "MATERIALIZATION_READY",
                    "RETIRED")

ADDITIVITY = ("additive", "semi_additive", "non_additive")
PARAMETER_CLASSES = ("semantic", "operational", "governed_policy")
OPERAND_CLASSES = ("measure", "entity_key", "event_timestamp", "as_of_timestamp",
                   "dimension", "status", "direction", "policy_input")
UNIT_KINDS = ("monetary", "count", "ratio", "duration_days", "rate", "score")
OUTPUT_TYPES = ("numeric", "integer", "boolean", "date")
AUTHORITY_LEVELS = ("none", "declared", "governed")
TEMPORAL_ANCHOR_KINDS = ("event", "as_of", "effective_interval", "contractual_future",
                         "pre_decision")
# Named because they are a CONTRACT, not a local literal: the LLM intent seam's output schema
# publishes these to the model (enrich_llm's `feature_intents` v2), and a schema that re-spelled
# them could promise a token TemporalSpecV2 refuses — the defect this naming closes.
WINDOW_UNITS = ("days", "minutes", "none")
CUTOFF_INCLUSIVITY = ("inclusive", "exclusive")
LEAKAGE_CLASSES = ("standard", "near_label", "outcome")

# A formula's result class decides which additivity claims are even POSSIBLE — the audit found 72
# recipes whose single authored additivity cannot describe all their measures. Closed and small on
# purpose; BR-6 (Formula-v2) grows it operation by operation.
RESULT_CLASS_ADDITIVITY: dict[str, tuple[str, ...]] = {
    "sum": ("additive",),
    "count": ("additive",),
    "distinct_count": ("additive",),
    "ratio": ("non_additive",),
    "share": ("non_additive",),
    "recency": ("non_additive",),
    "slope": ("non_additive",),
    "snapshot": ("semi_additive",),
    # BR-11: spread statistics (stddev/percentile bands) — a dispersion is never summable.
    "dispersion": ("non_additive",),
    # BR-12: window extrema (max DPD, worst bucket) and event flags — neither is summable.
    "extremum": ("non_additive",),
    "flag": ("non_additive",),
}

# The side door the one-output rule must close: parameter names that historically selected WHICH
# quantity a recipe emits. Under V2 each such value is its own recipe revision.
_OUTPUT_SELECTING_PARAMETER_NAMES = frozenset({"measure"})


class RecipeContractError(ValueError):
    """An invalid V2 definition — raised at CONSTRUCTION, so no invalid definition can exist."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecipeContractError(message)


def _closed(value: str, vocabulary: tuple[str, ...], label: str) -> None:
    _require(value in vocabulary, f"{label} {value!r} not in {vocabulary}")


@dataclass(frozen=True, slots=True)
class OutputSpecV2:
    """The ONE atomic output. Policies are named references or reviewed sentences — never blank
    where the unit kind makes them load-bearing (monetary ⟹ currency policy; ratio ⟹
    zero-denominator behavior)."""

    output_id: str
    display_label: str
    output_type: str                     # OUTPUT_TYPES
    additivity: str                      # ADDITIVITY
    unit_kind: str                       # UNIT_KINDS
    unit_policy: str = ""
    currency_policy: str = ""
    null_input_policy: str = ""
    empty_population_policy: str = ""
    zero_denominator_policy: str = ""
    valid_range: str = ""
    scale_policy: str = ""
    aggregation_over_entity: str = ""
    aggregation_over_time: str = ""

    def __post_init__(self) -> None:
        _require(bool(self.output_id.strip()), "output_id is mandatory")
        _require(bool(self.display_label.strip()), "display_label is mandatory")
        _closed(self.output_type, OUTPUT_TYPES, "output_type")
        _closed(self.additivity, ADDITIVITY, "additivity")
        _closed(self.unit_kind, UNIT_KINDS, "unit_kind")
        _require(bool(self.null_input_policy.strip()), "null_input_policy is mandatory")
        _require(bool(self.empty_population_policy.strip()),
                 "empty_population_policy is mandatory")
        if self.unit_kind == "monetary":
            _require(bool(self.currency_policy.strip()),
                     "a monetary output requires a currency policy")
        if self.unit_kind == "ratio":
            _require(bool(self.zero_denominator_policy.strip()),
                     "a ratio output requires a zero-denominator policy")


@dataclass(frozen=True, slots=True)
class OperandSpecV2:
    """One typed binding slot. ``concept`` obeys the same producibility rule as Need (through the
    alias seam); authority levels say what must be PROVEN before this operand may feed a
    suggestion versus an execution — BR-5 enforces, BR-2 declares."""

    role: str
    concept: str
    operand_class: str                   # OPERAND_CLASSES
    required: bool = True
    allowed_source_grains: tuple[str, ...] = ()
    join_role: str = ""
    temporal_role: str = ""
    distinct_binding_group: str = ""
    unit_expectation: str = ""
    currency_expectation: str = ""
    # BR-5: the economic-role constraint for GENERIC monetary concepts — "drawn_credit_exposure"
    # is not satisfied by any monetary_stock column; a candidate must carry GOVERNED economic-role
    # evidence matching this value, or the binding is BLOCKED (never bound by concept alone).
    # "" = the concept alone is specific enough (account_id, event_timestamp, ...).
    economic_role: str = ""
    sign_direction_expectation: str = ""
    status_policy_ref: str = ""
    relationship_requirement: str = ""
    suggestion_authority: str = "declared"     # AUTHORITY_LEVELS
    execution_authority: str = "governed"      # AUTHORITY_LEVELS

    def __post_init__(self) -> None:
        _require(bool(self.role.strip()), "operand role is mandatory")
        _closed(self.operand_class, OPERAND_CLASSES, "operand_class")
        _closed(self.suggestion_authority, AUTHORITY_LEVELS, "suggestion_authority")
        _closed(self.execution_authority, AUTHORITY_LEVELS, "execution_authority")
        _require(is_classifier_producible(canonical_concept_name(self.concept)),
                 f"operand concept {self.concept!r} is not producible by the classifier")


@dataclass(frozen=True, slots=True)
class ParameterSpecV2:
    """One immutable parameter. Every parameter is CLASSIFIED (semantic changes meaning and
    therefore identity; operational changes execution while preserving the definition;
    governed_policy references a reviewed policy and may not be a free literal) and every
    parameter PROJECTS into identity — the 145-recipe display-collision class cannot re-enter."""

    name: str
    parameter_class: str                 # PARAMETER_CLASSES
    allowed_values: tuple = ()
    identity_projection: str = ""        # e.g. "window={value}d" — must contain {value}
    display_projection: str = ""
    governed_policy_ref: str = ""

    def __post_init__(self) -> None:
        _require(bool(self.name.strip()), "parameter name is mandatory")
        _closed(self.parameter_class, PARAMETER_CLASSES, "parameter_class")
        _require(self.name not in _OUTPUT_SELECTING_PARAMETER_NAMES,
                 f"parameter {self.name!r} selects the emitted quantity — under the one-output "
                 "rule each value is its own recipe revision, never a parameter")
        _require("{value}" in self.identity_projection,
                 f"parameter {self.name!r} needs an identity projection containing {{value}}")
        _require("{value}" in self.display_projection,
                 f"parameter {self.name!r} needs a display projection containing {{value}}")
        if self.parameter_class == "governed_policy":
            _require(bool(self.governed_policy_ref.strip()),
                     f"governed-policy parameter {self.name!r} must reference a reviewed policy")
        else:
            _require(len(self.allowed_values) > 0,
                     f"parameter {self.name!r} needs a bounded allowed-value tuple")


@dataclass(frozen=True, slots=True)
class TemporalSpecV2:
    """The typed temporal contract BR-4 compiles into PIT text. BR-2 carries the structure and the
    one cross-check a schema can make alone: a window parameter it names must exist."""

    anchor_kind: str                     # TEMPORAL_ANCHOR_KINDS
    event_time_role: str = ""
    business_effective_role: str = ""
    knowledge_time_role: str = ""
    window_basis: str = ""
    window_unit: str = "days"            # days | minutes | none
    window_parameter: str = ""           # must name a declared parameter when set
    timezone_policy: str = ""
    calendar_policy: str = ""
    cutoff_inclusivity: str = "inclusive"
    future_horizon_policy: str = ""
    snapshot_policy: str = ""
    late_arrival_policy: str = ""
    temporal_authority_ref: str = ""

    def __post_init__(self) -> None:
        _closed(self.anchor_kind, TEMPORAL_ANCHOR_KINDS, "anchor_kind")
        _closed(self.window_unit, WINDOW_UNITS, "window_unit")
        _closed(self.cutoff_inclusivity, CUTOFF_INCLUSIVITY, "cutoff_inclusivity")
        if self.anchor_kind == "contractual_future":
            _require(bool(self.future_horizon_policy.strip()),
                     "a contractual-future anchor requires a future-horizon policy")


#: C-A3b — which kind-prefixed policy namespace must back a selection of each kind. The recipe's
#: ``policy_refs`` are flat strings; D2's kind prefix is what makes "this ref governs direction"
#: machine-checkable instead of a naming convention nobody enforces.
_SELECTION_POLICY_PREFIX: dict[SelectionKind, str] = {
    SelectionKind.TRANSACTION_DIRECTION: "direction_sign",
    SelectionKind.ELIGIBILITY: "eligible_status",
}


@dataclass(frozen=True, slots=True)
class EligibilitySpecV2:
    included: str = ""
    excluded: str = ""
    policy_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeakageSpecV2:
    classification: str = "standard"     # LEAKAGE_CLASSES
    permitted_stages: tuple[str, ...] = ()
    prohibited_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _closed(self.classification, LEAKAGE_CLASSES, "leakage classification")
        overlap = set(self.permitted_stages) & set(self.prohibited_stages)
        _require(not overlap, f"stages both permitted and prohibited: {sorted(overlap)}")


@dataclass(frozen=True, slots=True)
class FormulaReferenceV2:
    """An EXACT reference — never prose. ``result_class`` is what additivity validates against."""

    formula_schema_version: str          # "formula-v1" | "formula-v2"
    expectation_ref: str                 # reviewed expectation registry key
    result_class: str                    # RESULT_CLASS_ADDITIVITY keys

    def __post_init__(self) -> None:
        _closed(self.formula_schema_version, ("formula-v1", "formula-v2"),
                "formula_schema_version")
        _require(bool(self.expectation_ref.strip()), "expectation_ref is mandatory")
        _closed(self.result_class, tuple(RESULT_CLASS_ADDITIVITY), "result_class")


@dataclass(frozen=True, slots=True)
class RecipeReviewV1:
    """Revision-specific SME review metadata (the BR-23 schema's per-definition half). A fresh
    definition is honestly ``unreviewed`` — approval arrives as an event, never a default."""

    decision: str = "unreviewed"         # unreviewed | approved | changes_required | rejected | retired
    reviewer: str = ""
    reviewer_role: str = ""
    reviewed_on: str = ""
    reference: str = ""

    def __post_init__(self) -> None:
        _closed(self.decision,
                ("unreviewed", "approved", "changes_required", "rejected", "retired"),
                "review decision")
        if self.decision != "unreviewed":
            _require(bool(self.reviewer.strip()), "a recorded decision needs a reviewer")


@dataclass(frozen=True, slots=True)
class RecipeDefinitionV2:
    """One versioned, atomic, honestly-presented recipe. Constructing one runs every cross-field
    rule below — the audit's debt classes are impossible here, not just counted."""

    recipe_id: str
    revision: int
    family: str
    primary_objective: str
    business_definition: str
    decision_context: str
    computation_kind: str                # COMPUTATION_KINDS
    output: OutputSpecV2
    operands: tuple[OperandSpecV2, ...]
    source_grain: str
    output_grain: str
    temporal: TemporalSpecV2
    readiness: str                       # RECIPE_READINESS (UNASSESSED cannot exist here)
    supporting_objectives: tuple[str, ...] = ()
    parameters: tuple[ParameterSpecV2, ...] = ()
    eligibility: EligibilitySpecV2 = field(default_factory=EligibilitySpecV2)
    leakage: LeakageSpecV2 = field(default_factory=LeakageSpecV2)
    formula: FormulaReferenceV2 | None = None
    conceptual_reason: str = ""
    model_feature_ref: str = ""
    review: RecipeReviewV1 = field(default_factory=RecipeReviewV1)
    replaces_legacy_ids: tuple[str, ...] = ()
    legacy_aliases: tuple[str, ...] = ()
    #: C-A3b — the STRUCTURAL row selections this recipe declares (``direction/debit``).
    #:
    #: Before this field, ``posted_debit_amount`` and ``posted_credit_amount`` were structurally
    #: IDENTICAL — same operands (``with_direction=True``), same ``direction_sign:`` policy ref —
    #: and differed only in their recipe NAME and prose. A deterministic author could therefore
    #: only obtain "debit" by inferring it from the name, which is exactly what this field exists
    #: to stop.
    #:
    #: ``()`` means **this recipe declares no structural row selection** — a positive statement,
    #: not "not migrated yet". A recipe whose rows genuinely need selecting must say so; an empty
    #: tuple that actually meant "unknown" would let a recipe read complete while its semantics
    #: were undecided.
    row_selections: tuple[SemanticRowSelectionV1, ...] = ()
    schema_version: str = RECIPE_CONTRACT_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

        _require(bool(self.recipe_id.strip()), "recipe_id is mandatory")
        _require(self.revision >= 1, "revision starts at 1")
        _require(self.schema_version == RECIPE_CONTRACT_V2_SCHEMA_VERSION,
                 f"schema_version must be {RECIPE_CONTRACT_V2_SCHEMA_VERSION!r}")
        _closed(self.computation_kind, COMPUTATION_KINDS, "computation_kind")
        _closed(self.readiness, RECIPE_READINESS, "readiness")
        _require(bool(self.business_definition.strip()), "business_definition is mandatory")
        _require(bool(self.source_grain.strip()) and bool(self.output_grain.strip()),
                 "source and output grains are mandatory")

        leaves = set(selectable_leaves())
        _require(self.primary_objective in leaves,
                 f"primary objective {self.primary_objective!r} is not a selectable taxonomy leaf")
        _require(self.primary_objective not in self.supporting_objectives,
                 "the primary objective may not also be supporting")
        off_leaf = [o for o in self.supporting_objectives if o not in leaves]
        _require(not off_leaf, f"supporting objectives off the taxonomy: {off_leaf}")

        seen_selections: set[tuple[str, str]] = set()
        for sel in self.row_selections:
            _require(isinstance(sel, SemanticRowSelectionV1),
                     f"row_selections carries {type(sel).__name__}, not SemanticRowSelectionV1")
            tokens = SELECTION_TOKENS[sel.kind]
            _require(sel.semantic_value in tokens,
                     f"row selection {sel.kind.value}={sel.semantic_value!r} is not one of "
                     f"{sorted(tokens)} — a recipe declares a SEMANTIC token, never a source's "
                     f"physical literal")
            key = (sel.kind.value, sel.role)
            _require(key not in seen_selections,
                     f"duplicate row selection for (kind={key[0]}, role={key[1]!r})")
            seen_selections.add(key)
            # D2's kind-prefixed namespace is what makes this checkable at all: the recipe's
            # policy refs are flat strings, and the prefix is what says which KIND each governs.
            prefix = _SELECTION_POLICY_PREFIX[sel.kind]
            _require(any(ref.startswith(f"{prefix}:") for ref in self.eligibility.policy_refs),
                     f"a {sel.kind.value} selection requires an eligibility policy_ref prefixed "
                     f"{prefix!r} — the selection declares intent and the policy resolves it")

        roles = [op.role for op in self.operands]
        _require(len(roles) == len(set(roles)), "duplicate operand roles")
        _require(len(self.operands) > 0, "at least one operand is mandatory")
        names = [p.name for p in self.parameters]
        _require(len(names) == len(set(names)), "duplicate parameter names")
        groups: dict[str, int] = {}
        for op in self.operands:
            if op.distinct_binding_group:
                groups[op.distinct_binding_group] = groups.get(op.distinct_binding_group, 0) + 1
        lonely = [g for g, n in groups.items() if n < 2]
        _require(not lonely, f"distinct-binding groups with one member: {lonely}")

        if self.temporal.window_parameter:
            _require(self.temporal.window_parameter in set(names),
                     f"temporal window parameter {self.temporal.window_parameter!r} "
                     "is not a declared parameter")

        if self.computation_kind == "deterministic_formula":
            _require(self.formula is not None,
                     "an executable recipe requires an exact formula reference")
            _require(not self.conceptual_reason,
                     "an executable recipe may not carry a conceptual-only reason")
            _require(self.readiness != "CONCEPTUAL_ONLY",
                     "an executable recipe cannot be CONCEPTUAL_ONLY")
            for op in self.operands:
                _require(len(op.allowed_source_grains) > 0,
                         f"executable operand {op.role!r} needs a non-empty "
                         "allowed-source-grain set")
            allowed = RESULT_CLASS_ADDITIVITY[self.formula.result_class]
            _require(self.output.additivity in allowed,
                     f"output additivity {self.output.additivity!r} is incompatible with "
                     f"formula result class {self.formula.result_class!r} (allowed: {allowed})")
        elif self.computation_kind == "conceptual_pattern":
            _require(self.formula is None, "a conceptual pattern may not reference a formula")
            _require(bool(self.conceptual_reason.strip()),
                     "a conceptual pattern must state WHY no exact computation exists")
            _require(self.readiness in ("CONCEPTUAL_ONLY", "RETIRED"),
                     "a conceptual pattern's readiness is CONCEPTUAL_ONLY or RETIRED")
        else:  # governed_model_output
            _require(self.formula is None,
                     "a model output is not a deterministic formula (BR-7A owns its spec)")
            _require(bool(self.model_feature_ref.strip()),
                     "a governed model output must reference its ModelFeatureSpec")


def day_window_parameter(recipe: RecipeDefinitionV2) -> ParameterSpecV2 | None:
    """The recipe's day-scale ``window`` parameter, or None.

    None is a real answer: a minute-scale recipe (``window_minutes``) and a windowless one both
    have no day window — nothing to diverge from, nothing for a chooser to choose. THE shared
    definition: the S1C-1 corpus floors and S1C-3's chooser measurement both read this, so
    "which recipes have a day window" cannot drift between the ground truth and the metric."""
    for parameter in recipe.parameters:
        if parameter.name == "window" and parameter.allowed_values:
            return parameter
    return None


def primary_window_days(recipe: RecipeDefinitionV2) -> int | None:
    """``allowed_values[0]`` of the day-window parameter, or None — "the primary window of a
    recipe". First-in-list IS the authored default (``planning_request_from_recipe`` resolves an
    omitted parameter to exactly this value, pinned by test), so the corpus floors and the
    telemetry worker's request-bound primary provably agree."""
    parameter = day_window_parameter(recipe)
    if parameter is None:
        return None
    value = parameter.allowed_values[0]
    return value if isinstance(value, int) and not isinstance(value, bool) else None
