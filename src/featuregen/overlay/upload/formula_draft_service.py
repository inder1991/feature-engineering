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
from featuregen.overlay.upload.formula_draft_store import DraftRetired, request_draft
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
    "CandidateUnavailable",
    "DraftRequestedV1",
    "FORMULA_DRAFT_HANDLER",
    "FORMULA_DRAFT_TOPIC",
    "FrozenCandidateV1",
    "NotAFormulaCandidate",
    "RetiredAtRequest",
    "StrategyRefused",
    "frozen_candidate",
    "request_draft_for_candidate",
]

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
    now: str,
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
    provider_contract = (current_author_contract_hash()
                         if decision.strategy is FormulaStrategy.LLM_AUTHORED else None)
    config_payload: dict[str, Any] = {
        "identity_version": 2,
        "formula_strategy": str(decision.strategy),
        "strategy_identity_hash": decision.strategy_identity_hash,
    }
    if provider_contract is not None:
        config_payload["provider_contract_hash"] = provider_contract
    config_hash = jcs_sha256(config_payload)

    try:
        draft_id, created = request_draft(
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
            "reviewed_blueprint_hash, provider_contract_hash, method_override_revision_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (draft_id, facts.candidate_origin, str(decision.strategy),
             decision.strategy_identity_hash, facts.recipe_id, facts.recipe_revision_hash,
             facts.expectation_ref if facts.recipe_id else None,
             facts.expectation_generation if facts.recipe_id else None,
             assembled.reviewed_blueprint_revision
             if decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT else None,
             assembled.reviewed_blueprint_hash
             if decision.strategy is FormulaStrategy.REVIEWED_RECIPE_BLUEPRINT else None,
             provider_contract, facts.method_override_revision_id))

        # ▲ AND THE IDENTITY COMPANION — version 2, explicitly. Its composite FK to
        # (formula_draft_id, authoring_config_hash) is what makes "this companion describes that
        # draft" a constraint rather than a hope.
        from featuregen.overlay.upload.retirement_scope import retirement_scope_key

        conn.execute(
            "INSERT INTO formula_draft_authoring_identity (formula_draft_id, identity_version, "
            "retirement_scope_key, config_payload_json, config_hash) "
            "VALUES (%s, 2, %s, %s::jsonb, %s)",
            (draft_id,
             retirement_scope_key(
                 considered_revision_id=candidate.considered_revision_id, option_id=option_id,
                 planning_request_hash=candidate.planning_request_hash,
                 catalog_snapshot_hash=candidate.catalog_snapshot_hash,
                 definition_revision=candidate.definition_revision),
             json.dumps(config_payload, sort_keys=True), config_hash))

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
        warnings=tuple(decision.warnings), config_hash=config_hash, candidate=candidate)
