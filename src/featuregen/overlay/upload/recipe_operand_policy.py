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
# SE-5 (shape half) — structural contradictions, closed. These refuse KNOWN incompatibility
# only: a missing fact is never a shape refusal (missing evidence and contradictory evidence
# are different conditions — plan invariant 6). Authority floors are deliberately NOT enforced
# here: they stage behind the SE-4b confirmation funnel (SE-5 step 8), because on a catalog
# whose semantics are all proposed they would flood every binding provisional.
TYPE_INCOMPATIBLE = "TYPE_INCOMPATIBLE"
IDENTIFIER_NOT_A_MEASURE = "IDENTIFIER_NOT_A_MEASURE"

_ECONOMIC_ROLE_FIELD = "economic_role"

#: Operand classes with a hard physical-type requirement. Other classes (entity_key, status,
#: dimension, direction, policy_input) legitimately ride many types and get no type rule.
_CLASS_TYPE_FAMILIES: dict[str, tuple[str, ...]] = {
    "measure": ("numeric",),
    "event_timestamp": ("temporal",),
    "as_of_timestamp": ("temporal",),
}


def _type_family(data_type: str | None) -> str:
    """The declared type's coarse family — from the DECLARED type only (a structural fact the
    connector attested), never inferred from a column name."""
    t = (data_type or "").strip().lower()
    if not t:
        return "unknown"
    if any(t.startswith(p) for p in ("int", "bigint", "smallint", "tinyint", "numeric",
                                     "decimal", "float", "double", "real", "money", "number")):
        return "numeric"
    if any(p in t for p in ("timestamp", "date", "time")):
        return "temporal"
    if t.startswith("bool"):
        return "boolean"
    if any(t.startswith(p) for p in ("char", "varchar", "nvarchar", "text", "string")):
        return "text"
    return "other"


def shape_refusal(operand, col) -> tuple[str, str] | None:
    """``(code, resolution)`` when binding this column to this operand is STRUCTURALLY
    impossible; ``None`` when shape permits it. Two rules, both contradiction-only:

    * an IDENTIFIER concept (one with a registered namespace) never satisfies a ``measure``
      operand — an identifier can serve key/grouping/distinct-count roles, never a quantity,
      regardless of its physical type (plan invariant 9);
    * a declared type outside the operand class's required family refuses — a varchar cannot
      be summed, a status code cannot anchor an event window (``unknown``/``other`` types are
      NOT refused: absence of a type fact is not a contradiction)."""
    if operand.operand_class == "measure" and col.concept:
        from featuregen.overlay.upload.concepts import concept as registered_concept

        try:
            registered = registered_concept(col.concept)
        except Exception:
            registered = None
        if registered is not None and registered.namespace is not None:
            return (IDENTIFIER_NOT_A_MEASURE,
                    f"{col.object_ref} carries identifier concept {col.concept!r} "
                    f"(namespace {registered.namespace!r}) — an identifier can serve entity-key, "
                    "grouping or distinct-count roles, never a measure; bind a quantity column")
    allowed = _CLASS_TYPE_FAMILIES.get(operand.operand_class)
    if allowed:
        family = _type_family(col.data_type)
        if family not in ("unknown", "other") and family not in allowed:
            return (TYPE_INCOMPATIBLE,
                    f"{col.object_ref} is declared {col.data_type!r} ({family}); operand class "
                    f"{operand.operand_class!r} requires {'/'.join(allowed)} — bind a column of "
                    "the right shape or correct the declared type")
    return None


def _shape_filter(operand, ranked):
    """Split concept-ranked candidates into shape-permitted and shape-refused. Applied BEFORE
    tie logic on purpose: a structurally impossible column must not manufacture a tie that
    blocks the one legitimate candidate."""
    permitted, refused = [], []
    for score, col in ranked:
        refusal = shape_refusal(operand, col)
        if refusal is None:
            permitted.append((score, col))
        else:
            refused.append((col.object_ref, *refusal))
    return permitted, refused


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
    # C6: this operand's shortlist was CUT at the per-operand bound after authority ranking —
    # the audit fact that says "the search was bounded", persisted with the observation.
    shortlist_truncated: bool = False


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


