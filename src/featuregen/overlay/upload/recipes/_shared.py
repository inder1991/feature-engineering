"""Shared builders for the BR-15/BR-16 packs — construction sugar only, never semantics.

Every builder returns a fresh spec with the SAME declarations an inline literal would carry;
nothing here defaults a policy, a grain or a role. The earlier packs (retail/credit) predate
this module and keep their own local helpers."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    FormulaReferenceV2,
    OperandSpecV2,
    TemporalSpecV2,
)


def entity(role: str, concept: str, source: str) -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="entity_key",
                         allowed_source_grains=(source,))


def measure(role: str, concept: str, source: str, *, economic_role: str = "",
            unit: str = "monetary") -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation=unit,
                         economic_role=economic_role)


def dim(role: str, concept: str, source: str, *, group: str = "",
        required: bool = True) -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="dimension",
                         allowed_source_grains=(source,), distinct_binding_group=group,
                         required=required)


def status(role: str, concept: str, source: str, *, policy: str = "") -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="status",
                         allowed_source_grains=(source,), status_policy_ref=policy)


def policy_input(role: str, concept: str, source: str, *, policy: str) -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="policy_input",
                         allowed_source_grains=(source,), status_policy_ref=policy)


def event_ts(source: str, *, role: str = "event_ts", concept: str = "event_timestamp",
             group: str = "") -> OperandSpecV2:
    return OperandSpecV2(role=role, concept=concept, operand_class="event_timestamp",
                         allowed_source_grains=(source,), distinct_binding_group=group)


def as_of(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="as_of_date", concept="as_of_date",
                         operand_class="as_of_timestamp", allowed_source_grains=(source,))


def formula(pack: str, output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"{pack}:{output_id}",
                              result_class=result_class)


def event_window(role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


def snapshot_window(policy: str) -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="as_of", business_effective_role="as_of_date",
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive",
                          snapshot_policy=policy)
