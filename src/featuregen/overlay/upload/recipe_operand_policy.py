"""BR-5 — the V2 operand binding policy: fail closed, compose with the live tie-break, one
verdict for every consumer.

The amended invariant 5, mechanized. In the V2 EXECUTABLE path a tied required operand binds only
through an ADJUDICATED verdict — a warmed, content-addressed deliberation read from the SAME store
the live discovery surface uses (`tie_break.find_tie_break_verdict`; there is no second tie
mechanism). An UNADJUDICATED tie produces an AMBIGUOUS verdict with NO selected column, the tied
candidates named, and the resolution path stated. The legacy discovery surface is untouched by
this module's existence — its own flag governs it.

The semantic-compatibility half closes the "concept-compatible but banking-wrong" class:

* an operand with an ``economic_role`` binds ONLY when the candidate carries GOVERNED
  (human-confirmed) economic-role evidence matching it — a deposit balance cannot satisfy a
  drawn-credit-exposure role merely because both are ``monetary_stock``, because NOTHING satisfies
  that role without evidence;
* opposing legs (one ``distinct_binding_group``) refusing to land on one physical column unless a
  governed sign authority explains how one column carries both directions;
* a concept mismatch never binds at all — matching is the SAME two-tier matcher grounding uses
  (`templates._ranked_matches`), so authorization-status columns cannot satisfy
  settlement-status operands as a structural fact, not a policy.

Every verdict carries machine reason codes AND the human resolution path — a refusal nobody can
act on is a dead end, not governance.
"""
from __future__ import annotations

from dataclasses import dataclass

# Closed reason codes — BR-7's readiness fold and BR-8's blocker groups consume these verbatim.
AMBIGUOUS_BY_CLASS = {
    "entity_key": "AMBIGUOUS_ENTITY_BINDING",
    "measure": "AMBIGUOUS_MEASURE_BINDING",
    "event_timestamp": "AMBIGUOUS_TIME_BINDING",
    "as_of_timestamp": "AMBIGUOUS_TIME_BINDING",
    "status": "AMBIGUOUS_STATUS_BINDING",
    "direction": "AMBIGUOUS_STATUS_BINDING",
    "dimension": "AMBIGUOUS_RELATIONSHIP_BINDING",
    "policy_input": "AMBIGUOUS_STATUS_BINDING",
}
REQUIRED_OPERAND_MISSING = "REQUIRED_OPERAND_MISSING"
ECONOMIC_ROLE_UNPROVEN = "ECONOMIC_ROLE_UNPROVEN"
DISTINCT_BINDING_VIOLATED = "DISTINCT_BINDING_VIOLATED"

_ECONOMIC_ROLE_FIELD = "economic_role"


@dataclass(frozen=True, slots=True)
class OperandBindingVerdictV1:
    """One operand's verdict. ``status``: ``bound`` (unique, or adjudicated tie — then
    ``tie_break_verdict_ref`` names the deliberation), ``ambiguous`` (unadjudicated tie, NO
    selected column), ``unresolved`` (nothing matched — visible for optional operands, a blocker
    for required ones), ``blocked`` (matched but semantically unproven/incompatible)."""

    role: str
    status: str
    selected_ref: str | None = None
    tied_refs: tuple[str, ...] = ()
    tie_break_verdict_ref: str | None = None
    reason_codes: tuple[str, ...] = ()
    resolution: str = ""


def governed_economic_role(conn, catalog_source: str, object_ref: str) -> str | None:
    """The GOVERNED economic role of one column: the newest ACTIVE, human-CONFIRMED
    ``economic_role`` field-evidence value, else None. Proposals (AI or otherwise) below
    human×confirmed carry no authority here — an economic role is a banking judgment, and this
    check exists precisely so concept-compatibility cannot stand in for it."""
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    logical = logical_ref_of(conn, catalog_source, object_ref)
    rows = read_active_field_evidence(conn, logical, _ECONOMIC_ROLE_FIELD)
    confirmed = [r for r in rows if r.producer == "human" and r.strength == "confirmed"]
    return str(confirmed[-1].proposed_value) if confirmed else None


def v2_tie_break_key(definition, operand, tied_cols) -> str:
    """The V2 tie's content-addressed identity in the SHARED verdict store — also the verdict
    reference a bound operand carries (resolvable back to the stored deliberation)."""
    from featuregen.overlay.upload.tie_break import TieBreakCandidate, tie_break_input_hash

    tied = tuple(
        TieBreakCandidate(ref=c.object_ref, definition=c.definition or "",
                          ai_summary=c.ai_summary or "", semantic_terms=c.semantic_terms or "")
        for c in tied_cols)
    return tie_break_input_hash(
        template_id=f"v2:{definition.recipe_id}", need_role=operand.role,
        need_concept=operand.concept, intent=definition.business_definition, tied=tied)


