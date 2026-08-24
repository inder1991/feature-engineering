"""ONE composition of a formula-draft request — the route and the step-5 coordinator share it.

Extracted from ``api/routes/formula_drafts.py`` VERBATIM, not re-derived: the write path composes
seven load-bearing facts in one transaction (frozen candidate → resolved strategy → identity V2 →
money-guarded draft row → authoring plan → identity companion → outbox message), and the §8.3
lesson is that a second composition of one act is how the two drift. The route stays the HTTP
adapter — it maps the typed refusals below onto the exact status codes and bodies its tests pin —
and the coordinator (child step 5a) calls the same function and maps the same refusals onto member
states instead.

Every refusal here is TYPED and carries what the caller needs to act; none of them knows what HTTP
is. ``DraftRetired`` from the store is re-raised as :class:`RetiredAtRequest` with the frozen
candidate and the V2 config hash attached, because both callers need exactly those two things to
decorate the refusal (the route's 409 body, the coordinator's member blocker) and neither should
recompute an identity to describe a refusal about it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.overlay.upload.formula_draft_store import (
    DraftCeilingExhausted,
    DraftNotAnAnswer,
    DraftRetired,
    request_draft,
)
from featuregen.overlay.upload.formula_strategy import (
    FormulaStrategy,
    resolve_formula_strategy,
)
from featuregen.overlay.upload.formula_strategy_facts import (
    assemble_strategy_facts,
    current_author_contract_hash,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.outbox import OutboxMessage, insert_outbox_message_checked

__all__ = [
    "AUTHORING_ENVIRONMENT",
    "AuthoringRefused",
    "CandidateUnavailable",
    "DraftRequestedV1",
    "FORMULA_DRAFT_HANDLER",
    "FORMULA_DRAFT_TOPIC",
    "FrozenCandidateV1",
    "NotAFormulaCandidate",
    "NotAnAnswerAtRequest",
    "RetiredAtRequest",
    "StrategyRefused",
    "PER_DRAFT_CALL_ENVELOPE",
    "authoring_evidence_pins",
    "candidate_governance_blockers",
    "current_authoring_config",
    "frozen_candidate",
    "request_draft_for_candidate",
]

#: AUTHOR_FORMULA's authorization environment. Authoring runs in the CONTROL PLANE — there is no
#: serving environment for a draft, and inventing one would let an environment-scoped rule match
#: a claim nothing deploys. A namespace, not a claim; stated so nobody "fixes" it to an env id.
AUTHORING_ENVIRONMENT = "control-plane"


def authoring_evidence_pins(
    *, retirement_scope_key: str, catalog_snapshot_hash: str, strategy_identity_hash: str,
) -> dict[str, str]:
    """The pins an AUTHOR_FORMULA decision is taken against — ONE definition (Stage I Task 5).

    ▲ The service's decide and the worker's recheck must build these from the SAME function, or
    the two dictionaries drift apart the first time either changes and the drift check fires on
    healthy drafts — which teaches people to delete it (the generation lane's own words). The
    three pins are the candidate's facts hash (the retirement scope key — §0.1.4's authoring
    subject), the frozen catalog the model is given, and the evidence that chose the method.
    """
    return {
        "retirement_scope_key": retirement_scope_key,
        "catalog_snapshot_hash": catalog_snapshot_hash,
        "strategy_identity_hash": strategy_identity_hash,
    }


def candidate_governance_blockers(
    conn, *, candidate: FrozenCandidateV1, option_id: str, strategy,
    strategy_identity_hash: str, provider_contract_hash: str | None, config_hash: str,
    scope_key: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The candidate-level refusal FACTS — the ONE composition the plan preview and the
    AUTHOR_FORMULA decision both consult (§8.3 applied to reads: the first cut computed these in
    `member_authoring_plans` and never showed them to the decision, which made the gate
    ceremonial — the Task 5 review's 4a).

    * a retirement tombstone covering the candidate → ``FORMULA_DRAFT_RETIRED`` — UNLESS a
      valid coupon NAMES the covering tombstone (the store would mint and advance under it;
      round-3: a preview saying RETIRED for exactly the candidate the store admits is the
      two-answers-by-route class), in which case the WARNING ``RETIREMENT_OVERRIDDEN`` is the
      honest fact;
    * identity V2 active, a READY legacy V1 draft on this scope, LLM strategy, and no valid
      regeneration exception → ``LEGACY_REGENERATION_NOT_APPROVED``.

    Returns ``(blockers, warnings)``.
    """
    from featuregen.overlay.upload.formula_draft_store import formula_identity
    from featuregen.overlay.upload.formula_strategy import FormulaStrategy as _FS
    from featuregen.overlay.upload.retirement_scope import (
        covering_tombstones,
        valid_exception_for,
    )

    blockers: list[str] = []
    identity_hash = formula_identity(
        considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        authoring_config_hash=config_hash,
        definition_revision=candidate.definition_revision)
    warnings: list[str] = []
    # ▲ THE ONE LAW's preview (round-4): the SAME covering-set + coupon read the store gates on —
    # RETIRED iff some covering withdrawal lacks a valid naming coupon, RETIREMENT_OVERRIDDEN
    # iff every one is named. One answer, both routes, by construction rather than by parallel
    # derivations agreeing.
    covering_set = covering_tombstones(conn, scope_key=scope_key,
                                       formula_identity_hash=identity_hash)
    if covering_set:
        preview_now = conn.execute("SELECT now()").fetchone()[0]
        all_named = provider_contract_hash is not None and all(
            valid_exception_for(
                conn, target_formula_identity_hash=identity_hash,
                provider_contract_hash=provider_contract_hash,
                strategy_identity_hash=strategy_identity_hash,
                covering_tombstone_id=withdrawal.tombstone_id,
                now=preview_now) is not None
            for withdrawal in covering_set)
        if all_named:
            warnings.append("RETIREMENT_OVERRIDDEN")
        else:
            blockers.append("FORMULA_DRAFT_RETIRED")

    if strategy is _FS.LLM_AUTHORED:
        legacy_ready = conn.execute(
            "SELECT 1 FROM formula_draft_authoring_identity i "
            "  JOIN formula_draft d ON d.formula_draft_id = i.formula_draft_id "
            " WHERE i.identity_version = 1 AND i.retirement_scope_key = %s "
            "   AND d.state = 'READY' LIMIT 1", (scope_key,)).fetchone()
        if legacy_ready is not None:
            # Under the one law: with withdrawals covering, "is a re-buy approved" IS "is every
            # covering withdrawal named" — the same coupons answer both; with nothing covering,
            # the plain retry coupon (tombstone_id NULL) is the approval.
            if covering_set:
                approved = "RETIREMENT_OVERRIDDEN" in warnings
            else:
                approved = valid_exception_for(
                    conn, target_formula_identity_hash=identity_hash,
                    provider_contract_hash=provider_contract_hash,
                    strategy_identity_hash=strategy_identity_hash,
                    covering_tombstone_id=None,
                    now=conn.execute("SELECT now()").fetchone()[0]) is not None
            if not approved:
                blockers.append("LEGACY_REGENERATION_NOT_APPROVED")
    return tuple(blockers), tuple(warnings)


