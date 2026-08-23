"""§5 — the EXHAUSTIVE disposition table: every reason code × every one of the SIX actions.

This is the CONTRACT §4's illustrative matrix is not. A matrix of representative conditions is
exactly how a code with no disposition gets shipped — and today an unknown code raises through
``_explained`` at DISPLAY time, the wrong layer and the wrong moment: the build already ran.

**The domain is closed on both axes.** Codes come from ``semantic_eligibility_reasons``'s
``REASON_FAMILIES`` — the module's own pinned vocabulary, so this table and the family table are
exhaustive over the SAME set. Actions are ``ActionV1``, all six. The CI test asserts the full
product is covered and that every cell is a ``Disposition`` MEMBER — §5.1's rule that a cell
contains a disposition, never a UI behaviour, a narrative, or an ``n/a``.

**Three dispositions, and the third is not "ignored"** (§5.1):

* ``BLOCK`` — this action may not proceed.
* ``WARN`` — proceeds, and the caller MUST be told (D3). Neither "allowed" nor "blocked".
* ``DROP`` — this action is not the gate for this fact. The fact still gates WHERE IT LIVES —
  another action's column, another layer's fold — and a dropped code is recorded on the member
  verdict, never discarded.

**The rulings this table encodes**, each quotable back at a future change:

* §4.1 — production eligibility follows the SEALED METHOD, never the candidate's origin. Review
  and maturity facts are warnings; the production gates are the METHOD_CERTIFICATE_* rows.
* §6 — recipe readiness is a discovery/maturity projection. It authorizes NOTHING here.
* §4 — EXECUTE_SANDBOX does not require verification; execution is what PRODUCES verification.
  ``VERIFICATION_NOT_CURRENT`` is therefore ``DROP`` at EXECUTE_SANDBOX and ``BLOCK`` at
  PUBLISH_SANDBOX — the row the merged "Sandbox: Depends" column was built to evade.
* §4.1 — ``PROVIDER_UNAVAILABLE`` and the cost rows are conditions of BUYING a formula. Once one
  exists they answer a question nobody asked, and a blocker that does that is how a provider
  outage takes production down. The downstream gate for "no formula" is ``FORMULA_NOT_AUTHORED``.

▲ Until §7's service is the ONLY caller, ``evaluator_contracts.ACTIVATION_BLOCKER_DISPOSITIONS``
coexists with this table (it answers a narrower question — which frozen activation blockers the
V2 execution chain carries), and BOTH must stay exhaustive. Adding a code is a THREE-PART commit:
the reasons-module entry + family row, the row here, and — if the activation policy emits it —
the carried/dropped row and the emitted-count literal in ``test_evaluator_contracts.py``.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from featuregen.materialize.action_authorization import ActionV1
from featuregen.overlay.upload import semantic_eligibility_reasons as R

__all__ = ["ACTION_DISPOSITIONS", "Disposition", "UnknownReasonCode", "disposition_for",
           "fold_member_codes"]


class Disposition(StrEnum):
    BLOCK = "block"   # this action may not proceed
    WARN = "warn"     # proceeds, and the caller MUST be told (D3)
    DROP = "drop"     # this action is not the gate for this fact — never "ignored"


class UnknownReasonCode(LookupError):
    """A (code, action) pair with no cell — a PROGRAMMER error the CI product test makes
    impossible for vocabulary codes, so reaching this means a raw string bypassed the module."""


_B, _W, _D = Disposition.BLOCK, Disposition.WARN, Disposition.DROP
_A = ActionV1


def _row(author: Disposition, preview: Disposition, sandbox: Disposition,
         publish_sandbox: Disposition, materialize: Disposition,
         publish_production: Disposition) -> dict[ActionV1, Disposition]:
    """All six, positionally, in ladder order — a row CANNOT omit an action."""
    return {_A.AUTHOR_FORMULA: author, _A.GENERATE_PREVIEW: preview,
            _A.EXECUTE_SANDBOX: sandbox, _A.PUBLISH_SANDBOX: publish_sandbox,
            _A.MATERIALIZE_PRODUCTION: materialize, _A.PUBLISH_PRODUCTION: publish_production}


#: code -> {action -> disposition}. Rows grouped the way §4 argues them; the comment above each
#: group is the reason the cells are what they are.
_ROWS: dict[str, dict[ActionV1, Disposition]] = {
    # ── Semantic basis wrong / computation illegal or impossible: BLOCK everywhere. No action
    # can proceed over a mismatched concept, a denied join, an unlicensed use, or a missing plan.
    R.CONCEPT_MISMATCH: _row(_B, _B, _B, _B, _B, _B),
    R.OPERAND_CLASS_MISMATCH: _row(_B, _B, _B, _B, _B, _B),
    R.IDENTIFIER_NOT_A_MEASURE: _row(_B, _B, _B, _B, _B, _B),
    R.TYPE_INCOMPATIBLE: _row(_B, _B, _B, _B, _B, _B),
    R.ECONOMIC_ROLE_UNPROVEN: _row(_B, _B, _B, _B, _B, _B),
    R.BUSINESS_EVENT_MISMATCH: _row(_B, _B, _B, _B, _B, _B),
    R.SEMANTIC_CONFLICT: _row(_B, _B, _B, _B, _B, _B),
    R.SOURCE_GRAIN_MISMATCH: _row(_B, _B, _B, _B, _B, _B),
    R.SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW: _row(_B, _B, _B, _B, _B, _B),
    R.ADDITIVITY_INCOMPATIBLE: _row(_B, _B, _B, _B, _B, _B),
    R.UNIT_INCOMPATIBLE: _row(_B, _B, _B, _B, _B, _B),
    R.EVENT_TIME_REQUIRED: _row(_B, _B, _B, _B, _B, _B),
    R.AS_OF_TIME_REQUIRED: _row(_B, _B, _B, _B, _B, _B),
    R.KNOWLEDGE_TIME_REQUIRED: _row(_B, _B, _B, _B, _B, _B),
    R.TEMPORAL_POLICY_UNRESOLVED: _row(_B, _B, _B, _B, _B, _B),
    R.RELATIONSHIP_REQUIRED: _row(_B, _B, _B, _B, _B, _B),
    R.POPULATION_DATASET_UNDECLARED: _row(_B, _B, _B, _B, _B, _B),
    R.JOIN_PATH_DENIED: _row(_B, _B, _B, _B, _B, _B),
    R.PERSONAL_DATA_POLICY_REQUIRED: _row(_B, _B, _B, _B, _B, _B),
    R.PROTECTED_CHARACTERISTIC_BLOCKED: _row(_B, _B, _B, _B, _B, _B),
    R.REQUIRED_OPERAND_AMBIGUOUS: _row(_B, _B, _B, _B, _B, _B),
    R.SOURCE_SELECTION_AMBIGUOUS: _row(_B, _B, _B, _B, _B, _B),
    R.BINDING_NOT_BOUND: _row(_B, _B, _B, _B, _B, _B),
    R.BINDING_PLAN_DIVERGENCE: _row(_B, _B, _B, _B, _B, _B),
    R.UOA_MISMATCH: _row(_B, _B, _B, _B, _B, _B),
    R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE: _row(_B, _B, _B, _B, _B, _B),
    R.PHYSICAL_PLAN_MISSING: _row(_B, _B, _B, _B, _B, _B),
    R.POLICY_REFERENCE_UNRESOLVABLE: _row(_B, _B, _B, _B, _B, _B),
    R.OUTPUT_POLICY_INCOMPLETE: _row(_B, _B, _B, _B, _B, _B),
    R.POLICY_FAMILY_UNVERIFIABLE: _row(_B, _B, _B, _B, _B, _B),
    R.HISTORY_DEPTH_INSUFFICIENT: _row(_B, _B, _B, _B, _B, _B),
    # ── The world moved: regenerate before ANY durable act.
    R.SNAPSHOT_STALE_REGENERATE: _row(_B, _B, _B, _B, _B, _B),
    R.ACTIVATION_STATE_DRIFTED: _row(_B, _B, _B, _B, _B, _B),
    # ── §5.1's three policy rows + read scope: WARN at AUTHOR — authoring proceeds and the
    # formula is genuinely visible (§13 serves the business-level formula); the refusal arrives
    # at preview, which is the first act that would COMPUTE over the fact.
    R.TARGET_LEAKAGE_BLOCKED: _row(_W, _B, _B, _B, _B, _B),
    R.RENDERER_CANNOT_DISPATCH: _row(_W, _B, _B, _B, _B, _B),
    R.CURRENCY_POLICY_MISSING: _row(_W, _B, _B, _B, _B, _B),
    R.STATUS_POLICY_UNRESOLVED: _row(_W, _B, _B, _B, _B, _B),
    R.EXECUTION_AUTHORITY_UNMET: _row(_W, _B, _B, _B, _B, _B),
    R.EXECUTION_AUTHORITY_UNEVALUATED: _row(_W, _B, _B, _B, _B, _B),
    R.FORMULA_SCHEMA_UNSUPPORTED: _row(_W, _B, _B, _B, _B, _B),
    # ── Data checks outstanding: proceeds with the caller told — sandbox is precisely where the
    # observation gets produced — and BLOCKS production, where an unchecked fact becomes a value.
    R.IDENTIFIER_UNIQUENESS: _row(_W, _W, _W, _W, _B, _B),
    R.EVENT_HISTORY_VERIFICATION: _row(_W, _W, _W, _W, _B, _B),
    R.DIRECTIONAL_CARDINALITY_UNPROVEN: _row(_W, _W, _W, _W, _B, _B),
    # ── §6: readiness is a projection. It authorizes NOTHING — every fact it summarized gates by
    # its own name (review by RECIPE_REVIEW_NOT_CURRENT, engine by FORMULA_SCHEMA_UNSUPPORTED).
    R.READINESS_NOT_MATERIALIZATION_READY: _row(_D, _D, _D, _D, _D, _D),
    # ── §4.1: origin/maturity facts are WARNINGS, never production gates — the production gate
    # for all of them is the METHOD_CERTIFICATE_* set below, matched against the SEALED method.
    R.FORMULA_NOT_REVIEWED: _row(_W, _W, _W, _W, _W, _W),
    R.FORMULA_REVIEW_UNMEASURED: _row(_W, _W, _W, _W, _W, _W),
    R.RECIPE_REVIEW_NOT_CURRENT: _row(_W, _W, _W, _W, _B, _B),  # §4: business review DOES gate production (§14)
    R.REVIEWED_EXPECTATION_LEGACY_VERSION: _row(_W, _W, _W, _W, _D, _D),
    R.BLUEPRINT_DERIVED_NOT_REVIEWED: _row(_W, _W, _W, _W, _D, _D),
    # ── §6: measurement absence is not an engine claim. Fails closed where the projection was
    # load-bearing (production); the chain re-measures the engine itself upstream.
    R.ENGINE_CAPABILITY_UNMEASURED: _row(_W, _W, _W, _W, _B, _B),
    # ── Semantic-confirmation and external-attestation facts: not any of THESE actions' gate.
    # A governed formula stands on blueprint review or deterministic validation (the evaluator
    # table's DROPPED reasoning); external attestation gates Delivery I's action, not these six.
    R.PROPOSED_METADATA_ONLY: _row(_D, _D, _D, _D, _D, _D),
    R.SEMANTIC_AUTHORITY_INSUFFICIENT: _row(_D, _D, _D, _D, _D, _D),
    R.EXTERNAL_VALIDATION_OUTSTANDING: _row(_D, _D, _D, _D, _D, _D),
    # ── Artifact-lifecycle facts: not authoring/preview gates (the artifact does not exist yet
    # from where those acts stand); hard gates from the first act that consumes an artifact.
    R.ARTIFACT_NOT_SERVABLE: _row(_D, _D, _B, _B, _B, _B),
    R.ENVIRONMENT_INCOMPATIBLE: _row(_D, _D, _B, _B, _B, _B),
    # ── §4's evasion-killer: execution PRODUCES verification, publication CONSUMES it.
    R.VERIFICATION_NOT_CURRENT: _row(_D, _D, _D, _B, _B, _B),
    R.PUBLICATION_CAPABILITY_MISSING: _row(_D, _D, _D, _B, _D, _B),
    # ── §10: the certificate set, matched against the SEALED method. Not authoring's gate (the
    # certificate cannot exist before sealing); warns as soon as an artifact is on the way;
    # BLOCKS both production acts — the only categorical production gates §4.1 leaves standing.
    R.METHOD_CERTIFICATE_MISSING: _row(_D, _W, _W, _W, _B, _B),
    R.METHOD_CERTIFICATE_STALE: _row(_D, _W, _W, _W, _B, _B),
    R.METHOD_CERTIFICATE_MISMATCHED: _row(_D, _D, _D, _D, _B, _B),
    R.METHOD_IDENTITY_UNRECORDED: _row(_D, _D, _D, _D, _B, _B),
    # ── §11.2/§4.1: conditions of BUYING a formula. BLOCK the purchase; DROP downstream, where
    # the honest gate for "there is no formula" is FORMULA_NOT_AUTHORED — a provider outage must
    # not take production down.
    R.PROVIDER_UNAVAILABLE: _row(_B, _D, _D, _D, _D, _D),
    R.COST_AUTHORIZATION_MISSING: _row(_B, _D, _D, _D, _D, _D),
    R.COST_AUTHORIZATION_EXHAUSTED: _row(_B, _D, _D, _D, _D, _D),
    # ── §11/§4.1: the honest downstream gates. Authoring is the CURE for an absent formula, so
    # these never gate it; everything downstream refuses over an unpinned or absent formula.
    R.FORMULA_NOT_AUTHORED: _row(_D, _B, _B, _B, _B, _B),
    R.FORMULA_DRAFT_NOT_PINNED: _row(_D, _B, _B, _B, _B, _B),
    R.SELECTION_FORMULA_BINDING_MISSING: _row(_D, _B, _B, _B, _B, _B),
    # ── §0.1/§7.1: the spine. An act without its authorization or decision is a bypass by
    # definition, whichever act it is.
    R.ACTION_AUTHORIZATION_MISSING: _row(_B, _B, _B, _B, _B, _B),
    R.ACTION_AUTHORIZATION_REVOKED: _row(_B, _B, _B, _B, _B, _B),
    R.ACTION_DECISION_MISSING: _row(_B, _B, _B, _B, _B, _B),
    R.DECISION_DRIFT: _row(_B, _B, _B, _B, _B, _B),
    # ── child §3.2: the route selector, surfaced. Warnings at authoring (the route is chosen and
    # the caller is told); moot once a formula exists.
    R.LLM_AUTHORING_REQUIRED: _row(_W, _D, _D, _D, _D, _D),
    R.REVIEWED_LANE_UNAVAILABLE: _row(_W, _D, _D, _D, _D, _D),
    # ── child: hard authoring refusals. A refused deterministic instantiation does NOT fall back,
    # a failed validation is the answer, and no snapshot means nothing to author against.
    R.REVIEWED_BLUEPRINT_NOT_EXECUTABLE: _row(_B, _B, _B, _B, _B, _B),
    R.FORMULA_VALIDATION_FAILED: _row(_B, _B, _B, _B, _B, _B),
    R.CATALOG_SNAPSHOT_NOT_FROZEN: _row(_B, _B, _B, _B, _B, _B),
    # ── §11.1: identity and the money guard. A legacy draft is an auditable PREVIEW artifact and
    # an unprovable configuration cannot be certificate-matched; a tombstone refuses BEFORE any
    # provider work; regeneration under V2 is an approved, cost-confirmed act, and its absence
    # gates the purchase and the production acts — there is no V2 draft for the middle to gate.
    R.LEGACY_CONFIG_UNPROVEN: _row(_D, _W, _W, _W, _B, _B),
    R.FORMULA_DRAFT_RETIRED: _row(_B, _B, _B, _B, _B, _B),
    R.LEGACY_REGENERATION_NOT_APPROVED: _row(_B, _D, _D, _D, _B, _B),
}

#: §5's shape: ``(code, action) -> Disposition``, 6 cells per vocabulary code, proved complete
#: over ``REASON_FAMILIES`` × ``ActionV1`` by CI.
ACTION_DISPOSITIONS: Mapping[tuple[str, ActionV1], Disposition] = {
    (code, action): disposition
    for code, row in _ROWS.items() for action, disposition in row.items()}


def disposition_for(code: str, action: ActionV1) -> Disposition:
    try:
        return ACTION_DISPOSITIONS[(code, action)]
    except KeyError:
        raise UnknownReasonCode(
            f"no disposition for {code!r} at {action}: the code is not in the closed vocabulary "
            f"(semantic_eligibility_reasons.REASON_FAMILIES). Adding one is a three-part commit — "
            f"see this module's docstring") from None


def fold_member_codes(
    action: ActionV1, blocker_codes: tuple[str, ...], warning_codes: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Apply the table to one member's raw codes → ``(blockers, warnings, dropped)``.

    The TABLE is authoritative per (code, action), in both directions: a caller-supplied
    "warning" whose cell says BLOCK escalates, and a caller-supplied "blocker" whose cell says
    WARN or DROP is downgraded — that is the §5 mechanism that stops a newly added code silently
    becoming a sandbox blocker, and equally stops a caller laundering a block into a warning.

    A code OUTSIDE the vocabulary keeps its caller's channel — fail-closed for blockers (an
    unknown fact offered as a refusal stays one), and a warning we cannot classify stays a
    warning. Order within each channel is the caller's; duplicates are the caller's too.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    dropped: list[str] = []
    by_disposition = {Disposition.BLOCK: blockers, Disposition.WARN: warnings,
                      Disposition.DROP: dropped}
    for code in blocker_codes:
        cell = ACTION_DISPOSITIONS.get((code, action))
        by_disposition[cell if cell is not None else Disposition.BLOCK].append(code)
    for code in warning_codes:
        cell = ACTION_DISPOSITIONS.get((code, action))
        by_disposition[cell if cell is not None else Disposition.WARN].append(code)
    return tuple(blockers), tuple(warnings), tuple(dropped)