#: Concepts that ARE a direction representation — a bound operand carrying one licenses a
#: single magnitude column to serve opposing legs (amounts + indicator, the shape real
#: banking schemas use).
_DIRECTION_CONCEPTS = frozenset({"debit_credit_indicator"})

_SIGN_BLOCK_RESOLUTION = (
    "opposing legs bound one physical column with no governed sign REPRESENTATION — bind "
    "distinct columns, confirm a sign_convention fact on the shared column (signed-amount "
    "convention), or bind a direction column (debit_credit_indicator) in this recipe. The "
    "recipe's authored sign expectation is an EXPECTATION to validate against, never the "
    "authority that licenses the bind.")


def _direction_operand_bound(operands, verdicts) -> bool:
    """Did THIS request bind a direction-representation operand? (C3 sign law, leg one.)"""
    bound_roles = {v.role for v in verdicts if v.status == "bound"}
    return any(op.concept in _DIRECTION_CONCEPTS and op.role in bound_roles
               for op in operands)


def _sign_convention_cleared_by_ref(conn, catalog_source: str, refs) -> dict:
    """Leg two of the C3 sign law, BATCHED per binding run (the B6 load-once rule): which of
    ``refs`` carry a governed ``sign_convention`` fact whose CURRENT resolved authority
    clears the AUTHORING floor (C1's read-only pins + C2's matrix — the same laws everything
    else answers to). An authored string on the recipe is not evidence; an LLM proposal is
    not authority."""
    from featuregen.overlay.upload.field_resolution import current_resolution_pins
    from featuregen.overlay.upload.object_ref import normalize_ref
    from featuregen.overlay.upload.semantic_eligibility import clears

    logical_by_ref = {}
    for ref in dict.fromkeys(refs):
        parts = ref.split(".")
        if len(parts) >= 3:
            logical_by_ref[ref] = normalize_ref(catalog_source, parts[-3], parts[-2],
                                                parts[-1])
    if not logical_by_ref:
        return {}
    pins = current_resolution_pins(
        conn, logical_refs=list(logical_by_ref.values()), fields=("sign_convention",))
    cleared = {}
    for ref, logical in logical_by_ref.items():
        pin = pins.get((logical, "sign_convention"))
        cleared[ref] = bool(pin is not None and pin.value and clears(
            f"{pin.producer}/{pin.strength}", "authoring"))
    return cleared


def _resolve_opposing_legs(operands, verdicts, bound_by_group, final,
                           *, cleared_lookup) -> None:
    """The shared opposing-legs law over EVERY group at once. ``cleared_lookup(ref)`` answers
    leg two (a governed sign_convention at authoring authority on the shared column) — the
    capability binder answers it PURELY from the pre-compiled capability; the legacy binder
    reads it batched per run (reached only when real collisions exist, never per candidate)."""
    colliding = {}
    for group, members in bound_by_group.items():
        refs = [b.selected_ref for _sign, b in members if b.selected_ref]
        if len(refs) != len(set(refs)):
            colliding[group] = (members, refs)
    if not colliding:
        return
    if _direction_operand_bound(operands, verdicts):
        return
    for members, refs in colliding.values():
        if not all(cleared_lookup(ref) for ref in refs):
            _block_opposing_legs(final, members)


def _block_opposing_legs(final, members) -> None:
    for _sign, member in members:
        index = final.index(member)
        final[index] = OperandBindingVerdictV1(
            role=member.role, status="blocked", tied_refs=member.tied_refs,
            reason_codes=(DISTINCT_BINDING_VIOLATED,),
            resolution=_SIGN_BLOCK_RESOLUTION)


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
        # SE-5 shape half: structural contradictions filter BEFORE tie logic — a varchar
        # "amount" must neither bind a measure nor tie against the real one.
        ranked, shape_refused = _shape_filter(operand, ranked)
        if not ranked and shape_refused:
            # Columns with the concept EXIST but every one is structurally impossible — a
            # CONTRADICTION (blocked), not absence (unresolved): different fact, different code.
            verdicts.append(OperandBindingVerdictV1(
                role=operand.role, status="blocked",
                tied_refs=tuple(ref for ref, _code, _res in shape_refused),
                reason_codes=tuple(dict.fromkeys(code for _ref, code, _res in shape_refused)),
                resolution=shape_refused[0][2]))
            continue
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

    # Opposing legs: two members of one distinct group on ONE physical column is incompatible
    # unless the CATALOG carries a governed sign representation (C3 — the authored
    # sign_direction_expectation is an expectation, never the licensing authority).
    final: list[OperandBindingVerdictV1] = list(verdicts)
    if bound_by_group:
        shared = [b.selected_ref for members in bound_by_group.values()
                  for _sign, b in members if b.selected_ref]
        cleared = _sign_convention_cleared_by_ref(conn, catalog_source, shared)
        _resolve_opposing_legs(definition.operands, verdicts, bound_by_group, final,
                               cleared_lookup=lambda ref: cleared.get(ref, False))
    return tuple(final)


