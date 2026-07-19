"""Phase-3C.2a — the governed plan envelope: the server-persisted carry-forward that binds a chosen
considered-set option to its exact governed physical plan, so drafting never recomputes a permissive
path. Freshness is rechecked per-plan via ReplayFreshness (catalog churn is NOT an activation concern)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload.planner.contracts import (
    PLAN_CONTRACT_VERSION,
    BindingPlanningResultV1,
    BindingPlanV1,
    CatalogStateStampV1,
    ReplayFreshness,
)
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS
from featuregen.overlay.upload.planner.replay import (
    StoredEvidenceV1,
    compare,
    read_current_evidence,
)


@dataclass(frozen=True, slots=True)
class PlanEnvelopeV1:
    recipe_id: str
    physical_plan_id: str
    generation_run_id: str | None
    catalog_sources: tuple[str, ...]
    ordered_path: tuple[str, ...]
    contract_id: str | None
    contract_resolution_status: str
    contract_reason_codes: tuple[str, ...]
    catalog_fingerprint: dict[str, str]
    compiler_version: dict[str, str]
    input_stamps: tuple[dict[str, Any], ...]   # serialized CatalogStateStampV1 set (the freshness source)

    def to_json(self) -> dict:
        return {
            "recipe_id": self.recipe_id, "physical_plan_id": self.physical_plan_id,
            "generation_run_id": self.generation_run_id, "catalog_sources": list(self.catalog_sources),
            "ordered_path": list(self.ordered_path), "contract_id": self.contract_id,
            "contract_resolution_status": self.contract_resolution_status,
            "contract_reason_codes": list(self.contract_reason_codes),
            "catalog_fingerprint": dict(self.catalog_fingerprint),
            "compiler_version": dict(self.compiler_version),
            "input_stamps": [dict(s) for s in self.input_stamps]}

    @staticmethod
    def from_json(d: dict) -> PlanEnvelopeV1:
        return PlanEnvelopeV1(
            recipe_id=d["recipe_id"], physical_plan_id=d["physical_plan_id"],
            generation_run_id=d.get("generation_run_id"),
            catalog_sources=tuple(d.get("catalog_sources", [])),
            ordered_path=tuple(d.get("ordered_path", [])), contract_id=d.get("contract_id"),
            contract_resolution_status=d["contract_resolution_status"],
            contract_reason_codes=tuple(d.get("contract_reason_codes", [])),
            catalog_fingerprint=dict(d.get("catalog_fingerprint", {})),
            compiler_version=dict(d.get("compiler_version", {})),
            input_stamps=tuple(dict(s) for s in d.get("input_stamps", [])))


def _ordered_path(plan: BindingPlanV1) -> tuple[str, ...]:
    return tuple(f"{seg.catalog_source}:{seg.segment_kind}:{seg.realization_ref or seg.bridge_fact_key or ''}"
                 for seg in plan.path_segments)


def plan_envelope_from_result(result: BindingPlanningResultV1) -> PlanEnvelopeV1 | None:
    """Project the SELECTED governed contract plan into an envelope. None when the run has no selected
    contract plan (nothing governed to carry)."""
    pid = result.selected_contract_physical_plan_id
    if pid is None:
        return None
    plan = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
    if plan is None:
        return None
    stamps = plan.audit_envelope.catalog_state_stamps if plan.audit_envelope is not None else ()
    return PlanEnvelopeV1(
        recipe_id=result.recipe_id, physical_plan_id=plan.physical_plan_id,
        generation_run_id=result.run_id, catalog_sources=tuple(plan.participating_catalogs),
        ordered_path=_ordered_path(plan), contract_id=plan.contract_id,
        contract_resolution_status=str(plan.contract_resolution_status),
        contract_reason_codes=tuple(str(c) for c in plan.contract_reason_codes),
        catalog_fingerprint={s.catalog_source: s.compiler_input_fingerprint for s in stamps},
        compiler_version={"plan_contract": PLAN_CONTRACT_VERSION},
        input_stamps=tuple({"catalog_source": s.catalog_source,
                            "compiler_input_fingerprint": s.compiler_input_fingerprint,
                            "head_seq": s.head_seq, "projection_checkpoint": s.projection_checkpoint}
                           for s in stamps))


def recheck_plan_freshness(conn, envelope: PlanEnvelopeV1,
                           roles: Iterable[str] = ()) -> ReplayFreshness:
    """Compare the envelope's pinned per-catalog stamps to the CURRENT catalog state. Anything but
    `current` (drifted / incompatible / unverifiable) means the plan must be regenerated, not substituted."""
    stamps = tuple(
        CatalogStateStampV1(catalog_source=s["catalog_source"], head_seq=int(s["head_seq"]),
                            last_completed_at="", compiler_input_fingerprint=s["compiler_input_fingerprint"],
                            projection_checkpoint=int(s.get("projection_checkpoint", 0)))
        for s in envelope.input_stamps)
    stored = StoredEvidenceV1.from_stamps(stamps, _VERSIONS)
    return compare(stored, read_current_evidence(conn, stored, roles))
