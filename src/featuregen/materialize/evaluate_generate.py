"""S8 — ``evaluate_generate``: §0.3's Generate gate, implemented over the closed blocker vocabulary.

**Five requirements, and the two the activation policy cannot answer.** §0.3 asks for a selected
revision, a complete binding, a valid formula, resolvable policies and a renderer that can dispatch.
The first three are candidate-level facts the activation policy already computes, and they arrive
here as codes rather than being recomputed — a second opinion about "is this bound" would be free to
disagree with the one the UI shows. The last two are EXECUTION-CHAIN facts, discovered while
compiling: whether every governed policy reference resolves (S4) and whether this build's renderer
has a branch for every operator the graph contains (S8). Asking the activation policy for those
would mean compiling every candidate to answer a list query, which is why
:data:`~featuregen.overlay.upload.evaluator_contracts.EVALUATOR_ONLY_BLOCKERS` exists.

**Carried, not re-decided.** ``carried_blockers`` is the one place that says which codes an evaluator
gates on, and it RAISES on a code with no disposition rather than ignoring it — the exact failure
revision 17 shipped when it silently lost ``PERSONAL_DATA_POLICY_REQUIRED``. This module therefore
cannot quietly drop a blocker: an unknown code stops the evaluation instead of shrinking the list.

**Renderer dispatch is asked PER OPERATOR, over the graph's own kinds.** Not "is the engine
supported" — an engine with a branch for twelve of thirteen operators supports twelve of them, and a
feature using the thirteenth must refuse. The capability rows are the two separately recorded facts
(invariant 11); generation gates on ``renderer_dispatchable`` ALONE, because an execution proof is
what S9's verification and S10's publication rest on and requiring it here would make a
never-executed operator ungenerable — which is how it would never get proved.
"""
from __future__ import annotations

from collections.abc import Sequence

from featuregen.contracts.db import DbConn
from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1
from featuregen.formula.policy_store import (
    PolicyReferenceUnresolvable,
    resolve_policy_occurrence,
)
from featuregen.materialize.execution_proof_store import capability_of
from featuregen.materialize.operator_graph_v2 import OperatorGraphV2
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.evaluator_contracts import (
    EvaluatorAction,
    EvaluatorVerdictV1,
    carried_blockers,
)

__all__ = ["evaluate_generate", "undispatchable_kinds", "unresolvable_references"]


def unresolvable_references(
    conn: DbConn, occurrences: PolicyOccurrenceSetV1,
) -> tuple[str, ...]:
    """Every declared policy reference nothing currently realizes, as ``"<ref> as <role> over
    <dataset>"`` strings.

    Uses S4's resolver rather than re-deriving coverage: the resolver is what decides what "current"
    means, and a second answer here would let a feature generate against a realization the resolver
    would not have served.
    """
    unresolved: list[str] = []
    for occurrence in occurrences.occurrences:
        try:
            resolve_policy_occurrence(conn, occurrence)
        except PolicyReferenceUnresolvable:
            unresolved.append(
                f"{occurrence.policy_ref} as {occurrence.semantic_role} over "
                f"{occurrence.bound_dataset}")
    return tuple(sorted(set(unresolved)))


def undispatchable_kinds(
    conn: DbConn, graph: OperatorGraphV2, *, engine_id: str,
) -> tuple[str, ...]:
    """Operator kinds in ``graph`` this build's renderer cannot emit, sorted.

    An operator this build has never recorded counts as UNDISPATCHABLE, never as a default: a
    missing capability row means nobody established that the renderer can emit it, and treating an
    unrecorded operator as renderable is how a project that cannot run gets generated.
    """
    cannot: list[str] = []
    for kind in sorted({node.kind.value for node in graph.nodes}):
        capability = capability_of(conn, engine_id=engine_id, operator_kind=kind)
        if capability is None or not capability.renderer_dispatchable:
            cannot.append(kind)
    return tuple(cannot)


def evaluate_generate(
    conn: DbConn,
    *,
    generation_authorization_revision_id: str,
    activation_blockers: Sequence[str],
    occurrences: PolicyOccurrenceSetV1,
    graph: OperatorGraphV2,
    engine_id: str,
) -> EvaluatorVerdictV1:
    """§0.3's Generate verdict: allowed, or every blocker standing in the way.

    Args:
        generation_authorization_revision_id: the selected revision this generation is FOR. Blank
            is refused: invariant 17 says a generation is authorized for a target, and one that
            names no revision is authorized for nothing.
        activation_blockers: the codes the activation policy produced for this candidate. Passed in
            rather than recomputed, so this evaluator cannot disagree with the readiness the UI
            shows. Filtered through ``carried_blockers``, which RAISES on an unknown code.
        occurrences: the feature's policy occurrences (C-C7), resolved through S4's resolver.
        graph: the operator graph the renderer would be asked to emit.
        engine_id: which engine's capability to ask. Never defaulted — an engine this build has
            never heard of must be unsupported, not assumed.

    Returns:
        An :class:`EvaluatorVerdictV1`. Its own construction refuses a verdict that is allowed while
        carrying blockers, or refused with none, so the two halves cannot disagree.

    Raises:
        ValueError: no revision id, or an activation blocker with no disposition in this build. The
            second is deliberate: silently ignoring an unknown code is the defect the disposition
            table exists to prevent, and shrinking the blocker list is how a gate stops gating.
    """
    if not generation_authorization_revision_id.strip():
        raise ValueError(
            "evaluate_generate was called with no generation authorization revision: a generation "
            "is authorized FOR a target (invariant 17), and one naming no revision is authorized "
            "for nothing")

    blockers = list(carried_blockers(tuple(activation_blockers)))

    if unresolvable_references(conn, occurrences):
        blockers.append(R.POLICY_REFERENCE_UNRESOLVABLE)
    if undispatchable_kinds(conn, graph, engine_id=engine_id):
        blockers.append(R.RENDERER_CANNOT_DISPATCH)

    ordered = tuple(sorted(set(blockers)))
    return EvaluatorVerdictV1(action=EvaluatorAction.GENERATE, allowed=not ordered,
                              blockers=ordered)
