"""A5 — LOGICAL resolution, without physical evidence.

The planner's existing lane answers a PHYSICAL question: can this path be executed? Its honest
answer for an AI-proposed cross-catalog link is today's G3 refusal — the governed bridge hop
carries no directional realization, so its cardinality is unavailable and any measure staged
across it fails ``physical_cardinality_unavailable`` (``declarations.py``, gated by
``allow_provisional_bridge_cardinality=False``). That refusal is CORRECT and this module does not
touch it.

But the card and formula rungs do not ask that question. R9 says logical identity is feature
MEANING — the canonical definition, the operation, the typed operand bindings with their governed
semantic revisions, the ordered output grain, the SELECTED parameters, the ordered logical
relationship path, the formula policies, and R14's temporal join semantics. NOTHING in that list
is physical. So a link nobody has realized still has a complete, hashable meaning, and this module
resolves it: it reads a source→target-RESOLVED :class:`BindingPlanV1` and the request that
produced it, and projects :class:`LogicalFeaturePlanV2`. It never reads a cardinality, a
realization revision, a contract status or a physical read set.

**The path carries composite keys INTACT.** ``BindingPathSegmentV1`` now carries each crossing's
COMPLETE ordered endpoint tuples (A5, ``assembly.rollup_bridges``), and this module refuses a
governed-bridge segment that does not — a single-pair segment for a composite link is exactly the
defect the endpoint tuples exist to close, and inferring the missing members would re-open it.

**R14: temporal semantics are DECLARED, never defaulted.** ``temporal_semantics`` is an explicit
per-crossing input. A crossing the caller declares nothing for keeps its endpoints on
:attr:`LogicalResolutionV1.path` — the complete ordered path, with ``temporal_semantics=None`` —
and mints a :data:`TEMPORAL_JOIN_POLICY_MISSING` absence, which is exactly what the consuming
layer refuses PREVIEW on (the owner's matrix keeps FORMULA available: "missing temporal policy →
Formula: Allow, Preview: Block"). It never enters the digest-bearing ``relationship_path``,
because :class:`LogicalRelationshipSegmentV1` refuses to be built without a declared meaning and
this module will not fabricate one to satisfy it.

That split has one sharp edge, and it is closed structurally rather than by convention: two plans
differing ONLY in an undeclared crossing would share a ``logical_digest``. So a resolution also
carries :attr:`LogicalResolutionV1.plan_variant_address` — ``H(logical_digest | the COMPLETE
ordered path)``, the plan's §10/C3 ``served_plan_variant_id`` material — which separates them
whether or not their temporal meaning has been declared. ``is_complete`` says, in one property,
whether anything was left undeclared.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.feature_planning_contracts import FeaturePlanningRequestV1
from featuregen.overlay.upload.field_resolution import current_resolution_pins
from featuregen.overlay.upload.object_ref import parse_ref, qualify_object_ref
from featuregen.overlay.upload.planner.contracts import (
    BindingPathSegmentV1,
    BindingPlanningResultV1,
    BindingPlanV1,
    CandidateRole,
    PathResolutionStatus,
    SegmentKind,
)
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    LogicalFeaturePlanV2,
    LogicalOperandBindingV1,
    LogicalPlanProvenanceV1,
    LogicalRelationshipSegmentV1,
    LogicalTemporalJoinSemanticsV1,
    logical_digest,
)

# R14's consuming-layer refusal has ONE spelling, and A1 already registered it: the reason
# vocabulary owns it (`semantic_eligibility_reasons.TEMPORAL_JOIN_POLICY_MISSING`), the reason
# FAMILY maps it, and `action_dispositions` carries its disposition row. It is IMPORTED and
# re-exported here — never re-declared — by the precedent that vocabulary file states in its own
# comment and that `planner/physical_plan_v1` follows for `ALLOCATION_POLICY_REQUIRED`: a second
# module-local literal is a second spelling waiting to drift.
from featuregen.overlay.upload.semantic_eligibility_reasons import TEMPORAL_JOIN_POLICY_MISSING

__all__ = [
    "BRIDGE_ENDPOINT_TUPLES_MISSING",
    "CANONICAL_DEFINITION_REVISION_MISSING",
    "GOVERNED_SEMANTIC_REVISION_MISSING",
    "INTRA_CATALOG_REALIZATION_NOT_PROJECTED",
    "LOGICAL_PATH_NOT_RESOLVED",
    "LogicalResolutionAbsenceV1",
    "LogicalResolutionRefused",
    "LogicalResolutionV1",
    "OUTPUT_GRAIN_UNRESOLVED",
    "PLAN_VARIANT_ADDRESS_CONTRACT",
    "ResolvedRelationshipSegmentV1",
    "TEMPORAL_JOIN_POLICY_MISSING",
    "grain_refs_from_logical_plan",
    "resolve_logical_plan",
    "select_logical_plan_candidate",
    "semantic_revisions_for_plan",
]

#: An intra-catalog realization segment is a real relationship hop, but the segment carries only
#: the realization's REF — never its endpoint columns — so projecting it would mean inventing the
#: columns it joins on. Named, not silently dropped: a consumer can see that the logical path it
#: holds is shorter than the physical path it came from. (Closing this needs the realization's
#: endpoints on the segment; deliberately out of A5's scope.)
INTRA_CATALOG_REALIZATION_NOT_PROJECTED = "INTRA_CATALOG_REALIZATION_NOT_PROJECTED"

#: Refusal codes — a resolution that cannot be built HONESTLY is refused, never approximated.
LOGICAL_PATH_NOT_RESOLVED = "LOGICAL_PATH_NOT_RESOLVED"
OUTPUT_GRAIN_UNRESOLVED = "OUTPUT_GRAIN_UNRESOLVED"
GOVERNED_SEMANTIC_REVISION_MISSING = "GOVERNED_SEMANTIC_REVISION_MISSING"
BRIDGE_ENDPOINT_TUPLES_MISSING = "BRIDGE_ENDPOINT_TUPLES_MISSING"
CANONICAL_DEFINITION_REVISION_MISSING = "CANONICAL_DEFINITION_REVISION_MISSING"

PLAN_VARIANT_ADDRESS_CONTRACT = "logical_plan_variant_address_v1"
_SEGMENT_CONTRACT = "resolved_relationship_segment_v1"


class LogicalResolutionRefused(ValueError):
    """A logical plan that cannot be resolved honestly. ``code`` is the typed reason."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class LogicalResolutionAbsenceV1:
    """ONE thing the resolution could not declare, named rather than defaulted.

    ``subject`` addresses what the absence is about (a bridge fact key, a realization ref) so a
    consuming layer can act on the exact crossing instead of on the whole plan."""

    code: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class ResolvedRelationshipSegmentV1:
    """One crossing of the COMPLETE ordered logical path.

    Both endpoints carry their FULL ordered member tuples as canonical logical refs, paired
    positionally. ``temporal_semantics`` is ``None`` when nobody declared any — the honest
    absence, never a fabricated strategy."""

    bridge_fact_key: str
    left_endpoint_refs: tuple[str, ...]
    right_endpoint_refs: tuple[str, ...]
    temporal_semantics: LogicalTemporalJoinSemanticsV1 | None

    def address_payload(self) -> dict[str, Any]:
        """The segment's contribution to the plan-variant address — endpoints ALWAYS, temporal
        meaning when declared. A segment whose meaning is undeclared still addresses its own
        columns, which is what keeps two undeclared paths apart."""
        return {
            "contract": _SEGMENT_CONTRACT,
            "bridge_fact_key": self.bridge_fact_key,
            "left_endpoint_refs": list(self.left_endpoint_refs),
            "right_endpoint_refs": list(self.right_endpoint_refs),
            "temporal_semantics": (None if self.temporal_semantics is None
                                   else self.temporal_semantics.identity_payload()),
        }


