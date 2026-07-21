"""Phase 3C.2b-i-B · Task 0 — the B disposition enum + the A-outcome mapping.

``BDisposition`` is the FULL outcome vocabulary for the B (governed-LLM cross-catalog) pipeline.
This task only builds its entry point: ``map_a_outcome`` folds Phase A's
``MultiSourcePlanningResultV1`` (the multi-source assembly+contract planner's output, spec
3C.2b-i-A) onto a ``BDisposition``. Several members below (``proposal_lossy``,
``gauntlet_rejected``, the four ``concept_authority_*``/``concept_not_in_registry`` members,
``operation_unrecognized``, ``operation_deferred``) have NO A-level analog — they are produced by
LATER B-pipeline steps (concept-authority resolution, operation-alias resolution, gauntlet review)
that are not part of this task; they are declared here now so the disposition vocabulary is fixed
and total from the start.

Pure data + one pure function. NO behaviour beyond the mapping, NO I/O, NO pydantic.

The two-axis rule (this task's one invariant): A's output carries TWO independent resolution axes —
the assembly axis (``result_status: MultiSourceReason``, whether a physical multi-source plan was
found) and the contract axis (``contract_result_status: ContractResolutionStatus``, whether that
plan's declaration+safety contract compiled clean). ``governed`` is reachable ONLY when BOTH axes
report resolved AND both winning ids (``selected_plan_id``, ``selected_contract_id``) are actually
set. Collapsing the two axes (e.g. treating an assembly-resolved-but-contract-unresolved result as
governed) would silently promote an uncompiled/rejected contract to a live governed feature — so an
assembly-resolved, contract-not-resolved result maps to ``contract_unresolved`` and NEVER to
``governed``, independent of which particular non-resolved ``ContractResolutionStatus`` it is
(``not_compiled`` through ``safety_rejected``/``unresolved_freshness`` all fold to the same
disposition here; B has no need to distinguish them further)."""
from __future__ import annotations

from enum import StrEnum

from featuregen.overlay.upload.planner.contracts import ContractResolutionStatus
from featuregen.overlay.upload.planner.multisource_contracts import (
    MultiSourcePlanningResultV1,
    MultiSourceReason,
)

# ---------------------------------------------------------------------------
# Policy version constants — pinned literals, bumped on any policy change.
# ---------------------------------------------------------------------------

B_DISPOSITION_VERSION = "3c2bib.disp.1.0.0"
ROLE_POLICY_VERSION = "3c2bib.role.1.0.0"
OPERATION_ALIAS_VERSION = "3c2bib.op.1.0.0"


class BDisposition(StrEnum):
    """The full B outcome vocabulary. ``governed`` is the sole success; the rest partition into
    concept-authority, structural/entity, role/operation, contract, technical, and
    capture/ambiguity outcomes."""
    governed = "governed"
    # concept-authority (produced by a later B step; no A-level analog)
    concept_authority_missing = "concept_authority_missing"
    concept_authority_conflict = "concept_authority_conflict"
    concept_authority_stale = "concept_authority_stale"
    concept_not_in_registry = "concept_not_in_registry"
    # structural / entity governance
    source_entity_ungoverned = "source_entity_ungoverned"
    structural_need_ungoverned = "structural_need_ungoverned"
    # role / operation policy
    role_not_aggregatable = "role_not_aggregatable"
    operation_unrecognized = "operation_unrecognized"
    operation_deferred = "operation_deferred"
    operand_order_authority_missing = "operand_order_authority_missing"
    # contract axis
    contract_unresolved = "contract_unresolved"
    # technical / capture
    technical_failure = "technical_failure"
    budget_truncated = "budget_truncated"
    # operand / identity ambiguity
    unresolved_operand = "unresolved_operand"
    ambiguous_column_identity = "ambiguous_column_identity"
    # later B steps (gauntlet review; no A-level analog)
    proposal_lossy = "proposal_lossy"
    gauntlet_rejected = "gauntlet_rejected"


# ---------------------------------------------------------------------------
# A -> B semantic-reject mapping. TOTAL over every non-`resolved` MultiSourceReason.
# `resolved` is deliberately absent: it is consumed by the two-axis check in
# `map_a_outcome` before this table is ever consulted (see `.get(..., technical_failure)`
# below, which also makes the lookup itself safe if that invariant were ever violated).
# ---------------------------------------------------------------------------

_A_REASON_TO_B_DISPOSITION: dict[MultiSourceReason, BDisposition] = {
    # semantic — operand shape / aggregation policy
    MultiSourceReason.operand_shape_invalid: BDisposition.unresolved_operand,
    MultiSourceReason.unsupported_path_aggregation: BDisposition.role_not_aggregatable,
    MultiSourceReason.aggregation_unsafe_on_path: BDisposition.role_not_aggregatable,
    MultiSourceReason.ordering_anchor_missing: BDisposition.operand_order_authority_missing,
    # semantic — source/path/landing governance
    MultiSourceReason.source_binding_ungoverned: BDisposition.structural_need_ungoverned,
    MultiSourceReason.no_governed_path: BDisposition.structural_need_ungoverned,
    MultiSourceReason.no_common_physical_grain: BDisposition.structural_need_ungoverned,
    MultiSourceReason.realization_endpoint_ungoverned: BDisposition.source_entity_ungoverned,
    MultiSourceReason.ambiguous_physical_grain: BDisposition.ambiguous_column_identity,
    # semantic — no closer B member exists; safe non-governed fallback (never invents a member)
    MultiSourceReason.temporal_paths_incompatible: BDisposition.technical_failure,
    # technical (A's own "# technical" bucket)
    MultiSourceReason.operand_or_slot_not_preserved: BDisposition.technical_failure,
    MultiSourceReason.technical_failure: BDisposition.technical_failure,
    # capture-incomplete
    MultiSourceReason.budget_truncated: BDisposition.budget_truncated,
}


def map_a_outcome(result: MultiSourcePlanningResultV1) -> BDisposition:
    """Map Phase A's run-level outcome onto a ``BDisposition``, honoring the two-axis rule.

    Order of evaluation:
    1. Both axes resolved AND both winning ids set -> ``governed``.
    2. Both axes resolved but a winning id is missing (a broken invariant on A's side, not a
       semantic contract rejection) -> ``technical_failure``.
    3. Assembly axis resolved, contract axis NOT resolved -> ``contract_unresolved`` (never
       ``governed``, regardless of which non-resolved ``ContractResolutionStatus`` it is).
    4. Assembly axis NOT resolved -> ``result_status`` IS itself the specific A reason (semantic,
       technical, or capture-incomplete); looked up in ``_A_REASON_TO_B_DISPOSITION``, falling back
       to ``technical_failure`` for safety (the table is total over every non-``resolved`` member,
       so this fallback is never actually exercised in normal operation)."""
    assembly_resolved = result.result_status is MultiSourceReason.resolved
    contract_resolved = result.contract_result_status is ContractResolutionStatus.resolved

    if assembly_resolved and contract_resolved:
        if result.selected_plan_id is not None and result.selected_contract_id is not None:
            return BDisposition.governed
        return BDisposition.technical_failure

    if assembly_resolved:
        return BDisposition.contract_unresolved

    return _A_REASON_TO_B_DISPOSITION.get(result.result_status, BDisposition.technical_failure)
