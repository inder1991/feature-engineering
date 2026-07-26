"""Fail-closed authority envelope for one recipe-backed formula shadow item."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.operational_facts import (
    read_operational_value,
    read_verified_decision_value,
)
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
    event_time_facts: tuple[dict[str, Any], ...]
    policy_version: int = AUTHORITY_ENVELOPE_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "recipe_candidate_key": self.recipe_candidate_key,
            "bindings": list(self.bindings),
            "grain_facts": list(self.grain_facts),
            "event_time_facts": list(self.event_time_facts),
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

    event_time_facts: list[dict[str, Any]] = []
    event_refs = {
        expression.event_time_ref
        for expression in expectation.expressions
        if expression.event_time_ref is not None
    }
    for logical_ref in sorted(event_refs):
        temporal = read_verified_decision_value(
            conn, logical_ref, "temporal_role")
        if temporal.status != "resolved" or temporal.value != "event":
            return FormulaAuthorityRejection(
                f"EVENT_TIME_AUTHORITY_{temporal.status.upper()}",
                logical_ref=logical_ref,
                role=by_ref[logical_ref].role if logical_ref in by_ref else None,
            )
        event_time_facts.append({
            "logical_ref": logical_ref,
            "temporal_role": temporal.value,
            "decision_event_id": temporal.decision_event_id,
            "selected_evidence_ids": list(temporal.selected_evidence_ids),
            "policy_version": temporal.policy_version,
            "resolver_version": temporal.resolver_version,
        })

    return FormulaAuthorityEnvelopeV1(
        recipe_candidate_key=context.recipe_candidate_key,
        bindings=tuple(bindings),
        grain_facts=tuple(grain_facts),
        event_time_facts=tuple(event_time_facts),
    )


def verify_formula_authority_envelope(
    conn,
    envelope: dict[str, Any],
) -> str | None:
    """Re-resolve a frozen authority envelope; return a drift reason or ``None``.

    Worker-time verification never adopts current authority. Any changed value, evidence set,
    governing decision/fact, or accepted authority tier makes the frozen work stale.
    """
    if envelope.get("policy_version") != AUTHORITY_ENVELOPE_VERSION:
        return "AUTHORITY_POLICY_VERSION_DRIFT"
    bindings = envelope.get("bindings")
    grain_facts = envelope.get("grain_facts")
    event_time_facts = envelope.get("event_time_facts")
    if not all(isinstance(value, list) for value in (
        bindings, grain_facts, event_time_facts
    )):
        return "AUTHORITY_ENVELOPE_INVALID"
    for frozen in bindings:
        if not isinstance(frozen, dict) or not isinstance(
            frozen.get("logical_ref"), str
        ):
            return "AUTHORITY_ENVELOPE_INVALID"
        current = resolve_planner_concept_binding(conn, frozen["logical_ref"])
        if not isinstance(current, PlannerConceptBinding):
            return "CONCEPT_AUTHORITY_NO_LONGER_RESOLVED"
        current_material = {
            "authoritative_concept": current.authoritative_concept,
            "authority": current.authority.value,
            "evidence_ids": list(current.evidence_ids),
            "evidence_set_hash": current.evidence_set_hash,
            "value_hash": current.value_hash,
        }
        if any(frozen.get(key) != value for key, value in current_material.items()):
            return "CONCEPT_AUTHORITY_DRIFT"
        if frozen.get("expected_concept") != current.authoritative_concept:
            return "CONCEPT_BINDING_MISMATCH"
    for frozen in grain_facts:
        if not isinstance(frozen, dict) or not isinstance(
            frozen.get("logical_ref"), str
        ):
            return "AUTHORITY_ENVELOPE_INVALID"
        current = read_operational_value(conn, frozen["logical_ref"], "is_grain")
        if (
            current.status != "resolved"
            or current.value not in (True, "true")
            or current.fact_key != frozen.get("fact_key")
            or current.fact_event_id != frozen.get("fact_event_id")
            or current.policy_version != frozen.get("policy_version")
            or current.resolver_version != frozen.get("resolver_version")
        ):
            return "GRAIN_AUTHORITY_DRIFT"
    for frozen in event_time_facts:
        if not isinstance(frozen, dict) or not isinstance(
            frozen.get("logical_ref"), str
        ):
            return "AUTHORITY_ENVELOPE_INVALID"
        current = read_verified_decision_value(
            conn, frozen["logical_ref"], "temporal_role")
        if (
            current.status != "resolved"
            or current.value != frozen.get("temporal_role")
            or current.decision_event_id != frozen.get("decision_event_id")
            or list(current.selected_evidence_ids)
            != frozen.get("selected_evidence_ids")
            or current.policy_version != frozen.get("policy_version")
            or current.resolver_version != frozen.get("resolver_version")
        ):
            return "EVENT_TIME_AUTHORITY_DRIFT"
    return None