@dataclass(frozen=True, slots=True)
class LogicalResolutionV1:
    """A5's output: the R9 plan, its digest, the COMPLETE ordered path, and every named absence."""

    plan: LogicalFeaturePlanV2
    logical_digest: str
    path: tuple[ResolvedRelationshipSegmentV1, ...]
    absences: tuple[LogicalResolutionAbsenceV1, ...]
    plan_variant_address: str

    @property
    def is_complete(self) -> bool:
        """True when the plan declares everything its path traverses. A resolution that is NOT
        complete is still a real logical plan — the formula rung consumes it — but its digest
        covers only the declared crossings, so identity beyond this variant address is not
        claimed."""
        return not self.absences


def select_logical_plan_candidate(result: BindingPlanningResultV1) -> BindingPlanV1 | None:
    """The candidate a logical resolution is ABOUT — and the whole point of A5 is WHICH question
    picks it.

    ``governed_lens._selected_resolved_plan`` selects on ``contract_result_status is resolved``:
    a PHYSICAL verdict, which is why an unrealized bridge yields no option at all. This selects on
    the PATH resolving source→target and nothing else, so the G3 refusal — a compiled contract
    that failed ``physical_cardinality_unavailable``, or a plan never compiled at all — is
    irrelevant here, exactly as R9 says it should be.

    Precedence, deterministic: the assembler's own ``selected`` role, else the best
    ``preference_rank``, with ``physical_plan_id`` as the final tie-break (the same tie-break
    ``rank_and_classify`` uses). ``None`` when no path resolved — there is no meaning to resolve
    for a request whose relationship was never assembled."""
    crossings = [p for p in result.candidate_plans
                 if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved]
    if not crossings:
        return None
    return min(crossings, key=lambda p: (0 if p.candidate_role is CandidateRole.selected else 1,
                                         p.preference_rank if p.preference_rank >= 0 else 1 << 30,
                                         p.physical_plan_id))


