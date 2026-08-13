"""SE-1 — `FeatureIntentV1`: the LLM's allowed output. Meaning, never columns.

An intent describes a computation in typed semantic roles — what to measure, at which grain,
over which window, under which eligibility and leakage rules — and structurally CANNOT name
physical data: construction refuses binding hints on any operand, and the strict parser
(`parse_feature_intent`) rejects both unknown keys and the physical-reference vocabulary
(`table`, `column`, `object_ref`, `sql`, …) anywhere in the document, so a model that tries to
smuggle a column ref fails loudly at the boundary instead of quietly gaining authority.

Two ceilings are encoded here rather than left to review:

* a deterministic intent pins its `operation_class` to the closed Formula-V2 result-class
  vocabulary and must satisfy the SAME result-class/additivity law as a recipe — no
  free-text aggregation exists in this contract;
* readiness: an intent carries NO formula reference field at all. A fresh intent has no
  reviewed expectation, so executable readiness is unreachable from here by construction —
  the path runs through the governed formula seam, exactly like a recipe's (SE-6 step 11).

Identity is content-addressed: `feature_intent_id` hashes every field through the same
field-exhaustive canonicalizer as canonical-recipe-v2 inside a registered contract envelope,
so any change — a role, a window, a stage list — is a different intent.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.upload.feature_planning_contracts import (
    PlanningContractError,
    RequiredOperandV1,
)
from featuregen.overlay.upload.recipe_contract_v2 import (
    COMPUTATION_KINDS,
    RESULT_CLASS_ADDITIVITY,
    EligibilitySpecV2,
    LeakageSpecV2,
    OutputSpecV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipe_grounding_context import _canonical_dataclass

FEATURE_INTENT_CONTRACT = "feature-intent"
FEATURE_INTENT_VERSION = "1"
_OWNER = "featuregen.overlay.upload.feature_intent"

register_contract_version(FEATURE_INTENT_CONTRACT, FEATURE_INTENT_VERSION, owner=_OWNER)


class FeatureIntentError(PlanningContractError):
    """An invalid feature intent — refused at construction or parse, never bound."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FeatureIntentError(message)


@dataclass(frozen=True, slots=True)
class GenerationProvenanceV1:
    """Where an intent came from — mandatory, so an intent is never an orphan claim."""

    prompt_ref: str
    output_schema_version: str
    model: str
    call_ref: str
    confirmed_scope_hash: str
    #: B3: the frozen catalog context this intent was generated against — its OWN key; the
    #: scope hash above is the HUMAN's scope identity, never the catalog's.
    semantic_context_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("prompt_ref", "output_schema_version", "model", "call_ref",
                     "confirmed_scope_hash"):
            _require(bool(getattr(self, name).strip()),
                     f"generation provenance field {name!r} is mandatory")


@dataclass(frozen=True, slots=True)
class FeatureIntentV1:
    """One typed, physically-blind feature idea. See the module docstring for the two ceilings."""

    display_name: str
    business_definition: str
    primary_objective: str
    computation_kind: str                # COMPUTATION_KINDS
    output: OutputSpecV2
    output_grain_entity: str
    source_grain: str
    operands: tuple[RequiredOperandV1, ...]
    temporal: TemporalSpecV2
    generation_provenance: GenerationProvenanceV1
    supporting_objectives: tuple[str, ...] = ()
    operation_class: str = ""            # RESULT_CLASS_ADDITIVITY key; deterministic only
    eligibility: EligibilitySpecV2 = field(default_factory=EligibilitySpecV2)
    leakage: LeakageSpecV2 = field(default_factory=LeakageSpecV2)
    conceptual_reason: str = ""
    model_feature_ref: str = ""
    rationale: str = ""                  # explanatory ONLY — never evidence, never authority

    def __post_init__(self) -> None:
        from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

        _require(bool(self.display_name.strip()), "display_name is mandatory")
        _require(bool(self.business_definition.strip()), "business_definition is mandatory")
        _require(self.computation_kind in COMPUTATION_KINDS,
                 f"computation_kind {self.computation_kind!r} not in {COMPUTATION_KINDS}")
        _require(bool(self.output_grain_entity.strip()) and bool(self.source_grain.strip()),
                 "output_grain_entity and source_grain are mandatory")

        leaves = set(selectable_leaves())
        _require(self.primary_objective in leaves,
                 f"primary objective {self.primary_objective!r} is not a selectable leaf")
        off_leaf = [o for o in self.supporting_objectives if o not in leaves]
        _require(not off_leaf, f"supporting objectives off the taxonomy: {off_leaf}")

        _require(len(self.operands) > 0, "at least one operand is mandatory")
        roles = [op.role for op in self.operands]
        _require(len(roles) == len(set(roles)), "duplicate operand roles")
        hinted = [op.role for op in self.operands if op.binding_hint_refs]
        _require(not hinted,
                 f"an intent may not carry physical binding hints (operands {hinted}) — "
                 "the model proposes meaning, a deterministic stage assigns columns")

        if self.computation_kind == "deterministic_formula":
            _require(self.operation_class in RESULT_CLASS_ADDITIVITY,
                     f"operation_class {self.operation_class!r} is not a closed Formula-V2 "
                     f"result class (allowed: {tuple(RESULT_CLASS_ADDITIVITY)})")
            allowed = RESULT_CLASS_ADDITIVITY[self.operation_class]
            _require(self.output.additivity in allowed,
                     f"output additivity {self.output.additivity!r} is incompatible with "
                     f"operation class {self.operation_class!r} (allowed: {allowed})")
            _require(not self.conceptual_reason,
                     "a deterministic intent may not carry a conceptual-only reason")
        else:
            _require(not self.operation_class,
                     "operation_class is deterministic-only; an honest conceptual pattern "
                     "names no operation")
        if self.computation_kind == "conceptual_pattern":
            _require(bool(self.conceptual_reason.strip()),
                     "a conceptual pattern must say WHY it is conceptual-only")
        if self.computation_kind == "governed_model_output":
            _require(bool(self.model_feature_ref.strip()),
                     "a governed model output must reference a registered model-feature spec "
                     "the server offered — the model may not invent one")


