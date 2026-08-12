"""SE-1 — the origin-neutral planning contracts: one shape for recipes, LLM intents and users.

The semantic-eligibility program's first invariant is that a V2 recipe and an LLM feature
intent normalize to the SAME `FeaturePlanningRequestV1` before any physical binding — no caller
downstream may need to know a candidate's origin to evaluate column eligibility. This module
owns that shape and the three adapters into it.

Deliberate reuse over re-modeling: the atomic output, temporal, eligibility and leakage
declarations are the EXISTING V2 spec dataclasses (`OutputSpecV2`, `TemporalSpecV2`,
`EligibilitySpecV2`, `LeakageSpecV2`) — their construction rules (monetary ⟹ currency policy,
ratio ⟹ zero-denominator policy, permitted∩prohibited = ∅, …) ride along for free, and a second
vocabulary that could drift from the recipe contract structurally cannot exist.
`RequiredOperandV1` is the neutral PROJECTION of `OperandSpecV2` (same closed vocabularies, same
producibility rule), plus two fields the recipe form does not need: `alternative_concepts`
(retrieval-widening for LLM intents; never authority) and `binding_hint_refs` (physical refs a
USER typed — hints for the binder to rank, never pre-cleared bindings; construction refuses them
on any non-user origin, which is how "no physical references in authoritative LLM intent
output" is enforced structurally rather than by review).

Hashing: `planning_request_hash` canonicalizes with the SAME field-exhaustive mechanism as
canonical-recipe-v2 (every dataclass field is hash-bearing by construction, so ADDING a field
moves the hash — the SE-1 requirement) inside a registered `contract_hash_v1` envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.upload.concepts import canonical_concept_name, is_classifier_producible
from featuregen.overlay.upload.recipe_contract_v2 import (
    AUTHORITY_LEVELS,
    COMPUTATION_KINDS,
    OPERAND_CLASSES,
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
# The same private canonicalizer canonical-recipe-v2 uses — mechanism identity is the point:
# two canonicalizers that could disagree about a nested tuple would fork plan identity.
from featuregen.overlay.upload.recipe_grounding_context import _canonical_dataclass

PLANNING_REQUEST_CONTRACT = "feature-planning-request"
PLANNING_REQUEST_VERSION = "1"
_OWNER = "featuregen.overlay.upload.feature_planning_contracts"

register_contract_version(PLANNING_REQUEST_CONTRACT, PLANNING_REQUEST_VERSION, owner=_OWNER)

PLANNING_ORIGINS = ("recipe_v2", "llm_intent", "user_definition")


class PlanningContractError(ValueError):
    """An invalid planning contract — refused at construction, before anything binds."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanningContractError(message)


def _closed(value: str, vocabulary: tuple[str, ...], label: str) -> None:
    _require(value in vocabulary, f"{label} {value!r} not in {vocabulary}")


@dataclass(frozen=True, slots=True)
class RequiredOperandV1:
    """One exact role a feature needs — the neutral projection of `OperandSpecV2`.

    Field-for-field the recipe operand's vocabulary (closed classes, producible concepts,
    authority floors), so `planning_request_from_recipe` is a copy, never a translation.
    Two additions beyond the recipe form, both origin-gated at the request level:
    `alternative_concepts` widen retrieval only; `binding_hint_refs` exist only for the
    user-definition origin."""

    role: str
    concept: str
    operand_class: str                   # OPERAND_CLASSES — the recipe contract's, verbatim
    required: bool = True
    alternative_concepts: tuple[str, ...] = ()
    allowed_source_grains: tuple[str, ...] = ()
    join_role: str = ""
    temporal_role: str = ""
    distinct_binding_group: str = ""
    unit_expectation: str = ""
    currency_expectation: str = ""
    economic_role: str = ""
    sign_direction_expectation: str = ""
    status_policy_ref: str = ""
    relationship_requirement: str = ""
    suggestion_authority: str = "declared"     # AUTHORITY_LEVELS
    execution_authority: str = "governed"      # AUTHORITY_LEVELS
    binding_hint_refs: tuple[str, ...] = ()    # user origin ONLY; hints, never bindings

    def __post_init__(self) -> None:
        _require(bool(self.role.strip()), "operand role is mandatory")
        _closed(self.operand_class, OPERAND_CLASSES, "operand_class")
        _closed(self.suggestion_authority, AUTHORITY_LEVELS, "suggestion_authority")
        _closed(self.execution_authority, AUTHORITY_LEVELS, "execution_authority")
        for name in (self.concept, *self.alternative_concepts):
            _require(is_classifier_producible(canonical_concept_name(name)),
                     f"operand concept {name!r} is not producible by the classifier")