def semantic_revisions_for_plan(conn, plan: BindingPlanV1) -> dict[tuple[str, str], str]:
    """The governed semantic revision of every column this plan binds, keyed
    ``(catalog_source, object_ref)``.

    ONE batched read through the SAME governed authority the lens's role bindings use
    (``field_resolution.current_resolution_pins`` over the ``concept`` field), so "under which
    governed revision does this column mean what the operand needs?" has one answer in the
    platform rather than two. The revision id is the winning evidence row's ``evidence_id`` — the
    exact fact the current meaning stands on. A column with no active concept evidence is ABSENT
    from the mapping (never an empty string): :func:`resolve_logical_plan` refuses on it rather
    than binding an operand to a meaning nobody governs."""
    pairs = [(b.bound_catalog_source, b.bound_object_ref) for b in plan.ingredient_bindings]
    refs = list(dict.fromkeys(qualify_object_ref(catalog, ref) for catalog, ref in pairs))
    pins = current_resolution_pins(conn, logical_refs=refs, fields=("concept",))
    out: dict[tuple[str, str], str] = {}
    for catalog, ref in pairs:
        pin = pins.get((qualify_object_ref(catalog, ref), "concept"))
        if pin is not None and pin.evidence_id:
            out[(catalog, ref)] = pin.evidence_id
    return out


def grain_refs_from_logical_plan(plan: LogicalFeaturePlanV2) -> tuple[tuple[str, str], ...]:
    """The draft worker's ``grain_refs`` — ``(catalog_source, schema.table.column)`` pairs — from
    the logical plan's ORDERED output grain.

    The shape is the platform's existing one: ``FeatureIdea.grain_refs`` and
    ``governed_lens.fold_governed_binding_plan``'s ``grain_refs`` both carry exactly this pair,
    and ``key_entities_for`` reads it directly. Order is the grain's own order, which is identity
    — never sorted. This is the derivation B3 wires into the draft worker; A5 builds and tests it
    and rewires nothing."""
    out: list[tuple[str, str]] = []
    for ref in plan.output_grain_key_refs:
        source, schema, table, column = parse_ref(ref)
        out.append((source, f"{schema}.{table}.{column}"))
    return tuple(out)


def _qualified(catalog: str | None, ref: str) -> str:
    if not catalog:
        raise LogicalResolutionRefused(
            f"crossing endpoint {ref!r} names no catalog — a logical ref cannot be built from it",
            code=BRIDGE_ENDPOINT_TUPLES_MISSING)
    return qualify_object_ref(catalog, ref)


