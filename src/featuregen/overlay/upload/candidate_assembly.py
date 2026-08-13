"""SE-10 — candidate assembly: one meaning, one card; an order somebody designed.

Three folds, all pure (no I/O, no LLM, no clock), each answering a question the legacy
pipeline never asked:

**The semantic computation signature** — WHAT does this candidate compute? A content hash over
output meaning, operation, grain, the temporal contract, resolved parameter values, and the
BOUND role assignments. Deliberately blind to origin, definition id, display names, and prose:
a recipe and an LLM intent that compute the same thing over the same columns collide — that
collision is the merge key. Names and physical refs alone never merge anything (two columns
named `balance` with different concepts produce different signatures because the signature
carries the binding, and the binding carried the meaning).

**The merge** — equivalent realizations become ONE candidate. The V2 recipe is primary over an
LLM twin (it carries SME-authored policies); among recipes, CURRENT review approval outranks
unreviewed — "carries reviewed SME semantics" is the candidate's own review-validity fold
result, a store lookup made upstream, never an assumption. Losing twins are recorded as
corroborating provenance, not shown as duplicate cards.

**The order** — today NOTHING owns candidate ordering (SE-0's audit: deterministic ranking was
authored-registry order; LLM ranking was never built). Here it is designed: a deterministic
composite key — binding state, then review validity, then authored readiness, then
applicability strength (primary before supporting), with the signature as the final content-
addressed tiebreak. No score, no weights, no model: every position is explainable by naming
the first key where two candidates differ. Blocked/ambiguous/missing candidates are never
"ranked low" — they go to a separate actionable section carrying their named resolutions,
because a refusal placed under a ranking reads as a bad candidate instead of a decidable one.
"""
from __future__ import annotations

from dataclasses import dataclass

ASSEMBLY_VERSION = "candidate-assembly@1"

#: Display precedence for binding states — bound candidates rank; everything else is
#: actionable. (fold_binding_state's blocked>ambiguous>missing precedence is for FOLDING a
#: candidate's own operands; this order is for laying decided-vs-undecided work side by side.)
_BINDING_RANK = {"bound": 0, "ambiguous": 1, "missing": 2, "blocked": 3}

#: Authored readiness, best-first, for the ordering key. RETIRED ranks last by construction —
#: an applicability lens should never emit one, but the order must still be total.
_READINESS_RANK = {
    "MATERIALIZATION_READY": 0, "FORMULA_VALIDATED": 1, "FORMULA_AUTHORABLE": 2,
    "MATERIALIZATION_BLOCKED": 3, "FORMULA_BLOCKED": 4, "CONCEPTUAL_ONLY": 5, "RETIRED": 6,
}

_ORIGIN_PRIMACY = {"recipe_v2": 0, "user_definition": 1, "llm_intent": 2}


def mechanism_identity(candidate) -> str:
    """D2 — the candidate's EXECUTABLE mechanism: the pinned formula expectation reference,
    or "" when the candidate carries none (a conceptual pattern, or an intent whose formula
    is unauthored). Two different non-empty mechanisms are two different computations —
    never one card."""
    formula = candidate.planning_request.formula
    return formula.expectation_ref if formula is not None else ""


def semantic_signature(candidate) -> str:
    """The candidate's computation identity — hash of meaning + binding + MECHANISM (D2:
    the candidate seen is the candidate governed, so a different formula expectation is a
    visibly different card, never a secret behind a shared one), never of origin."""
    from featuregen.overlay.field_evidence import canonical_hash

    request = candidate.planning_request
    return canonical_hash({
        "mechanism": mechanism_identity(candidate),
        "version": ASSEMBLY_VERSION,
        "output": {"type": request.output.output_type,
                   "unit_kind": request.output.unit_kind,
                   "additivity": request.output.additivity},
        "operation": request.computation_kind,
        "objective": request.primary_objective,
        "grain": [request.source_grain, request.output_grain],
        "temporal": {"anchor_kind": request.temporal.anchor_kind,
                     "window_basis": request.temporal.window_basis,
                     "window_unit": request.temporal.window_unit,
                     "window_parameter": request.temporal.window_parameter,
                     "cutoff_inclusivity": request.temporal.cutoff_inclusivity},
        "parameters": sorted([name, repr(value)]
                             for name, value in request.parameter_values),
        "bindings": sorted([verdict.role, verdict.selected_ref]
                           for verdict in candidate.verdicts
                           if verdict.status == "bound" and verdict.selected_ref),
    })