def current_authoring_config(strategy_decision) -> tuple[str | None, dict[str, Any], str]:
    """The identity-V2 configuration for THIS strategy decision — the ONE composition
    (§8.3, whose sentence this file quotes and, until the Task 6 review, violated in two other
    places). Returns ``(provider_contract_hash, config_payload, config_hash)``: LLM drafts fold
    the FROZEN provider contract (where prompt identity actually lives); reviewed drafts fold
    none, because no provider would be called."""
    provider_contract = (current_author_contract_hash()
                         if strategy_decision.strategy is FormulaStrategy.LLM_AUTHORED else None)
    config_payload: dict[str, Any] = {
        "identity_version": 2,
        "formula_strategy": str(strategy_decision.strategy),
        "strategy_identity_hash": strategy_decision.strategy_identity_hash,
    }
    if provider_contract is not None:
        config_payload["provider_contract_hash"] = provider_contract
    return provider_contract, config_payload, jcs_sha256(config_payload)


class AuthoringRefused(Exception):
    """The AUTHOR_FORMULA decision refused — a typed, member-level answer both callers map
    (route → 409 with the blockers; coordinator → member BLOCKED), never a 500."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        super().__init__(", ".join(blockers))
        self.blockers = blockers


#: The lane this work travels on — moved here WITH the producer so the one string keeps living
#: beside the code that writes it; the route re-exports both names unchanged.
FORMULA_DRAFT_TOPIC = "formula_draft.requested.v1"
FORMULA_DRAFT_HANDLER = "formula_draft.author.v1"


@dataclass(frozen=True, slots=True)
class FrozenCandidateV1:
    """The option AS FROZEN on its considered revision — server-resolved, never body-supplied."""

    considered_revision_id: str
    idea: Any
    catalog_snapshot_hash: str
    planning_request_hash: str
    definition_revision: str


class CandidateUnavailable(Exception):
    """The frozen candidate cannot be resolved. ``kind`` is closed and each value maps onto one
    HTTP status in the route: ``unknown_revision`` (404), ``option_not_in_revision`` (422 — a
    stale tab naming an option from a superseded revision), ``identity_unsupported`` (409 — the
    stored revision predates exact option identity; the remedy is to regenerate)."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class NotAFormulaCandidate(Exception):
    """A conceptual pattern or a governed model output — neither is a formula, so neither may
    mint a draft. Carries the resolved strategy and the refusal code."""

    def __init__(self, strategy: FormulaStrategy, code: str) -> None:
        super().__init__(code)
        self.strategy = strategy
        self.code = code


