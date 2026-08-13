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

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.column_capabilities import ColumnCapabilityV1
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload.recipe_operand_policy import _CLASS_TYPE_FAMILIES

SEMANTIC_AUTHORITY_POLICY_VERSION = "semantic-authority@1"

#: The §7 matrix, as data — FOUR use columns per evidence class (remediation C2). Keys are
#: evidence classes; values say what the class may CLEAR at each rung of use:
#:
#: * ``retrieval`` — may the value even be SHOWN/shortlisted as context? (Everything with a
#:   value retrieves — retrieval is honesty, not authority; only true absence retrieves
#:   nothing.)
#: * ``suggestion_at_declared`` — may it satisfy an operand whose suggestion floor is
#:   `declared`? (Every V2 operand's floor is `declared` today — measured.)
#: * ``authoring`` — may a governed CONTRACT be authored over it (create_contract /
#:   author_formula)? Same bar as suggestion today: a proposal never underwrites a contract.
#: * ``execution_at_governed`` — may a MATERIALIZATION execute over it? Only a human
#:   confirmation or a source attestation clears execution; a source DECLARATION is enough
#:   to suggest and author against, but running a pipeline over it requires the stronger
#:   fact. ``llm/proposed`` NEVER clears execution.
#:
#: Anything absent here is unknown and clears nothing (fail-closed). The matrix content is
#: part of ``authority_matrix_hash`` — growing it MOVES the policy hash by design (frozen
#: options pinned to the old hash surface ACTIVATION_STATE_DRIFTED and regenerate).
AUTHORITY_MATRIX: dict[str, dict[str, bool]] = {
    "human/confirmed":  {"retrieval": True, "suggestion_at_declared": True,
                         "authoring": True, "execution_at_governed": True},
    "source/attested":  {"retrieval": True, "suggestion_at_declared": True,
                         "authoring": True, "execution_at_governed": True},
    "source/declared":  {"retrieval": True, "suggestion_at_declared": True,
                         "authoring": True, "execution_at_governed": False},
    "human/proposed":   {"retrieval": True, "suggestion_at_declared": False,
                         "authoring": False, "execution_at_governed": False},
    "llm/proposed":     {"retrieval": True, "suggestion_at_declared": False,
                         "authoring": False, "execution_at_governed": False},
    "graph_hint":       {"retrieval": True, "suggestion_at_declared": False,
                         "authoring": False, "execution_at_governed": False},
    "absent":           {"retrieval": False, "suggestion_at_declared": False,
                         "authoring": False, "execution_at_governed": False},
}


def clears(authority: str, use: str) -> bool:
    """May evidence of class ``authority`` clear the ``use`` rung? Fail-closed on both axes:
    an unknown class clears nothing, an unknown use is never cleared."""
    return AUTHORITY_MATRIX.get(authority, {}).get(use, False)


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
    R.TARGET_LEAKAGE_BLOCKED: "this column carries the target (or a target-defining flag) — "
                              "reading it is leakage; no confirmation can make it an input",
    R.PROTECTED_CHARACTERISTIC_BLOCKED: "a protected characteristic / special-category value "
                                        "is never a feature input — fair-lending and GDPR "
                                        "hard block",
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
    R.PERSONAL_DATA_POLICY_REQUIRED: "this column is personal data and no active use policy "
                                     "licenses it — a governance owner declares the purpose "
                                     "under Governance -> Data-use policies",
    R.SOURCE_GRAIN_MISMATCH: "this operand requires a differently-shaped source (event rows "
                             "vs point-in-time snapshots) — bind a column from a table of the "
                             "declared shape the recipe expects",
    R.UNIT_INCOMPATIBLE: "this column carries a currency (a monetary quantity) but the operand "
                         "expects a non-monetary unit — bind a column of the right unit",
    R.ADDITIVITY_INCOMPATIBLE: "this operation SUMS a value whose declared additivity cannot "
                               "be summed this way (a stock/ratio is not a flow) — change the "
                               "operation or bind an additive measure",
    R.STATUS_POLICY_UNRESOLVED: "this recipe reads a governed status policy no resolver serves "
                                "yet — declare the source's status meanings, or accept the "
                                "unfiltered read at your own review",
    R.HISTORY_DEPTH_INSUFFICIENT: "this variant's window looks back further than the source "
                                  "declares it keeps — reduce the window (a shorter variant "
                                  "of the same recipe may already serve), or extend the "
                                  "source's declared history",
    R.SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW: "this table is declared a SNAPSHOT — it cannot "
                                            "anchor an event window; bind an event source, or "
                                            "correct the table's classification",
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
    # C4: the ACTIVE policy revisions that license this column's personal data (empty when
    # none needed or none granted) — provenance the served idea carries forward.
    personal_data_policy_revision_ids: tuple[str, ...] = ()
    # C5: capability axes this evaluation NEEDED and could not read (the fact is absent, not
    # failing) — the tri-state family fold's "missing" input. Never a refusal by itself.
    facts_absent: tuple[str, ...] = ()