@dataclass(frozen=True, slots=True)
class CorroborationV1:
    """A merged-away twin, kept as provenance: the human sees ONE card that says who else
    arrived at the same computation, not two cards racing."""

    origin: str
    source_definition_id: str
    source_revision: str
    review_current: bool


@dataclass(frozen=True, slots=True)
class AssembledCandidateV1:
    signature: str
    candidate: object                     # the primary V2RecipeCandidateV1-shaped candidate
    corroborations: tuple[CorroborationV1, ...]
    assembly_version: str = ASSEMBLY_VERSION


def _primacy_key(candidate):
    """Who fronts a merged group: authored recipes over LLM intents (SME policies), CURRENT
    review over unreviewed, then the stable definition id so primacy never flaps."""
    request = candidate.planning_request
    return (_ORIGIN_PRIMACY.get(request.origin, len(_ORIGIN_PRIMACY)),
            0 if candidate.review_current else 1,
            request.source_definition_id)


def _order_key(assembled: AssembledCandidateV1):
    candidate = assembled.candidate
    return (_BINDING_RANK.get(candidate.binding_state, len(_BINDING_RANK)),
            0 if candidate.review_current else 1,
            _READINESS_RANK.get(candidate.readiness, len(_READINESS_RANK)),
            0 if candidate.relationship == "primary" else 1,
            assembled.signature)


@dataclass(frozen=True, slots=True)
class AssembledSetV1:
    """The considered set, assembled: ranked cards, actionable work, and the order's basis —
    stated, because an unexplained order is a score in disguise."""

    ranked: tuple[AssembledCandidateV1, ...]
    actionable: tuple[AssembledCandidateV1, ...]
    order_basis: str = ("binding_state, review_validity, readiness, "
                        "applicability_strength, signature")
    assembly_version: str = ASSEMBLY_VERSION


def assemble_candidates(candidates) -> AssembledSetV1:
    """Merge by semantic signature — which since D2 INCLUDES the executable mechanism —
    choose primacy, split ranked-vs-actionable, order both.

    Two different formula expectations are two visibly distinct cards, never one card with a
    secret difference. A cross-origin merge (recipe + intent twin) is only possible when the
    executable semantics are IDENTICAL — and the planning contract itself guarantees that is
    well-defined: a deterministic request always carries its formula (atomically enforced)
    and a conceptual pattern never does, so "same signature" always means "same mechanism"."""
    groups: dict[str, list] = {}
    for candidate in candidates:
        groups.setdefault(semantic_signature(candidate), []).append(candidate)

    assembled = []
    for signature, members in groups.items():
        members.sort(key=_primacy_key)
        primary, twins = members[0], members[1:]
        assembled.append(AssembledCandidateV1(
            signature=signature,
            candidate=primary,
            corroborations=tuple(
                CorroborationV1(origin=t.planning_request.origin,
                                source_definition_id=t.planning_request.source_definition_id,
                                source_revision=t.planning_request.source_revision,
                                review_current=t.review_current)
                for t in twins)))

    assembled.sort(key=_order_key)
    ranked = tuple(a for a in assembled if a.candidate.binding_state == "bound")
    actionable = tuple(a for a in assembled if a.candidate.binding_state != "bound")
    return AssembledSetV1(ranked=ranked, actionable=actionable)


__all__ = ["ASSEMBLY_VERSION", "AssembledCandidateV1", "AssembledSetV1", "CorroborationV1",
           "assemble_candidates", "mechanism_identity", "semantic_signature"]