def feature_intent_id(intent: FeatureIntentV1) -> str:
    """Content-addressed identity over the FULL intent body (every field hash-bearing)."""
    return contract_hash_v1(FEATURE_INTENT_CONTRACT, FEATURE_INTENT_VERSION,
                            {"intent": _canonical_dataclass(intent)})


# ── strict parsing — the wire boundary where a model's output becomes (or fails to become) an
# intent. Unknown keys are refused everywhere; the physical vocabulary is refused BY NAME so the
# error teaches the schema instead of hiding the attempt inside "unknown key". ────────────────

FORBIDDEN_PHYSICAL_KEYS = frozenset({
    "catalog_source", "source", "table", "table_name", "column", "column_name",
    "object_ref", "logical_ref", "graph_ref", "sql", "expression", "derives_from",
    "physical_ref", "dataset",
})

_SECTION_TYPES: dict[str, type] = {
    "output": OutputSpecV2,
    "temporal": TemporalSpecV2,
    "eligibility": EligibilitySpecV2,
    "leakage": LeakageSpecV2,
    "generation_provenance": GenerationProvenanceV1,
}


def _reject_foreign_keys(mapping: Mapping[str, Any], allowed: frozenset[str],
                         path: str) -> None:
    for key in mapping:
        _require(isinstance(key, str), f"non-string key at {path}")
        if key in FORBIDDEN_PHYSICAL_KEYS:
            raise FeatureIntentError(
                f"physical reference key {key!r} at {path} — a feature intent describes "
                "meaning; a deterministic stage assigns physical data")
        _require(key in allowed, f"unknown key {key!r} at {path}")


def _coerce(value: Any, path: str) -> Any:
    _require(not isinstance(value, Mapping), f"unexpected nested object at {path}")
    if isinstance(value, list):
        return tuple(_coerce(item, f"{path}[]") for item in value)
    return value


def _parse_section(cls: type, doc: Mapping[str, Any], path: str) -> Any:
    assert is_dataclass(cls)
    allowed = frozenset(f.name for f in fields(cls))
    _reject_foreign_keys(doc, allowed, path)
    kwargs = {key: _coerce(value, f"{path}.{key}") for key, value in doc.items()}
    return cls(**kwargs)


def parse_feature_intent(doc: Mapping[str, Any]) -> FeatureIntentV1:
    """Parse one model-emitted document into a validated intent, strictly.

    Every construction rule runs (the dataclasses validate themselves); every unknown or
    physical key anywhere in the document is a named refusal. A malformed sibling in a batch
    is the CALLER's concern — this parses exactly one document."""
    allowed = frozenset(f.name for f in fields(FeatureIntentV1))
    _reject_foreign_keys(doc, allowed, "intent")
    kwargs: dict[str, Any] = {}
    for key, value in doc.items():
        if key in _SECTION_TYPES:
            _require(isinstance(value, Mapping), f"intent.{key} must be an object")
            kwargs[key] = _parse_section(_SECTION_TYPES[key], value, f"intent.{key}")
        elif key == "operands":
            _require(isinstance(value, list) and value, "intent.operands must be a non-empty list")
            kwargs[key] = tuple(
                _parse_section(RequiredOperandV1, item, f"intent.operands[{i}]")
                for i, item in enumerate(value))
        else:
            kwargs[key] = _coerce(value, f"intent.{key}")
    return FeatureIntentV1(**kwargs)


__all__ = [
    "FEATURE_INTENT_CONTRACT", "FEATURE_INTENT_VERSION", "FORBIDDEN_PHYSICAL_KEYS",
    "FeatureIntentError", "FeatureIntentV1", "GenerationProvenanceV1",
    "feature_intent_id", "parse_feature_intent",
]
