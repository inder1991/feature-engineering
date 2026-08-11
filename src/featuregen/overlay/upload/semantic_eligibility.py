"""SE-4 — the semantic-eligibility policy: the authority matrix as one pure, versioned fold.

``evaluate_operand(operand, capability)`` answers the plan's question — may THIS column serve
THIS role? — deterministically, over typed facts only, with EVERY applicable reason code
collected (never first-failure-wins) and a fixed precedence choosing the primary. The four
statuses, exactly the plan's:

* ``not_applicable`` — no semantic match at all (the concept is a different meaning);
* ``blocked``       — a KNOWN contradiction: wrong class, wrong shape, unproven economic role.
  Missing evidence is never blocked — missing and contradictory are different conditions;
* ``provisional``   — plausible but below the operand's suggestion-authority floor, or waiting
  on setup/check work. Carries the named human action;
* ``eligible``      — clears the floor with no contradiction.

**The authority matrix** is DATA (content-hashed into every verdict): evidence classes —
derived from the capability's ``producer/strength`` pins — mapped to what they may clear.
LLM proposals retrieve and rank but never clear a ``declared`` floor; display-only graph
values clear nothing; a conflict blocks. **Floors are computed but STAGED**: the verdict
records floor-below honestly as ``provisional``; whether a surface treats provisional as
undisplayable is the caller's rollout decision (SE-5 step 8 — the funnel gates it), never
this fold's.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.column_capabilities import ColumnCapabilityV1
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload.recipe_operand_policy import _CLASS_TYPE_FAMILIES
from featuregen.overlay.upload import semantic_eligibility_reasons as R

SEMANTIC_AUTHORITY_POLICY_VERSION = "semantic-authority@1"

#: The §7 matrix, as data. Keys are evidence classes; values say what the class may CLEAR.
#: ``suggestion_at_declared`` — may it satisfy an operand whose suggestion floor is `declared`?
#: (Every V2 operand's floor is `declared` today — measured.) Anything absent here is unknown
#: and clears nothing (fail-closed).
AUTHORITY_MATRIX: dict[str, dict[str, bool]] = {
    "human/confirmed":  {"suggestion_at_declared": True},
    "source/attested":  {"suggestion_at_declared": True},
    "source/declared":  {"suggestion_at_declared": True},
    "human/proposed":   {"suggestion_at_declared": False},   # a proposal is a proposal
    "llm/proposed":     {"suggestion_at_declared": False},
    "graph_hint":       {"suggestion_at_declared": False},   # display-only current value
    "absent":           {"suggestion_at_declared": False},
}


def authority_matrix_hash() -> str:
    from featuregen.overlay.upload.field_resolution import canonical_hash

    return canonical_hash({
        "version": SEMANTIC_AUTHORITY_POLICY_VERSION,
        "matrix": {k: dict(sorted(v.items())) for k, v in sorted(AUTHORITY_MATRIX.items())},
        "precedence": list(R.REASON_PRECEDENCE),
    })


#: The named human/operator action per primary code — a refusal nobody can act on is a dead
#: end, not governance.
_RESOLUTIONS: dict[str, str] = {
    R.CONCEPT_MISMATCH: "this column means something else — no action makes it serve this role",
    R.OPERAND_CLASS_MISMATCH: "the concept cannot serve this operand class — bind a column "
                              "whose meaning can",
    R.IDENTIFIER_NOT_A_MEASURE: "an identifier can serve key/grouping/distinct-count roles, "
                                "never a quantity — bind a measure column",
    R.TYPE_INCOMPATIBLE: "the declared type cannot carry this role — bind a column of the "
                         "right shape or correct the declared type",
    R.ECONOMIC_ROLE_UNPROVEN: "a human confirms this column's economic role, or the binding "
                              "stays blocked — concept compatibility never substitutes",
    R.PROPOSED_METADATA_ONLY: "confirm the AI-proposed concept in the Governance screen's "
                              "concept-confirmation queue — a proposal retrieves, it never "
                              "clears a declared floor",
    R.SEMANTIC_AUTHORITY_INSUFFICIENT: "raise the value's authority: confirm it, or attest it "
                                       "at the source",
    R.SEMANTIC_CONFLICT: "active evidence disagrees — resolve the conflict before this value "
                         "can clear anything",
    R.RELATIONSHIP_REQUIRED: "govern the relationship this operand rides (verify the join / "
                             "realization), then regenerate",
    R.CURRENCY_POLICY_MISSING: "declare the currency (fixed code or per-row column) — money "
                               "is refused until it is known",
}


@dataclass(frozen=True, slots=True)
class OperandEligibilityVerdictV1:
    """One ``(operand, capability)`` decision — §6.5's shape, pure-fold fields."""

    operand_role: str
    object_ref: str
    status: str                           # eligible | provisional | blocked | not_applicable
    reason_codes: tuple[str, ...]
    primary_reason_code: str | None
    primary_family: str | None
    authority_floor_required: str
    authority_observed: str
    missing_checks: tuple[str, ...]       # the needs_setup / needs_data_check subset
    resolution: str
    policy_version: str
    policy_content_hash: str


