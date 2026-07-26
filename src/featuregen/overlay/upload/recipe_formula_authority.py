"""Fail-closed authority envelope for one recipe-backed formula shadow item."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.planner.b_concept_authority import (
    ConceptRejection,
    PlannerConceptBinding,
    resolve_planner_concept_binding,
)

AUTHORITY_ENVELOPE_VERSION = 1


@dataclass(frozen=True, slots=True)
class FormulaAuthorityRejection:
    reason: str
    logical_ref: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class FormulaAuthorityEnvelopeV1:
    recipe_candidate_key: str
    bindings: tuple[dict[str, Any], ...]
    grain_facts: tuple[dict[str, Any], ...]
    policy_version: int = AUTHORITY_ENVELOPE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "recipe_candidate_key": self.recipe_candidate_key,
            "bindings": list(self.bindings),
            "grain_facts": list(self.grain_facts),
            "policy_version": self.policy_version,
        }

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.to_json())


def build_formula_authority_envelope(
    conn,
    *,
    context,
    expectation,
) -> FormulaAuthorityEnvelopeV1 | FormulaAuthorityRejection:
    """Verify exact recipe roles against raw authoritative concept evidence and grain facts.

    The graph's display concept remains only a discovery hint. Every formula-bearing role must
    resolve through the planner-specific concept authority reader and equal the recipe-authored
    expected concept. Every output grain key must additionally carry a current VERIFIED grain fact.
    """
    by_ref = {binding.logical_ref: binding for binding in context.need_bindings}
    required_refs = {
        ref
        for expression in expectation.expressions
        for ref in (
            expression.operand_ref,
            expression.event_time_ref,
        )
        if ref is not None
    } | set(expectation.grain_key_refs)
    bindings: list[dict[str, Any]] = []
    for logical_ref in sorted(required_refs):
        source_binding = by_ref.get(logical_ref)
        if source_binding is None:
            return FormulaAuthorityRejection(
                "ROLE_BINDING_NOT_PRESERVED", logical_ref=logical_ref)
        resolved = resolve_planner_concept_binding(conn, logical_ref)
        if isinstance(resolved, ConceptRejection):
            return FormulaAuthorityRejection(
                resolved.reason.value,
                logical_ref=logical_ref,
                role=source_binding.role,
            )
        if not isinstance(resolved, PlannerConceptBinding):
            return FormulaAuthorityRejection(
                "CONCEPT_AUTHORITY_UNVERIFIABLE",
                logical_ref=logical_ref,
                role=source_binding.role,
            )
        if resolved.authoritative_concept != source_binding.expected_concept:
            return FormulaAuthorityRejection(
                "CONCEPT_BINDING_MISMATCH",
                logical_ref=logical_ref,
                role=source_binding.role,
            )
        bindings.append({
            "role": source_binding.role,
            "logical_ref": logical_ref,
            "expected_concept": source_binding.expected_concept,
            "authoritative_concept": resolved.authoritative_concept,
            "authority": resolved.authority.value,
            "evidence_ids": list(resolved.evidence_ids),
            "evidence_set_hash": resolved.evidence_set_hash,
            "value_hash": resolved.value_hash,
            "diagnostics": list(resolved.diagnostics),
        })

    grain_facts: list[dict[str, Any]] = []
    for logical_ref in expectation.grain_key_refs:
        grain = read_operational_value(conn, logical_ref, "is_grain")
        if (
            grain.status != "resolved"
            or grain.value not in (True, "true")
            or grain.fact_event_id is None
        ):
            return FormulaAuthorityRejection(
                f"GRAIN_AUTHORITY_{grain.status.upper()}",
                logical_ref=logical_ref,
                role=by_ref[logical_ref].role if logical_ref in by_ref else None,
            )
        grain_facts.append({
            "logical_ref": logical_ref,
            "fact_key": grain.fact_key,
            "fact_event_id": grain.fact_event_id,
            "policy_version": grain.policy_version,
            "resolver_version": grain.resolver_version,
        })

    return FormulaAuthorityEnvelopeV1(
        recipe_candidate_key=context.recipe_candidate_key,
        bindings=tuple(bindings),
        grain_facts=tuple(grain_facts),
    )
