"""Draft formula — the ASYNC per-candidate authoring request, and its polling read.

**Two endpoints, and neither runs a model.**

* ``POST /considered-revisions/{revision_id}/options/{option_id}/formula-drafts`` validates the
  candidate against the frozen revision, records the request in a SHORT transaction, enqueues the
  work and returns ``202``.
* ``GET /formula-drafts/{formula_draft_id}`` reports where it has got to.

The route holds ``get_conn``'s transaction for one INSERT and one outbox write. Two provider calls
plus validation plus admission happen in the worker, which commits between every stage — so a client
disconnect orphans nothing and the database is never held open across a model.

**DRAFTING IS NOT SELECTING, and this module cannot make it so.** ``POST /contract/draft`` records a
Gate-1 choice as its first act: on that route, drafting IS selecting. The product rule is the
opposite — a user must be able to inspect a formula and then decide — so this route exists separately
and imports no selection writer at all. A test asserts that absence, because "we simply do not call
it" is a habit and an import is a fact.

**Double-clicking must not buy two answers.** ``request_draft`` is idempotent on the FORMULA
IDENTITY, not on a caller-supplied key: a client minting a fresh key per click would defeat a
key-based guard, and what is being protected is money. The second call returns ``202`` with the same
id and ``created: false`` — the client's question ("is a draft coming for this candidate?") has the
same answer either way.

**The candidate comes from the SERVER's frozen record.** The revision and option are looked up, never
taken on trust from the body — the rule ``contract.py`` calls BLOCKER 1: a client payload naming its
own candidate would author against a definition nobody froze.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import get_conn, get_identity, require_permission
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.formula_draft_store import (
    DraftRetired,
    read_draft,
    request_draft,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.outbox import OutboxMessage, insert_outbox_message_checked

__all__ = ["FORMULA_DRAFT_HANDLER", "FORMULA_DRAFT_TOPIC", "router"]

#: The lane this work travels on. Named here beside the producer, and consumed by the worker's route
#: map — one string, two readers, so a rename cannot half-land.
FORMULA_DRAFT_TOPIC = "formula_draft.requested.v1"
FORMULA_DRAFT_HANDLER = "formula_draft.author.v1"

DRAFT_ID_HEADER = "X-Formula-Draft-Id"
_DRAFT_PREFIX = "fd"

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


def _frozen_candidate(conn: psycopg.Connection, revision_id: str, option_id: str) -> dict[str, Any]:
    """The option AS FROZEN on its considered revision, or a 404/422.

    Read server-side rather than accepted from the body (contract.py's BLOCKER 1): a client naming
    its own candidate would have the model author against a definition nobody froze, and the draft's
    identity would then pin a snapshot that never described it.

    The option is resolved through ``gate1._chosen_option_from_revision`` — the SHIPPED resolver,
    which also cross-checks the opaque option map against its public projection. There is no
    ``contract_considered_option`` table: options live inside ``considered_json``, and a second
    reader walking that blob would be a second opinion about which candidate an id names.
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
        raise HTTPException(status_code=404, detail="unknown considered revision")

    considered = revision[2] if isinstance(revision[2], dict) else {}
    try:
        idea, _source, candidate_identity = _chosen_option_from_revision(considered, option_id)
    except UnknownConsideredOption as exc:
        # 422 rather than 404: the revision exists and this option is not part of it — a stale tab
        # naming an option from a superseded revision, which is a client error with a fixable cause.
        raise HTTPException(
            status_code=422,
            detail="option is not part of this considered revision") from exc
    except Gate1Error as exc:
        # The revision itself does not support exact option identity. A 409 rather than a 500: it is
        # a fact about the stored revision, and the remedy is to regenerate the considered set.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "considered_revision_id": revision[0],
        # The frozen catalog the model will be given. Part of the draft's identity, so a catalog
        # that moves produces a DIFFERENT draft rather than silently reusing an answer about a world
        # that no longer exists. Falls back to the revision's own content hash when no metadata
        # snapshot was pinned — a stable value either way, never an empty one.
        "catalog_snapshot_hash": revision[1] or revision[3],
        # WHAT was asked, pinned: a re-ask under a changed question is a different draft.
        "planning_request_hash": candidate_identity,
        # The user's own revision of the definition. Editing it is asking a different question, so
        # it must not be served the previous answer.
        "definition_revision": getattr(idea, "definition", "") or "",
    }


