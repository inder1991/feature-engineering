"""SE-7 — the enforced projection: assembled semantic candidates become Gate-1 candidates.

The recipe lens is SERVED from the semantic engine — frozen context →
capability binder → eligibility fold → assembly — and this module is the seam that turns an
``AssembledCandidateV1`` into the exact carriers Gate-1 already speaks (SE-10 step 6: reuse the
``FeatureIdea``/``RoleBinding``/``Requirement`` carriers; never a third role-binding structure).

The projection is a TRANSLATION, not a re-decision — every semantic verdict maps onto the
closed legacy vocabulary it corresponds to:

* Typed gauntlet requirements land as legacy ``Requirement`` objects through their EXACT
  equivalents (identifier uniqueness IS the grain-uniqueness check; event-history depth IS the
  temporal-population check) — minted through ``build_requirement`` so the registry validates
  them, with the semantic origin named in ``detail``.
* Authority-floor codes (a proposal that retrieves but never clears) are NOT external data
  checks — they become ``RoleBinding.confirmation_required``, the carrier that has always meant
  "the human must confirm this binding at Gate 1".
* A candidate the gauntlet REFUSES (bound target, blocked binding) or whose temporal contract
  did not compile is a REJECTION with its named codes — never a served card, never silently
  dropped.

Origin-honest: projected ideas carry ``generation_source="recipe"`` with the V2 recipe id, and
their role-binding authorities are the observed ``producer/strength`` pins — the UI renders
what the engine measured, not a rounded-up story.

Three honesty rules govern what leaves here, all three added after the 2026-08-24 quality audit
of the live AML run (135 cards served on catalog ``cib``, SME-keep 0/135):

* **A card is an offer to compute something.** A candidate with ANY unbound REQUIRED operand
  cannot compute, so it never reaches ``ideas``/``actionable_ideas`` — it goes to
  :attr:`SemanticProjectionV1.needs_setup`, naming each unbound operand and what the binder
  actually found for it — no column carries the concept, several do and the tie is
  unadjudicated, or one matched and the evidence did not clear. 390 of that run's 591 required
  operands were unbound.
* **A badge states the corpus's truth.** ``verification`` is the WEAKER of the gauntlet's design
  verdict and the readiness rungs in hand — 132 of those cards came from ``FORMULA_BLOCKED``
  recipes and wore ``DESIGN-CHECKED`` because this module never read a readiness at all.
* **The card says what the recipe says.** ``rationale``, ``aggregation`` and the typed operand
  fields are the definition's own declarations, projected. They were reading
  ``conceptual_reason`` — which the recipe contract FORBIDS on an executable recipe, so it was
  empty on every one of the run's cards — while ``business_definition`` and ``decision_context``
  sat populated and unread.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.candidate_assembly import AssembledSetV1
from featuregen.overlay.upload.typed_gauntlet import validate_candidate

#: Typed-gauntlet requirement codes → the legacy closed REQUIREMENT_CODES vocabulary. Each row
#: is a semantic identity, not an approximation — the detail text still names the deeper check.
_REQUIREMENT_PROJECTION = {
    R.IDENTIFIER_UNIQUENESS: "GRAIN_IS_UNIQUE",
    R.EVENT_HISTORY_VERIFICATION: "TEMPORAL_IS_POPULATED",
    R.CURRENCY_POLICY_MISSING: "CURRENCY_CONSISTENT",
    R.RELATIONSHIP_REQUIRED: "JOIN_CONNECTIVITY",
}

#: Verdict codes that mean "a human confirms this binding at Gate 1" — the RoleBinding carrier's
#: own confirmation_required flag, never an external-check Requirement.
_CONFIRMATION_CODES = frozenset({R.PROPOSED_METADATA_ONLY, R.SEMANTIC_AUTHORITY_INSUFFICIENT})

#: T3 — the rungs of BR-7's ``READINESS_LADDER`` at which a DESIGN check has something to BE a
#: check of: an exact computation the platform accepts. Below them the design was never settled
#: (``FORMULA_BLOCKED`` has a NAMED unresolved authority; ``CONCEPTUAL_ONLY`` has no exact
#: computation at all), and ``RETIRED`` is a terminal, not a rung. ``MATERIALIZATION_BLOCKED`` IS
#: in the set on purpose: its design is settled and only the selected ENGINE cannot run it, which
#: is an execution fact, not a design one.
EXECUTABLE_READINESS = frozenset({
    "FORMULA_AUTHORABLE", "FORMULA_VALIDATED",
    "MATERIALIZATION_BLOCKED", "MATERIALIZATION_READY"})

#: The two stamps this projection can mint, from migration 0973's own CHECK vocabulary
#: (``UNVERIFIED | DESIGN-CHECKED | DATA-CHECKED | USEFULNESS-CHECKED``). The upper two are
#: EARNED downstream by evidence this seam has none of, so they are not spellable here.
_DESIGN_CHECKED = "DESIGN-CHECKED"
_UNVERIFIED = "UNVERIFIED"


def card_verification(validation_status: str, readiness_statements) -> str:
    """T3 — the §14.5 stamp, DERIVED from every statement in hand rather than defaulted.

    ``FeatureIdea.verification`` defaults to ``DESIGN-CHECKED``, and this projection used to let
    it: the live AML run therefore badged 132 cards from ``FORMULA_BLOCKED`` recipes as
    design-checked. The stamp now needs BOTH halves — the typed gauntlet's ``design_checked``
    verdict AND a readiness that says an exact computation exists.

    ``readiness_statements`` is every rung anybody has stated about this candidate (the lens's
    own fold, and the REGISTRY's row when the origin is a recipe). The WEAKER answer wins: a
    fold that lost a blocker cannot re-mint the badge, and a clean registry row cannot rescue a
    candidate this catalog blocked. An empty statement set earns nothing — nobody said it was
    ready, and silence is not a readiness.
    """
    if validation_status != "design_checked":
        return _UNVERIFIED
    stated = [rung for rung in readiness_statements if rung]
    if stated and all(rung in EXECUTABLE_READINESS for rung in stated):
        return _DESIGN_CHECKED
    return _UNVERIFIED


@dataclass(frozen=True, slots=True)
class UnboundOperandV1:
    """One REQUIRED operand this catalog could not bind, in the BINDER's own words.

    Every field is copied from a verdict the binder already produced — the concept the recipe
    asked for, the status it reached, its reason codes, the columns it was looking at, and its
    named remedy. ``status`` is ``""`` when the binder emitted no verdict for the role at all:
    honest absence, not a guess, and the fail-closed direction (a role nobody ruled on has
    certainly not bound).

    ``tied_refs`` is the field that keeps this honest. "Did not bind" is THREE conditions, and
    only one of them is an absence: ``unresolved`` means no column carries the concept;
    ``ambiguous`` means SEVERAL do and nobody has adjudicated between them; ``blocked`` means one
    or more matched and the evidence did not clear. The last two are PRESENCE, and the refs are
    the columns a human would be choosing between — dropping them turned "adjudicate this tie"
    into "onboard this data", which is the wrong remedy given to the wrong owner."""

    role: str
    concept: str
    operand_class: str
    status: str                           # bound-less verdict status; "" = no verdict emitted
    reason_codes: tuple[str, ...]
    resolution: str
    #: The columns the verdict was looking at — the tie's members, or the blocked candidates.
    #: Empty for a true absence and for a role the binder never ruled on.
    tied_refs: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"role": self.role, "concept": self.concept,
                "operand_class": self.operand_class, "status": self.status,
                "reason_codes": list(self.reason_codes), "resolution": self.resolution,
                "tied_refs": list(self.tied_refs), "sentence": self.sentence()}

    def sentence(self) -> str:
        """What the binder ACTUALLY found, worded from its own status — never from the lane's
        name. Every branch is a statement this seam can support from the verdict in hand."""
        concept = self.concept
        if not self.status:
            return f"the binder returned no verdict for the {self.role!r} operand ({concept})"
        if self.status == "ambiguous":
            listed = ", ".join(self.tied_refs)
            count = len(self.tied_refs)
            return (f"{count} column{'s' if count != 1 else ''} carr"
                    f"{'y' if count != 1 else 'ies'} {concept} and the tie is "
                    f"unadjudicated: {listed}" if self.tied_refs else
                    f"{concept} matched more than one column and the tie is unadjudicated")
        if self.status == "blocked":
            codes = ", ".join(self.reason_codes) or "no code given"
            listed = ", ".join(self.tied_refs)
            return (f"{concept} is carried by {listed} and the binding is blocked ({codes})"
                    if self.tied_refs else
                    f"{concept} matched and the binding is blocked ({codes})")
        if self.status == "unresolved":
            # The ONE condition that is genuinely an absence, and the only one allowed to say so.
            return f"no read-scoped column carries {concept}"
        # T6's rider (hardening, not a bug fixed — nothing reaches this today). The absence
        # sentence above used to be the FALL-THROUGH, so any status outside ""/ambiguous/blocked
        # claimed absence. `unbound_required_operands` admits a `bound` verdict carrying no
        # `selected_ref` — that is its definition of unbound — and such a verdict would have been
        # reported as "no read-scoped column carries X" while the binder had, in its own words,
        # BOUND the operand. The four verdict statuses are closed today and all four are handled
        # above; a fifth would arrive here and say only what this seam can support.
        return (f"the binder reported {self.status!r} with no column selected for the "
                f"{self.role!r} operand ({concept})")


@dataclass(frozen=True, slots=True)
class NeedsSetupCandidateV1:
    """T2 — a candidate held OUT of every served lane because a REQUIRED operand never bound.

    Deliberately NOT a card: it carries no computation, no requirements and no option identity,
    because there is nothing here to offer, save or govern until the binding is settled. What it
    does carry is what an operator can act on — WHICH operands did not bind, what the binder
    found for each, and the binder's own remedy.

    It names no OTHER catalog, on purpose. The projection is handed one assembled set and one
    catalog name; it takes no connection and holds no cross-catalog concept inventory, so
    "``monetary_flow`` lives in ``ftr``" is a claim it cannot make from anything it can see.
    That refusal-with-directions is T5's, which plans with the inventory in hand.

    The concept aggregate is deliberately named ``unbound_concepts`` and NOT ``missing``: on the
    FTR fixture 36 of 66 unbound required operands are ``ambiguous`` — the catalog carries the
    concept on several columns and nobody has adjudicated between them — so an aggregate whose
    NAME says "missing" is false for more than half of them. The name states what every member
    has in common (it did not bind) and leaves what the binder found to
    :meth:`UnboundOperandV1.sentence`, which is keyed to its status."""

    name: str
    source_definition_id: str
    recipe_id: str | None
    catalog_source: str                   # the catalog this was PLANNED over, no other
    unbound_concepts: tuple[str, ...]     # authored operand order, deduped; status-neutral
    unbound_operands: tuple[UnboundOperandV1, ...]

    def to_json(self) -> dict:
        return {"name": self.name, "source_definition_id": self.source_definition_id,
                "recipe_id": self.recipe_id, "catalog_source": self.catalog_source,
                "unbound_concepts": list(self.unbound_concepts),
                "unbound_operands": [o.to_json() for o in self.unbound_operands],
                "sentence": self.sentence()}

    def sentence(self) -> str:
        """The candidate-level answer: one clause per unbound operand, each in the words its own
        status earns. Built HERE so every surface that has to say this — the assist route today,
        T9's lane tomorrow — says it the same way from the same facts."""
        return "; ".join(operand.sentence() for operand in self.unbound_operands)


def unbound_required_operands(candidate) -> tuple[UnboundOperandV1, ...]:
    """Every REQUIRED operand of this candidate's request that did not bind to a column.

    Read from the REQUEST's own operand declarations and the binder's verdicts, not from the
    folded ``binding_state``: a required role the binder never ruled on leaves that fold saying
    ``bound`` (it folds over the statuses it was given), and a fold that can silently promote a
    candidate is exactly what this rule exists to stop. "Bound" here means the same thing it
    means everywhere else in this module — a ``bound`` verdict WITH a selected ref.
    """
    by_role = {verdict.role: verdict for verdict in candidate.verdicts}
    unbound = []
    for operand in candidate.planning_request.operands:
        if not operand.required:
            continue                      # an absent optional operand degrades, never blocks
        verdict = by_role.get(operand.role)
        if verdict is not None and verdict.status == "bound" and verdict.selected_ref:
            continue
        unbound.append(UnboundOperandV1(
            role=operand.role, concept=operand.concept,
            operand_class=operand.operand_class,
            status=(verdict.status if verdict is not None else ""),
            reason_codes=(tuple(verdict.reason_codes) if verdict is not None else ()),
            resolution=(verdict.resolution if verdict is not None else ""),
            tied_refs=(tuple(verdict.tied_refs) if verdict is not None else ())))
    return tuple(unbound)


def _needs_setup(candidate, unbound, catalog_source: str) -> NeedsSetupCandidateV1:
    request = candidate.planning_request
    return NeedsSetupCandidateV1(
        name=request.output.display_label or candidate.recipe_id,
        source_definition_id=(getattr(candidate, "variant_key", "")
                              or candidate.recipe_id),
        recipe_id=(candidate.recipe_id if request.origin == "recipe_v2" else None),
        catalog_source=catalog_source,
        unbound_concepts=tuple(dict.fromkeys(operand.concept for operand in unbound)),
        unbound_operands=unbound)


@dataclass(frozen=True, slots=True)
class SemanticProjectionV1:
    """One projection pass: served ideas + ACTIONABLE options + honest refusals + setup work.

    A3 (validated finding 8): actionable candidates (blocked/ambiguous/missing with a named
    resolution) are OPTIONS now, not rejections — they project as ideas too, so they mint
    option ids and decision rows and can be SAVED as ideas while create_contract stays
    blocked. Only gauntlet-refused, temporal-uncompiled, and malformed output remain
    rejections.

    T2 narrows that law without weakening it: a candidate whose REQUIRED operands did not bind
    is still VISIBLE and still carries its named resolutions, but as ``needs_setup`` rather than
    as a card. Its codes ride ``rejected_ids`` exactly as before, so the disposition lens folds
    the same universe it always did."""

    ideas: list
    actionable_ideas: list                # undecided work, save_idea-able, never hidden
    rejections: list                      # the V1 wire shape: {name, reason, code}
    grounded_ids: frozenset
    rejected_ids: dict
    binding_by_id: dict
    #: T2's lane. Defaulted so every existing construction site and reader stays valid — a
    #: caller that never learned about it sees exactly the ideas/rejections it always did.
    needs_setup: tuple[NeedsSetupCandidateV1, ...] = ()


def _role_bindings(candidate, catalog_source: str):
    from featuregen.overlay.upload.feature_assist import RoleBinding

    eligibility = candidate.eligibility or {}
    bindings = []
    for verdict in candidate.verdicts:
        if verdict.status != "bound" or not verdict.selected_ref:
            continue
        chosen = eligibility.get((verdict.role, verdict.selected_ref))
        bindings.append(RoleBinding(
            role=verdict.role,
            ref=(catalog_source, verdict.selected_ref),
            authority=chosen.authority_observed if chosen is not None else "",
            confirmation_required=bool(_CONFIRMATION_CODES
                                       & set(verdict.reason_codes))))
    return tuple(bindings)


def _requirements(validation, catalog_source: str):
    from featuregen.overlay.upload.validation_requirements import (
        REQUIREMENT_SCHEMA_REGISTRY,
        build_requirement,
    )

    projected = []
    for requirement in validation.requirements:
        legacy_code = _REQUIREMENT_PROJECTION.get(requirement.code)
        if legacy_code is None:           # e.g. a floor code riding a bound verdict — not an
            continue                      # external check; it already set confirmation_required
        projected.append(build_requirement(
            code=legacy_code,
            operand=(catalog_source, requirement.object_ref),
            detail=f"[{requirement.code}] {requirement.detail}",
            # Each code's OWN registered schema version — CURRENCY_CONSISTENT registers at
            # v2 (measure-suggestion params), and minting it at the v1 default CRASHED the
            # serving path the first time a currency-expecting operand bound a currency-less
            # column (found by the E1 gold corpus, case 6).
            schema_version=REQUIREMENT_SCHEMA_REGISTRY[legacy_code].schema_version))
    return tuple(projected)


#: The fallback this module has always used when a non-bound candidate's verdicts named no code
#: at all. Named rather than re-spelled, so the rejection dict and `rejected_ids` cannot drift.
_NOT_BINDABLE = ("SEMANTIC_NOT_BINDABLE",)


def _rejection(candidate, codes, reason: str) -> dict:
    label = candidate.planning_request.output.display_label or candidate.recipe_id
    return {"name": label, "reason": reason,
            "code": codes[0] if codes else _NOT_BINDABLE[0]}


def _registry_row(candidate):
    """The REGISTRY's own definition for a recipe-origin candidate, or None.

    ``v2_recipe_by_id`` RETURNS None for an unknown id (it never raises), so the answer is
    checked rather than caught: the previous ``except Exception`` here was catching the
    AttributeError from dereferencing that None, which made "this id is not in the registry"
    indistinguishable from a real fault."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    if candidate.planning_request.origin != "recipe_v2":
        return None
    return v2_recipe_by_id(candidate.recipe_id)


def _rationale(candidate, recipe, description: str) -> str:
    """T4 — the card's causal 'why', from the words the DEFINITION already carries.

    A recipe answers with its ``business_definition`` (WHAT this measures) followed by its
    ``decision_context`` (WHICH decision it serves) — both authored, both SME-reviewed, both
    populated on all 317 registry recipes, and both previously unread here.

    ``conceptual_reason`` is read only where the recipe contract permits it to exist: on a
    CONCEPTUAL PATTERN, where it states why no exact computation exists and is therefore the
    honest rationale. On an executable candidate the contract FORBIDS it (construction refuses
    a deterministic request that carries one), which is why reading it as the rationale produced
    an empty line on every executable card the live run served.

    Not truncated. The card's ``description`` already carries the same authored sentence in
    full, and a 200-character cut would make the rationale disagree with the definition it is
    quoting.
    """
    request = candidate.planning_request
    if recipe is not None:
        parts = (recipe.business_definition, recipe.decision_context)
    elif request.computation_kind == "conceptual_pattern":
        parts = (description, request.conceptual_reason)
    else:
        parts = (description,)
    return " — ".join(part.strip() for part in parts if part and part.strip())


def _class_refs(request, bound_by_role: dict, operand_class: str,
                catalog_source: str) -> tuple[tuple[str, str], ...]:
    """The bound refs of the operands the REQUEST declares to be of this class — the author's
    own typing, never a guess from the column. Authored operand order, so the card is
    deterministic."""
    return tuple((catalog_source, bound_by_role[operand.role])
                 for operand in request.operands
                 if operand.operand_class == operand_class and operand.role in bound_by_role)


def _time_ref(request, bound_by_role: dict, catalog_source: str):
    """The bound ref of the operand the TEMPORAL CONTRACT names as its clock.

    ``TemporalSpecV2`` names the role, per anchor kind: ``event_time_role`` for event and
    pre-decision anchors, ``business_effective_role`` for effective-interval and the as-of
    family, ``knowledge_time_role`` for the correction clock. Asked in that order, and only a
    role the binder actually bound answers — 20 of the 317 registry recipes name no temporal
    operand at all, and for those the honest answer is None, not the first timestamp in sight.
    """
    temporal = request.temporal
    for role in (temporal.event_time_role, temporal.business_effective_role,
                 temporal.knowledge_time_role):
        if role and role in bound_by_role:
            return (catalog_source, bound_by_role[role])
    return None


def _served_idea(assembled, validation, *, catalog_source: str,
                 candidate_status: str = ""):
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    candidate = assembled.candidate
    request = candidate.planning_request
    recipe = _registry_row(candidate)
    # B4 (GEN-05 closed): origin is a FACT, translated 1:1 — an intent id never wears a
    # recipe badge, and the candidate's OWN business definition survives to the card.
    generation_source = {"recipe_v2": "recipe", "llm_intent": "llm_intent",
                         "user_definition": "user_defined"}.get(request.origin,
                                                               request.origin)
    description = candidate.display_definition
    if not description and recipe is not None:
        description = recipe.business_definition
    if not description and request.computation_kind == "conceptual_pattern":
        description = request.conceptual_reason
    bound_refs = [v.selected_ref for v in candidate.verdicts
                  if v.status == "bound" and v.selected_ref]
    bound_by_role = {v.role: v.selected_ref for v in candidate.verdicts
                     if v.status == "bound" and v.selected_ref}
    # T4: the recipe's OWN declared operation. `RESULT_CLASS_ADDITIVITY`'s closed vocabulary
    # ("sum" / "ratio" / "recency" / …) is what this feature does; a conceptual pattern or an
    # unauthored intent declares none, and `aggregation` says so with None rather than with a
    # word nobody wrote.
    result_class = request.formula.result_class if request.formula is not None else ""
    plan = getattr(candidate, "binding_plan", None) or {}
    # C4: the licensing provenance rides the card exactly as the legacy path carried it —
    # the union of the bound operands' ACTIVE policy revisions (empty = nothing needed one).
    eligibility = candidate.eligibility or {}
    licence_ids = tuple(sorted({
        rid for v in candidate.verdicts if v.status == "bound" and v.selected_ref
        for rid in getattr(eligibility.get((v.role, v.selected_ref)), 
                           "personal_data_policy_revision_ids", ())}))
    return FeatureIdea(
        name=request.output.display_label or candidate.recipe_id,
        description=description,
        derives_from=list(bound_refs),
        aggregation=result_class or None,
        grain_table=plan.get("population_ref"),
        # WHAT THIS FEATURE IS COMPUTED PER, resolved by the planning lens (which holds the
        # connection this projection does not). Without it the draft worker refuses at REQUESTED
        # with GRAIN_NOT_RESOLVED, before any provider call — which is what silently stopped every
        # governed-path candidate, LLM-proposed ones included, from ever getting a formula.
        grain_refs=tuple(tuple(pair) for pair in (plan.get("grain_refs") or ())),
        window=(f"{plan['window']}d" if plan.get("window") else None),
        derives_pairs=tuple((catalog_source, ref) for ref in bound_refs),
        rationale=_rationale(candidate, recipe, description),
        # The gauntlet's OWN tri-state, unmoved: it answers "which external checks are
        # outstanding", which is a different question from how far this has been checked.
        validation_status=("DESIGN_CHECKED" if validation.status == "design_checked"
                           else "NEEDS_EXTERNAL_VALIDATION"),
        # T3: and this answers that one — the gauntlet's verdict AND every readiness statement
        # in hand (the lens's fold, plus the registry's row for a recipe origin), weakest wins.
        verification=card_verification(
            validation.status,
            (getattr(candidate, "readiness", ""),
             recipe.readiness if recipe is not None else "")),
        requirements=_requirements(validation, catalog_source),
        generation_source=generation_source,
        candidate_status=candidate_status,
        recipe_id=(candidate.recipe_id if request.origin == "recipe_v2" else None),
        source_definition_id=(getattr(candidate, "variant_key", "")
                              or candidate.recipe_id),
        # T4: the typed computation, projected from the request's own declarations — which were
        # in scope here all along while these five fields fell to their empty defaults.
        # `critic_note` is NOT among them and stays empty on purpose: no critic runs on the
        # engine path, and an empty advisory note is the honest report of that.
        operation_kind=result_class,
        measure_refs=_class_refs(request, bound_by_role, "measure", catalog_source),
        grouping_refs=_class_refs(request, bound_by_role, "dimension", catalog_source),
        time_ref=_time_ref(request, bound_by_role, catalog_source),
        operation_class=result_class,
        input_role_bindings=_role_bindings(candidate, catalog_source),
        operand_roles=tuple(sorted(
            (v.selected_ref, v.role) for v in candidate.verdicts
            if v.status == "bound" and v.selected_ref)),
        personal_data_policy_revision_ids=licence_ids,
        param_alternatives=getattr(candidate, "param_alternatives", ""),
    )


def project_assembled_set(assembled_set: AssembledSetV1, *, catalog_source: str,
                          target_ref: str | None = None) -> SemanticProjectionV1:
    """Serve the ranked candidates through the typed gauntlet; refuse the rest by name.

    T2's rule runs FIRST in both loops, because it is the one that decides whether there is a
    card to be had at all: a candidate whose REQUIRED operands did not bind is setup work, and
    setup work is neither a recommendation nor an option nor a refusal.

    What each arm records in ``rejected_ids`` differs, and the difference is the DISPOSITION
    family the fold lands on (``taxonomy.disposition.evaluate_dispositions``):

    * the ACTIONABLE arm is unchanged — its candidates' codes rode ``rejected_ids`` before T2
      and still do, so those ids fold exactly where they always did;
    * the RANKED arm's belt records NOTHING. An id in ``rejected_ids`` folds to
      ``SAFETY_REJECTED``, whose documented meaning is "it bound (grounding COMPLETED) but the
      gauntlet refused it (safety FAILED)" — and a diverted candidate did neither. Recording
      nothing puts it in the fold's ``else`` branch: grounding COMPLETED with ``no_binding``,
      safety NOT_EVALUATED, ``UNBUILDABLE`` — "it was in scope and grounding ran, but nothing
      bound", which is literally what happened."""
    ideas: list = []
    rejections: list = []
    grounded: set = set()
    rejected: dict = {}
    binding_by_id: dict = {}
    actionable_uoa: list = []
    needs_setup: list = []

    for assembled in assembled_set.ranked:
        candidate = assembled.candidate
        # T2, on the RANKED arm too. `binding_state` folds over the statuses the binder gave it,
        # so a required role it was never given a verdict for folds as `bound` and lands here —
        # a silent promotion, which is precisely the direction a fold must never move.
        unbound = unbound_required_operands(candidate)
        if unbound:
            # Deliberately records NOTHING in `rejected` or `grounded`: the disposition fold
            # reads those two, and either one would claim something that did not happen
            # (SAFETY_REJECTED = bound then refused; ELIGIBLE = bound and passed). Saying
            # nothing is what puts this candidate in UNBUILDABLE, the family whose documented
            # meaning — "grounding ran, nothing bound" — is exactly true of it.
            needs_setup.append(_needs_setup(candidate, unbound, catalog_source))
            continue
        # B10: bound but at the WRONG unit of analysis — a visible actionable option with the
        # roll-up resolution, never a silently-served ready card.
        if R.UOA_MISMATCH in getattr(candidate, "plan_refusals", ()):
            validation = validate_candidate(candidate)
            rejected[candidate.recipe_id] = (R.UOA_MISMATCH,)
            actionable_uoa.append(_served_idea(
                assembled, validation, catalog_source=catalog_source,
                candidate_status="uoa_mismatch"))
            continue
        if candidate.temporal_blocker:    # the temporal contract did not compile — setup work
            rejected[candidate.recipe_id] = (R.TEMPORAL_POLICY_UNRESOLVED,)
            rejections.append(_rejection(
                candidate, (R.TEMPORAL_POLICY_UNRESOLVED,), candidate.temporal_blocker))
            continue
        validation = validate_candidate(candidate, target_ref=target_ref)
        if validation.status == "refused":
            codes = tuple(r["code"] for r in validation.refusals)
            rejected[candidate.recipe_id] = codes
            rejections.append(_rejection(
                candidate, codes, "refused by the typed design gauntlet"))
            continue
        ideas.append(_served_idea(assembled, validation, catalog_source=catalog_source))
        grounded.add(candidate.recipe_id)
        floors = any(_CONFIRMATION_CODES & set(v.reason_codes)
                     for v in candidate.verdicts if v.status == "bound")
        binding_by_id[candidate.recipe_id] = "acceptable" if floors else "exact"

    actionable_ideas: list = list(actionable_uoa)
    for assembled in assembled_set.actionable:
        candidate = assembled.candidate
        codes = tuple(dict.fromkeys(
            code for v in candidate.verdicts for code in v.reason_codes))
        # UNCHANGED, deliberately: whichever lane the candidate lands in below, the disposition
        # lens folds the same codes over the same universe it always did.
        rejected[candidate.recipe_id] = codes or _NOT_BINDABLE
        unbound = unbound_required_operands(candidate)
        if unbound:
            needs_setup.append(_needs_setup(candidate, unbound, catalog_source))
            continue
        # A3: the candidate is a visible OPTION carrying its own undecided state — the named
        # resolution rides the card's critic-note-free channel (candidate_status = the honest
        # binding state; the wire section carries blockers from the activation fold).
        validation = validate_candidate(candidate)
        actionable_ideas.append(_served_idea(
            assembled, validation, catalog_source=catalog_source,
            candidate_status=candidate.binding_state))

    return SemanticProjectionV1(
        ideas=ideas, actionable_ideas=actionable_ideas, rejections=rejections,
        grounded_ids=frozenset(grounded), rejected_ids=rejected, binding_by_id=binding_by_id,
        needs_setup=tuple(needs_setup))


__all__ = ["EXECUTABLE_READINESS", "NeedsSetupCandidateV1", "SemanticProjectionV1",
           "UnboundOperandV1", "card_verification", "project_assembled_set",
           "unbound_required_operands"]