@dataclass(frozen=True, slots=True)
class FeaturePlanningRequestV1:
    """The origin-neutral input to physical planning — what every binder-facing caller holds.

    `parameter_values` are the RESOLVED, identity-bearing (name, value) pairs — a request is one
    variant, never a family; two windows are two requests with two hashes."""

    origin: str                          # PLANNING_ORIGINS
    source_definition_id: str
    source_revision: str
    source_content_hash: str
    primary_objective: str
    output: OutputSpecV2
    operands: tuple[RequiredOperandV1, ...]
    source_grain: str
    output_grain: str
    temporal: TemporalSpecV2
    computation_kind: str                # COMPUTATION_KINDS
    supporting_objectives: tuple[str, ...] = ()
    parameter_values: tuple[tuple[str, Any], ...] = ()
    eligibility: EligibilitySpecV2 = field(default_factory=EligibilitySpecV2)
    leakage: LeakageSpecV2 = field(default_factory=LeakageSpecV2)
    formula: FormulaReferenceV2 | None = None
    conceptual_reason: str = ""
    model_feature_ref: str = ""

    def __post_init__(self) -> None:
        from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

        _closed(self.origin, PLANNING_ORIGINS, "origin")
        _closed(self.computation_kind, COMPUTATION_KINDS, "computation_kind")
        _require(bool(self.source_definition_id.strip()), "source_definition_id is mandatory")
        _require(bool(self.source_content_hash.strip()), "source_content_hash is mandatory")
        _require(bool(self.source_grain.strip()) and bool(self.output_grain.strip()),
                 "source and output grains are mandatory")
        leaves = set(selectable_leaves())
        _require(self.primary_objective in leaves,
                 f"primary objective {self.primary_objective!r} is not a selectable leaf")
        off_leaf = [o for o in self.supporting_objectives if o not in leaves]
        _require(not off_leaf, f"supporting objectives off the taxonomy: {off_leaf}")

        _require(len(self.operands) > 0, "at least one operand is mandatory")
        roles = [op.role for op in self.operands]
        _require(len(roles) == len(set(roles)), "duplicate operand roles")
        names = [n for n, _ in self.parameter_values]
        _require(len(names) == len(set(names)), "duplicate parameter values")

        if self.origin != "user_definition":
            hinted = [op.role for op in self.operands if op.binding_hint_refs]
            _require(not hinted,
                     f"binding hints are user-definition-only; {self.origin!r} origin carries "
                     f"physical refs on operands {hinted} — the origin may not choose columns")
        if self.computation_kind == "deterministic_formula":
            _require(self.formula is not None,
                     "a deterministic request requires an exact formula reference")
            _require(not self.conceptual_reason,
                     "a deterministic request may not carry a conceptual-only reason")
        if self.computation_kind == "conceptual_pattern":
            _require(bool(self.conceptual_reason.strip()),
                     "a conceptual pattern must say WHY it is conceptual-only")
            _require(self.formula is None, "a conceptual pattern carries no formula reference")
        if self.computation_kind == "governed_model_output":
            _require(bool(self.model_feature_ref.strip()),
                     "a governed model output must reference a registered model-feature spec")


def planning_request_hash(request: FeaturePlanningRequestV1) -> str:
    """Content identity: field-exhaustive canonicalization (a new dataclass field moves this
    hash automatically) inside the registered contract envelope."""
    return contract_hash_v1(PLANNING_REQUEST_CONTRACT, PLANNING_REQUEST_VERSION,
                            {"request": _canonical_dataclass(request)})


def _operand_from_spec(spec) -> RequiredOperandV1:
    return RequiredOperandV1(
        role=spec.role, concept=spec.concept, operand_class=spec.operand_class,
        required=spec.required, allowed_source_grains=spec.allowed_source_grains,
        join_role=spec.join_role, temporal_role=spec.temporal_role,
        distinct_binding_group=spec.distinct_binding_group,
        unit_expectation=spec.unit_expectation,
        currency_expectation=spec.currency_expectation,
        economic_role=spec.economic_role,
        sign_direction_expectation=spec.sign_direction_expectation,
        status_policy_ref=spec.status_policy_ref,
        relationship_requirement=spec.relationship_requirement,
        suggestion_authority=spec.suggestion_authority,
        execution_authority=spec.execution_authority)