class StrategyRefused(Exception):
    """The resolver refused with blockers (e.g. a genuine bind failure named by code)."""

    def __init__(self, strategy: FormulaStrategy, blockers: tuple[str, ...]) -> None:
        super().__init__(", ".join(blockers))
        self.strategy = strategy
        self.blockers = blockers


class NotAnAnswerAtRequest(Exception):
    """The identity's existing draft records a FAILURE, not a formula (§11.1.2) — re-buying the
    answer is an approved regeneration, never an automatic re-spend. The store raised this as
    control flow; uncaught it was a 500 on the route and a whole-job "coordinator crash" — found
    by the run-spine session's mapping."""


class RetiredAtRequest(Exception):
    """The identity belongs to a RETIRED draft — the store's considered refusal, re-raised with
    the candidate and config hash both callers need to describe it."""

    def __init__(self, message: str, *, candidate: FrozenCandidateV1, config_hash: str) -> None:
        super().__init__(message)
        self.candidate = candidate
        self.config_hash = config_hash


@dataclass(frozen=True, slots=True)
class DraftRequestedV1:
    formula_draft_id: str
    created: bool
    strategy: FormulaStrategy
    warnings: tuple[str, ...]
    config_hash: str
    candidate: FrozenCandidateV1
    #: Task 5: the request-time AUTHOR_FORMULA decision this draft was admitted under.
    action_decision_revision_id: str = ""


#: The development envelope's ceilings — BOUNDED, visible, and replaced by a real approval
#: surface when production opens (§21). ▲ Sized from the PER-DRAFT worst case, not one
#: structured call (the Task 5 review's C-1: an authoring run is up to 8 author turns × 5
#: physical attempts each, plus the critic's own 5-attempt envelope — 45 calls — and
#: AUTHOR_TOKEN_BUDGET alone is 200k, so a 200k ceiling left the critic literally zero and one
#: repair anywhere exhausted mid-draft into a FALSE technical_failure plus a false
#: security-audit signal). Both ceilings raised TOGETHER, since per-call reservation is
#: max_tokens/max_calls and raising one alone reshapes what a single call may reserve.
#: ▲ THE ONE per-draft provider-call bound — every quoter and every ceiling imports THIS
#: (the Task 5 re-review found the job path still quoting 5 from its own stale copy, which
#: re-created exactly the one-repair-exhausts-mid-draft defect C-1 fixed here).
PER_DRAFT_CALL_ENVELOPE = 45          # 8 author turns × 5 physical attempts + critic envelope of 5

_DEV_ENVELOPE_MAX_CALLS = PER_DRAFT_CALL_ENVELOPE
_DEV_ENVELOPE_MAX_TOKENS = 250_000    # AUTHOR_TOKEN_BUDGET (200k) + critic allowance (50k)
_DEV_ENVELOPE_MAX_COST = "25.00"
# ▲ A deliberate trade, named (re-review): per-call reservation is max_tokens/max_calls ≈ 5.6k,
# well under one call's possible usage — so the seam's guarantee here is BOUNDED OVERSHOOT THEN
# REFUSE (the ledger enforces both totals across calls; a single call may start with less
# headroom than it ends up using) rather than strict pre-egress worst-case. Strictness would
# need a per-call worst-case constant the provider contract does not yet declare.