def _primary(codes: list[str]) -> str | None:
    for code in R.REASON_PRECEDENCE:
        if code in codes:
            return code
    return codes[0] if codes else None


def evaluate_operand(operand: RequiredOperandV1,
                     capability: ColumnCapabilityV1) -> OperandEligibilityVerdictV1:
    codes: list[str] = []
    blocked = False

    # 1. Controlled meaning: the concept must MATCH (primary or a declared alternative) —
    #    anything else is a different meaning entirely, not a lesser one.
    accepted = {operand.concept, *operand.alternative_concepts}
    if capability.concept not in accepted:
        return _verdict(operand, capability, "not_applicable", [R.CONCEPT_MISMATCH])

    # 2. Structural contradictions (KNOWN incompatibility — never missing evidence).
    if operand.operand_class == "measure" and capability.identifier_like:
        codes.append(R.IDENTIFIER_NOT_A_MEASURE)
        blocked = True
    elif (capability.possible_operand_classes
          and operand.operand_class not in capability.possible_operand_classes):
        codes.append(R.OPERAND_CLASS_MISMATCH)
        blocked = True
    allowed_families = _CLASS_TYPE_FAMILIES.get(operand.operand_class)
    if allowed_families and capability.type_family not in ("unknown", "other") \
            and capability.type_family not in allowed_families:
        codes.append(R.TYPE_INCOMPATIBLE)
        blocked = True

    # 3. Economic role: binds ONLY over governed evidence matching it (the binder's own law,
    #    folded here so the two paths cannot diverge).
    if operand.economic_role and capability.economic_role != operand.economic_role:
        codes.append(R.ECONOMIC_ROLE_UNPROVEN)
        blocked = True

    # 4. The authority floor (computed ALWAYS; staged by callers). Unknown classes fail closed.
    observed = capability.concept_authority
    clears = AUTHORITY_MATRIX.get(observed, {}).get("suggestion_at_declared", False)
    if not clears and not blocked:
        codes.append(R.PROPOSED_METADATA_ONLY if observed == "llm/proposed"
                     else R.SEMANTIC_AUTHORITY_INSUFFICIENT)

    # 5. Setup/check work this fold can already see.
    if operand.currency_expectation and not capability.currency:
        codes.append(R.CURRENCY_POLICY_MISSING)
    if operand.relationship_requirement \
            and "relationship_state_absent" in capability.missing_context:
        codes.append(R.RELATIONSHIP_REQUIRED)

    if blocked:
        status = "blocked"
    elif codes:
        status = "provisional"
    else:
        status = "eligible"
    return _verdict(operand, capability, status, codes)


def _verdict(operand: RequiredOperandV1, capability: ColumnCapabilityV1,
             status: str, codes: list[str]) -> OperandEligibilityVerdictV1:
    primary = _primary(codes)
    checks = tuple(code for code in codes
                   if R.reason_family(code) in ("needs_setup", "needs_data_check"))
    return OperandEligibilityVerdictV1(
        operand_role=operand.role,
        object_ref=capability.object_ref,
        status=status,
        reason_codes=tuple(codes),
        primary_reason_code=primary,
        primary_family=R.reason_family(primary) if primary else None,
        authority_floor_required=operand.suggestion_authority,
        authority_observed=capability.concept_authority,
        missing_checks=checks,
        resolution=_RESOLUTIONS.get(primary, "") if primary else "",
        policy_version=SEMANTIC_AUTHORITY_POLICY_VERSION,
        policy_content_hash=authority_matrix_hash())


__all__ = ["AUTHORITY_MATRIX", "OperandEligibilityVerdictV1",
           "SEMANTIC_AUTHORITY_POLICY_VERSION", "authority_matrix_hash", "evaluate_operand"]
