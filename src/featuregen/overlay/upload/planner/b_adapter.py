"""Phase 3C.2b-i-B · Task 9 — the normalization adapter (raw LLM proposal -> governed intent | reject).

:func:`normalize_feature_idea` is the composition pipeline that turns ONE untrusted LLM feature
proposal into A's :class:`MultiSourcePlannerIntentV1`, or the FIRST failing step's :class:`BDisposition`.
It composes the T2–T8 governance pieces by their REAL signatures — the gauntlet + preservation
brake (T4), the closed operation grammar (T8 :func:`normalize_operation`), the concept-authority
resolver (T5), the refined computation-role policy (T6), the source-side structural binding (T7),
and the governed output-field derivation (T8 :func:`resolve_output_policy`).

THE M1 FIX (the reason an operand's role is concept-driven, never positional): each operand's
computation role is assigned from :func:`computation_role` over its authoritative concept and then
CROSS-CHECKED against the role the requested aggregation requires (``_REQUIRED_ROLE``). A SUM asked
of a non-measure (or a COUNT asked of a non-counted) is rejected ``role_not_aggregatable`` — the
adapter never trusts operand position to categorize a measure.

Shadow-only; NO data plane. Read-only over A / the gauntlet / the governed stores. Frozen slotted
dataclasses; no pydantic. A is UNCHANGED; nothing here touches the considered set / ``is_live``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.feature_assist import Requirement
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.b_concept_authority import (
    ConceptRejection,
    resolve_planner_concept_binding,
)
from featuregen.overlay.upload.planner.b_concept_authority import (
    reason_to_b_disposition as concept_reason_to_b_disposition,
)
from featuregen.overlay.upload.planner.b_dispositions import BDisposition
from featuregen.overlay.upload.planner.b_gauntlet import (
    GauntletRejectionV1,
    run_gauntlet_and_preserve,
)
from featuregen.overlay.upload.planner.b_operation import (
    OperationRejection,
    normalize_operation,
)
from featuregen.overlay.upload.planner.b_operation import (
    reason_to_b_disposition as operation_reason_to_b_disposition,
)
from featuregen.overlay.upload.planner.b_output_policy import resolve_output_policy
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1
from featuregen.overlay.upload.planner.b_role_policy import (
    RolePolicyRejection,
    computation_role,
)
from featuregen.overlay.upload.planner.b_scope import RequestContextV1
from featuregen.overlay.upload.planner.b_source_grain import (
    SourceBindingRejection,
    resolve_source_binding,
)
from featuregen.overlay.upload.planner.b_source_grain import (
    reason_to_b_disposition as source_reason_to_b_disposition,
)
from featuregen.overlay.upload.planner.contracts import (
    OPERATION_POLICY_VERSION,
    to_additivity_class,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalExpressionV1,
    MultiSourcePlannerIntentV1,
    OperandSlotV1,
    PathAggregation,
    PathStrategyV1,
    SemanticRole,
)

__all__ = [
    "ADAPTER_POLICY_VERSION",
    "NormalizedIntentV1",
    "normalize_feature_idea",
]

ADAPTER_POLICY_VERSION = "3c2bib.adapter.1.0.0"

# The flattened schema every operand graph ref is scoped to (the graph writer always renders
# ``public.<table>.<column>``); the REAL declared schema is recovered per column from
# ``graph_node.schema_name`` for the concept resolver (the T5/T7/T8 seam).
_SCHEMA = "public"

# THE M1 CROSS-CHECK MAP: the concept-driven computation role each supported aggregation REQUIRES.
# The operand's own concept role (from :func:`computation_role`) must equal this — a mismatch is
# ``role_not_aggregatable`` (e.g. a SUM asked of a counted identifier). Total over every aggregation
# :func:`normalize_operation` can emit (sum/min/max/count/count_distinct).
_REQUIRED_ROLE: dict[PathAggregation, SemanticRole] = {
    PathAggregation.sum: SemanticRole.measure,
    PathAggregation.min: SemanticRole.measure,
    PathAggregation.max: SemanticRole.measure,
    PathAggregation.count: SemanticRole.counted,
    PathAggregation.count_distinct: SemanticRole.counted,
}


@dataclass(frozen=True, slots=True)
class NormalizedIntentV1:
    """A raw proposal normalized into A's governed intent, carrying the Slice-3 tri-state VERBATIM
    from the gauntlet's vetted idea (``validation_status`` + typed ``requirements``) — never
    recomputed here. Returned only when EVERY governance step resolved; any failing step returns its
    :class:`BDisposition` instead."""

    intent: MultiSourcePlannerIntentV1
    validation_status: str
    requirements: tuple[Requirement, ...]


def _operand_logical_ref(conn: DbConn, catalog_source: str, object_ref: str) -> str:
    """Build the SCHEMA-PRESERVING ``logical_ref`` for a flattened ``public.<table>.<column>`` operand
    ref, recovering the real declared schema from ``graph_node.schema_name`` (``NULL``/blank ->
    ``public``, matching ``normalize_ref``'s own default). The concept field-evidence is keyed on the
    real declared schema (``DPL_EIB_COMPLIANCE`` for real FTR data), so the ``public``-flattened ref
    would MISS it — the same seam T7/T8 recover."""
    parts = object_ref.split(".")
    table, column = parts[-2], parts[-1]
    row = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, object_ref),
    ).fetchone()
    schema = (row[0] if row is not None else None) or _SCHEMA
    return normalize_ref(catalog_source, schema, table, column)


def normalize_feature_idea(
    conn: DbConn,
    adapter: CatalogAdapter,
    *,
    proposal: RawFeatureProposalV1,
    ctx: RequestContextV1,
    roles: tuple[str, ...],
    now: datetime,
    fresh_within: timedelta,
) -> NormalizedIntentV1 | BDisposition:
    """Normalize ONE raw LLM proposal into a governed :class:`MultiSourcePlannerIntentV1`, or return
    the FIRST failing governance step's :class:`BDisposition` (fail-closed).

    Ordered composition (each step's reject short-circuits):

    1. Gauntlet + preservation (T4) — the safety brake + raw≡vetted preservation + tri-state.
       ``target_ref=None`` (this unsupervised roll-up has no label column).
    2. Operation grammar (T8) — the closed alias grammar; a deferred/ordered/unrecognized op rejects.
    3. Single-operand shape — the supported ops are single-operand; require exactly one computation
       operand (0 => nothing to govern; >1 => this slice has no cross-catalog combine).
    4. Resolve the one operand: concept authority (T5), computation role + the M1 cross-check (T6),
       source-side structural binding (T7), governed output-field derivation (T8).
    5–7. Assemble the single MEASURE/COUNTED slot, the IDENTITY final expression, and the intent
         (stamped with A's ``OPERATION_POLICY_VERSION``); the tri-state rides from the vetted idea.
    """
    # 1. Gauntlet + preservation (T4).
    vet = run_gauntlet_and_preserve(
        conn, proposal=proposal, identity_map=ctx.identity_map, target_ref=None,
        roles=roles, now=now, fresh_within=fresh_within)
    if isinstance(vet, GauntletRejectionV1):
        return vet.disposition

    # 2. Operation grammar (T8).
    op = normalize_operation(proposal.operation)
    if isinstance(op, OperationRejection):
        return operation_reason_to_b_disposition(op.reason)

    # 3. Single-operand shape (the supported ops are single-operand identity roll-ups).
    if len(vet.computation_operands) != 1:
        return BDisposition.unresolved_operand

    catalog_source, object_ref = vet.computation_operands[0]
    logical_ref = _operand_logical_ref(conn, catalog_source, object_ref)

    # 4a. Concept authority (T5).
    cb = resolve_planner_concept_binding(conn, logical_ref)
    if isinstance(cb, ConceptRejection):
        return concept_reason_to_b_disposition(cb.reason)

    # 4b. Computation role (T6) + THE M1 CROSS-CHECK. The operand's role is concept-driven, and it
    #     must match the role the requested aggregation requires — never trust operand position.
    c = concept(cb.authoritative_concept)
    if c is None:
        # T5 already guarantees the concept is in the registry; fail closed if that ever drifts.
        return BDisposition.concept_not_in_registry
    role = computation_role(c)
    if isinstance(role, RolePolicyRejection):
        return BDisposition.role_not_aggregatable
    if role != _REQUIRED_ROLE[op.path_aggregation]:
        return BDisposition.role_not_aggregatable

    # 4c. Source-side structural binding (T7).
    sb = resolve_source_binding(
        conn, adapter, catalog_source=catalog_source, object_ref=object_ref, now=now)
    if isinstance(sb, SourceBindingRejection):
        return source_reason_to_b_disposition(sb.reason)

    # 4d. Governed output-field derivation (T8).
    outp = resolve_output_policy(
        conn, catalog_source=catalog_source, object_ref=object_ref,
        aggregation=op.path_aggregation, concept_additivity=to_additivity_class(c.additivity),
        now=now)

    # 4e. The single governed operand slot.
    operand = OperandSlotV1(
        slot_id="m", semantic_role=role, catalog_source=catalog_source, object_ref=object_ref,
        authoritative_concept=cb.authoritative_concept,
        path_strategy=PathStrategyV1(
            aggregation=op.path_aggregation, output_type=outp.output_type,
            output_additivity=outp.output_additivity,
            external_type_required=outp.external_type_required, ordering_anchor_concept=None),
        source_binding=sb)

    # 5. The final expression — a lone operand's own path aggregation IS the roll-up (IDENTITY).
    final = FinalExpressionV1(
        operation=op.final_operation, ordered_slot_ids=("m",), time_slot_id=None, window=None,
        output_additivity=outp.output_additivity)

    # 6. The governed intent (A's OPERATION_POLICY_VERSION; target_entity is SERVER-derived via ctx).
    intent = MultiSourcePlannerIntentV1(
        target_entity=ctx.target_entity, operands=(operand,), final_expression=final,
        operation_policy_version=OPERATION_POLICY_VERSION)

    # 7. Carry the honest tri-state verbatim from the vetted idea.
    return NormalizedIntentV1(
        intent=intent, validation_status=vet.idea.validation_status,
        requirements=vet.idea.requirements)
