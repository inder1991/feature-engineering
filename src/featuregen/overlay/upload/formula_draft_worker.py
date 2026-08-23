"""The "Draft formula" lane: author ONE candidate's formula, then decide whether it may be used.

**Nothing here holds a transaction across a model.** That is the whole reason this lane exists
rather than the request thread or the general ``process_one`` consumer, both of which would. Every
stage below commits before the next begins, so a crash leaves the row saying exactly how far it got
and a client disconnect orphans nothing.

**The stages are OBSERVED, not narrated.** ``formula_authoring_trace_event.kind`` is a closed
four-word vocabulary written by the orchestrator as it goes (``author_turn``, ``critic_result``,
``validation_result``, then a terminal), and :func:`_observed_state` maps it onto the draft's state
machine. The alternative — moving the state to ``CRITIC_REVIEW`` around the call because that is
roughly when a critic runs — would put a sentence on a user's screen that no event supports.

**Progress only moves FORWARD.** A multi-turn run genuinely goes author → validate → author again,
so the observed kind can go backwards; the draft's state does not follow it back. "Critic review has
begun" stays true afterwards, and the state machine has exactly one path by design.

**RETRY IS A SECOND BILL.** Everything this module can name, it fails PERMANENTLY. A retryable
failure re-runs authoring, and a deterministic fault retried to the attempt budget would buy the
same refusal ``max_attempts`` times. Only a genuinely transient fault — a lost lease, an
unreachable database — is left retryable.

**BLOCKED is a product result; FAILED is an outage.** A formula can be perfectly good and still name
an operator this engine has never proved. Recording that as FAILED would send an operator to
investigate an incident when the honest answer is "this needs a capability nobody has proved yet" —
a different person, a different remedy, a different screen.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from featuregen.config import get_settings
from featuregen.formula.control import LeaseFenceLost, RecoveryRequiresReconciliation
from featuregen.formula.recipe_authoring import (
    FrozenRecipeReadContext,
    recipe_tool_runner_v2,
)
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthoringIntent
from featuregen.identity.current_principal import (
    PrincipalResolutionStatus,
    resolve_current_principal,
)
from featuregen.intake.llm import current_llm_client
from featuregen.materialize.admission_v2 import (
    ResolvedFeatureInputV2,
    admit_artifacts_v2,
    implied_operator_signatures,
)
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.execution_proof_store import (
    renderer_supported_operators,
    unqualified_operators,
)
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.formula_draft_store import (
    DraftRetired,
    DraftStateV1,
    InvalidTransition,
    advance,
    read_draft,
    record_admission,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.queue import (
    FormulaDraftQueueClaim,
    claim_formula_draft,
    complete_formula_draft,
    fail_formula_draft,
    renew_formula_draft,
)

__all__ = [
    "FORMULA_DRAFT_ENGINE_ENV",
    "FormulaDraftWorkerOutcome",
    "process_formula_draft_once",
]

#: WHOSE advertised set a draft is admitted against. Read from the environment and never defaulted,
#: for ``admit_artifacts_v2``'s stated reason: an engine this build has never heard of must be
#: unsupported, and a default would quietly pick one. Unset is not an error — see
#: :func:`_admit`, which lets the formula be READY and simply records no decision about an engine
#: nobody named.
FORMULA_DRAFT_ENGINE_ENV = "FEATUREGEN_FORMULA_DRAFT_ENGINE"

#: The wire version this lane authors at. V3 is the Formula-V2 LANGUAGE at wire version 3 — the
#: shape ``admission_v2`` accepts (``_V2_LANGUAGE_VERSIONS``) and the renderer dispatches.
_DRAFT_FORMULA_SCHEMA_VERSION = 3

#: ``formula_authoring_trace_event.kind`` → the state that kind PROVES has begun. The terminals
#: (``completed``, ``failed``) are deliberately absent: what a finished run MEANS is decided below
#: from the folded result and from admission, never from the fact that it stopped.
_KIND_STATE: Mapping[str, DraftStateV1] = {
    "author_turn": DraftStateV1.AUTHORING,
    "critic_result": DraftStateV1.CRITIC_REVIEW,
    "validation_result": DraftStateV1.VALIDATING,
}

#: The one legal path, in order, so a sync can walk from where the row is to where the trace says it
#: has reached WITHOUT skipping a state. `advance` refuses a skip; this is how the caller obeys.
_PATH: tuple[DraftStateV1, ...] = (
    DraftStateV1.REQUESTED, DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW,
    DraftStateV1.VALIDATING, DraftStateV1.ADMISSION, DraftStateV1.READY,
)

#: A candidate set generated without a pinned catalog snapshot. Named as a BLOCKER rather than
#: raised as a failure: there is nothing wrong with the platform, and the remedy — regenerate the
#: candidates against a snapshot — belongs to whoever asked for them.
_NO_SNAPSHOT = {
    "code": "CATALOG_SNAPSHOT_UNPINNED",
    "reason": "this candidate set was generated without a pinned catalog snapshot, so there is no "
              "frozen catalog to author against; regenerate the candidates to draft a formula",
}
_NO_DECLARED_GRAIN = {
    "code": "GRAIN_NOT_RESOLVED",
    "reason": "this candidate does not say what grain it is computed per, so a formula authored "
              "for it would be computed per something nobody chose; regenerate the candidate set",
}
_NO_GROUNDED_REFS = {
    "code": "CANDIDATE_UNGROUNDED",
    "reason": "this candidate names no columns that exist in the catalog, so there is nothing to "
              "author a formula over",
}


class _DraftBlocked(Exception):
    """A governed refusal discovered mid-drive, carrying the blocker that names it.

    An exception because the refusal is found deep inside authoring setup and has to unwind past
    several stages — but it is NOT a failure, and :func:`_drive` turns it into a BLOCKED draft
    rather than letting the poison guard record it as an outage.
    """

    def __init__(self, blocker: Mapping[str, str]) -> None:
        super().__init__(blocker.get("reason", ""))
        self.blocker = dict(blocker)


@dataclass(frozen=True, slots=True)
class FormulaDraftWorkerOutcome:
    status: str
    formula_draft_id: str | None = None
    state: str | None = None


def process_formula_draft_once(
    conn: psycopg.Connection,
    *,
    owner: str,
    lease_seconds: float = 600,
) -> FormulaDraftWorkerOutcome:
    """Claim at most one draft job and drive it to a terminal state.

    ``conn`` is used for the claim and for the lane's own bookkeeping. Each STAGE opens its own
    transaction and commits it, which is what keeps a provider call outside one.
    """
    claim = claim_formula_draft(conn, owner=owner, lease_seconds=lease_seconds)
    if claim is None:
        return FormulaDraftWorkerOutcome(status="idle")

    draft_id = str(claim.payload.get("formula_draft_id") or "")
    if not draft_id:
        # A message with no subject cannot be retried into one.
        fail_formula_draft(
            conn, claim, error="formula draft message carries no formula_draft_id", permanent=True)
        return FormulaDraftWorkerOutcome(status="permanent")

    try:
        state = _drive(conn, claim, draft_id, lease_seconds=lease_seconds)
    except _DraftBlocked as exc:
        # A product result. The queue message is COMPLETED, not failed: there is nothing to retry
        # and nothing for an operator to fix on this lane.
        state = _terminalize(conn, draft_id, DraftStateV1.BLOCKED, blockers=[exc.blocker])
        complete_formula_draft(conn, claim)
        counters.incr("featuregen.formula_draft.blocked")
        log("featuregen.formula_draft.blocked", formula_draft_id=draft_id,
            code=exc.blocker.get("code"))
        return FormulaDraftWorkerOutcome(
            status="ok", formula_draft_id=draft_id, state=state.value)
    except DraftRetired as exc:
        # ▲ ITS OWN ARM, and it must come before the generic one. Falling through to that handler
        # called `_terminalize`, which calls `advance`, which raises `DraftRetired` AGAIN — out of
        # the exception handler, so the queue item was never completed and stayed leased until its
        # lease expired, then redelivered to do the same thing again.
        #
        # The draft's state is NOT changed. It was retired, which is the answer; writing FAILED
        # over it would replace an operator's decision with a verdict about the candidate, and the
        # candidate was never the problem. The queue item COMPLETES: a retirement is a terminal
        # answer, and a redelivery would only reach the same one after paying for it.
        complete_formula_draft(conn, claim)
        counters.incr("featuregen.formula_draft.retired")
        log("featuregen.formula_draft.retired", formula_draft_id=draft_id, reason=str(exc))
        return FormulaDraftWorkerOutcome(status="retired", formula_draft_id=draft_id)
    except (LeaseFenceLost, RecoveryRequiresReconciliation) as exc:
        # TRANSIENT by nature: the lease moved under us, or a run needs reconciling before it can be
        # resumed. Neither is a statement about the candidate, so neither is written to the draft.
        fail_formula_draft(conn, claim, error=f"{type(exc).__name__}: {exc}", permanent=False)
        counters.incr("featuregen.formula_draft.retryable")
        return FormulaDraftWorkerOutcome(status="retryable", formula_draft_id=draft_id)
    except Exception as exc:  # noqa: BLE001 — bounded poison guard, as the shadow lane
        # Everything else is named on the DRAFT so a user sees why, and failed PERMANENTLY on the
        # queue so the same fault is not paid for again.
        _terminalize(conn, draft_id, DraftStateV1.FAILED, failure_reason=f"{type(exc).__name__}: {exc}")
        fail_formula_draft(conn, claim, error=f"{type(exc).__name__}: {exc}", permanent=True)
        counters.incr("featuregen.formula_draft.failed")
        log("featuregen.formula_draft.failed", level="error", formula_draft_id=draft_id,
            error=f"{type(exc).__name__}: {exc}")
        return FormulaDraftWorkerOutcome(
            status="permanent", formula_draft_id=draft_id, state=DraftStateV1.FAILED.value)

    complete_formula_draft(conn, claim)
    counters.incr(f"featuregen.formula_draft.{state.value.lower()}")
    log("featuregen.formula_draft.finished", formula_draft_id=draft_id, state=state.value)
    return FormulaDraftWorkerOutcome(
        status="ok", formula_draft_id=draft_id, state=state.value)


def _drive(
    conn: psycopg.Connection,
    claim: FormulaDraftQueueClaim,
    draft_id: str,
    *,
    lease_seconds: float,
) -> DraftStateV1:
    """The eight steps, each committing before the next."""
    draft = read_draft(conn, draft_id)
    if draft is None:
        raise ValueError(f"formula draft {draft_id} does not exist")
    if draft.state.is_terminal:
        # A redelivery of a message whose work is done. Completing it is the whole handling — and it
        # is why re-authoring is never triggered by a duplicate.
        return draft.state

    # ── 1. the FROZEN facts, and nothing else ────────────────────────────────────────────────────
    requested_by = conn.execute(
        "SELECT requested_by FROM formula_draft WHERE formula_draft_id = %s",
        (draft_id,)).fetchone()[0]
    facts = _frozen_facts(conn, draft.considered_revision_id, draft.option_id, requested_by)
    if facts.get("blocker") is not None:
        return _terminalize(conn, draft_id, DraftStateV1.BLOCKED, blockers=[facts["blocker"]])

    # ── 2-6. author, critique, parse, validate, persist ──────────────────────────────────────────
    if draft.state is DraftStateV1.REQUESTED:
        with conn.transaction():
            # STAMP THE RUN ID BEFORE THE RUN, not after it. The id is derived from the draft, so it
            # is known in advance — and recording it up front means it survives EVERY way the run
            # can end, including the ones that never return a result at all. An earlier version set
            # it only from the folded result, so a provider that refused outright produced a FAILED
            # draft naming no run, and there was nothing for an operator to go and look at.
            advance(conn, draft_id, DraftStateV1.AUTHORING,
                    authoring_run_id=_deterministic_run_id(draft_id))

    result = _author(conn, claim, draft_id, facts, lease_seconds=lease_seconds)

    # A RUN THAT PARSED NOTHING HAS NO FORMULA, and must not be walked towards READY carrying an
    # empty one. `{}` satisfies NOT NULL, so the original constraint let a READY draft carry nothing
    # at all — which every reader downstream would treat as a formula. Found end to end; the
    # constraint now forbids it too. It is BLOCKED with the disposition that explains why: the run
    # happened, it was paid for, and it did not yield an artifact.
    proposal = _proposal_material(result)
    if not proposal:
        # NO FORMULA — but the two reasons are not the same kind of thing, and the run says which.
        #
        # `technical_failure` means the platform could not complete: the provider refused, the
        # network went, something broke. That IS an outage and belongs to whoever is on call. Found
        # live — an expired API key surfaced as "this formula could not be produced", which reads as
        # a fact about the candidate and sends the user to re-examine a feature that was never the
        # problem. The run id rides along because an operator cannot investigate a run nobody named.
        #
        # Anything else — the model gave up, nothing parsed — is a product result about this
        # candidate, and BLOCKED is the honest word for it.
        if result.technical_status == "technical_failure":
            return _terminalize(
                conn, draft_id, DraftStateV1.FAILED,
                failure_reason=f"the authoring run could not complete (run "
                               f"{result.authoring_run_id}); this is a platform or provider fault, "
                               f"not a problem with the candidate")
        return _terminalize(conn, draft_id, DraftStateV1.BLOCKED, blockers=[{
            "code": "NO_FORMULA_PRODUCED",
            "reason": f"the authoring run finished as {result.authoring_disposition} without a "
                      f"parseable formula"}])

    # The run is folded. Walk the row to ADMISSION through whatever states the trace did not have
    # time to report — the path is fixed, and `advance` refuses a skip.
    #
    # The hash is the RUN'S OWN `candidate_proposal_hash`, not one computed here. It is the identity
    # every other stage of the platform keys on, and a second hashing of the same artifact is a
    # second opinion about what that artifact IS.
    _walk_to(conn, draft_id, DraftStateV1.ADMISSION,
             authoring_run_id=result.authoring_run_id,
             formula_content_hash=result.candidate_proposal_hash,
             formula_json=proposal)

    # ── 7-8. admission against the PINNED capability set ─────────────────────────────────────────
    return _admit(conn, draft_id, facts["intent"], result)


def _frozen_facts(
    conn: psycopg.Connection, revision_id: str, option_id: str, requested_by: str,
) -> dict[str, Any]:
    """Only what was frozen: the option, its grounded refs, its snapshot, and the asked hypothesis.

    Read here rather than carried on the queue message, because a payload is a copy and a copy can
    describe a revision that has since been superseded. The revision itself is immutable, so reading
    it late is reading the same bytes the request was validated against.
    """
    from featuregen.overlay.upload.contract.gate1 import (
        Gate1Error,
        UnknownConsideredOption,
        _chosen_option_from_revision,
    )

    row = conn.execute(
        "SELECT r.considered_json, r.metadata_snapshot_id, i.hypothesis, i.definition "
        "FROM contract_considered_revision r "
        "JOIN contract_intent i ON i.intent_id = r.intent_id "
        "WHERE r.considered_revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        raise ValueError(f"considered revision {revision_id} does not exist")

    # THE SNAPSHOT IS CHECKED FIRST, before the option is even resolved. With no frozen catalog
    # there is no world to author against, so whether this particular candidate resolves is moot —
    # and the answer a user needs ("regenerate the candidates") is the same either way.
    snapshot_id = row[1]
    if not snapshot_id:
        return {"blocker": _NO_SNAPSHOT}

    considered = row[0] if isinstance(row[0], dict) else {}
    try:
        idea, _source, _identity = _chosen_option_from_revision(considered, option_id)
    except (Gate1Error, UnknownConsideredOption) as exc:
        # A BLOCKER, not a failure — the same judgement the route makes when it answers 409/422
        # rather than 500. A revision that cannot resolve exact option identity is a fact about
        # what was STORED, with a remedy that belongs to whoever asked for the candidates.
        # Recording it as FAILED would page an operator about an outage that is not happening.
        return {"blocker": {"code": "CANDIDATE_UNRESOLVABLE", "reason": str(exc)}}

    # THE REFS THE SNAPSHOT IS KEYED BY, which are not the refs the candidate carries.
    #
    # `derives_from` is a bare object_ref (`public.bo_cib_customer.business_dt`) and the graph stores
    # object_refs PUBLIC-FLATTENED, while the snapshot is keyed by the SCHEMA-PRESERVING logical ref
    # (`cib::bo_dpl_cib.bo_cib_customer.business_dt`). Two things have to be added: the catalog
    # source, which `derives_pairs` carries precisely so downstream never re-derives it ambiguously,
    # and the real schema, which only the graph row knows.
    #
    # `logical_ref_of` is the SHIPPED translation for exactly this and does both. Rebuilding it here
    # by string concatenation is what produced the live failure this replaced: the refs looked
    # plausible, matched nothing in the snapshot, and the loader refused the whole draft.
    from featuregen.overlay.upload.column_authority import logical_ref_of

    if not idea.grain_refs:
        # WHAT THIS FEATURE IS COMPUTED PER is not optional. A candidate frozen before the typed
        # computation was carried through declares no grain at all, and authoring one against a
        # guessed grain produces a formula for a feature nobody asked for.
        return {"blocker": _NO_DECLARED_GRAIN}

    pairs = tuple(idea.derives_pairs or ())
    if not pairs:
        # No catalog source recorded, so no ref can be resolved. Not a failure — a candidate the
        # generator produced before pairs were carried, which cannot be authored against a snapshot.
        return {"blocker": _NO_GROUNDED_REFS}

    # TWO SETS, because they answer two different questions and live snapshots make the difference
    # visible: a real snapshot holds `column_field` items and NO table items at all.
    #
    #   `column_refs` — what the frozen catalog is loaded for. Every ref here must EXIST in the
    #      snapshot; the loader refuses the whole draft otherwise, and asking it for a table it was
    #      never built to hold is asking it to fail.
    #   `readable_refs` — what the model is permitted to NAME. The table belongs here: it is what a
    #      formula's `source_relation` points at, and the set costs nothing to widen.
    #
    # An earlier version passed one combined set to both and blocked live on
    # `SNAPSHOT_MISSING_REFS: ['cib::bo_dpl_cib.bo_cib_customer']` — a table the snapshot was never
    # going to contain.
    column_refs: set[str] = set()
    readable_refs: set[str] = set()
    for source, object_ref in pairs:
        column = logical_ref_of(conn, str(source), str(object_ref))
        column_refs.add(column)
        readable_refs.add(column)
        head, _, _tail = str(object_ref).rpartition(".")
        if head:
            readable_refs.add(logical_ref_of(conn, str(source), head, kind="table"))
    if not column_refs:
        return {"blocker": _NO_GROUNDED_REFS}

    return {
        "blocker": None,
        "snapshot_id": str(snapshot_id),
        "requested_by": requested_by,
        # What the snapshot is loaded for (must exist in it) …
        "column_refs": frozenset(column_refs),
        # … and what the model may name (wider: includes the tables).
        "allowed_refs": frozenset(readable_refs),
        "intent": AuthoringIntent(
            name=idea.name,
            hypothesis=row[2],
            target_entity=idea.grain_table or "",
            # THE DECLARED GRAIN, ORDERED, AND NO FALLBACK. This previously used every operand ref
            # when no grain was declared — a guess that always fired, because the serializer dropped
            # the typed computation and so a declared grain never survived to be read. Every formula
            # was authored against a grain listing each column it derives from.
            #
            # It must also agree EXACTLY with `restore_formula_v3._intent_of`: the restorer derives
            # this intent and the checkpoint compares the two hashes, so a worker that guessed and a
            # restorer that refused would make every draft unrestorable. One rule, stated twice, is
            # a rule that drifts — so both read `grain_refs` and both refuse without one.
            target_grain_keys=tuple(
                logical_ref_of(conn, str(source), str(ref)) for source, ref in idea.grain_refs),
        ),
    }


def _author(
    conn: psycopg.Connection,
    claim: FormulaDraftQueueClaim,
    draft_id: str,
    facts: Mapping[str, Any],
    *,
    lease_seconds: float,
):
    """Steps 2-6, inside the orchestrator, with NO transaction of ours held around it.

    ``run_authoring_v2_replay`` runs the bounded author, the critic, the typed parse, the
    deterministic validation and the immutable trace write — resumably, checkpointing as it goes.
    Reimplementing any of that here would be a second opinion about what a valid formula is.
    """
    principal = resolve_current_principal(
        conn, facts["requested_by"], None, datetime.now(UTC))
    if principal.status is not PrincipalResolutionStatus.CURRENT or principal.principal is None:
        # A GOVERNED REFUSAL, not an outage — the judgement the shipped shadow lane already makes,
        # which records the same condition on its AUTHORIZATION axis with `technical_axis: "OK"`
        # and `authoring_axis: "NOT_RUN"`. Nothing is broken: the platform declined to read the
        # catalog on behalf of a principal it cannot vouch for, which is the check working.
        #
        # Raised as a blocker rather than an exception because FAILED pages whoever is on call, and
        # the remedy here belongs to whoever administers accounts. Found live: a draft requested
        # under a header identity with no local account came back FAILED, sending an operator to
        # look for an incident that was not happening.
        raise _DraftBlocked({
            "code": f"REQUESTER_{principal.status.value.upper()}",
            "reason": principal.reason or (
                "the account that requested this draft cannot be verified, so the catalog was not "
                "read on its behalf"),
        })

    try:
        frozen = FrozenRecipeReadContext.load(
            conn, facts["snapshot_id"], facts["column_refs"])
    except ValueError as exc:
        # The frozen catalog does not describe a column this candidate needs. A fact about what was
        # SNAPSHOTTED, with a remedy (regenerate the candidates against a current snapshot) that
        # belongs to whoever asked for them — so a blocker, not an outage. Found live, reported as
        # FAILED, which sent an operator looking for an incident that was not happening.
        raise _DraftBlocked({
            "code": "SNAPSHOT_MISSING_REFS",
            "reason": str(exc)}) from exc
    client = current_llm_client()

    def progress() -> None:
        """Renew the lease, and move the row to whatever the TRACE says has begun.

        Both on the same hook because both are things that must happen between stages and never
        during one: at this point the orchestrator has committed its checkpoint and holds no
        transaction of ours.
        """
        _renew(claim, lease_seconds=lease_seconds)
        _sync_from_trace(draft_id)

    # ▲ DISPATCH BY THE STORED PLAN, NEVER BY RECOMPUTATION — owner ruling 2026-08-23 item 2. The
    # strategy was resolved at request time from evidence and FOLDED INTO the draft's identity; the
    # registry or a review moving since then must not re-route a draft whose identity claims the
    # first answer. A draft with no plan row predates the strategy contract and authors by LLM,
    # which is what every such draft's identity actually recorded.
    _recheck_authoring_decision(conn, draft_id)
    reviewed = _reviewed_blueprint_for(conn, draft_id)

    return run_authoring_v2_replay(
        conn,
        facts["intent"],
        client,
        client,
        roles=principal.principal.role_claims,
        actor=principal.principal,
        tool_runner=recipe_tool_runner_v2(facts["allowed_refs"], frozen_context=frozen),
        authoring_run_id=_deterministic_run_id(draft_id),
        facts_reader=frozen.formula_facts_v2,
        critic_metadata_loader=frozen.get_column_metadata,
        progress_callback=progress,
        formula_schema_version=_DRAFT_FORMULA_SCHEMA_VERSION,
        reviewed_blueprint=reviewed,
        # §11.2 — the ceiling the coordinator recorded, if the plan carries one: every physical
        # call this run makes reserves worst-case against it at the dispatch seam, and
        # exhaustion refuses BEFORE egress (PROVIDER_AUTH_ERROR, fail closed).
        spend=_spend_binding_for(conn, draft_id),
    )


def _spend_binding_for(conn, draft_id: str):
    """The plan row's approved ceiling as a per-call worst-case binding, or ``None``.

    Per-call worst case is the APPROVAL'S OWN ARITHMETIC — ``max_tokens / max_calls`` and
    ``max_cost / max_calls`` — so both ceilings are enforced jointly without this worker inventing
    a price: reserving ceiling/calls per call means the approved call count exactly exhausts the
    approved totals.
    """
    from decimal import Decimal

    from featuregen.overlay.upload.dispatch_audit import SpendBindingV1

    row = conn.execute(
        "SELECT a.spend_authorization_id, a.max_calls, a.max_tokens, a.max_cost "
        "  FROM formula_draft_authoring_plan p "
        "  JOIN llm_spend_authorization_revision a "
        "    ON a.spend_authorization_id = p.llm_spend_authorization_id "
        " WHERE p.formula_draft_id = %s", (draft_id,)).fetchone()
    if row is None:
        return None
    spend_authorization_id, max_calls, max_tokens, max_cost = row
    return SpendBindingV1(
        spend_authorization_id=spend_authorization_id,
        call_tokens=-(-int(max_tokens) // int(max_calls)),        # ceil division
        call_cost=Decimal(str(max_cost)) / int(max_calls))


def _frozen_grounding_context(conn, draft_id: str):
    """The grounding context the SERVING run froze for this draft's candidate, rehydrated.

    ▲ The premise this replaces was wrong, and the correction is what turned the lane on: the
    contexts ARE persisted — `considered_json["recipe_grounding_context_by_candidate_key"]` holds
    every served recipe candidate's context, written by gate1's engine pass at generation. The
    worker therefore binds against the SAME bytes the serving run bound with — never a
    re-grounding, whose answer could differ the moment the registry or catalog moved.

    ``None`` — honest absence — for a legacy revision frozen before the engine pass, an option
    whose candidate key is ambiguous, or a context that never bound. The resolver's bindability
    fact reads the same source at request time, so a REVIEWED plan should always find one here;
    ``None`` despite a REVIEWED plan is a plan/revision disagreement, blocked loudly by the caller.
    """
    from featuregen.overlay.upload.contract.gate1 import (
        _chosen_option_from_revision,
        _revision_recipe_candidate_key,
    )
    from featuregen.overlay.upload.recipe_grounding_context import (
        grounding_context_from_json,
    )

    row = conn.execute(
        "SELECT d.option_id, r.considered_json "
        "  FROM formula_draft d "
        "  JOIN contract_considered_revision r "
        "    ON r.considered_revision_id = d.considered_revision_id "
        " WHERE d.formula_draft_id = %s", (draft_id,)).fetchone()
    if row is None:
        return None
    option_id, considered = row
    considered = considered if isinstance(considered, dict) else {}
    try:
        idea, _source, _identity = _chosen_option_from_revision(considered, option_id)
    except Exception:  # noqa: BLE001 — a legacy revision that cannot resolve options has no context
        idea = None
    key = _revision_recipe_candidate_key(considered, option_id=option_id, feature=idea)
    payload = (considered.get("recipe_grounding_context_by_candidate_key") or {}).get(key)         if key else None
    if not isinstance(payload, dict):
        return None
    return grounding_context_from_json(payload)


#: ▲ THE BOUND-CONTEXT SEAM for the reviewed lane — defaulting to the REAL loader above. Kept as a
#: seam (module attribute, not a hard call) because the reviewed path's tests drive it with
#: hand-built contexts, and because a deployment that must disable the lane does it by posture in
#: `formula_strategy_facts`, never by surgery here.
_BOUND_CONTEXT_LOADER = _frozen_grounding_context


def _recheck_authoring_decision(conn, draft_id: str) -> None:
    """§8.2's SECOND LOOK for authoring (Stage I Task 5) — mirror of the generation lane's shape.

    A draft with a plan row is a GOVERNED draft: the service records its AUTHOR_FORMULA decision
    on that row in the same transaction, so a plan row with NULL here can only be a bypass (a
    hand-enqueued draft) or a pre-1119 plan — and both refuse as `ACTION_DECISION_MISSING`,
    because absence must never read as permission. A draft with NO plan row predates the
    strategy contract entirely and keeps its documented legacy path. Drift is a REFUSAL a person
    re-requests, never a re-decision.
    """
    from featuregen.materialize.action_decision import DecisionDrift, DecisionMissing, recheck
    from featuregen.overlay.upload.formula_draft_service import authoring_evidence_pins
    from featuregen.overlay.upload.retirement_scope import retirement_scope_key

    row = conn.execute(
        "SELECT p.action_decision_revision_id, p.strategy_identity_hash, "
        "       d.considered_revision_id, d.option_id, d.planning_request_hash, "
        "       d.catalog_snapshot_hash, d.definition_revision "
        "  FROM formula_draft_authoring_plan p "
        "  JOIN formula_draft d ON d.formula_draft_id = p.formula_draft_id "
        " WHERE p.formula_draft_id = %s", (draft_id,)).fetchone()
    if row is None:
        return                                # pre-contract draft: the documented legacy path
    decision_id = row[0]
    if decision_id is None:
        raise _DraftBlocked({
            "code": "ACTION_DECISION_MISSING",
            "reason": "this governed draft carries no request-time AUTHOR_FORMULA decision — a "
                      "queue bypass by definition; request it again through the service"})
    pins = authoring_evidence_pins(
        retirement_scope_key=retirement_scope_key(
            considered_revision_id=row[2], option_id=row[3], planning_request_hash=row[4],
            catalog_snapshot_hash=row[5], definition_revision=row[6]),
        catalog_snapshot_hash=row[5], strategy_identity_hash=row[1])
    try:
        recheck(conn, decision_id, current_pins=pins)
    except DecisionMissing as exc:
        raise _DraftBlocked({"code": "ACTION_DECISION_MISSING", "reason": str(exc)}) from exc
    except DecisionDrift as exc:
        raise _DraftBlocked({"code": "ACTION_DECISION_DRIFTED", "reason": str(exc)}) from exc


def _reviewed_blueprint_for(conn, draft_id: str):
    """The BOUND blueprint this draft's plan pinned, verified — or ``None`` for the LLM lane.

    Raises:
        _DraftBlocked: the plan says REVIEWED and the blueprint cannot be rebuilt, has moved since
            the plan pinned it, or no bound context is obtainable. ▲ Named, never a silent LLM
            fallback — a fallback would hide a broken reviewed blueprint, change the cost, and
            change which certificate production needs.
    """
    plan = conn.execute(
        "SELECT formula_strategy, recipe_id, reviewed_blueprint_revision, "
        "       reviewed_blueprint_hash "
        "  FROM formula_draft_authoring_plan WHERE formula_draft_id = %s",
        (draft_id,)).fetchone()
    if plan is None or plan[0] != "REVIEWED_RECIPE_BLUEPRINT":
        return None

    from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
        BlueprintDerivationRefusal,
        derive_blueprint_v2,
    )
    from featuregen.overlay.upload.recipe_formula_contracts import (
        RecipeFormulaPreflightError,
    )
    from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
        bind_formula_expectation_v2,
        expectation_content_hash_v2,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    _strategy, recipe_id, pinned_revision, pinned_hash = plan
    definition = v2_recipe_by_id(recipe_id)
    derived = None if definition is None else derive_blueprint_v2(definition)
    if derived is None or isinstance(derived, BlueprintDerivationRefusal):
        raise _DraftBlocked({
            "code": "REVIEWED_BLUEPRINT_NOT_EXECUTABLE",
            "reason": f"the plan pinned a reviewed blueprint for {recipe_id!r} and the registry "
                      f"no longer derives one: "
                      f"{getattr(derived, 'code', 'recipe missing')}"})
    # ▲ THE PLAN PINNED BYTES, AND THE BYTES MUST NOT HAVE MOVED. The draft's identity folded the
    # strategy over THIS blueprint; authoring a newer one under the old identity would seal a
    # method claim about bytes nobody chose.
    if expectation_content_hash_v2(derived) != pinned_hash:
        raise _DraftBlocked({
            "code": "REVIEWED_BLUEPRINT_NOT_EXECUTABLE",
            "reason": f"the reviewed blueprint for {recipe_id!r} moved after this draft's plan "
                      f"pinned it ({pinned_hash} -> {expectation_content_hash_v2(derived)}); a "
                      f"NEW draft request resolves against the current one"})

    context = _BOUND_CONTEXT_LOADER(conn, draft_id) if _BOUND_CONTEXT_LOADER else None
    if context is None:
        raise _DraftBlocked({
            "code": "REVIEWED_BLUEPRINT_NOT_EXECUTABLE",
            "reason": "the frozen considered revision holds no grounding context for this "
                      "draft's candidate; the resolver's bindability fact reads the same source, "
                      "so a REVIEWED plan reaching this worker without one is a plan/revision "
                      "disagreement"})
    try:
        return bind_formula_expectation_v2(context, derived)
    except RecipeFormulaPreflightError as exc:
        raise _DraftBlocked({
            "code": "REVIEWED_BLUEPRINT_NOT_EXECUTABLE",
            "reason": f"the reviewed blueprint for {recipe_id!r} does not bind to this "
                      f"candidate: {exc.code}"}) from None


def _admit(
    conn: psycopg.Connection, draft_id: str, intent: AuthoringIntent, result,
) -> DraftStateV1:
    """Step 7-8 — decide admissibility against the advertised set, and record it separately.

    The decision is written to ``formula_draft_admission`` under its OWN identity whether it admits
    or refuses, so a later capability set can be compared against what was decided rather than
    re-deciding blindly. That is what makes an engine gaining an operator free.
    """
    import os

    engine_id = os.environ.get(FORMULA_DRAFT_ENGINE_ENV, "").strip()
    draft = read_draft(conn, draft_id)
    # LOUD, not skipped. By the time admission runs the walk to ADMISSION has stored the formula and
    # its hash; if either is missing, something upstream is wrong and recording an admission "about"
    # a formula nobody can name would file a decision against nothing. An earlier draft of this
    # function guarded with `if formula_hash:` and silently skipped — which would have produced a
    # READY draft with no admission row, indistinguishable from a deployment that names no engine.
    if draft is None or not draft.formula_content_hash:
        raise RuntimeError(
            f"formula draft {draft_id} reached admission with no stored formula hash")
    formula_hash = draft.formula_content_hash

    if not engine_id:
        # No engine named by this deployment. The FORMULA is still real and the user can read it —
        # what is missing is a decision about where it could run, and that is exactly what the
        # separate admission identity models. Recording an admission against an unnamed engine would
        # be inventing the one fact `admit_artifacts_v2` refuses to default.
        log("featuregen.formula_draft.admission_skipped", formula_draft_id=draft_id,
            reason=f"{FORMULA_DRAFT_ENGINE_ENV} is unset")
        return _terminalize(conn, draft_id, DraftStateV1.READY)

    # THE ADMISSION IDENTITY IS KEYED ON WHAT GATES IT — the renderer-supported set, since that is
    # what admission now decides against. Keying it on the qualified set instead would re-decide
    # every formula the first time a gold proof landed, buying nothing: proofs change whether the
    # platform VOUCHES for an operator, not whether the formula may be compiled.
    supported = renderer_supported_operators(conn, engine_id=engine_id)
    try:
        admit_artifacts_v2(
            conn, [ResolvedFeatureInputV2(intent, result)], engine_id=engine_id)
    except MaterializationRefused as exc:
        # The code is TYPED on the exception by construction ("never a bare string"), so it is
        # read rather than reconstructed — the same code the corpus report and the execution screen
        # explain, which is what stops one refusal being described two ways.
        blockers = [{"code": exc.code.value, "reason": exc.detail or str(exc)}]
        with conn.transaction():
            record_admission(
                conn, formula_draft_id=draft_id, formula_content_hash=formula_hash,
                engine_id=engine_id, advertised=supported, admitted=False, blockers=blockers)
        return _terminalize(conn, draft_id, DraftStateV1.BLOCKED, blockers=blockers)

    with conn.transaction():
        record_admission(
            conn, formula_draft_id=draft_id, formula_content_hash=formula_hash,
            engine_id=engine_id, advertised=supported, admitted=True)

    # QUALIFICATION IS REPORTED, NOT REQUIRED. The formula compiles, so a user may read the code —
    # but every operator nobody has proved against reviewed gold is named here, because "unverified"
    # on its own tells a user nothing to act on and an operator nothing to go and prove.
    #
    # These are NOT blockers. A blocker means the draft could not be produced; this draft was
    # produced and is readable. Recording them as blockers would refuse a formula for the platform's
    # own incomplete evidence, which is the confusion this whole step exists to remove.
    unqualified = unqualified_operators(
        conn, engine_id=engine_id, signatures=implied_operator_signatures(result.candidate_proposal))
    if unqualified:
        log("featuregen.formula_draft.unqualified_operators", formula_draft_id=draft_id,
            engine_id=engine_id,
            operators=[f"{kind}:{variant}" for kind, variant in unqualified],
            detail="the formula compiles and may be read; these operators have no gold proof yet, "
                   "so the platform does not vouch for the numbers they would produce")
    return _terminalize(conn, draft_id, DraftStateV1.READY)


# ── state movement ───────────────────────────────────────────────────────────────────────────────
def _observed_state(conn: psycopg.Connection, run_id: str) -> DraftStateV1 | None:
    """The FURTHEST state the trace proves has begun, or ``None`` if it proves nothing yet.

    The furthest rather than the latest, because a multi-turn run genuinely revisits the author
    after a validation and the draft's state does not walk backwards with it.
    """
    kinds = {row[0] for row in conn.execute(
        "SELECT DISTINCT kind FROM formula_authoring_trace_event WHERE authoring_run_id = %s",
        (run_id,)).fetchall()}
    reached = [_KIND_STATE[kind] for kind in kinds if kind in _KIND_STATE]
    if not reached:
        return None
    return max(reached, key=_PATH.index)


def _walk_to(
    conn: psycopg.Connection, draft_id: str, target: DraftStateV1, **payload: Any,
) -> DraftStateV1:
    """Advance one legal step at a time up to ``target``, committing each.

    A loop rather than a jump because ``advance`` refuses a skip on purpose: a READY draft whose
    trace never records a critic is exactly what that refusal exists to prevent, and a caller that
    wanted to skip would be asking the state machine to lie. The result payload rides on the LAST
    step, so a crash part-way leaves a row that has not yet claimed a formula it has not stored.
    """
    while True:
        draft = read_draft(conn, draft_id)
        if draft is None or draft.state.is_terminal:
            return draft.state if draft else target
        index = _PATH.index(draft.state)
        if index >= _PATH.index(target):
            return draft.state
        step = _PATH[index + 1]
        with conn.transaction():
            advance(conn, draft_id, step, **(payload if step is target else {}))


def _terminalize(
    conn: psycopg.Connection,
    draft_id: str,
    state: DraftStateV1,
    *,
    blockers: Sequence[Mapping[str, str]] = (),
    failure_reason: str | None = None,
) -> DraftStateV1:
    """Put the draft in a terminal state, walking the path first only when READY requires it.

    ONLY READY needs the walk, and the asymmetry is deliberate. BLOCKED, FAILED and CANCELLED are
    reachable from anywhere because each can be truthfully known early — a candidate with no pinned
    catalog snapshot is blocked at REQUESTED, before a model is asked anything. READY may only be
    declared after the run that produced the formula, so it walks the path and `advance` refuses any
    skip: a READY draft whose trace records no critic stays impossible.
    """
    if state is DraftStateV1.READY:
        _walk_to(conn, draft_id, DraftStateV1.ADMISSION)
    try:
        with conn.transaction():
            advance(conn, draft_id, state, blockers=blockers, failure_reason=failure_reason)
    except InvalidTransition:
        # Already terminal — a redelivery, or a race with a cancellation. The stored verdict wins:
        # overwriting it would let a retry silently replace a user's cancel with a READY.
        current = read_draft(conn, draft_id)
        return current.state if current else state
    return state


def _sync_from_trace(draft_id: str) -> None:
    """Move the row to whatever the trace proves, on the lane's OWN connection.

    Its own connection because the orchestrator's connection is mid-run and its checkpoint
    transaction is not ours to join; a state update inside it would be rolled back with any turn
    that is retried, and the user's screen would go backwards.

    Best-effort by design: this is progress reporting. A failure here must never fail a draft that
    the model is in the middle of authoring — the row simply reports the earlier stage a moment
    longer, and the terminal write below is what the result actually depends on.
    """
    dsn = get_settings().dsn
    if not dsn:
        return
    try:
        with psycopg.connect(dsn, autocommit=True) as sync_conn:
            draft = read_draft(sync_conn, draft_id)
            if draft is None:
                return
            # CHECKED EXPLICITLY, before anything else. A retired draft may have no state to walk
            # to — the trace may show nothing new since the last turn — so relying on `_walk_to`
            # raising would let the loop continue whenever there was no transition to attempt. The
            # question here is not "has it progressed" but "should this run still be spending".
            if draft.is_retired:
                raise DraftRetired(
                    f"formula draft {draft_id} was retired "
                    f"({draft.retirement.reason}) while it was being authored")
            if draft.state.is_terminal or draft.authoring_run_id is None:
                return
            observed = _observed_state(sync_conn, draft.authoring_run_id)
            if observed is None:
                return
            _walk_to(sync_conn, draft_id, observed)
    except DraftRetired:
        # ▲ NOT a progress-reporting failure, and this is the one exception to "best-effort".
        # `_sync_from_trace` runs as the authoring loop's progress callback, i.e. BETWEEN provider
        # calls — so swallowing this here meant the loop carried on and paid for the next turn of a
        # draft somebody had already withdrawn. Re-raised so the run stops at the nearest turn
        # boundary, which is the whole purpose of withdrawing one.
        raise
    except Exception as exc:  # noqa: BLE001 — progress reporting never fails a paid run
        log("featuregen.formula_draft.progress_unavailable", level="warning",
            formula_draft_id=draft_id, error=f"{type(exc).__name__}: {exc}")


def _renew(claim: FormulaDraftQueueClaim, *, lease_seconds: float) -> None:
    """Extend the lease durably, or lose the fence loudly.

    On its own connection for the shadow lane's reason: the renewal has to be visible to every
    other worker immediately, and a renewal inside the run's transaction would not be until it
    commits — by which time the lease it was extending has already expired.
    """
    dsn = get_settings().dsn
    if not dsn:
        raise LeaseFenceLost("formula draft lease cannot be renewed durably")
    try:
        with psycopg.connect(dsn) as lease_conn:
            renewed = renew_formula_draft(lease_conn, claim, lease_seconds=lease_seconds)
    except Exception as exc:
        raise LeaseFenceLost("formula draft lease renewal is unavailable") from exc
    if not renewed:
        raise LeaseFenceLost("formula draft lease was lost")


def _deterministic_run_id(draft_id: str) -> str:
    """One authoring run per draft, named from the draft.

    Deterministic so a resumed job RESUMES rather than opening a second run: the orchestrator is
    replay-shaped and will pick up its own checkpoints, which is the difference between a retry that
    finishes the work and a retry that pays for it again.
    """
    return f"far-{canonical_hash({'formula_draft_id': draft_id})[:32]}"


def _proposal_material(result) -> dict[str, Any]:
    """The folded proposal as plain JSON, for storage.

    ``candidate_proposal`` is ``None`` when nothing parsed — an outcome the caller turns into a
    BLOCKED draft rather than a READY one holding ``{}``.
    """
    from dataclasses import asdict, is_dataclass

    proposal = result.candidate_proposal
    if proposal is None:
        return {}
    if is_dataclass(proposal):
        return _plain(asdict(proposal))
    if isinstance(proposal, Mapping):
        return _plain(dict(proposal))
    return {}


def _plain(value: Any) -> Any:
    """Enums to their values, tuples to lists — a shape ``jsonb`` and a hash both accept."""
    from enum import Enum

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