def planning_request_from_recipe(definition: RecipeDefinitionV2,
                                 parameter_values: Mapping[str, Any] | None = None,
                                 ) -> FeaturePlanningRequestV1:
    """Project one V2 recipe (at ONE resolved variant) into the neutral request.

    `parameter_values` must cover every declared parameter; omitted → each parameter's first
    allowed value (the registry's authored default convention). A governed-policy parameter has
    no free value — its identity contribution is the policy ref itself."""
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash

    provided = dict(parameter_values or {})
    resolved: list[tuple[str, Any]] = []
    for parameter in definition.parameters:
        if parameter.parameter_class == "governed_policy":
            value = provided.pop(parameter.name, parameter.governed_policy_ref)
        elif parameter.name in provided:
            value = provided.pop(parameter.name)
            if value not in parameter.allowed_values:
                raise PlanningContractError(
                    f"parameter {parameter.name!r} value {value!r} outside the bounded "
                    f"allowed set {parameter.allowed_values}")
        else:
            value = parameter.allowed_values[0]
        resolved.append((parameter.name, value))
    if provided:
        raise PlanningContractError(
            f"unknown parameters for {definition.recipe_id!r}: {sorted(provided)}")

    return FeaturePlanningRequestV1(
        origin="recipe_v2",
        source_definition_id=definition.recipe_id,
        source_revision=str(definition.revision),
        source_content_hash=canonical_recipe_v2_hash(definition),
        primary_objective=definition.primary_objective,
        supporting_objectives=definition.supporting_objectives,
        output=definition.output,
        operands=tuple(_operand_from_spec(op) for op in definition.operands),
        source_grain=definition.source_grain,
        output_grain=definition.output_grain,
        temporal=definition.temporal,
        computation_kind=definition.computation_kind,
        parameter_values=tuple(resolved),
        eligibility=definition.eligibility,
        leakage=definition.leakage,
        formula=definition.formula,
        conceptual_reason=definition.conceptual_reason,
        model_feature_ref=definition.model_feature_ref)


def planning_request_from_feature_intent(intent) -> FeaturePlanningRequestV1:
    """Project one validated `FeatureIntentV1` into the neutral request — same output shape as
    the recipe adapter, which is the whole point. The intent carries no formula reference by
    construction (a fresh intent has no reviewed expectation), so a deterministic intent
    projects as… deterministic WITHOUT a formula? No: the planning contract requires one. The
    honest projection is `conceptual_pattern` with the FORMULA-PENDING reason until an
    expectation is authored and reviewed — an intent is an idea with typed roles, and only the
    formula seam can promote it. This is SE-6 step 11 made structural."""
    from featuregen.overlay.upload.feature_intent import FeatureIntentV1, feature_intent_id

    _require(isinstance(intent, FeatureIntentV1), "expected a FeatureIntentV1")
    if intent.computation_kind == "deterministic_formula":
        computation_kind = "conceptual_pattern"
        conceptual_reason = ("formula pending: a model-proposed computation becomes executable "
                            "only through an authored, reviewed formula expectation")
    else:
        computation_kind = intent.computation_kind
        conceptual_reason = intent.conceptual_reason
    return FeaturePlanningRequestV1(
        origin="llm_intent",
        source_definition_id=feature_intent_id(intent),
        source_revision="1",
        source_content_hash=feature_intent_id(intent),
        primary_objective=intent.primary_objective,
        supporting_objectives=intent.supporting_objectives,
        output=intent.output,
        operands=intent.operands,
        source_grain=intent.source_grain,
        output_grain=intent.output_grain_entity,
        temporal=intent.temporal,
        computation_kind=computation_kind,
        eligibility=intent.eligibility,
        leakage=intent.leakage,
        formula=None,
        conceptual_reason=conceptual_reason,
        model_feature_ref=intent.model_feature_ref)


def planning_request_from_user_definition(
        *, definition_id: str, primary_objective: str, output: OutputSpecV2,
        operands: tuple[RequiredOperandV1, ...], source_grain: str, output_grain: str,
        temporal: TemporalSpecV2, content_hash: str,
        supporting_objectives: tuple[str, ...] = (),
        eligibility: EligibilitySpecV2 | None = None,
        leakage: LeakageSpecV2 | None = None) -> FeaturePlanningRequestV1:
    """The RESTRICTED user-definition adapter: typed roles in, physical refs demoted to hints.

    A user may have typed exact columns — those arrive on the operands as `binding_hint_refs`,
    which the binder may use to RANK candidates it independently found eligible, never to skip
    eligibility. The projection is honest about what it is: a user definition with no reviewed
    formula is a conceptual pattern until the formula seam promotes it."""
    return FeaturePlanningRequestV1(
        origin="user_definition",
        source_definition_id=definition_id,
        source_revision="1",
        source_content_hash=content_hash,
        primary_objective=primary_objective,
        supporting_objectives=supporting_objectives,
        output=output,
        operands=operands,
        source_grain=source_grain,
        output_grain=output_grain,
        temporal=temporal,
        computation_kind="conceptual_pattern",
        eligibility=eligibility or EligibilitySpecV2(),
        leakage=leakage or LeakageSpecV2(),
        conceptual_reason="user definition: computation not yet authored through the "
                          "governed formula seam")


__all__ = [
    "PLANNING_ORIGINS", "PLANNING_REQUEST_CONTRACT", "PLANNING_REQUEST_VERSION",
    "FeaturePlanningRequestV1", "PlanningContractError", "RequiredOperandV1",
    "planning_request_from_feature_intent", "planning_request_from_recipe",
    "planning_request_from_user_definition", "planning_request_hash",
]