def _primary(codes: list[str]) -> str | None:
    for code in R.REASON_PRECEDENCE:
        if code in codes:
            return code
    return codes[0] if codes else None


def _grain_axis(grain: str) -> str | None:
    """The coarse row-shape axis of an authored source-grain name — the half the catalog can
    verify. ``None`` = a shape (interval/report/pull/...) no catalog fact can check yet."""
    if "snapshot" in grain:
        return "snapshot"
    if grain == "transaction" or grain.endswith("_event"):
        return "event"
    return None


def _absent_axes(operand: RequiredOperandV1, capability: ColumnCapabilityV1,
                 output) -> tuple[str, ...]:
    """C5: which capability axes THIS evaluation needed and could not read. Absence never
    refuses on its own — it makes the family unverifiable, which blocks DESIGN_CHECKED."""
    absent = []
    if operand.unit_expectation and not capability.currency:
        # The only unit fact the catalog carries today is a currency (a monetary-unit fact);
        # without one, no unit expectation is verifiable.
        absent.append("unit")
    if output is not None and operand.operand_class == "measure":
        agg = f"{output.aggregation_over_entity} {output.aggregation_over_time}".lower()
        if "sum" in agg and not capability.additivity:
            absent.append("additivity")
    if operand.allowed_source_grains:
        axes = {_grain_axis(g) for g in operand.allowed_source_grains}
        if (None not in axes and len(axes) == 1
                and capability.table_event_or_snapshot not in ("event", "snapshot")):
            absent.append("table_shape")
    return tuple(absent)