def _crossing(segment: BindingPathSegmentV1,
              temporal_semantics: Mapping[str, LogicalTemporalJoinSemanticsV1],
              ) -> ResolvedRelationshipSegmentV1:
    """ONE governed-bridge segment as a logical crossing — refusing every shortcut.

    The endpoint tuples are taken VERBATIM from the segment. A segment carrying none is refused
    rather than degraded to its thin first-member fields: for a single-member link the two agree,
    so nothing is gained, and for a composite one the degradation is precisely the single-pair
    join A5 exists to stop."""
    if not segment.bridge_from_member_refs or not segment.bridge_to_member_refs:
        raise LogicalResolutionRefused(
            f"governed_bridge segment {segment.bridge_fact_key!r} carries no ordered endpoint "
            "member tuples; a composite link degraded to its first member is a DIFFERENT join, so "
            "the missing members are refused rather than inferred",
            code=BRIDGE_ENDPOINT_TUPLES_MISSING)
    return ResolvedRelationshipSegmentV1(
        bridge_fact_key=segment.bridge_fact_key or "",
        left_endpoint_refs=tuple(
            _qualified(segment.bridge_from_catalog_source, ref)
            for ref in segment.bridge_from_member_refs),
        right_endpoint_refs=tuple(
            _qualified(segment.bridge_to_catalog_source, ref)
            for ref in segment.bridge_to_member_refs),
        temporal_semantics=temporal_semantics.get(segment.bridge_fact_key or ""))


def _formula_policy_identities(request: FeaturePlanningRequestV1) -> tuple[tuple[str, str], ...]:
    """The request's own governed policy identities, by the SAME pooling law
    ``recipe_review_validity._policy_refs`` reads (eligibility policies + per-operand status
    policies), plus the exact formula reference when the request carries one.

    The parameter half of that pooling is deliberately NOT repeated here: a governed-policy
    parameter's VALUE is its policy ref, so it is already identity-bearing through R9's SELECTED
    parameter pairs, and hashing it twice would say nothing new. Roles are distinct and
    deterministic, as ``LogicalFeaturePlanV2`` requires."""
    identities: list[tuple[str, str]] = []
    for index, ref in enumerate(request.eligibility.policy_refs):
        identities.append((f"eligibility_policy[{index}]", ref))
    for operand in request.operands:
        if operand.status_policy_ref:
            identities.append((f"operand_status_policy:{operand.role}", operand.status_policy_ref))
    if request.formula is not None:
        identities.append((
            "formula",
            f"{request.formula.formula_schema_version}|{request.formula.expectation_ref}"
            f"|{request.formula.result_class}"))
    return tuple(identities)