@router.post(
    "/considered-revisions/{revision_id}/options/{option_id}/formula-drafts",
    status_code=202,
    dependencies=[Depends(require_permission("feature:generate"))],
)
def request_formula_draft(
    revision_id: str, option_id: str, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Record a formula-draft request and enqueue it. Returns ``202`` — never a formula.

    **This route never selects.** It records that a formula was ASKED FOR; the Gate-1 choice is a
    separate user action on a separate route. Inspecting must be free.

    The request row and the queue message are written in the SAME transaction, so there is no window
    where a draft exists with nobody to drive it, and none where a queue row names a draft that is
    not there.
    """
    candidate = _frozen_candidate(conn, revision_id, option_id)
    minted = mint_id(_DRAFT_PREFIX)

    try:
        draft_id, created = request_draft(
            conn,
            formula_draft_id=minted,
            considered_revision_id=candidate["considered_revision_id"],
            option_id=option_id,
            planning_request_hash=candidate["planning_request_hash"],
            catalog_snapshot_hash=candidate["catalog_snapshot_hash"],
            authoring_config_hash=_authoring_config_hash(),
            definition_revision=candidate["definition_revision"],
            requested_by=identity.subject,
            requested_at=_now(conn))
    except DraftRetired as exc:
        # ▲ 409, NOT a 500. `request_draft` raises this deliberately — the identity belongs to a
        # RETIRED draft — and with nothing catching it the global handler turned a considered
        # refusal into "Internal Server Error", which tells a caller that the platform broke rather
        # than that their request asked for something withdrawn.
        #
        # 409 rather than 422: the request is well-formed and would have been valid yesterday. What
        # conflicts is the state of the world. The body carries what somebody actually needs to act
        # — which draft, why, what replaced it, and WHICH input has to change — because
        # `formula_identity_hash` is unique, so retrying with a new draft id lands on the same row.
        retired = _retired_detail(conn, candidate, option_id)
        raise HTTPException(status_code=409, detail={
            "code": "FORMULA_DRAFT_RETIRED",
            "message": str(exc),
            **retired,
            "identity_bearing_inputs": [
                "authoring_config_hash", "catalog_snapshot_hash",
                "planning_request_hash", "definition_revision"],
            "remedy": ("this identity was retired, and a new draft id does not change it — either "
                       "use the replacement, or re-request once an identity-bearing input has "
                       "genuinely changed"),
        }) from exc

    if created:
        # Enqueued ONLY for a genuinely new draft. Re-enqueuing an existing one would put a second
        # job on the lane for work already in flight or already finished.
        insert_outbox_message_checked(
            conn,
            OutboxMessage(
                message_id=f"formula-draft:{draft_id}",
                partition_key=f"formula-draft:{candidate['considered_revision_id']}",
                topic=FORMULA_DRAFT_TOPIC,
                payload={"formula_draft_id": draft_id}))

    response.headers[DRAFT_ID_HEADER] = draft_id
    counters.incr("featuregen.formula_draft.requested" if created
                  else "featuregen.formula_draft.deduplicated")
    log("featuregen.formula_draft.requested", formula_draft_id=draft_id,
        considered_revision_id=revision_id, option_id=option_id, created=created)
    return {
        "formula_draft_id": draft_id,
        "status": "requested",
        "stage": "queued",
        # FALSE is the double-click answer, and it is reported rather than hidden: a client that
        # showed "started" for a request that started nothing would be describing a spend that did
        # not happen.
        "created": created,
        "detail": ("the formula draft was requested; a worker authors it" if created else
                   "an identical draft already exists for this candidate, catalog snapshot and "
                   "configuration — nothing was queued and nothing was spent"),
    }


def _retired_detail(conn, candidate, option_id: str) -> dict[str, object]:
    """Which draft was retired, why, and what replaced it — read for the 409 body.

    A refusal that named only "retired" would send the caller to ask three more questions, and the
    replacement is usually the answer they need.
    """
    from featuregen.overlay.upload.formula_draft_store import formula_identity

    identity = formula_identity(
        considered_revision_id=candidate["considered_revision_id"], option_id=option_id,
        planning_request_hash=candidate["planning_request_hash"],
        catalog_snapshot_hash=candidate["catalog_snapshot_hash"],
        authoring_config_hash=_authoring_config_hash(),
        definition_revision=candidate["definition_revision"])
    row = conn.execute(
        "SELECT d.formula_draft_id, r.reason, r.detail, r.replacement_draft_id "
        "  FROM formula_draft d "
        "  JOIN formula_draft_retirement r ON r.formula_draft_id = d.formula_draft_id "
        " WHERE d.formula_identity_hash = %s", (identity,)).fetchone()
    if row is None:
        return {"formula_identity_hash": identity}
    return {
        "formula_identity_hash": identity,
        "retired_draft_id": row[0],
        "reason": row[1],
        "detail": row[2],
        "replacement_draft_id": row[3],
    }


@router.get("/formula-drafts/{formula_draft_id}",
            dependencies=[Depends(require_permission("feature:generate"))])
def formula_draft_status(formula_draft_id: str, conn: _Conn) -> dict[str, Any]:
    """Where a draft has got to, and what it produced once it has.

    Polling is the first release's answer deliberately: it needs no connection held open, survives a
    client reload, and the row it reads is the same durable record the worker advances. SSE can be
    added over the identical state later without changing what a state MEANS.
    """
    draft = read_draft(conn, formula_draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="unknown formula draft")

    return {
        "formula_draft_id": draft.formula_draft_id,
        "considered_revision_id": draft.considered_revision_id,
        "option_id": draft.option_id,
        "state": draft.state.value,
        # The words the card shows, from the SERVER, so the API and the screen cannot describe one
        # state with two different sentences.
        #
        # ▲ RETIREMENT OVERRIDES THE LABEL. Retirement is an append BESIDE the draft, never an edit
        # of it, so a retired draft's `state` still says READY — and a card rendered from `state`
        # alone said "Formula ready" about something an operator had withdrawn. The state is still
        # reported truthfully; what the screen is told to SHOW accounts for the retirement.
        "stage": ("Retired" if draft.is_retired else draft.stage_label),
        "terminal": draft.state.is_terminal or draft.is_retired,
        # The whole retirement, not a boolean: "retired" alone sends a person to ask why, and the
        # replacement is usually the answer they actually need.
        "retired": draft.is_retired,
        "retirement": (None if draft.retirement is None else {
            "reason": draft.retirement.reason,
            "detail": draft.retirement.detail,
            "replacement_draft_id": draft.retirement.replacement_draft_id,
            "retired_by": draft.retirement.retired_by,
            "retired_at": draft.retirement.retired_at,
        }),
        "formula_source": "llm_authored",
        "authoring_run_id": draft.authoring_run_id,
        "formula_content_hash": draft.formula_content_hash,
        "formula": draft.formula_json,
        # BLOCKED is a product result and its blockers are the answer, not an error payload.
        "blockers": list(draft.blockers),
        "failure_reason": draft.failure_reason,
    }


def _authoring_config_hash() -> str:
    """The authoring configuration and model contract this build would author under.

    Read from the shipped settings rather than recorded per request, because it describes the
    BUILD: two drafts authored under different model contracts are not interchangeable, and the
    identity has to notice.
    """
    from featuregen.formula.audited import current_formula_generation_settings

    settings = current_formula_generation_settings()
    from featuregen.canonical import jcs_sha256

    return jcs_sha256({
        "model": getattr(settings, "model", ""),
        "max_tokens": getattr(settings, "max_tokens", 0),
        "prompt_id": getattr(settings, "prompt_id", ""),
    })


def _now(conn: psycopg.Connection) -> str:
    """The database's clock, so two application instances cannot disagree about when."""
    return str(conn.execute("SELECT now()").fetchone()[0])
