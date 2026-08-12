"""Phase-3C.2a — the governed plan envelope: the server-persisted carry-forward that binds a chosen
considered-set option to its exact governed physical plan, so drafting never recomputes a permissive
path. Freshness is rechecked per-plan via ReplayFreshness (catalog churn is NOT an activation concern)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload.grounding_trace import (
    CROSS_CATALOG_CONTRACT,
    CROSS_CATALOG_PATH_SEGMENT,
    GroundingDependencyPinV1,
    SuggestionDependencyClass,
    SuggestionRelationshipDependencyV1,
    dependency_pin,
    relationship_leg,
)
from featuregen.overlay.upload.planner.contracts import (
    PLAN_CONTRACT_VERSION,
    BindingPathSegmentV1,
    BindingPlanningResultV1,
    BindingPlanV1,
    CatalogStateStampV1,
    ReplayFreshness,
    SegmentKind,
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
    # H3c — the grain the governed plan planned toward. Carried so the confirm-time revalidation can
    # REBUILD the exact plan (``plan_bindings`` needs the target grain) and reproduce the SAME
    # physical_plan_id / declaration id. Additive (default None): it is NOT part of any id material,
    # so it never moves an id; pre-H3c snapshots deserialize to None (behaviour-neutral).
    target_entity: str | None = None
    bridge_realization_dependencies: tuple[dict[str, str], ...] = ()

    def to_json(self) -> dict:
        return {
            "recipe_id": self.recipe_id, "physical_plan_id": self.physical_plan_id,
            "generation_run_id": self.generation_run_id, "catalog_sources": list(self.catalog_sources),
            "ordered_path": list(self.ordered_path), "contract_id": self.contract_id,
            "contract_resolution_status": self.contract_resolution_status,
            "contract_reason_codes": list(self.contract_reason_codes),
            "catalog_fingerprint": dict(self.catalog_fingerprint),
            "compiler_version": dict(self.compiler_version),
            "input_stamps": [dict(s) for s in self.input_stamps],
            "target_entity": self.target_entity,
            "bridge_realization_dependencies": [
                dict(item) for item in self.bridge_realization_dependencies],
        }

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
            input_stamps=tuple(dict(s) for s in d.get("input_stamps", [])),
            target_entity=d.get("target_entity"),
            bridge_realization_dependencies=tuple(
                dict(item) for item in d.get("bridge_realization_dependencies", [])),
        )


def _ordered_path(plan: BindingPlanV1) -> tuple[str, ...]:
    def identity(segment) -> str:
        if segment.bridge_realization_revision is not None:
            return segment.bridge_realization_revision.realization_revision_id
        return segment.realization_ref or segment.bridge_fact_key or ""

    return tuple(
        f"{seg.catalog_source}:{seg.segment_kind}:{identity(seg)}"
        for seg in plan.path_segments
    )


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
                           for s in stamps),
        target_entity=result.target_entity,   # H3c: the grain the confirm-time rebuild plans toward
        bridge_realization_dependencies=tuple(
            {
                "realization_revision_id":
                    segment.bridge_realization_revision.realization_revision_id,
                "dependency_snapshot_id":
                    segment.bridge_realization_revision.dependency_snapshot_id,
            }
            for segment in plan.path_segments
            if segment.bridge_realization_revision is not None
        ),
    )


# ── Task 2A: the cross-catalog half of the grounding trace (freeze 0F-7) ────────────────────────
# The same-catalog gauntlet retains the ordered join path `classify_join_path` selected. Its
# cross-catalog counterpart is the compiled plan's ordered path segments, and the same rule applies:
# the trace RECORDS the crossing the planner already chose — it never re-plans, and no consumer may.
#
#: Which frozen relationship kind (D3 vocabulary) each plan segment realizes. `direct_catalog` is
#: absent on purpose: it says "this ingredient lives in this catalog" and crosses nothing, so it is
#: not a traversal and gets no leg.
_SEGMENT_RELATIONSHIP_KIND: dict[SegmentKind, str] = {
    SegmentKind.governed_bridge: "crosswalk",          # a governed cross-catalog identifier bridge
    SegmentKind.intra_catalog_realization: "direct_equality",   # a physical key equality, one catalog
    SegmentKind.semantic_rollup: "semantic_only",      # an entity roll-up, asserted semantically
}
#: A crossing whose immutable realization revision is attached was compiled for production; one
#: carrying only endpoints and a bridge fact is a discovery/sandbox path (contracts.py Task 9).
_SAFETY_WITH_REALIZATION = "clearing"
_SAFETY_WITHOUT_REALIZATION = "unverified"


def _segment_endpoints(segment: BindingPathSegmentV1) -> tuple[tuple[str, str], tuple[str, str]]:
    """The crossing's addressed endpoints, in the direction of travel. A segment that carries no
    explicit bridge endpoints falls back to its own catalog + entity names — which is what such a
    segment actually asserts, rather than a column pair it never named."""
    from_ref = (segment.bridge_from_catalog_source or segment.catalog_source,
                segment.bridge_from_object_ref or segment.from_entity or "")
    to_ref = (segment.bridge_to_catalog_source or segment.catalog_source,
              segment.bridge_to_object_ref or segment.to_entity or "")
    return from_ref, to_ref


def plan_relationship_dependencies(plan: BindingPlanV1
                                   ) -> tuple[SuggestionRelationshipDependencyV1, ...]:
    """The compiled plan's ordered directional realizations (freeze 0F-7).

    ``relationship_ref`` is the SEMANTIC hop the segment realizes (``relationship_id``, else the
    governed bridge fact that stands for it) — direction-free, because one relationship exposes many
    directional realizations. The direction, the cardinality AS CROSSED and the realization's own
    identity live in ``from_ref``/``to_ref``/``realization_content_hash``. The exact
    ``realization_revision_id`` is NOT here: it is a currentness pointer and rides on the dependency
    pin (:func:`plan_dependency_pins`), so replaying the identical crossing under a new revision
    does not fork the candidate's identity.
    """
    legs: list[SuggestionRelationshipDependencyV1] = []
    for segment in plan.path_segments:
        kind = _SEGMENT_RELATIONSHIP_KIND.get(segment.segment_kind)
        if kind is None:
            continue
        from_ref, to_ref = _segment_endpoints(segment)
        revision = segment.bridge_realization_revision
        legs.append(relationship_leg(
            relationship_ref=(segment.relationship_id or segment.bridge_fact_key
                              or segment.realization_ref or ""),
            relationship_kind=kind,
            from_ref=from_ref, to_ref=to_ref,
            realization_content={
                "segment_kind": segment.segment_kind.value,
                "catalog_source": segment.catalog_source,
                "from_entity": segment.from_entity, "to_entity": segment.to_entity,
                "from_ref": [from_ref[0], from_ref[1]], "to_ref": [to_ref[0], to_ref[1]],
                "cardinality": segment.cardinality, "direction": segment.direction,
                "bridge_fact_key": segment.bridge_fact_key,
                "realization_ref": segment.realization_ref,
                "realization_id": None if revision is None else revision.realization_id,
                "relationship_version": segment.relationship_version,
            },
            cardinality=segment.cardinality or "unknown",
            safety_status=(_SAFETY_WITH_REALIZATION if revision is not None
                           else _SAFETY_WITHOUT_REALIZATION),
            review_status=("governed_bridge" if segment.bridge_fact_key else "unlinked")))
    return tuple(legs)


def plan_dependency_pins(plan: BindingPlanV1) -> tuple[GroundingDependencyPinV1, ...]:
    """One pin per traversed segment plus the plan's contract resolution.

    A segment pin's ``current_revision_id`` is the exact ``realization_revision_id`` the compile
    consumed — the provenance a later reader compares against the current realization to decide
    whether this candidate is still true. The CONTENT hashed beside it is the crossing's logical
    shape, so the two questions ("is it the same crossing?" and "is it the same revision?") stay
    separable, which is the whole point of the split.
    """
    pins: list[GroundingDependencyPinV1] = []
    for index, segment in enumerate(plan.path_segments):
        if segment.segment_kind not in _SEGMENT_RELATIONSHIP_KIND:
            continue
        from_ref, to_ref = _segment_endpoints(segment)
        revision = segment.bridge_realization_revision
        pins.append(dependency_pin(
            dependency_class=SuggestionDependencyClass.VALIDATION,
            dependency_kind=CROSS_CATALOG_PATH_SEGMENT,
            dependency_key=f"{plan.physical_plan_id}::segment::{index}",
            content={"segment_kind": segment.segment_kind.value,
                     "from_ref": [from_ref[0], from_ref[1]],
                     "to_ref": [to_ref[0], to_ref[1]],
                     "cardinality": segment.cardinality, "direction": segment.direction,
                     "bridge_fact_key": segment.bridge_fact_key,
                     "relationship_id": segment.relationship_id,
                     "relationship_version": segment.relationship_version},
            current_revision_id=None if revision is None else revision.realization_revision_id))
    pins.append(dependency_pin(
        dependency_class=SuggestionDependencyClass.VALIDATION,
        dependency_kind=CROSS_CATALOG_CONTRACT,
        dependency_key=plan.contract_id or plan.physical_plan_id,
        content={"contract_resolution_status": str(plan.contract_resolution_status),
                 "path_resolution_status": str(plan.path_resolution_status),
                 "resolution_status": str(plan.resolution_status),
                 "safety": str(plan.safety),
                 "reason_codes": sorted(str(code) for code in plan.contract_reason_codes)},
        current_revision_id=plan.contract_id))
    return tuple(pins)


def plan_operand_roles(plan: BindingPlanV1) -> tuple[tuple[str, str, str], ...]:
    """``(catalog_source, object_ref, need_role)`` per bound ingredient, in the plan's own binding
    order — the cross-catalog counterpart of the template-declared operand roles the gauntlet
    records."""
    return tuple((b.bound_catalog_source, b.bound_object_ref, b.need_role)
                 for b in plan.ingredient_bindings)


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