def evaluate_operand(operand: RequiredOperandV1,
                     capability: ColumnCapabilityV1, *,
                     output=None, temporal_anchor: str = "",
                     window_days: int | None = None) -> OperandEligibilityVerdictV1:
    """``output``/``temporal_anchor`` (C3, optional): the REQUEST-level context the
    additivity law needs — what operation consumes this measure, anchored how. Omitted by
    single-operand callers; the binder passes them so the check runs on every real fold."""
    codes: list[str] = []
    blocked = False

    # 0. The safety law FIRST (the legacy _safe_to_bind, folded): a leakage anchor or a
    #    protected/special-category concept is never a valid feature input — for any origin,
    #    at any authority, even when a definition is mis-authored to NEED such a concept.
    if capability.leakage_anchor:
        return _verdict(operand, capability, "blocked", [R.TARGET_LEAKAGE_BLOCKED])
    if capability.blocked_sensitivity:
        return _verdict(operand, capability, "blocked", [R.PROTECTED_CHARACTERISTIC_BLOCKED])

    # 1. Controlled meaning: the concept must MATCH — the exact name, a declared
    #    alternative, or (C7) a registered DESCENDANT whose is-a path reaches a wanted name
    #    (a specialized flow IS the flow it specializes). A namespace MATE is a join-candidacy
    #    peer, never a meaning-substitute — it stays CONCEPT_MISMATCH by design.
    accepted = {operand.concept, *operand.alternative_concepts}
    if capability.concept not in accepted:
        from featuregen.overlay.upload.concepts import concept_path

        if not accepted.intersection(concept_path(capability.concept)):
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

    # 2b. The dataset axis (deeper SE-8): a table DECLARED (or better) to be a snapshot
    #     cannot anchor an event window — the plan's own rule, with the matrix deciding what
    #     counts as declared-or-better. A merely-proposed "snapshot" blocks NOTHING (it may not
    #     clear an event-source requirement either — the runtime history check owns that case).
    if (operand.operand_class == "event_timestamp"
            and capability.table_event_or_snapshot == "snapshot"
            and AUTHORITY_MATRIX.get(capability.table_event_or_snapshot_authority,
                                     {}).get("suggestion_at_declared", False)):
        codes.append(R.SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW)
        blocked = True

    # 2f. History depth (C9) — OPTIONAL by design: absence changes NOTHING (the runtime
    #     EVENT_HISTORY_VERIFICATION data check stays the named homework); only a KNOWN
    #     contradiction bites — a DECLARED depth (declared-or-better authority) the variant's
    #     window EXCEEDS. Surgical with B5's variants: the 180-day variant blocks, the
    #     30-day sibling of the same recipe stays eligible.
    if (operand.operand_class == "event_timestamp" and window_days
            and capability.table_history_depth_days is not None
            and AUTHORITY_MATRIX.get(capability.table_history_depth_authority,
                                     {}).get("suggestion_at_declared", False)
            and int(window_days) > capability.table_history_depth_days):
        codes.append(R.HISTORY_DEPTH_INSUFFICIENT)
        blocked = True

    # 2c. Source-grain shape (C3): the operand's allowed source grains name row shapes; the
    #     catalog can prove the COARSE half — the event/snapshot axis — at declared-or-better
    #     (the same at-declared+ posture as 2b). Grains outside the two recognizable shapes
    #     skip enforcement honestly (no catalog fact can check them yet).
    if operand.allowed_source_grains:
        axes = {_grain_axis(g) for g in operand.allowed_source_grains}
        if None not in axes and len(axes) == 1:
            required_axis = next(iter(axes))
            if (capability.table_event_or_snapshot in ("event", "snapshot")
                    and capability.table_event_or_snapshot != required_axis
                    and AUTHORITY_MATRIX.get(capability.table_event_or_snapshot_authority,
                                             {}).get("suggestion_at_declared", False)):
                codes.append(R.SOURCE_GRAIN_MISMATCH)
                blocked = True

    # 2e. Additivity vs operation (C3): SUMMING a value whose declared additivity says it
    #     cannot be summed that way — a stock (semi_additive) sums across entities at a
    #     point in time but never across time without an as-of anchor; a non-additive value
    #     (rate/ratio/score) never sums at all. Absent additivity blocks nothing.
    if output is not None and operand.operand_class == "measure" and capability.additivity:
        agg_text = f"{output.aggregation_over_entity} {output.aggregation_over_time}".lower()
        if "sum" in agg_text:
            if capability.additivity == "non_additive":
                codes.append(R.ADDITIVITY_INCOMPATIBLE)
                blocked = True
            elif (capability.additivity == "semi_additive"
                    and temporal_anchor == "event"):
                codes.append(R.ADDITIVITY_INCOMPATIBLE)
                blocked = True

    # 2d. Unit contradiction (C3): a currency-bearing column IS a monetary quantity — an
    #     operand expecting a non-monetary unit (count/rate/score) cannot be served by it.
    #     (The absent-facts half of unit verification reports through C5's family tri-state.)
    if (operand.unit_expectation and operand.unit_expectation != "monetary"
            and capability.currency):
        codes.append(R.UNIT_INCOMPATIBLE)
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
    # C3: the recipe reads a governed status policy and no resolver serves it yet — named,
    # visible setup work riding every candidate that depends on it (never a silent skip).
    if operand.status_policy_ref:
        codes.append(R.STATUS_POLICY_UNRESOLVED)
    # C4 (D14): unlicensed personal data is a POLICY question with an owner — a purpose
    # declared in Governance clears it; nothing else does.
    if capability.personal_data_required and not capability.personal_data_licensed:
        codes.append(R.PERSONAL_DATA_POLICY_REQUIRED)

    if blocked:
        status = "blocked"
    elif codes:
        status = "provisional"
    else:
        status = "eligible"
    return _verdict(operand, capability, status, codes, output=output)


def _verdict(operand: RequiredOperandV1, capability: ColumnCapabilityV1,
             status: str, codes: list[str], output=None) -> OperandEligibilityVerdictV1:
    primary = _primary(codes)
    checks = tuple(code for code in codes
                   if R.reason_family(code) in ("needs_setup", "needs_data_check"))
    return OperandEligibilityVerdictV1(
        personal_data_policy_revision_ids=capability.personal_data_policy_revision_ids,
        facts_absent=_absent_axes(operand, capability, output),
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
           "SEMANTIC_AUTHORITY_POLICY_VERSION", "authority_matrix_hash", "clears",
           "evaluate_operand"]
