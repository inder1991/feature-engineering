"""Phase G §3.1 — the TRIGGER SURFACE: materialization becomes something a person can invoke.

``POST /materialization-runs`` mints the durable request and enqueues the job; ``GET
/materialization-runs/{request_id}`` reports what became of it. Both are flag-gated, both are
``require_confirmer``-gated, and neither runs a compile.

**THE ROUTE MUST NOT RUN THE CHAIN, and that is settled on evidence rather than on taste.**
``get_conn`` holds ONE connection and ONE transaction open for the whole life of a request
(``api/deps.py:100-116``), and a compile is bounded by ``COMPILE_BUDGET_SECONDS`` (600s) plus the
deployment's configured L0 timeout — with ``LocalClusterSubmitter.timeout_seconds`` defaulting to
3600s once G-2 lands. A route that compiled inline would hold a database transaction open for tens
of minutes, and a client disconnect or an app restart would orphan the work with a half-written
project tree behind it. So this surface is a PRODUCER: it validates what is cheap to validate,
freezes the declarations onto the queue in the same transaction that mints the request, and returns
``202``. The worker (``materialize/queue_lane.py``) is the only thing that compiles. The test suite
reads this module's AST and asserts it never names ``compile_feature_group``.

**ONE SWITCH, NOT TWO.** :func:`~featuregen.materialize.queue_lane.materialization_enabled` is
imported and consulted — the same function object the worker stage calls, over the same truthy set
(``{"1", "true", "yes", "on"}`` after ``.strip().lower()``). A route that re-implemented the check as
``== "1"`` would 404 for a deployment that wrote ``FEATUREGEN_MATERIALIZE_ENABLED=true`` while its
worker happily drained the backlog — a split brain whose symptom ("the API says this feature does
not exist") points nowhere near its cause. The switch is also the FIRST dependency of every route
here, ahead of authentication and body validation, so a flag-off deployment opens no connection,
parses no declaration and is indistinguishable from one where these paths were never registered.

**WHY 404 AND NOT 503.** A 503 says "this exists and is unwell"; the flag says "this deployment does
not run materialization at all". The refusal body is Starlette's own ``{"detail": "Not Found"}``,
byte for byte, so the surface leaks nothing about whether the feature is built.

**THE MEMBERS ARE SUPPLIED, BECAUSE NOTHING DURABLE MAPS THEM (Task 2's §15.2, unresolved).** There
is no group→member table anywhere in the tree: ``materialization_request`` carries the group NAME,
``group_binding`` maps that name to a contract hash and a physical target, and
``group_plan_revision`` records a plan HASH — none of them stores membership, and ``work_item_id``
appears in no schema outside ``recipe_formula_shadow_work_item`` itself. ``resolve_feature_inputs``
therefore takes a set of work-item ids, ``compile_feature_group`` says in its own docstring that
"nothing maps a logical group to its members yet, so the caller supplies them", and so does this
route. What it CAN do — and does, before minting anything — is prove that every id names a real
``recipe_formula_shadow_work_item`` row, which is the one durable anchor from which both the
authoring run and the authoring intent are recoverable (``materialize/resolve.py``). Inventing a
mapping table here would need a migration this task may not add, and guessing membership from a
previous generation's plan would compile a membership nobody decided.

**A 202 IS A PROMISE THAT SOMETHING WILL RUN, so it is only made when something will.**
``record_request`` returns the EXISTING row on an idempotency hit (correctly: a retried HTTP call
must not become a second run) and ``enqueue_checked`` returns the EXISTING queue id — including one
the worker has dead-lettered. Composed naively, those two correct behaviours produce a route that
answers ``202`` over a job no worker will ever claim again, forever, silently.
:func:`_refuse_if_nothing_will_run` is the one place that rule lives: a request is accepted only
when it is non-terminal AND its queue row is still claimable. Everything else is a ``409`` that
NAMES what it found and says what to do about it. See that function for why re-arming the dead row
was rejected, and why minting a fresh key on the caller's behalf is worse than either.
"""

from __future__ import annotations