def _consult_v2_tie_break(conn, definition, operand, tied_cols):
    """The SAME verdict store the live surface consults — keyed for V2 on the recipe id and its
    business definition (the V2 analogue of template intent). Warming covers V2 recipes at the
    BR-17 cutover; until a verdict exists this returns None and the caller FAILS CLOSED."""
    from featuregen.overlay.upload.tie_break import find_tie_break_verdict

    key = v2_tie_break_key(definition, operand, tied_cols)
    verdict = find_tie_break_verdict(conn, input_hash=key,
                                     tied_refs=(c.object_ref for c in tied_cols))
    return (verdict, key) if verdict is not None else (None, key)


def bind_v2_operands(conn, definition, *, catalog_source: str,
                     roles=()) -> tuple[OperandBindingVerdictV1, ...]:
    """Bind every operand of one V2 definition against one catalog, fail-closed. Uses the SAME
    two-tier matcher grounding uses (no second matcher, no second tie mechanism) — the formula
    path and the suggestion path consume THIS verdict tuple, never their own binding."""
    from featuregen.overlay.upload.templates import Need, _load_columns, _ranked_matches

    cols = _load_columns(conn, catalog_source, roles)
    verdicts: list[OperandBindingVerdictV1] = []
    bound_by_group: dict[str, list[tuple[str, OperandBindingVerdictV1]]] = {}

    for operand in definition.operands:
        probe = Need(operand.role, operand.concept, optional=not operand.required)
        ranked, _truncated = _ranked_matches(conn, cols, probe)
        if not ranked:
            verdicts.append(OperandBindingVerdictV1(
                role=operand.role, status="unresolved",
                reason_codes=(REQUIRED_OPERAND_MISSING,) if operand.required else (),
                resolution=("no read-scoped column carries this concept — onboard the data or "
                            "retire the operand" if operand.required else
                            "optional operand absent; the recipe's degrade policy applies")))
            continue

        top_score = ranked[0][0]
        tied_cols = tuple(c for score, c in ranked if score == top_score)
        col = tied_cols[0]
        verdict_ref: str | None = None

        if len(tied_cols) > 1:
            verdict, tie_key = _consult_v2_tie_break(conn, definition, operand, tied_cols)
            if verdict is None:
                if operand.required:
                    verdicts.append(OperandBindingVerdictV1(
                        role=operand.role, status="ambiguous",
                        tied_refs=tuple(c.object_ref for c in tied_cols),
                        reason_codes=(AMBIGUOUS_BY_CLASS[operand.operand_class],),
                        resolution=("adjudicate this tie at ingest warming, or narrow the "
                                    "operand's concept/economic role — an unadjudicated tie "
                                    "never binds in the executable path")))
                else:
                    verdicts.append(OperandBindingVerdictV1(
                        role=operand.role, status="unresolved",
                        tied_refs=tuple(c.object_ref for c in tied_cols),
                        resolution="optional operand tied and unadjudicated — left unbound, "
                                   "never silently selected"))
                continue
            by_ref = {c.object_ref: c for c in tied_cols}
            col = by_ref[verdict.ranking[0]]
            verdict_ref = f"tie_break:{tie_key}"

        reason_codes: list[str] = []
        if operand.economic_role:
            governed = governed_economic_role(conn, catalog_source, col.object_ref)
            if governed != operand.economic_role:
                verdicts.append(OperandBindingVerdictV1(
                    role=operand.role, status="blocked",
                    tied_refs=(col.object_ref,),
                    reason_codes=(ECONOMIC_ROLE_UNPROVEN,),
                    resolution=(f"this operand requires economic role "
                                f"{operand.economic_role!r}; {col.object_ref} carries "
                                f"{governed or 'no governed economic-role evidence'} — a human "
                                "confirms the column's economic role, or the recipe stays "
                                "blocked (concept compatibility alone never satisfies it)")))
                continue

        binding = OperandBindingVerdictV1(
            role=operand.role, status="bound", selected_ref=col.object_ref,
            tied_refs=tuple(c.object_ref for c in tied_cols) if len(tied_cols) > 1 else (),
            tie_break_verdict_ref=verdict_ref, reason_codes=tuple(reason_codes))
        verdicts.append(binding)
        if operand.distinct_binding_group:
            bound_by_group.setdefault(operand.distinct_binding_group, []).append(
                (operand.sign_direction_expectation, binding))

    # Opposing legs: two members of one distinct group on ONE physical column is incompatible —
    # unless a governed sign authority explains how one column carries both directions.
    final: list[OperandBindingVerdictV1] = list(verdicts)
    for _group, members in bound_by_group.items():
        refs = [b.selected_ref for _sign, b in members if b.selected_ref]
        if len(refs) != len(set(refs)):
            has_sign_authority = all(sign.strip() for sign, _b in members)
            if not has_sign_authority:
                for _sign, member in members:
                    index = final.index(member)
                    final[index] = OperandBindingVerdictV1(
                        role=member.role, status="blocked",
                        tied_refs=member.tied_refs,
                        reason_codes=(DISTINCT_BINDING_VIOLATED,),
                        resolution=("opposing legs bound one physical column with no governed "
                                    "sign authority — bind distinct columns, or attach the sign "
                                    "policy that separates the directions"))
    return tuple(final)