def _development_spend_envelope(
    conn, *, actor_subject: str, config_hash: str, provider_contract_hash: str | None,
) -> str:
    """Mint the bounded development ceiling for ONE draft identity — Task 5 AC3's chosen posture.

    Scoped to the draft's CONFIG (so a re-request of the same draft reuses the same ceiling
    rather than stacking fresh ones) and expiring on a DAY boundary two days out — the expiry is
    inside `authorize_spend`'s idempotency identity, so a within-the-day re-request is the SAME
    authorization and a next-day one is a new bounded envelope, never an unbounded accumulation
    within a working session.
    """
    from featuregen.overlay.upload.llm_spend import authorize_spend

    # ▲ UTC midnight, EXPLICITLY: a bare date cast to timestamptz lands at SESSION-tz midnight
    # (probed at Asia/Dubai by the Task 5 review), which made "the day boundary" drift with
    # whoever's session applied it. With expiry now inside authorize_spend's idempotency
    # identity, a within-the-day re-request is genuinely ONE authorization and the next UTC day
    # genuinely mints a fresh bounded envelope — the renewal the first cut only claimed.
    # ▲ Rendered with an EXPLICIT offset, because this string sits inside the spend idempotency
    # identity: a bare ::text renders in the SESSION timezone, and two sessions with different
    # tz would mint two "identical" same-day envelopes (re-review, minor 2).
    expires = conn.execute(
        "SELECT to_char((now() AT TIME ZONE 'utc')::date + 2, 'YYYY-MM-DD') "
        "|| 'T00:00:00+00:00'").fetchone()[0]
    return authorize_spend(
        conn, action="AUTHOR_FORMULA", actor_subject=actor_subject,
        job_identity=f"draft-dev-envelope:{config_hash}",
        member_identities=[config_hash],
        provider_contract_hash=provider_contract_hash or "none",
        max_calls=_DEV_ENVELOPE_MAX_CALLS, max_tokens=_DEV_ENVELOPE_MAX_TOKENS,
        currency="USD", max_cost=_DEV_ENVELOPE_MAX_COST,
        pricing_version="development", expires_at=expires)


def frozen_candidate(conn, revision_id: str, option_id: str) -> FrozenCandidateV1:
    """The option AS FROZEN on its considered revision, or a typed refusal.

    Read server-side rather than accepted from a body (contract.py's BLOCKER 1): a client naming
    its own candidate would have the model author against a definition nobody froze, and the
    draft's identity would then pin a snapshot that never described it. Resolved through
    ``gate1._chosen_option_from_revision`` — the SHIPPED resolver, which also cross-checks the
    opaque option map against its public projection.
    """
    from featuregen.overlay.upload.contract.gate1 import (
        Gate1Error,
        UnknownConsideredOption,
        _chosen_option_from_revision,
    )

    revision = conn.execute(
        "SELECT considered_revision_id, metadata_snapshot_content_hash, considered_json, "
        "considered_content_hash FROM contract_considered_revision "
        "WHERE considered_revision_id = %s", (revision_id,)).fetchone()
    if revision is None:
        raise CandidateUnavailable("unknown_revision", "unknown considered revision")

    considered = revision[2] if isinstance(revision[2], dict) else {}
    try:
        idea, _source, candidate_identity = _chosen_option_from_revision(considered, option_id)
    except UnknownConsideredOption as exc:
        raise CandidateUnavailable(
            "option_not_in_revision",
            "option is not part of this considered revision") from exc
    except Gate1Error as exc:
        raise CandidateUnavailable("identity_unsupported", str(exc)) from exc

    return FrozenCandidateV1(
        considered_revision_id=revision[0],
        idea=idea,
        # Part of the draft's identity: a catalog that moves produces a DIFFERENT draft rather
        # than silently reusing an answer about a world that no longer exists. Falls back to the
        # revision's own content hash when no metadata snapshot was pinned.
        catalog_snapshot_hash=revision[1] or revision[3],
        planning_request_hash=candidate_identity,
        definition_revision=getattr(idea, "definition", "") or "")


