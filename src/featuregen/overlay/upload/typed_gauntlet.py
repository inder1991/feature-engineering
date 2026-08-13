"""SE-9 — the typed post-binding gauntlet: measures checked as measures, keys as keys.

The legacy `_validate_idea` receives a bag of physical refs and checks every one as a measure
(`measure_refs = all derives pairs` — the verified defect). This validator receives a BOUND
semantic candidate — typed operands, per-role verdicts, the compiled temporal state — and
computes the design tri-state from ACTUAL unmet conditions:

* ``refused``      — a hard safety truth: a bound column IS the prediction target, or the
  binding itself is blocked (the fold's contradictions carried through);
* ``not_bindable`` — the candidate never bound (ambiguous / missing operands): there is nothing
  to validate and the binding state is the whole story — validation must not overwrite it;
* ``needs_external_validation`` — bound, safe, with NAMED outstanding work: runtime checks
  (identifier uniqueness, event-history verification) and setup/confirmation items riding the
  bound verdicts. Each requirement is typed, versioned, and carries the ref it applies to;
* ``design_checked`` — bound, safe, nothing outstanding. STILL not "proven useful" (predictive
  evidence is a different axis, deliberately outside this program).

Pure and total: no I/O, no LLM, no clock. The verdict pins the gauntlet version AND the
authority-policy hash, so a stored validation is reproducible evidence, never a vibe.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload import semantic_eligibility_reasons as R

TYPED_GAUNTLET_VERSION = "typed-gauntlet@1"

#: Verdict reason codes that ride a BOUND operand and become REQUIREMENTS (outstanding work),
#: never refusals — the staged-floor rule and invariant 6, carried into validation.
_REQUIREMENT_CODES = frozenset({
    R.PROPOSED_METADATA_ONLY, R.SEMANTIC_AUTHORITY_INSUFFICIENT,
    R.CURRENCY_POLICY_MISSING, R.RELATIONSHIP_REQUIRED,
    R.STATUS_POLICY_UNRESOLVED,                     # C3: named setup work, never silent
})


@dataclass(frozen=True, slots=True)
class TypedRequirementV1:
    code: str
    family: str                           # the reasons vocabulary's product family
    object_ref: str                       # the ref the check applies to ("" = candidate-level)
    detail: str
    schema_version: str = TYPED_GAUNTLET_VERSION


@dataclass(frozen=True, slots=True)
class TypedValidationV1:
    status: str                           # refused | not_bindable | needs_external_validation | design_checked
    refusals: tuple[dict, ...]
    requirements: tuple[TypedRequirementV1, ...]
    gauntlet_version: str
    policy_content_hash: str


def validate_candidate(candidate, *, target_ref: str | None = None) -> TypedValidationV1:
    """Validate one bound semantic candidate (a `V2RecipeCandidateV1`-shaped object)."""
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash

    policy_hash = authority_matrix_hash()

    if candidate.binding_state in ("ambiguous", "missing"):
        return TypedValidationV1("not_bindable", (), (), TYPED_GAUNTLET_VERSION, policy_hash)

    if candidate.binding_state == "blocked":
        refusals = tuple(
            {"code": code, "object_ref": verdict.selected_ref or "", "role": verdict.role}
            for verdict in candidate.verdicts if verdict.status == "blocked"
            for code in verdict.reason_codes)
        return TypedValidationV1("refused", refusals, (), TYPED_GAUNTLET_VERSION, policy_hash)

    operands_by_role = {op.role: op for op in candidate.planning_request.operands}
    refusals: list[dict] = []
    requirements: list[TypedRequirementV1] = []

    for verdict in candidate.verdicts:
        if verdict.status != "bound" or not verdict.selected_ref:
            continue
        operand = operands_by_role.get(verdict.role)
        # HARD SAFETY: a bound column that IS the prediction target — physical-level leakage,
        # the one check only the caller's target_ref can make.
        if target_ref is not None and verdict.selected_ref == target_ref:
            refusals.append({"code": R.TARGET_LEAKAGE_BLOCKED,
                             "object_ref": verdict.selected_ref, "role": verdict.role})
            continue
        # TYPED runtime checks — by the operand's CLASS, never applied to everything.
        if operand is not None and operand.operand_class == "entity_key":
            requirements.append(TypedRequirementV1(
                code=R.IDENTIFIER_UNIQUENESS, family=R.reason_family(R.IDENTIFIER_UNIQUENESS),
                object_ref=verdict.selected_ref,
                detail="profile this key's uniqueness at the declared grain before execution"))
        if (operand is not None and operand.operand_class == "event_timestamp"
                and candidate.planning_request.temporal.anchor_kind == "event"):
            requirements.append(TypedRequirementV1(
                code=R.EVENT_HISTORY_VERIFICATION,
                family=R.reason_family(R.EVENT_HISTORY_VERIFICATION),
                object_ref=verdict.selected_ref,
                detail="verify this source carries event HISTORY deep enough for the window — "
                       "a current-only snapshot cannot support an event window"))
        # Outstanding confirmation/setup work riding the bound verdict (staged floors et al).
        for code in verdict.reason_codes:
            if code in _REQUIREMENT_CODES:
                requirements.append(TypedRequirementV1(
                    code=code, family=R.reason_family(code),
                    object_ref=verdict.selected_ref, detail=verdict.resolution))

    # C3: a load-bearing output policy the author never wrote — a ratio-shaped output
    # divides, so its zero-denominator behavior is part of the DESIGN, not a runtime nicety.
    output = candidate.planning_request.output
    agg_text = (f"{output.aggregation_over_entity} "
                f"{output.aggregation_over_time}").lower()
    ratio_like = (output.unit_kind in ("rate", "ratio", "share", "percentage")
                  or "ratio" in agg_text or "share" in agg_text)
    if ratio_like and not output.zero_denominator_policy.strip():
        requirements.append(TypedRequirementV1(
            code=R.OUTPUT_POLICY_INCOMPLETE,
            family=R.reason_family(R.OUTPUT_POLICY_INCOMPLETE),
            object_ref="",
            detail="a ratio-shaped output divides, and its authored contract does not say "
                   "what a zero denominator returns — author the zero_denominator_policy"))

    if candidate.temporal_blocker:
        requirements.append(TypedRequirementV1(
            code=R.TEMPORAL_POLICY_UNRESOLVED,
            family=R.reason_family(R.TEMPORAL_POLICY_UNRESOLVED),
            object_ref="", detail=candidate.temporal_blocker))

    # SE-8 steps 2+3: the FEATURE-LEVEL dataset decision rides the candidate, not any single
    # operand. An undeclared population and an unproven cross-dataset hop are named setup work —
    # candidate-level requirements with the story's own facts in the detail.
    story = getattr(candidate, "dataset_story", None)
    if story is not None:
        if R.POPULATION_DATASET_UNDECLARED in story.codes:
            requirements.append(TypedRequirementV1(
                code=R.POPULATION_DATASET_UNDECLARED,
                family=R.reason_family(R.POPULATION_DATASET_UNDECLARED),
                object_ref="",
                detail="no DECLARED-grain entity key anchors the population — confirm the "
                       "grain of the table that defines who this feature computes over"))
        if R.RELATIONSHIP_REQUIRED in story.codes:
            requirements.append(TypedRequirementV1(
                code=R.RELATIONSHIP_REQUIRED,
                family=R.reason_family(R.RELATIONSHIP_REQUIRED),
                object_ref="",
                detail="bound operands span "
                       f"{', '.join(story.dataset_tables)} — govern the relationship "
                       "(verify the join) before this computes as ONE feature"))

    if refusals:
        status = "refused"
    elif requirements:
        status = "needs_external_validation"
    else:
        status = "design_checked"
    return TypedValidationV1(status, tuple(refusals), tuple(requirements),
                             TYPED_GAUNTLET_VERSION, policy_hash)


__all__ = ["TYPED_GAUNTLET_VERSION", "TypedRequirementV1", "TypedValidationV1",
           "validate_candidate"]