def resolve_logical_plan(
    *,
    request: FeaturePlanningRequestV1,
    plan: BindingPlanV1,
    semantic_revisions: Mapping[tuple[str, str], str],
    temporal_semantics: Mapping[str, LogicalTemporalJoinSemanticsV1] | None = None,
    provenance: LogicalPlanProvenanceV1 | None = None,
) -> LogicalResolutionV1:
    """Resolve one request+plan pair into its LOGICAL identity. Pure; reads no database.

    ``semantic_revisions`` maps ``(catalog_source, object_ref)`` to the governed semantic revision
    each bound column means under (see :func:`semantic_revisions_for_plan`).
    ``temporal_semantics`` maps a crossing's ``bridge_fact_key`` to its DECLARED R14 semantics;
    a crossing absent from it keeps ``None`` and mints a
    :data:`TEMPORAL_JOIN_POLICY_MISSING` absence.

    Refuses (:class:`LogicalResolutionRefused`) rather than approximating: a path that did not
    resolve source→target, a plan with no governed output grain, a request with a blank canonical
    definition revision, an operand bound to a column no governed semantic revision covers, and a
    crossing missing its ordered endpoint tuples.

    Physical facts are not read at ALL — not the contract status, not a cardinality, not a
    realization revision, not the physical read set. An AI-proposed link nobody has realized
    resolves exactly as a realized one does."""
    if plan.path_resolution_status is not PathResolutionStatus.source_to_target_resolved:
        raise LogicalResolutionRefused(
            f"plan {plan.physical_plan_id} is {plan.path_resolution_status.value}, not "
            "source_to_target_resolved: there is no relationship to give a logical meaning",
            code=LOGICAL_PATH_NOT_RESOLVED)
    if plan.output_grain_ref is None:
        raise LogicalResolutionRefused(
            f"plan {plan.physical_plan_id} reports no governed output grain; the ordered output "
            "grain is R9 identity material and is never guessed from a table name",
            code=OUTPUT_GRAIN_UNRESOLVED)
    if not request.source_revision.strip():
        # `source_revision` is a required field of the request and every production builder
        # populates it, so a blank one is a caller defect — refused under its own name rather than
        # silently substituted with the content hash. Two different addresses for "which revision
        # of the definition" is how a canonical identity quietly acquires a second spelling.
        raise LogicalResolutionRefused(
            f"request {request.source_definition_id!r} carries a blank source_revision; R9's "
            "canonical definition REVISION is identity material and has no substitute",
            code=CANONICAL_DEFINITION_REVISION_MISSING)

    bindings: list[LogicalOperandBindingV1] = []
    for binding in plan.ingredient_bindings:
        key = (binding.bound_catalog_source, binding.bound_object_ref)
        revision = semantic_revisions.get(key)
        if not revision:
            raise LogicalResolutionRefused(
                f"operand {binding.need_role!r} binds {key[0]}::{key[1]} but no governed semantic "
                "revision covers it; an operand bound to a meaning nobody governs has no logical "
                "identity to record",
                code=GOVERNED_SEMANTIC_REVISION_MISSING)
        bindings.append(LogicalOperandBindingV1(
            role=binding.need_role,
            logical_column_ref=qualify_object_ref(*key),
            governed_semantic_revision_id=revision))

    declared = dict(temporal_semantics or {})
    path: list[ResolvedRelationshipSegmentV1] = []
    absences: list[LogicalResolutionAbsenceV1] = []
    for segment in plan.path_segments:
        if segment.segment_kind is SegmentKind.governed_bridge:
            crossing = _crossing(segment, declared)
            path.append(crossing)
            if crossing.temporal_semantics is None:
                absences.append(LogicalResolutionAbsenceV1(
                    code=TEMPORAL_JOIN_POLICY_MISSING,
                    subject=crossing.bridge_fact_key,
                    detail="no LogicalTemporalJoinSemanticsV1 was declared for this crossing; the "
                           "platform never applies a temporal policy nobody assessed"))
        elif segment.segment_kind is SegmentKind.intra_catalog_realization:
            absences.append(LogicalResolutionAbsenceV1(
                code=INTRA_CATALOG_REALIZATION_NOT_PROJECTED,
                subject=segment.realization_ref or "",
                detail="the segment carries the realization's ref but not the columns it joins "
                       "on, so this hop is absent from the logical relationship path"))

    resolved_plan = LogicalFeaturePlanV2(
        canonical_definition_content_hash=request.source_content_hash,
        canonical_definition_revision_id=request.source_revision,
        operation=request.computation_kind,
        operand_bindings=tuple(bindings),
        output_grain_key_refs=(qualify_object_ref(*plan.output_grain_ref),),
        selected_parameters=tuple(request.parameter_values),
        relationship_path=tuple(
            LogicalRelationshipSegmentV1(
                left_endpoint_refs=crossing.left_endpoint_refs,
                right_endpoint_refs=crossing.right_endpoint_refs,
                temporal_semantics=crossing.temporal_semantics)
            for crossing in path if crossing.temporal_semantics is not None),
        formula_policy_identities=_formula_policy_identities(request),
        provenance=provenance or LogicalPlanProvenanceV1())
    digest = logical_digest(resolved_plan)
    return LogicalResolutionV1(
        plan=resolved_plan,
        logical_digest=digest,
        path=tuple(path),
        absences=tuple(absences),
        plan_variant_address=materialize_hash({
            "contract": PLAN_VARIANT_ADDRESS_CONTRACT,
            "logical_digest": digest,
            "path": [crossing.address_payload() for crossing in path],
        }))