def request_draft_for_candidate(
    conn, *, revision_id: str, option_id: str, formula_draft_id: str, requested_by: str,
    now: str, spend_authorization_id: str | None = None,
    mint_development_envelope: bool = True,
) -> DraftRequestedV1:
    """Record a formula-draft request and enqueue its work — the ONE write composition.

    Everything durable lands in the CALLER's transaction: the draft row (money-guarded on the
    formula identity), the authoring plan the worker re-reads instead of recomputing, the V2
    identity companion, and the outbox message — so there is no window where a draft exists with
    nobody to drive it, and none where a queue row names a draft that is not there.
    """
    candidate = frozen_candidate(conn, revision_id, option_id)

    # ▲ THE METHOD IS RESOLVED FROM EVIDENCE, HERE, BEFORE THE DRAFT IDENTITY EXISTS — owner
    # ruling 2026-08-23 item 2. The strategy and its evidence hash are FOLDED INTO the identity
    # below, so "which method was chosen" is part of "is this the same draft" — a re-request after
    # the registry moves is a different draft, not a silent re-route of this one.
    assembled = assemble_strategy_facts(
        conn, considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        idea=candidate.idea, catalog_snapshot_hash=candidate.catalog_snapshot_hash)
    decision = resolve_formula_strategy(assembled.facts)

    if decision.strategy in (FormulaStrategy.NON_FORMULA, FormulaStrategy.MODEL_WORKFLOW):
        raise NotAFormulaCandidate(
            decision.strategy,
            decision.blockers[0] if decision.blockers else "NOT_A_FORMULA")
    if decision.blockers:
        raise StrategyRefused(decision.strategy, tuple(decision.blockers))

    # ▲ IDENTITY V2 — the corrected composition, ACTIVATED. The old `_authoring_config_hash` was
    # a CONSTANT (getattr on a dict), so the money guard was blind to model, prompts and method
    # since it shipped. Safe to correct ONLY because 1103 moved retirement off the identity hash
    # first. LLM drafts fold the FROZEN provider contract (where prompt identity actually lives);
    # reviewed drafts fold none, because no provider would be called.
    provider_contract, config_payload, config_hash = current_authoring_config(decision)

    # ▲ STAGE I TASK 5 — the per-draft AUTHOR_FORMULA decision, IN this one transaction. The
    # subject is the CANDIDATE (§0.1.4): its five facts ARE the retirement scope key, so the
    # authorization's resource is that key — one tuple, three uses. Placed BEFORE the
    # money-guarded INSERT: a refused decision spends nothing and writes nothing but itself.
    from featuregen.materialize.action_authorization import ActionV1, authorize_action
    from featuregen.materialize.action_decision import ActionRequestV1, decide
    from featuregen.overlay.upload.retirement_scope import retirement_scope_key

    scope_key = retirement_scope_key(
        considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        definition_revision=candidate.definition_revision)
    authorization = authorize_action(
        conn, action=ActionV1.AUTHOR_FORMULA, resource_identity_hash=scope_key,
        actor_subject=requested_by, environment_id=AUTHORING_ENVIRONMENT)
    # ▲ The decision receives the candidate's REAL facts (Task 5 review 4a) — the same builder
    # the plan preview reads — so the §5 fold is what refuses a retired or unapproved-legacy
    # candidate here, BEFORE the money guard and before any queue row. A gate that cannot refuse
    # is ceremonial; this one is not, and its refusal test authors nothing.
    member_blockers, governance_warnings = candidate_governance_blockers(
        conn, candidate=candidate, option_id=option_id, strategy=decision.strategy,
        strategy_identity_hash=decision.strategy_identity_hash,
        provider_contract_hash=provider_contract, config_hash=config_hash,
        scope_key=scope_key)
    member_warnings = tuple(decision.warnings) + governance_warnings
    decision_id, authoring_decision = decide(
        conn,
        ActionRequestV1(
            action=ActionV1.AUTHOR_FORMULA, resource_identity_hash=scope_key,
            member_names=(option_id,),
            member_blockers={option_id: member_blockers} if member_blockers else {},
            member_warnings={option_id: member_warnings} if member_warnings else {},
            evidence_pins=authoring_evidence_pins(
                retirement_scope_key=scope_key,
                catalog_snapshot_hash=candidate.catalog_snapshot_hash,
                strategy_identity_hash=decision.strategy_identity_hash)),
        authorization_id=authorization.authorization_id)
    if not authoring_decision.allowed:
        refused = tuple(dict.fromkeys(
            tuple(authoring_decision.blockers)
            + tuple(code for verdict in authoring_decision.per_member
                    for code in verdict.blockers)))
        raise AuthoringRefused(refused)

    # ▲ SPEND IS MANDATORY ON EVERY PATH THAT CAN REACH THE LLM LANE (Task 5 AC3). The chosen
    # posture is ROUTE-MINTS-DEV-ENVELOPE (§0.1.0): with no caller-supplied ceiling, the server
    # mints a BOUNDED development envelope — spend stays an enforced, reserved-per-call act at
    # the dispatch seam instead of an absent one, and the surface keeps working under the
    # development policy ("permission is broad and SERVER-owned"). Production opening replaces
    # this with a real approval surface (§21's release-readiness list). The alternative —
    # route-dies — would 409 every "Draft formula" click until an approval UI exists, which
    # punishes the user for a surface nobody has built.
    try:
        draft_id, created, coupon_ride = request_draft(
            conn,
            formula_draft_id=formula_draft_id,
            considered_revision_id=candidate.considered_revision_id,
            option_id=option_id,
            planning_request_hash=candidate.planning_request_hash,
            catalog_snapshot_hash=candidate.catalog_snapshot_hash,
            authoring_config_hash=config_hash,
            definition_revision=candidate.definition_revision,
            requested_by=requested_by,
            requested_at=now,
            provider_contract_hash=provider_contract,
            strategy_identity_hash=decision.strategy_identity_hash,
            now=now)
    except DraftRetired as exc:
        raise RetiredAtRequest(str(exc), candidate=candidate, config_hash=config_hash) from exc
    except DraftNotAnAnswer as exc:
        raise NotAnAnswerAtRequest(str(exc)) from exc
    except DraftCeilingExhausted as exc:
        # ▲ The SAME family as COST_AUTHORIZATION_MISSING, one condition later: there the job's
        # ceiling expired before the gate; here the approved ceiling is spent to zero. Both are
        # cost-authorization refusals, so both ride the AuthoringRefused arm every caller
        # already has — the route's 409 (code = first blocker) and the coordinator's member
        # refusal — and the store guarantees the naming coupon was NOT consumed.
        raise AuthoringRefused(("COST_AUTHORIZATION_EXHAUSTED",)) from exc

    if created and decision.strategy is FormulaStrategy.LLM_AUTHORED:
        # ▲ THE MINT RIDES THE MONEY OF THE COUPON IT CONSUMED (whole-branch C1) — resolved
        # AFTER the store call because only the store knows which coupon it consumed. Priority:
        # the consumed coupon's own authorization; else the caller's job ceiling; else the
        # no-coupon preference (an unexpired approval whose coupon was not part of THIS mint —
        # the pre-arm-adjacent paths approved_ceiling_for now exclusively serves); else the
        # bounded development envelope, or the job path's refusal by name. The refusal after
        # the mint is safe: everything durable is in the caller's transaction, so a raise
        # unwinds the draft AND the coupon consumption together.
        if coupon_ride is not None:
            spend_authorization_id = coupon_ride
        elif spend_authorization_id is None:
            from featuregen.overlay.upload.formula_draft_store import formula_identity
            from featuregen.overlay.upload.retirement_scope import approved_ceiling_for

            spend_authorization_id = approved_ceiling_for(
                conn,
                target_formula_identity_hash=formula_identity(
                    considered_revision_id=candidate.considered_revision_id,
                    option_id=option_id,
                    planning_request_hash=candidate.planning_request_hash,
                    catalog_snapshot_hash=candidate.catalog_snapshot_hash,
                    authoring_config_hash=config_hash,
                    definition_revision=candidate.definition_revision),
                provider_contract_hash=provider_contract,
                strategy_identity_hash=decision.strategy_identity_hash)
        if spend_authorization_id is None:
            if not mint_development_envelope:
                # ▲ THE JOB PATH REFUSES, NEVER SUBSTITUTES (Task 5 review 4b): a coordinator
                # member reaches here with None exactly when the job's cost-confirmed ceiling
                # EXPIRED — and quietly swapping in a $25 dev envelope would replace the
                # ceiling a person confirmed with one nobody did. §11.2's posture: refuse by
                # name; re-confirming the cost is the remedy, and it is the user's.
                raise AuthoringRefused(("COST_AUTHORIZATION_MISSING",))
            spend_authorization_id = _development_spend_envelope(
                conn, actor_subject=requested_by, config_hash=config_hash,
                provider_contract_hash=provider_contract)

    if created:
        # ▲ THE PLAN, PERSISTED IN THE SAME TRANSACTION AS THE DRAFT AND ITS QUEUE MESSAGE. The
        # worker RE-READS this row and never recomputes the strategy — a registry or review
        # moving between the request and the work must not silently re-route a draft whose
        # identity folded the FIRST answer. 1104's CHECKs enforce the shape.
        facts = assembled.facts
        conn.execute(
            "INSERT INTO formula_draft_authoring_plan (formula_draft_id, candidate_origin, "
            "formula_strategy, strategy_identity_hash, recipe_id, recipe_revision_hash, "
            "expectation_ref, expectation_generation, reviewed_blueprint_revision, "
            "reviewed_blueprint_hash, provider_contract_hash, method_override_revision_id, "
            "llm_spend_authorization_id, action_decision_revision_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (draft_id, facts.candidate_origin, str(decision.strategy),
             decision.strategy_identity_hash, facts.recipe_id, facts.recipe_revision_hash,
             facts.expectation_ref if facts.recipe_id else None,
             facts.expectation_generation if facts.recipe_id else None,
             assembled.reviewed_blueprint_revision
             if decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT else None,
             assembled.reviewed_blueprint_hash
             if decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT else None,
             provider_contract, facts.method_override_revision_id,
             # §11.2 — the job's ceiling rides the PLAN, and only where a provider will be
             # called: a reviewed plan spends nothing, and pinning a ceiling to it would claim a
             # purchase that cannot happen.
             (spend_authorization_id
              if decision.strategy is FormulaStrategy.LLM_AUTHORED else None),
             # Task 5 AC4 — durable where the worker re-reads: the request-time decision id,
             # rechecked at act time exactly as the generation lane rechecks its own.
             decision_id))

        # ▲ AND THE IDENTITY COMPANION — version 2, explicitly. Its composite FK to
        # (formula_draft_id, authoring_config_hash) is what makes "this companion describes that
        # draft" a constraint rather than a hope. The scope key is the SAME hoisted value the
        # authorization named — one tuple, three uses, computed once.
        conn.execute(
            "INSERT INTO formula_draft_authoring_identity (formula_draft_id, identity_version, "
            "retirement_scope_key, config_payload_json, config_hash) "
            "VALUES (%s, 2, %s, %s::jsonb, %s)",
            (draft_id, scope_key, json.dumps(config_payload, sort_keys=True), config_hash))

        # Enqueued ONLY for a genuinely new draft. Re-enqueuing an existing one would put a second
        # job on the lane for work already in flight or already finished.
        insert_outbox_message_checked(
            conn,
            OutboxMessage(
                message_id=f"formula-draft:{draft_id}",
                partition_key=f"formula-draft:{candidate.considered_revision_id}",
                topic=FORMULA_DRAFT_TOPIC,
                payload={"formula_draft_id": draft_id}))

    counters.incr("featuregen.formula_draft.requested" if created
                  else "featuregen.formula_draft.deduplicated")
    log("featuregen.formula_draft.requested", formula_draft_id=draft_id,
        considered_revision_id=revision_id, option_id=option_id, created=created)
    return DraftRequestedV1(
        formula_draft_id=draft_id, created=created, strategy=decision.strategy,
        # ▲ The FOLDED warning set — what the DECISION persisted after the §5 disposition fold,
        # not the raw pre-fold union (round-4: the durable record is the truth, and returning a
        # different set is the two-answers divergence inverted).
        warnings=tuple(authoring_decision.warnings), config_hash=config_hash,
        candidate=candidate,
        action_decision_revision_id=decision_id)