import dataclasses
import datetime as _datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.control_plane import (
    RunEventKind,
    fold_run_status,
    read_run_events,
)
from featuregen.materialize.publish import read_active_revision
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    PAYLOAD_VERSION,
    MaterializationJobV1,
    decode_job,
    enqueue_materialization,
    materialization_enabled,
    materialization_message_id,
)
from featuregen.materialize.request_store import (
    MaterializationRequestV1,
    RequestLifecycle,
    read_request,
    record_request,
)
from featuregen.materialize.validation import (
    ValidationLevel,
    ValidationStatus,
    read_validation_reports,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.queue import QueueIdempotencyConflict

#: The 202's id, echoed as a header for the same reason ``X-Ingestion-Run-Id`` is
#: (``uploads.py:157``): a client that lost the body can still name the work it started.
REQUEST_ID_HEADER = "X-Materialization-Request-Id"

#: The id prefix. A materialization request is not the Phase-06 ``req_`` aggregate and must not be
#: confusable with one in a log line.
_REQUEST_PREFIX = "mreq"

#: The queue statuses from which a worker can still reach the job. ``done`` and ``dead`` are the two
#: it cannot: the first was drained, the second was dead-lettered.
_CLAIMABLE = frozenset({"ready", "leased"})

#: The largest group this route will accept, enforced by the MODEL so Pydantic refuses an oversized
#: body before the handler touches it.
#:
#: **Why a cap at all.** ``work_item_ids`` is caller-controlled and nothing in this application caps
#: a request body — there is no size middleware and ``deploy/kind/nginx.conf`` sets no
#: ``client_max_body_size``. The pre-flight runs after ``get_conn`` has opened its transaction, so an
#: unbounded member list would hold that transaction for as long as it took to scan: the exact
#: failure mode this route exists to avoid, reintroduced at the check meant to prevent it.
#:
#: **Why this number.** A group is a published Hive table's column set (``build_group_plan`` requires
#: the planned features to be EXACTLY the group's members, and ``expected_schema`` becomes the
#: table's columns). Five hundred and twelve features in one table is already implausibly wide, so
#: the cap refuses abuse without refusing any group anyone would author. It is a bound, not a policy
#: about how many features a group SHOULD have — that decision belongs to whoever authors one.
MAX_GROUP_MEMBERS = 512

#: The single durable anchor a member id names — the only table from which BOTH the authoring run id
#: and the authoring intent are recoverable (``materialize/resolve.py``). Read-only here.
_MEMBERS_EXIST = (
    "SELECT work_item_id FROM recipe_formula_shadow_work_item WHERE work_item_id = ANY(%s)")


def require_materialization_enabled() -> None:
    """The deployment's switch, consulted BEFORE anything else on every route in this module.

    Flag off is not a degraded mode and not a permission failure: it is a deployment that does not
    run materialization, and the honest HTTP answer is that these paths are not there. The body is
    Starlette's own 404 payload verbatim so the two are indistinguishable.
    """
    if not materialization_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(require_materialization_enabled)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class MaterializationRunIn(BaseModel):
    """What a trigger declares. Everything here is a DECISION the platform cannot derive.

    The four declaration fields are carried as plain JSON objects and handed to the LANE's own
    :func:`~featuregen.materialize.queue_lane.decode_job` rather than re-described as Pydantic
    models. That is deliberate: the queue payload IS this lane's frozen work item, and a second
    description of its shape at the trigger would be the first thing to drift from the shape the
    worker reads. Validating through the worker's reader means a body this route accepts is, by
    construction, a job that worker can decode whole.

    ``extra="forbid"`` is LOAD-BEARING, not tidiness (the same argument ``integrations.py:160-162``
    makes). ``published_schema=None`` means "no table is published yet"; ``adds_feature_for``
    DERIVES §10.3's schema-evolution question from it; and ``select_publisher`` deliberately gives it
    no default so that a caller must state what is published rather than "silently inherit the
    lenient answer" (``publish.py:559-567``). Without this line a typo — ``publishedSchema`` — drops
    the caller's answer and substitutes exactly that lenient default, reintroducing at the wire the
    thing the chain's signature was shaped to make impossible.
    """

    model_config = ConfigDict(extra="forbid")

    #: §10.1's publication unit. Publication is atomic per group and ``authorize_compilation``
    #: authorizes a group-wide read set, so the trigger is per-group and never per-feature.
    logical_group_name: str = Field(min_length=1)
    #: The group's members, as ``recipe_formula_shadow_work_item`` ids. SUPPLIED, because nothing
    #: durable maps a group to its members — see the module docstring. Capped by the MODEL, so an
    #: oversized body is refused before the handler scans it inside the request transaction.
    work_item_ids: list[str] = Field(max_length=MAX_GROUP_MEMBERS)
    #: What stops one retried call from becoming two runs. The caller's, not this route's: a key
    #: minted here would make every retry a new run.
    idempotency_key: str = Field(min_length=1)
    #: §5's two declarations, and §4's population.
    cadence: dict[str, Any]
    availability_promise: dict[str, Any]
    mechanism: str = Field(min_length=1)
    spine_declaration: dict[str, Any] | None = None
    #: The live column list of the currently published table; ``None`` for "no table yet".
    published_schema: list[str] | None = None
    #: §5.4's declared tightenings, if any.
    contract_overrides: dict[str, Any] | None = None
    #: B4 — the GOVERNED OPTION this run is being asked for: the exact
    #: ``semantic_option_decision (considered_revision_id, option_id)`` a human acted on. Optional
    #: as a PAIR, because the work-item-driven path predates the link and must keep working; stated
    #: half-way it is a 422, because half a key names no approval.
    considered_revision_id: str | None = None
    option_id: str | None = None


@router.post("/materialization-runs", status_code=202,
             dependencies=[Depends(require_confirmer)])
def trigger_materialization(
    body: MaterializationRunIn, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Mint the request, freeze the job onto the queue, and return ``202`` — never compile.

    The pre-flight is everything that is CHEAP and DURABLE, run before the request row is minted so
    a typo leaves nothing behind: the group is well formed, every member names a real work item, and
    the declarations decode as a job. What it deliberately does not do is resolve, admit or
    authorize — those read the whole governed catalog per member and are the compile, not a check on
    it.

    The request row and the queue row are written in the SAME transaction (``get_conn``'s), so there
    is no window in which a request exists with nobody to drive it, and no window in which a queue
    row names a request that is not there.
    """
    _require_usable_identifiers(body)
    members = _well_formed_group(body.work_item_ids)
    _require_every_member_exists(conn, members)
    activation = _require_the_option_allows_materialization(conn, body, identity)
    minted = mint_id(_REQUEST_PREFIX)
    job = _job(minted, members, body)

    recorded = _record(conn, minted, body, members, identity, activation)
    duplicate = recorded.request_id != minted
    if duplicate:
        # The stored request is the one being answered about, and the message id is derived from it.
        job = dataclasses.replace(job, request_id=recorded.request_id)
    _refuse_if_nothing_will_run(recorded)

    try:
        queue_id = enqueue_materialization(conn, recorded, job=job)
    except QueueIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=f"idempotency key {body.idempotency_key!r} already named materialization "
                   f"request {recorded.request_id!r} with DIFFERENT declarations, so this call "
                   f"cannot be answered with it ({exc}). Re-trigger with a fresh idempotency key",
        ) from exc
    _refuse_if_nothing_will_run(recorded, queue_status=_queue_status(conn, queue_id))

    response.headers[REQUEST_ID_HEADER] = recorded.request_id
    counters.incr("materialize.trigger.duplicate" if duplicate else "materialize.trigger.queued")
    log("materialize.trigger.queued", request_id=recorded.request_id,
        logical_group_name=recorded.logical_group_name, members=len(members),
        duplicate=duplicate, queue_id=queue_id)
    return {
        "request_id": recorded.request_id,
        "logical_group_name": recorded.logical_group_name,
        "lifecycle_state": recorded.lifecycle_state.value,
        "work_item_ids": list(members),
        "considered_revision_id": recorded.considered_revision_id,
        "option_id": recorded.option_id,
        "queue_id": queue_id,
        "duplicate": duplicate,
        "detail": ("this idempotency key already named a materialization request; it is still "
                   "queued and nothing was enqueued a second time" if duplicate else
                   "the materialization request was recorded and its job enqueued; a worker "
                   "compiles it"),
    }


@router.get("/materialization-runs/{request_id}", dependencies=[Depends(require_confirmer)])
def materialization_run_detail(request_id: str, conn: _Conn) -> dict[str, Any]:
    """What became of one request — its coordination state, and the plane's verdict when there is one.

    **THE TWO RECORDS ARE NOT MERGED, because they answer different questions.**
    ``materialization_request`` says what is being ATTEMPTED (who asked, is it claimed, has its lease
    expired); the append-only control plane says what HAPPENED. So ``lifecycle_state`` is read off
    the row and ``run_status`` is FOLDED from the events — never a second status stored anywhere.

    **AND AN EMPTY PLANE IS A NORMAL ANSWER (Task 3's trap).** A refusal before the project is sealed
    has nowhere in the plane to be recorded — ``materialization_run_event.generation_id`` is a
    foreign key to a generation row that only exists once a project has been rendered and hashed — so
    a request can be terminally ``failed`` with no generation and no run event at all. That is the
    ordinary shape of a governed refusal, not a corrupt record. ``fold_run_status`` RAISES on an
    empty stream by design ("reporting one would be an invention about a run nobody recorded"), so
    this route never hands it one: it reads the events first and, when there are none, reports
    ``run_status: null`` alongside a reason that says which silence it is.
    """
    stored = read_request(conn, request_id=request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="materialization request not found")
    # Read ONCE and threaded, not recomputed per field: `_is_stranded` costs a queue lookup, and
    # two calls could in principle straddle a worker claiming the request between them and report
    # `never_accepted` beside a reason that says it is pending.
    stranded = _is_stranded(conn, stored)
    status, reason = _run_verdict(conn, stored, stranded=stranded)
    terminal = _terminal_event(conn, stored)
    revision = _published_by_this_run(conn, stored)
    read_scope_verified, read_scope_detail = _read_scope(conn, stored)
    return {
        "request_id": stored.request_id,
        "logical_group_name": stored.logical_group_name,
        "requested_by": stored.requested_by,
        "authorized_roles": list(stored.authorized_roles),
        "idempotency_key": stored.idempotency_key,
        "activation_state": dict(stored.activation_state),
        "considered_revision_id": stored.considered_revision_id,
        "option_id": stored.option_id,
        "lifecycle_state": stored.lifecycle_state.value,
        "terminal": stored.lifecycle_state.is_terminal(),
        "generation_id": stored.generation_id,
        "run_id": stored.run_id,
        "run_status": status,
        "run_status_reason": reason,
        #: WHAT the plane recorded, not how it felt about it. `refused` is an OUTCOME — a
        #: PUBLICATION_REFUSED run compiled, rendered, sealed and PROVED its build, and its request
        #: is `committed` because its evidence is on the plane. A surface that rendered it as an
        #: error would be lying about the one terminal G-1 was designed to reach.
        "terminal_event": None if terminal is None else terminal.value,
        "outcome": _outcome(stored, terminal, stranded=stranded),
        "refusal_code": _refusal_code(conn, stored, terminal),
        #: WHETHER ANYBODY VERIFIED THAT THIS RUN MAY READ ITS INPUTS, read off the run's own L1
        #: report and never off a flag this process can see. A deployment that DECLARES it has no
        #: authorization model gets `false` here for as long as the report exists — the run is
        #: legitimate and the declaration accepted it in advance, and a surface that omitted it
        #: would make an unverified run indistinguishable from a verified one. `null` is an honest
        #: absence with three causes, and the detail says which.
        "read_scope_verified": read_scope_verified,
        "read_scope_detail": read_scope_detail,
        #: The name a reader can query, or `None` for "not published yet" — never a name this
        #: route derived. `group_binding.physical_target` NAMES `sandbox_feature.<group>` whether or
        #: not anything was ever published there, so reporting it would be inventing a table.
        "published_object": None if revision is None else revision.published_object,
        "published_generation_id": None if revision is None else revision.generation_id,
        "published_at": None if revision is None else revision.activated_at,
        "resolved_input_digest": stored.resolved_input_digest,
        "requested_at": stored.requested_at.isoformat(),
        "accepted_at": None if stored.accepted_at is None else stored.accepted_at.isoformat(),
        "lease_expires_at": (None if stored.lease_expires_at is None
                             else stored.lease_expires_at.isoformat()),
        "updated_at": stored.updated_at.isoformat(),
    }


# ── the pre-flight ───────────────────────────────────────────────────────────────────────────────


def _require_usable_identifiers(body: MaterializationRunIn) -> None:
    """The two caller-supplied identifiers must be REAL, not merely present.

    ``Field(min_length=1)`` admits ``" "``, and ``MaterializationRequestV1.__post_init__`` then
    refuses it as blank with a ``ValueError``. Left to fall through, that ``ValueError`` would arrive
    at :func:`_record`'s handler and be dressed up as a **409 Conflict** — which is the wrong answer
    twice over: nothing conflicts, and the caller would go looking for the request their key
    supposedly clashes with. Checked here, it is what it is: an unusable identifier, 422.

    Deliberately NOT normalized. ``uploads.py`` strips and folds a catalog source with an argument
    for why one name must be one catalog; the same argument has not been made for a logical group,
    and quietly trimming one here would decide — from a route — that ``"cif_daily "`` and
    ``"cif_daily"`` are the same ``group_binding``. That is a governance call, not a parse.
    """
    for field, value in (("logical_group_name", body.logical_group_name),
                         ("idempotency_key", body.idempotency_key)):
        if not value.strip():
            raise HTTPException(
                status_code=422,
                detail=f"{field} is {value!r}: a whitespace-only identifier is not a name, and "
                       f"recording one would put a request in the table that nothing could name "
                       f"back")


def _well_formed_group(work_item_ids: list[str]) -> tuple[str, ...]:
    """The group's members, ordered and distinct, or a 422 that names the problem.

    The same two rules ``resolve_feature_inputs`` enforces, applied HERE — where they cost nothing
    and are legible — rather than inside the worker, where the only available outcome is a
    dead-lettered job an operator has to go and read. Ordered ascending because that is the order
    the resolver imposes anyway, so the payload records the membership the compile will see.

    Duplicate detection uses a ``seen`` set — O(n), and the resolver's own idiom
    (``resolve.py:142-150``). Written as ``work_item_ids.count(item)`` it would be O(n^2) over
    caller-controlled input, burned inside the transaction ``get_conn`` has already opened;
    :data:`MAX_GROUP_MEMBERS` bounds the input and this bounds the work per member.
    """
    if not work_item_ids:
        raise HTTPException(
            status_code=422,
            detail="a materialization group needs at least one work item: compilation is "
                   "authorized group-wide and there is nothing to authorize for an empty group")
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in work_item_ids:
        if not item.strip():
            raise HTTPException(
                status_code=422,
                detail="a blank work item id names no durable feature identity")
        if item in seen:
            duplicates.append(item)
        seen.add(item)
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"work item(s) {sorted(set(duplicates))} appear twice in the group; a duplicate "
                   f"member would resolve to two features occupying one Hive column")
    return tuple(sorted(work_item_ids))


def _require_every_member_exists(conn: psycopg.Connection, members: tuple[str, ...]) -> None:
    """One existence read — NOT a compile.

    This is the whole of what a trigger can honestly check about membership: that each id names a
    durable ``recipe_formula_shadow_work_item`` row. Whether that row's authoring run terminated,
    resolved and hashes to its manifest is ``resolve_feature_inputs``'s question, and answering it
    here would mean replaying every member's trace inside the request transaction — which is the
    compile this route exists not to run.
    """
    found = {row[0] for row in conn.execute(_MEMBERS_EXIST, (list(members),)).fetchall()}
    missing = sorted(set(members) - found)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"no work item(s) {missing}: a materialization group's members are supplied as "
                   f"recipe_formula_shadow_work_item ids (nothing maps a logical group to its "
                   f"members), and an id naming no row could never be resolved")


#: The activation ACTION this surface asks about. One string, in one place, so the route and the
#: policy cannot come to disagree about which rung of the five-rung ladder a compile sits on.
_MATERIALIZATION_ACTION = "execute_materialization"


def _contract_minted_from(
    conn: psycopg.Connection, *, considered_revision_id: str, option_id: str,
) -> str | None:
    """The governed contract this option produced — SUCCESSOR 4, the read half of migration 1069.

    C3 made ``requirements_closed`` a real read of the CONTRACT-keyed validation store, and this
    route had no contract to name, so it passed ``None`` and the read answered ``False`` on the one
    route that gates materialization: ``EXTERNAL_VALIDATION_OUTSTANDING`` could only ever clear
    through the ``DESIGN_CHECKED`` short-circuit. The missing piece was a RECORD, not a query;
    1069 makes ``confirm_contract`` stamp the option key on the contract row it mints, and this is
    the lookup along it.

    **The rule, stated because a rule a reader has to infer is a rule that drifts: the HIGHEST
    version linked to this option.** ``confirm_contract`` never rewrites a contract (1012 makes the
    table WORM) — a re-confirm INSERTs a new version under the same feature identity — so an option
    confirmed twice has two linked rows and the later one is the live governed definition. The
    validation store is per-contract-version (``feature_validation_requirement`` and the 1009 event
    stream are both keyed by ``contract_id``), so reading the newest version asks about the
    requirements that are actually owed rather than a superseded version's answers. ``version`` is
    unique per ``feature_name`` (0961) and the confirm route refuses a draft whose name is not the
    chosen option's, so all rows on one link share a name and the ordering is total; the trailing
    key only makes that explicit rather than implied.

    **``None`` is a real answer and the caller must fail closed on it** — an option nobody confirmed,
    or a contract governed before 1069 existed. There is NO fallback: resolving by ``feature_name``
    would be a guess (a name is not identity, and a re-mint proves it), and inventing a contract
    would put a passing verdict on evidence nobody recorded.
    """
    row = conn.execute(
        "SELECT contract_id FROM contract "
        "WHERE considered_revision_id = %s AND option_id = %s "
        "ORDER BY version DESC, created_at DESC, contract_id DESC LIMIT 1",
        (considered_revision_id, option_id)).fetchone()
    return row[0] if row is not None else None


def _intent_of_revision(
    conn: psycopg.Connection, *, considered_revision_id: str,
) -> str | None:
    """The INTENT the cited considered revision belongs to — B5's missing half.

    ``assemble_current_activation_state``'s UOA re-read compares the option's frozen
    ``confirmed_uoa_entity`` against the intent's LATEST confirmed one, and it needs the intent to
    do it. With no intent it fails closed (``uoa_now = frozen_uoa is None``), which is right in the
    abstract and wrong here: this route always HAS an intent — every considered revision carries
    one — it simply was not asking. The consequence was that every option served under a confirmed
    unit of analysis was permanently ``ACTIVATION_STATE_DRIFTED`` on the materialization ladder,
    with the refusal telling the operator to regenerate into exactly the same state.

    ``None`` is still a real answer, for a revision id naming no row, and the assembler's
    fail-closed arm is still the right one for it. It is not reached from this route's happy path:
    ``load_frozen_option_facts`` has already proved the decision row exists, and the decision row's
    revision is this one.
    """
    row = conn.execute(
        "SELECT intent_id FROM contract_considered_revision WHERE considered_revision_id = %s",
        (considered_revision_id,)).fetchone()
    return row[0] if row is not None else None


def _require_the_option_allows_materialization(
    conn: psycopg.Connection, body: MaterializationRunIn, identity: IdentityEnvelope,
) -> dict[str, Any]:
    """The GOVERNED gate (B4): a blocked option cannot be materialized, and the refusal SAYS WHY.

    Returns what will be recorded in ``activation_state`` — the decision verbatim, which is what
    that column was for (``request_store.py:203-206`` calls it *"evidence about the world the
    decision was made in"*).

    **The two layers are both real reads, and neither is asserted.** ``load_frozen_option_facts``
    returns what the human was SHOWN, frozen at generation; ``assemble_current_activation_state``
    re-reads what is true NOW (is the review still valid, has a policy hash moved, do the operands
    still clear the execution floor). ``activation_decision`` folds them. A route that consulted
    only the frozen layer would let an approval outlive the thing it approved.

    SUCCESSOR 4 — the current layer is also told WHICH CONTRACT to read the validation store under
    (:func:`_contract_minted_from`, migration 1069). Before that link existed this route passed no
    contract at all, so C3's ``requirements_closed`` answered ``False`` here forever and a recorded
    data check could never open the gate. An option whose contract predates the link resolves to
    ``None`` and still cannot — fail closed is the same answer it always gave, now for a reason that
    names itself.

    **409, not 403.** Nothing here is a permission failure — the caller passed
    ``require_confirmer``. It is a conflict with a durable governed record: this option, right now,
    is not in a state that may be materialized. The body carries every blocker's ``code`` AND its
    ``next_step``, because a refusal a person cannot act on is an outage with better manners. The
    shape is exactly the one ``contract.py`` already returns for ``ACTIVATION_BLOCKED``, so a client
    that renders one renders both.

    **No option key is not a refusal.** The work-item-driven path predates this link and must keep
    working; requiring a key would break a shipped surface. What is refused is HALF a key (422 — an
    option is addressed by both halves) and a key naming no decision row (404 — the caller is citing
    an approval that does not exist, which is worth distinguishing from one that is merely blocked).
    """
    if body.considered_revision_id is None and body.option_id is None:
        return {}
    if body.considered_revision_id is None or body.option_id is None:
        raise HTTPException(
            status_code=422,
            detail="a governed option is named by BOTH considered_revision_id and option_id; half "
                   "a key names no approval and would record provenance nothing could resolve")

    from featuregen.overlay.upload.activation_policy import activation_decision
    from featuregen.overlay.upload.semantic_option_decision import (
        OPTION_DECISION_INTEGRITY_CODE,
        OPTION_DECISION_INTEGRITY_NEXT_STEP,
        OptionDecisionIntegrityError,
        assemble_current_activation_state,
        load_frozen_option_facts,
    )

    try:
        frozen = load_frozen_option_facts(
            conn, considered_revision_id=body.considered_revision_id, option_id=body.option_id)
    except OptionDecisionIntegrityError as e:
        # S1A-5a §4. The cited decision row cannot authenticate itself — its plan fails the
        # manifest's seal, or its governed cross-catalog read set carries a column nobody can
        # attribute to a catalog. Unhandled this was an opaque 500; it is a 409 for the same
        # reason a blocked option is: a durable governed record is in a state this request cannot
        # be served from, and the caller's move is to regenerate. Raised BEFORE anything is
        # minted, like every other refusal on this path.
        raise HTTPException(status_code=409, detail={
            "code": OPTION_DECISION_INTEGRITY_CODE,
            "message": str(e),
            "next_step": OPTION_DECISION_INTEGRITY_NEXT_STEP,
        }) from e
    if frozen is None:
        raise HTTPException(
            status_code=404,
            detail=f"no option decision {body.option_id!r} in considered revision "
                   f"{body.considered_revision_id!r}: a materialization request may only cite an "
                   f"option that was actually served, because that row IS the approval")
    current = assemble_current_activation_state(
        conn, frozen=frozen, snapshot_id=frozen.snapshot_id or None,
        # B5 — the intent the option was served under. Without it the UOA re-read has nothing to
        # compare against and fails closed, which made every option carrying a confirmed unit of
        # analysis permanently ACTIVATION_STATE_DRIFTED on this ladder (:func:`_intent_of_revision`
        # says why in full).
        intent_id=_intent_of_revision(
            conn, considered_revision_id=body.considered_revision_id),
        contract_id=_contract_minted_from(
            conn, considered_revision_id=body.considered_revision_id, option_id=body.option_id))
    decision = activation_decision(
        frozen, current, _MATERIALIZATION_ACTION, actor=identity.subject)
    blockers = [{"code": blocker.code, "next_step": blocker.next_step}
                for blocker in decision.blockers]
    if not decision.allowed:
        raise HTTPException(status_code=409, detail={
            "code": "ACTIVATION_BLOCKED",
            "action": _MATERIALIZATION_ACTION,
            "considered_revision_id": body.considered_revision_id,
            "option_id": body.option_id,
            "blockers": blockers,
        })
    return {"action": _MATERIALIZATION_ACTION, "allowed": True, "blockers": blockers,
            "considered_revision_id": body.considered_revision_id, "option_id": body.option_id}


def _job(request_id: str, members: tuple[str, ...],
         body: MaterializationRunIn) -> MaterializationJobV1:
    """The declarations as the WORKER's job, validated by the worker's own reader.

    ``decode_job`` is the only description of this payload's shape in the tree, and every refusal it
    raises is a ``ValueError``. Surfacing those as 422 rather than letting them become a
    dead-lettered message a tick later is the difference between a caller who can fix their request
    and an operator who has to read a queue row's ``last_error``.
    """
    try:
        return decode_job({
            "version": PAYLOAD_VERSION,
            "request_id": request_id,
            "work_item_ids": list(members),
            "spine_declaration": body.spine_declaration,
            "cadence": body.cadence,
            "availability_promise": body.availability_promise,
            "mechanism": body.mechanism,
            "published_schema": body.published_schema,
            "contract_overrides": body.contract_overrides,
        })
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"the declarations do not form a materialization job the worker could read: "
                   f"{exc}") from exc


def _record(conn: psycopg.Connection, request_id: str, body: MaterializationRunIn,
            members: tuple[str, ...], identity: IdentityEnvelope,
            activation: dict[str, Any]) -> MaterializationRequestV1:
    """Mint the request — or return the one this idempotency key already named.

    ``authorized_roles`` is the requester's role CLAIMS, snapshotted here and never re-read: Gate 2
    judges the run against the scope its requester actually held, which is why
    ``compile_feature_group`` takes no roles parameter at all.

    ``resolved_input_digest`` is the group's declared MEMBERSHIP, canonicalized. It is set rather
    than left null because it is one of ``IDEMPOTENT_IDENTITY_FIELDS``: with it, one key reused for
    a different set of features is REFUSED by the store; without it, the store would happily answer
    the second caller with the first caller's request and both would believe their group was queued.

    ``activation_state`` records the switch that was OBSERVED at the moment of asking. It is
    evidence about the world the decision was made in (``request_store.py:203-206``), and the store
    is deliberately opaque to its content — which is exactly why the value must be read rather than
    asserted. ``require_materialization_enabled`` proved the switch was on to admit this request,
    but that reading happened at a different instant on a value nothing caches, and a literal
    ``True`` here would record a claim this function never made: an operator reading the row back
    could not tell an observation from a constant. So the flag is consulted again, and what is
    stored is whatever it said.

    B4 folds the GOVERNED decision into that same evidence, VERBATIM — the action, the verdict and
    every blocker with its next step, exactly as ``activation_decision`` returned it. It is recorded
    rather than re-derived for the identical reason the flag is: a reader must be able to tell what
    was observed from what a later build would compute.
    """
    try:
        return record_request(
            conn,
            request_id=request_id,
            logical_group_name=body.logical_group_name,
            requested_by=identity.subject,
            authorized_roles=identity.role_claims,
            idempotency_key=body.idempotency_key,
            activation_state={"flag": MATERIALIZATION_FLAG,
                              "enabled": materialization_enabled(), "surface": "http",
                              **({"activation": activation} if activation else {})},
            resolved_input_digest=materialize_hash({
                "logical_group_name": body.logical_group_name,
                "work_item_ids": list(members)}),
            considered_revision_id=body.considered_revision_id,
            option_id=body.option_id,
        )
    except ValueError as exc:
        # By the time this runs, every ValueError `MaterializationRequestV1.__post_init__` could
        # raise about the CALLER's input has already been refused as a 422 by the pre-flight (blank
        # identifiers, blank members) or is unreachable (`authorized_roles` cannot be empty behind
        # `require_confirmer`; `request_id` and `activation_state` are this module's own). What is
        # left is the one refusal that really IS a conflict: this key already names a request that
        # differs in group, actor or membership.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        # `record_request`'s one non-ValueError refusal, and it is deliberately not a 500: under an
        # isolation level stricter than READ COMMITTED the INSERT can conflict with a row this
        # transaction's snapshot cannot yet see. Nothing is wrong with the CALL — another writer
        # holds the key uncommitted — so the honest answer is "try again", not "the server broke".
        raise HTTPException(
            status_code=503, headers={"Retry-After": "1"},
            detail=f"another caller is recording a materialization request under idempotency key "
                   f"{body.idempotency_key!r} and has not committed yet; retry ({exc})") from exc


# ── the one rule about what a 202 means ──────────────────────────────────────────────────────────


def _refuse_if_nothing_will_run(request: MaterializationRequestV1, *,
                                queue_status: str | None = None) -> None:
    """Refuse with 409 unless a worker can still REACH this request. The dead-letter trap, closed.

    Two correct behaviours compose into a wrong answer. ``record_request`` returns the stored row on
    an idempotency hit, because a retried HTTP call must not become a second run. ``enqueue_checked``
    returns the stored queue id, because one message id is one unit of work. So a retry of a request
    whose job was dead-lettered — or whose lifecycle the worker already terminalized — receives the
    ids of work that is over, and a route that answered ``202`` would be promising a run that no
    worker will ever claim. Nothing would surface it: the queue depth would not move, the counters
    would not advance, and the caller would be told their group was accepted.

    **THE DECISION: refuse, and name the state.** A 409 is exactly what this is — the request
    conflicts with a durable record of the same name. Where a fresh idempotency key IS the remedy,
    the detail says so, and it genuinely works: a fresh key mints a fresh request id, which derives
    a fresh message id, which is a fresh queue row (a named test proves that end to end). Where it
    is NOT the remedy — a request another worker is compiling right now — the detail says that
    instead. :func:`_unreachable_detail` is where the three cases are told apart, and why.

    **The two alternatives, and why they were rejected.**

    *Re-arm the dead queue row* (``UPDATE queue SET status='ready', attempts=0``). It would destroy
    the dead-letter record — the operator's only evidence of WHY the job was poison — and it would
    usually not even work: the worker dead-letters a permanent failure and terminalizes the request
    in the same pass, so the re-armed message would find a ``failed`` request, and
    ``compile_feature_group`` REPLAYS a terminal request rather than running it. The route would
    have destroyed evidence to arrange a replay. Worse, re-arming is a write into the fence-guarded
    lifecycle the worker owns, issued from a surface that holds no lease.

    *Mint a fresh key silently on the caller's behalf.* That is the idempotency key doing the
    opposite of its job: the caller's retry — the ordinary case of a lost response — would become a
    second run, which is the exact duplication migration 1053's UNIQUE constraint exists to prevent.

    The check runs TWICE by design, and the order is the point: once before the enqueue (a terminal
    request must not have work pushed at it at all) and once after (only then is the stored queue
    row's status known). ``queue_status=None`` means the queue has not been consulted yet.
    """
    if request.lifecycle_state.is_terminal():
        raise HTTPException(
            status_code=409,
            detail=f"materialization request {request.request_id!r} is already "
                   f"{request.lifecycle_state.value!r} (generation "
                   f"{request.generation_id!r}, run {request.run_id!r}): this idempotency key names "
                   f"work that is OVER, and a re-delivery would replay it rather than run it. "
                   f"Re-trigger with a fresh idempotency key; read GET "
                   f"/materialization-runs/{request.request_id} for what the first attempt decided")
    if queue_status is not None and queue_status not in _CLAIMABLE:
        counters.incr("materialize.trigger.unreachable")
        log("materialize.trigger.unreachable", level="warning", request_id=request.request_id,
            queue_status=queue_status, lifecycle_state=request.lifecycle_state.value)
        raise HTTPException(status_code=409, detail=_unreachable_detail(request, queue_status))


def _unreachable_detail(request: MaterializationRequestV1, queue_status: str) -> str:
    """WHY the queue row cannot be reached — and therefore what the caller should do instead.

    A closed queue row does NOT always mean "nobody is working this", and the remedy differs so
    sharply between the cases that one message cannot serve them. Three states, told apart by the
    request's own lease rather than by the queue status alone:

    * **A worker is compiling it right now.** ``queue_lane._unclaimable`` dead-letters a delivery it
      may not drive while DELIBERATELY leaving the request non-terminal under a live lease — that is
      its whole design ("the honest state is a non-terminal request with an expiring lease"). Telling
      this caller to re-trigger with a fresh key would mint a SECOND request for a group already
      being compiled; the two would derive different generation ids and collide on
      ``record_group_binding``'s unique logical name, so one whole transaction would roll back.
      Point at the status route and nothing else.
    * **The job was drained** (``done``) and the request is somehow not terminal. The message was
      PROCESSED, so dead-letter language would be a lie about what happened to it. Point at the
      status route, where the request's own record is.
    * **Genuinely dead-lettered, and nobody holds it.** Only here is a fresh idempotency key the
      right advice, and only here is the dead row purely evidence.
    """
    reference = f"read GET /materialization-runs/{request.request_id}"
    lease = request.lease_expires_at
    if lease is not None and _is_being_worked(request):
        return (
            f"materialization request {request.request_id!r} is {request.lifecycle_state.value!r} "
            f"under a LIVE lease (expires {lease.isoformat()}): a worker is "
            f"compiling it now, and its queued message was closed as {queue_status!r} by a delivery "
            f"that was not allowed to drive a claimed request. Do NOT re-trigger — a second request "
            f"for a group already compiling would collide on its logical name. {reference}")
    if queue_status == "done":
        return (
            f"materialization request {request.request_id!r} is "
            f"{request.lifecycle_state.value!r} and its queued job was already drained "
            f"({queue_status!r}), so nothing further will run for it and answering 202 would promise "
            f"a run that cannot happen. {reference} for what that delivery decided before "
            f"re-triggering anything")
    return (
        f"materialization request {request.request_id!r} already exists and its queued job is "
        f"{queue_status!r} with no worker holding it, so no worker will claim it again — answering "
        f"202 would promise a run that cannot happen. The dead-lettered row is left exactly as the "
        f"worker wrote it (it is the evidence of why the job failed); re-trigger with a fresh "
        f"idempotency key to mint a new request and a new queue row. {reference}")


def _is_being_worked(request: MaterializationRequestV1) -> bool:
    """Whether a worker demonstrably still holds this request — a live lease, and nothing else.

    The exact inverse of ``queue_lane._adoptable``'s lease test, and for the same reason: a live
    lease is the only evidence available that another worker has not gone away, and it is what
    ``expired_requests`` reads too. Read here only to choose an ERROR MESSAGE — this route never
    writes a lifecycle and never adopts anything.
    """
    return (not request.lifecycle_state.is_terminal()
            and request.lease_expires_at is not None
            and request.lease_expires_at > _datetime.datetime.now(tz=_datetime.UTC))


def _queue_status(conn: psycopg.Connection, queue_id: int) -> str:
    """The stored queue row's status — read back rather than assumed, because ``enqueue_checked``
    returns an existing id as readily as a new one and says nothing about its state."""
    row = conn.execute("SELECT status FROM queue WHERE id = %s", (queue_id,)).fetchone()
    if row is None:  # pragma: no cover — the row was written in this same transaction
        raise HTTPException(status_code=500,
                            detail=f"queue row {queue_id} vanished inside its own transaction")
    return str(row[0])


# ── the status fold ──────────────────────────────────────────────────────────────────────────────


#: What the SURFACE calls each shape, and the vocabulary a client renders from. It is deliberately
#: NOT `RunStatus` — a status is folded from the plane and exists only once a run does, while this
#: also has to name the two shapes that never reached one (`pending`, `never_accepted`). Four of the
#: six are outcomes and two are in-flight; NONE of them is an error, which is the whole point of
#: having the word `refused` here rather than leaving a client to infer failure from a red-looking
#: terminal.
_OUTCOMES = {
    RunEventKind.PUBLISHED: "published",
    RunEventKind.PUBLICATION_REFUSED: "refused",
    RunEventKind.RUN_FAILED: "failed",
    RunEventKind.GATES_FAILED: "rejected",
}


def _terminal_event(conn: psycopg.Connection,
                    request: MaterializationRequestV1) -> RunEventKind | None:
    """The run's terminal event, or ``None`` when the plane holds none for it.

    ``None`` is an ORDINARY answer twice over and a client must be able to tell the two apart from
    the fields beside it: a request still being worked has not reached one yet, and a refusal before
    the project was sealed has nowhere in the plane to be recorded at all (a run event needs a
    generation, and a generation needs a rendered project). ``run_status_reason`` says which.
    """
    if request.run_id is None:
        return None
    events = read_run_events(conn, request.run_id)
    return events[-1].event_kind if events and events[-1].is_terminal() else None


def _read_scope(conn: psycopg.Connection,
                request: MaterializationRequestV1) -> tuple[bool | None, str]:
    """Whether L1's THIRD question was answered by the engine, and one sentence saying so.

    **READ FROM THE PERSISTED REPORT, never from the environment.** The declaration
    (``FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_MODEL``) is a property of the deployment AT
    THE TIME THE RUN RAN, and this process may be a different pod on a different day with a
    different ConfigMap. A route that consulted the variable would report today's posture beside a
    run that was compiled under another one — and, worse, would answer identically for a declared
    deployment whose engine actually answered. What is durable is the L1 report's own findings, so
    that is what is read.

    Three answers, and the ``None`` cases are told apart in words rather than collapsed:

    * ``None`` — nothing was checked. No generation (no project was sealed), no L1 report (the run
      stopped before it), or an L1 that could not run at all. Never ``True``: a check that did not
      happen is not a check that passed, which is the same rule ``ValidationStatus.ERROR`` carries
      zero findings for.
    * ``False`` — L1 ran, the engine could not answer, and the deployment had DECLARED that it
      cannot. The run proceeded on that acceptance and the report records it per table.
    * ``True`` — the engine answered read scope for every table L1 checked.
    """
    if request.generation_id is None:
        return None, ("no project was sealed for this request, so L1 never ran and nothing about "
                      "read scope was recorded")
    l1 = [report for report in read_validation_reports(conn, generation_id=request.generation_id)
          if report.level is ValidationLevel.L1]
    if not l1:
        return None, ("no L1 validation is recorded for this run, so the tables it would read were "
                      "never checked — read scope among them")
    report = l1[-1]                                    # oldest-first, so the newest L1 is the last
    if report.status is ValidationStatus.ERROR:
        return None, (f"L1 could not run ({report.report_id}): the metastore did not answer, so "
                      f"read scope was not checked either way")
    accepted = [finding for finding in report.findings
                if finding.code is ValidationFindingCode.READ_SCOPE_UNVERIFIED]
    if accepted:
        tables = ", ".join(sorted(finding.location for finding in accepted))
        return False, (
            f"read scope was NOT verified for {len(accepted)} table(s) ({tables}): this deployment "
            f"declares no authorization model, so the engine was never asked whether the "
            f"authorized roles may read them. The run was allowed to proceed because that "
            f"declaration accepts exactly this, and L1 report {report.report_id} records it")
    return True, (f"the engine answered read scope for every table L1 checked (report "
                  f"{report.report_id})")


def _published_by_this_run(conn: psycopg.Connection, request: MaterializationRequestV1):
    """Migration 1055's pointer **only when THIS request put it there**.

    The route answers "what became of this request", and a group's active revision is a fact about
    the GROUP: a run that was refused would otherwise report the object an EARLIER run published, and
    an operator reading a refusal beside a live table name would reasonably conclude their run had
    published it. Scoped by ``run_id``, which is what the pointer records.

    A group whose newest revision belongs to another run therefore reads as *"not published yet"* on
    this request — true of this request, which is the question asked. What is currently published for
    the group is a different question and needs its own surface rather than this field.
    """
    if request.run_id is None:
        return None
    # C-D6 — the LEGACY scope, explicitly. `MaterializationRequestV1` carries no environment
    # (request_store.py:212-215), so this route can only ask about rows written before environments
    # were recorded. That is truthful today and becomes WRONG the moment generations start carrying
    # one, which is why the parameter is passed rather than defaulted: the day it must change, this
    # line is what a reader greps for.
    revision = read_active_revision(conn, request.logical_group_name, environment_id=None)
    return revision if revision is not None and revision.run_id == request.run_id else None


def _is_stranded(conn: psycopg.Connection, request: MaterializationRequestV1) -> bool:
    """DEFERRED-WORK **A.35**: a request left at ``requested`` that no worker will ever claim again.

    An unconfigured deployment (a missing ``FEATUREGEN_MATERIALIZE_INVENTORY``, say) makes every
    delivery fail, and when the queue's attempt budget runs out the message is dead-lettered while
    the request is still ``requested`` holding no lease — the lane deliberately does not accept
    before it has a configuration, because accepting first would burn every arriving request into a
    terminal no operator fix could recover. So nothing will ever deliver it again.

    **THE CHOICE MADE HERE (D4): NAME THE CLASS, DO NOT ADD THE EDGE.** A.35's alternative was a
    ``requested → failed`` transition with the reconciler as its only writer, and it was rejected
    for three reasons. (1) The reconciler already REFUSES to invent a verdict for this class — it
    reports ``NO_LEGAL_TERMINAL`` and gauges the standing count — and the edge would turn that
    refusal into a terminalization on the evidence "the message is unreachable", which is a
    judgement about work nobody ever claimed. (2) The operator's actual complaint is that the STATUS
    SURFACE showed it as pending indefinitely; that is a surface defect and it is fixed here, with
    no state-machine change. (3) Terminalizing is a one-way door on a row whose whole value is that
    it is honest about never having been claimed, while saying so is not — and §3.3's rule is that a
    re-run is a NEW request anyway, so nothing is lost while the old row stands.

    Read from the queue rather than from the request, because the request row cannot know: a
    ``requested`` row looks identical whether its message is waiting, being retried, or dead. The
    message id is DERIVED (``materialization_message_id``), so this is a primary-key lookup and not
    a scan.
    """
    if request.lifecycle_state is not RequestLifecycle.REQUESTED:
        return False
    row = conn.execute(
        "SELECT status FROM queue WHERE message_id = %s",
        (materialization_message_id(request.request_id),)).fetchone()
    # No queue row at all is stranded too, and more plainly so: the request was minted and its job
    # was never enqueued, so there is nothing anywhere that could drive it.
    return row is None or str(row[0]) not in _CLAIMABLE


def _outcome(request: MaterializationRequestV1, terminal: RunEventKind | None, *,
             stranded: bool) -> str:
    """The one word a surface leads with — and `refused` is an OUTCOME, not an error.

    §0.0's correction, made a property of the API rather than a note in a plan.
    ``PUBLICATION_REFUSED`` is G-1's SUCCESS terminal: the project WAS compiled, rendered, sealed
    and PROVED to build in a separate interpreter, and the request lands ``committed`` because the
    evidence is on the plane. What is refused is publication, and until an environment has a passing
    capability attestation that is the truthful end of every run. A client that painted it red would
    be telling an operator their feature was rejected.

    ``never_accepted`` is DEFERRED-WORK A.35's class, named rather than repaired — see
    :func:`_is_stranded` for the choice and why the state-machine edge was rejected.
    """
    if terminal is not None:
        return _OUTCOMES.get(terminal, terminal.value.lower())
    if stranded:
        return "never_accepted"
    if not request.lifecycle_state.is_terminal():
        return "pending"
    # Terminal with no run event: a governed refusal before the project was sealed, which has
    # nowhere in the plane to be recorded. `run_status_reason` is where that is said.
    return "failed" if request.lifecycle_state is RequestLifecycle.FAILED else "refused"


def _refusal_code(conn: psycopg.Connection, request: MaterializationRequestV1,
                  terminal: RunEventKind | None) -> str | None:
    """``CAPABILITY_UNPROVEN`` / ``PUBLISH_MECHANISM_UNSUPPORTED`` — read off the terminal event's
    own detail, which is where the chain put it (``"<code>: <detail>"``).

    Parsed rather than stored a second time: the detail IS the durable statement, and a column
    holding the code beside it would be a second answer that could disagree. ``None`` whenever the
    terminal is not a refusal, so a client cannot render a code for a run that was not refused.
    """
    if terminal is not RunEventKind.PUBLICATION_REFUSED or request.run_id is None:
        return None
    events = read_run_events(conn, request.run_id)
    detail = events[-1].detail if events else None
    return None if not detail or ":" not in detail else detail.split(":", 1)[0].strip()


def _run_verdict(conn: psycopg.Connection, request: MaterializationRequestV1, *,
                 stranded: bool) -> tuple[str | None, str | None]:
    """``(run_status, reason)`` — the plane's fold, or a plain statement of why there is none.

    Exactly one of the two is ever non-null. A reason is not an error message: "this request never
    reached the plane" is the ordinary durable shape of a pre-render refusal, and of every request
    that has not yet been claimed.
    """
    if request.run_id is None and stranded:
        return None, (
            f"this request was never accepted: it is still {request.lifecycle_state.value!r}, it "
            f"holds no lease, and its queued job is no longer claimable — so no worker will deliver "
            f"it again and nothing ran. Check the materialization lane's configuration "
            f"(FEATUREGEN_MATERIALIZE_* on the worker), then re-trigger with a FRESH idempotency "
            f"key: a re-run is a new request (§3.3), and this row deliberately stays as it is "
            f"because §3.2 ships no legal terminal from 'requested' (DEFERRED-WORK A.35)")
    if request.run_id is None:
        return None, (
            f"no run exists for this request: it is {request.lifecycle_state.value!r}. "
            f"A request that ends before its project is sealed records nothing in the control "
            f"plane — a run event needs a generation, and a generation needs a rendered project — "
            f"so its own lifecycle is the whole record"
            if request.lifecycle_state.is_terminal() else
            f"no run exists yet: this request is {request.lifecycle_state.value!r}, and a run is "
            f"named only once a project has been rendered and sealed")
    events = read_run_events(conn, request.run_id)
    if not events:  # pragma: no cover — a run id is stamped in the same transaction as its event
        return None, (
            f"this request names run {request.run_id!r} and the control plane holds no events for "
            f"it: a status folded from nothing would be an invention about a run nobody recorded")
    return fold_run_status(events).value, None