# ── SE-5 (full): the capability-input binder — one engine for every origin ─────────────────────
#
# `bind_planning_request` binds an origin-neutral FeaturePlanningRequestV1 over the FROZEN
# Layer-A context: shortlists come from the context's concept index (the concept-closure rule),
# capabilities compile in ONE batched read for the whole request, and every (operand, candidate)
# pair runs through SE-4's `evaluate_operand` — so semantic eligibility, the authority floors
# (computed, staged), the economic-role law and the shape laws all decide here, identically for
# recipes, LLM intents and user definitions. `bind_v2_operands` above remains the LIVE-COLUMN
# compatibility wrapper for its existing callers; new callers take this engine.
#
# The plan's step-6 bounds move HERE (the frozen templates module keeps its own copies):

MAX_CANDIDATES_PER_OPERAND = 16
MAX_BINDING_ASSIGNMENTS = 4096

#: C6 retrieval ranking — authority tiers, strongest first. Retrieval ORDER only: eligibility
#: still decides every survivor exactly; an unknown authority ranks last, never errors.
_AUTHORITY_RANK: dict[str, int] = {
    "human/confirmed": 6, "source/attested": 5, "source/declared": 4,
    "human/proposed": 3, "llm/proposed": 2, "graph_hint": 1, "absent": 0,
}


def bind_planning_request(conn, request, context):
    """Bind every operand of one planning request against one frozen context, fail-closed.

    Returns ``(verdicts, eligibility)``: the binding verdict tuple (the same
    :class:`OperandBindingVerdictV1` vocabulary every consumer already reads) plus the full
    per-candidate eligibility audit — ``{(role, object_ref): OperandEligibilityVerdictV1}`` —
    the losing-shortlist evidence SE-10 persists.

    Selection law: ELIGIBLE candidates outrank PROVISIONAL ones (the authority payoff — a
    confirmed concept beats a proposed twin without a tie); a tie WITHIN the preferred tier
    consults the SHARED tie-break store and otherwise fails closed (ambiguous, no selection);
    blocked / not_applicable candidates never bind and never manufacture a tie."""
    from featuregen.overlay.upload.column_capabilities import compile_capabilities

    shortlists = request_shortlists(request, context)
    all_refs = list(dict.fromkeys(ref for refs in shortlists.values() for ref in refs))
    capabilities = compile_capabilities(conn, context, all_refs)   # ONE query, whole request
    return bind_with_capabilities(conn, request, context, capabilities)


def request_shortlists(request, context) -> dict[str, tuple[str, ...]]:
    """PURE: per-role shortlists from the frozen concept index (B6 split — callers batch the
    capability read across MANY requests, then fold each purely)."""
    index = context.concept_index
    closure = getattr(context, "concept_closure", {}) or {}
    shortlists: dict[str, tuple[str, ...]] = {}
    for operand in request.operands:
        wanted = {operand.concept, *operand.alternative_concepts}
        refs: list[str] = []
        for concept_name in (operand.concept, *operand.alternative_concepts):
            refs.extend(index.get(concept_name, ()))
        # C7 — closure widening: a column enriched with a concept whose CLOSURE (self +
        # is-a ancestors + namespace mates) reaches a wanted name is RETRIEVED too.
        # Retrieval only: eligibility still refuses a mismatched meaning exactly.
        for enriched, members in closure.items():
            if enriched not in wanted and wanted.intersection(members):
                refs.extend(index.get(enriched, ()))
        deduped = list(dict.fromkeys(refs))
        # C6: NO blind cut here — the per-operand bound applies AFTER authority ranking (in
        # bind_with_capabilities, where the evidence pins exist). A pre-ranking cut in stable
        # ref order silently dropped a human-confirmed column at index 20 of 25. The
        # assignments cap stays as the safety bound retrieval can never exceed.
        shortlists[operand.role] = tuple(deduped[:MAX_BINDING_ASSIGNMENTS])
    return shortlists