class RegenerationNotApprovable(Exception):
    """The target cannot take a regeneration exception — `kind` is closed: `unknown_draft`
    (404-shaped), `deterministic_lane` (409 — Option 2: free by construction, nothing to
    approve), `not_a_formula` (409 — the candidate resolves to no formula at all)."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def approve_regeneration_for_draft(
    conn, *, formula_draft_id: str, actor_subject: str, max_calls: int, max_tokens: int,
    max_cost: str, currency: str, pricing_version: str, expires_at: str, max_uses: int = 1,
) -> tuple[tuple[str, ...], str, bool]:
    """Task 6's approval act, with every binding derived SERVER-SIDE from the target draft.

    The exception authorizes the identity a re-request would mint TODAY — the candidate's frozen
    facts under the CURRENT strategy resolution and CURRENT provider contract — because that is
    the identity `request_draft` will check. The three bindings come from that derivation, never
    from a request body: a caller supplying its own target identity would be approving a
    regeneration of something other than what the retry will actually attempt.

    Cost-confirmed (§11.2): the NOT NULL spend authorization is minted here from the approver's
    ceilings, expiry through `canonical_approval_expiry` (the replay lesson), and the exception's
    own idempotency means a replayed approval is one coupon, not a stack.

    Returns ``(exception_id, spend_authorization_id, created)``.
    """
    from featuregen.overlay.upload.llm_spend import (
        authorize_spend,
        canonical_approval_expiry,
    )
    from featuregen.overlay.upload.retirement_scope import approve_regeneration_exception

    target = conn.execute(
        "SELECT considered_revision_id, option_id FROM formula_draft "
        "WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if target is None:
        raise RegenerationNotApprovable(
            "unknown_draft", f"no draft {formula_draft_id!r}: an approval regenerates a recorded "
                             f"attempt, and there is nothing recorded here")

    candidate = frozen_candidate(conn, target[0], target[1])
    assembled = assemble_strategy_facts(
        conn, considered_revision_id=candidate.considered_revision_id, option_id=target[1],
        idea=candidate.idea, catalog_snapshot_hash=candidate.catalog_snapshot_hash)
    decision = resolve_formula_strategy(assembled.facts)

    if decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT:
        raise RegenerationNotApprovable(
            "deterministic_lane",
            "this candidate resolves to the reviewed deterministic lane, whose retries are FREE "
            "BY CONSTRUCTION (owner ruling, Option 2): re-request the draft — no approval "
            "exists to give, and none is needed")
    if decision.strategy is not FormulaStrategy.LLM_AUTHORED:
        raise RegenerationNotApprovable(
            "not_a_formula",
            "this candidate resolves to no formula at all, so there is no regeneration to "
            "approve")

    provider_contract, _config_payload, config_hash = current_authoring_config(decision)
    from featuregen.overlay.upload.formula_draft_store import formula_identity

    target_identity = formula_identity(
        considered_revision_id=candidate.considered_revision_id, option_id=target[1],
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        authoring_config_hash=config_hash,
        definition_revision=candidate.definition_revision)

    expires = canonical_approval_expiry(conn, expires_at)
    spend_authorization_id = authorize_spend(
        conn, action="AUTHOR_FORMULA", actor_subject=actor_subject,
        job_identity=f"regeneration:{target_identity}",
        member_identities=[target_identity],
        provider_contract_hash=provider_contract,
        max_calls=max_calls, max_tokens=max_tokens, currency=currency, max_cost=max_cost,
        pricing_version=pricing_version, expires_at=expires)
    from featuregen.overlay.upload.retirement_scope import retirement_scope_key

    exception_ids, created = approve_regeneration_exception(
        conn, target_formula_identity_hash=target_identity,
        provider_contract_hash=provider_contract,
        strategy_identity_hash=decision.strategy_identity_hash,
        actor_subject=actor_subject, llm_spend_authorization_id=spend_authorization_id,
        expires_at=expires, max_uses=max_uses,
        # The candidate's OWN scope key — the derivation must not depend on any draft row
        # existing at the target identity (review item 3c).
        scope_key=retirement_scope_key(
            considered_revision_id=candidate.considered_revision_id, option_id=target[1],
            planning_request_hash=candidate.planning_request_hash,
            catalog_snapshot_hash=candidate.catalog_snapshot_hash,
            definition_revision=candidate.definition_revision))
    log("featuregen.regeneration.approved", exception_ids=list(exception_ids),
        formula_draft_id=formula_draft_id, created=created)
    return exception_ids, spend_authorization_id, created
