"""Phase-3B.3a A2 — per-need candidate discovery within ONE catalog. Preserves every concept-matched
column (accepted OR rejected) up to the per-need bound, each with its candidate-local eligibility verdict
(role/grain/concept/safety). Consumes RESOLVED_NEED_METADATA for the grain constraint."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.catalog_realizations import object_grain, table_of
from featuregen.overlay.upload.need_metadata import RESOLVED_NEED_METADATA
from featuregen.overlay.upload.planner.contracts import (
    MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG,
    BindingQuality,
    BindingSafety,
    IngredientCandidateV1,
    ReasonCode,
)
from featuregen.overlay.upload.planner.safety import evaluate_binding_safety
from featuregen.overlay.upload.templates import Template, _Col, _load_columns


@dataclass(frozen=True, slots=True)
class CandidateDiscoveryV1:
    candidates: dict[str, tuple[IngredientCandidateV1, ...]]   # need role -> candidates (accepted + rejected)
    candidate_columns_truncated: bool
    total_candidate_columns_considered: int


def _quality(col: _Col, concept: str, grain_ok: bool) -> BindingQuality:
    if col.concept == concept and grain_ok:
        return BindingQuality.grain_and_role_fit
    if col.concept == concept:
        return BindingQuality.exact_concept
    if col.entity is not None:
        return BindingQuality.entity_tagged
    return BindingQuality.weak


def discover_ingredient_candidates(conn, template: Template, catalog_source: str,
                                   *, roles: Iterable[str] = (),
                                   columns: list[_Col] | None = None) -> CandidateDiscoveryV1:
    # 3B.4 F5: when a frozen column snapshot is supplied (from the compiler context), discover over
    # EXACTLY those columns so the planner_input_hash covers the same data the selection consumed — a
    # fresh _load_columns under READ COMMITTED could differ. build_compiler_context loads via the
    # identical `_load_columns(conn, src, roles)`, so passing its snapshot is behaviour-neutral.
    cols = columns if columns is not None else _load_columns(conn, catalog_source, roles)
    resolved = {r.role: r for r in RESOLVED_NEED_METADATA.get(template.id, ())}
    out: dict[str, tuple[IngredientCandidateV1, ...]] = {}
    truncated = False
    total = 0
    for need in template.needs:
        rn = resolved.get(need.role)
        allowed = rn.allowed_source_grains if rn is not None else need.allowed_source_grains
        join_role = str(rn.join_role) if rn is not None else str(need.join_role or "")
        temporal_role = str(rn.temporal_role) if rn is not None else str(need.temporal_role or "")
        # tier-1 candidate columns: an exact concept match (the strongest single-catalog signal). Sorted
        # deterministically by object_ref so truncation is stable.
        matches = sorted((c for c in cols if c.concept == need.concept), key=lambda c: c.object_ref)
        if len(matches) > MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG:
            matches = matches[:MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG]
            truncated = True
        cands: list[IngredientCandidateV1] = []
        for col in matches:
            total += 1
            grain = object_grain(conn, catalog_source, table_of(col.object_ref))
            grain_ok = not allowed or (grain is not None and grain in allowed)
            safety = evaluate_binding_safety(col)
            reasons: list[ReasonCode] = []
            if safety is BindingSafety.unsafe:
                reasons.append(ReasonCode.binding_safety_rejected)
            if not grain_ok:
                reasons.append(ReasonCode.grain_incompatible)
            eligible = safety is BindingSafety.safe and grain_ok
            cands.append(IngredientCandidateV1(
                recipe_id=template.id, need_role=need.role, concept=need.concept,
                required_grains=tuple(allowed), join_role=join_role, temporal_role=temporal_role,
                catalog_source=catalog_source, object_ref=col.object_ref, actual_source_grain=grain,
                binding_quality=_quality(col, need.concept, grain_ok), eligible=eligible,
                safety=safety, reason_codes=tuple(reasons)))
        out[need.role] = tuple(cands)
    return CandidateDiscoveryV1(candidates=out, candidate_columns_truncated=truncated,
                                total_candidate_columns_considered=total)