def bind_with_capabilities(conn, request, context, capabilities):
    """The fold over PRE-COMPILED capabilities. ``conn`` is used ONLY by the tie-break
    consultation (reached solely when a genuine same-tier tie exists — bounded by real ties,
    never by candidate count)."""
    from featuregen.overlay.upload.semantic_eligibility import evaluate_operand

    shortlists = request_shortlists(request, context)
    columns_by_ref = {c.object_ref: c for c in context.columns}
    eligibility: dict[tuple[str, str], object] = {}
    verdicts: list[OperandBindingVerdictV1] = []
    bound_by_group: dict[str, list[tuple[str, OperandBindingVerdictV1]]] = {}

    hint_refs = frozenset(
        ref for op in request.operands for ref in getattr(op, "binding_hint_refs", ()))

    for operand in request.operands:
        # C6 — rank BEFORE truncating, with the evidence in hand: authority tier →
        # exact-concept-before-alternative → governed economic role → the user's hint →
        # stable ref order. THEN cut at the per-operand bound. The hint is a RANKING signal
        # only (user-origin, already validated) — eligibility still decides every survivor.
        ranked = sorted(
            (ref for ref in shortlists[operand.role] if ref in capabilities),
            key=lambda ref: (
                -_AUTHORITY_RANK.get(capabilities[ref].concept_authority, 0),
                0 if capabilities[ref].concept == operand.concept else 1,
                0 if (operand.economic_role
                      and capabilities[ref].economic_role == operand.economic_role) else 1,
                0 if ref in hint_refs else 1,
                ref))
        truncated = len(ranked) > MAX_CANDIDATES_PER_OPERAND
        shortlist = ranked[:MAX_CANDIDATES_PER_OPERAND]

        tiers: dict[str, list[str]] = {"eligible": [], "provisional": []}
        blocked_refs: list[tuple[str, tuple[str, ...]]] = []
        for ref in shortlist:
            capability = capabilities.get(ref)
            if capability is None:
                continue
            verdict = evaluate_operand(
                operand, capability, output=request.output,
                temporal_anchor=request.temporal.anchor_kind)
            eligibility[(operand.role, ref)] = verdict
            if verdict.status in tiers:
                tiers[verdict.status].append(ref)
            elif verdict.status == "blocked":
                blocked_refs.append((ref, verdict.reason_codes))

        bindable = tiers["eligible"] or tiers["provisional"]
        # C7: an EXACT-name candidate (the operand's concept or a declared alternative)
        # outranks a closure-widened descendant in SELECTION — the closure adds recall when
        # the exact meaning is absent; it never manufactures a tie against it.
        if bindable:
            wanted_names = {operand.concept, *operand.alternative_concepts}
            exact = [ref for ref in bindable
                     if capabilities[ref].concept in wanted_names]
            if exact:
                bindable = exact
        if not bindable:
            # C6 rule 3: a REQUIRED operand whose shortlist was CUT and yielded no winner
            # fails closed as AMBIGUOUS-with-truncation — an incomplete search must never
            # report the confident "nothing carries this concept".
            if truncated and operand.required:
                verdicts.append(OperandBindingVerdictV1(
                    role=operand.role, status="ambiguous", tied_refs=tuple(shortlist),
                    reason_codes=(AMBIGUOUS_BY_CLASS[operand.operand_class],),
                    shortlist_truncated=True,
                    resolution=("the candidate search was bounded and no survivor was "
                                "eligible — narrow the operand's concept/economic role, or "
                                "adjudicate the tie at ingest warming")))
                continue
            if blocked_refs:
                codes = tuple(dict.fromkeys(
                    code for _ref, ref_codes in blocked_refs for code in ref_codes))
                verdicts.append(OperandBindingVerdictV1(
                    role=operand.role, status="blocked",
                    tied_refs=tuple(ref for ref, _codes in blocked_refs),
                    reason_codes=codes,
                    resolution=eligibility[(operand.role, blocked_refs[0][0])].resolution))
            else:
                verdicts.append(OperandBindingVerdictV1(
                    role=operand.role, status="unresolved",
                    reason_codes=(REQUIRED_OPERAND_MISSING,) if operand.required else (),
                    resolution=("no read-scoped column carries this concept — onboard the "
                                "data or retire the operand" if operand.required else
                                "optional operand absent; the recipe's degrade policy applies")))
            continue

        selected_ref = bindable[0]
        verdict_ref: str | None = None
        own_hints = frozenset(getattr(operand, "binding_hint_refs", ()))
        hinted = [ref for ref in bindable if ref in own_hints]
        if len(bindable) > 1 and len(hinted) == 1:
            # C6 rule 4: the user NAMED this column — that is the requester's own
            # adjudication among BINDABLE peers. It never promotes a blocked or ineligible
            # ref (those never reach `bindable`), and it never manufactures a tie.
            selected_ref = hinted[0]
            verdict_ref = "user_hint"
        elif len(bindable) > 1:
            tied_cols = tuple(columns_by_ref[ref] for ref in bindable)
            tie_verdict, tie_key = _consult_request_tie_break(
                conn, request, operand, tied_cols)
            if tie_verdict is None:
                if operand.required:
                    verdicts.append(OperandBindingVerdictV1(
                        role=operand.role, status="ambiguous", tied_refs=tuple(bindable),
                        reason_codes=(AMBIGUOUS_BY_CLASS[operand.operand_class],),
                        resolution=("adjudicate this tie at ingest warming, or narrow the "
                                    "operand's concept/economic role — an unadjudicated tie "
                                    "never binds in the executable path")))
                else:
                    verdicts.append(OperandBindingVerdictV1(
                        role=operand.role, status="unresolved", tied_refs=tuple(bindable),
                        resolution="optional operand tied and unadjudicated — left unbound, "
                                   "never silently selected"))
                continue
            selected_ref = tie_verdict.ranking[0]
            verdict_ref = f"tie_break:{tie_key}"

        selected_eligibility = eligibility[(operand.role, selected_ref)]
        binding = OperandBindingVerdictV1(
            role=operand.role, status="bound", selected_ref=selected_ref,
            tied_refs=tuple(bindable) if len(bindable) > 1 else (),
            tie_break_verdict_ref=verdict_ref,
            reason_codes=selected_eligibility.reason_codes,   # floor codes RIDE the binding
            resolution=selected_eligibility.resolution,
            shortlist_truncated=truncated)
        verdicts.append(binding)
        if operand.distinct_binding_group:
            bound_by_group.setdefault(operand.distinct_binding_group, []).append(
                (operand.sign_direction_expectation, binding))

    # Opposing legs: identical law to the live-column binder (one rule, two engines is a bug).
    final: list[OperandBindingVerdictV1] = list(verdicts)
    _resolve_opposing_legs(
        request.operands, verdicts, bound_by_group, final,
        cleared_lookup=lambda ref: bool(
            (capability := capabilities.get(ref)) is not None
            and capability.sign_convention_cleared))
    return tuple(final), eligibility


def _consult_request_tie_break(conn, request, operand, tied_cols):
    """The SAME verdict store, keyed on the planning request's source definition — for a
    recipe-origin request this is byte-identical to the V2 key (same recipe id, same business
    definition), so adjudications warmed for the live surface resolve here too."""
    from featuregen.overlay.upload.tie_break import (
        TieBreakCandidate,
        find_tie_break_verdict,
        tie_break_input_hash,
    )

    tied = tuple(
        TieBreakCandidate(ref=c.object_ref, definition=c.definition or "",
                          ai_summary=c.ai_summary or "", semantic_terms=c.semantic_terms or "")
        for c in tied_cols)
    key = tie_break_input_hash(
        template_id=f"v2:{request.source_definition_id}", need_role=operand.role,
        need_concept=operand.concept,
        intent=getattr(request, "business_definition", "") or request.source_definition_id,
        tied=tied)
    verdict = find_tie_break_verdict(conn, input_hash=key,
                                     tied_refs=(c.object_ref for c in tied_cols))
    return (verdict, key) if verdict is not None else (None, key)
