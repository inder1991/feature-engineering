"""Phase G T8 — the TRIGGER SURFACE: the two routes that make materialization invocable.

**The three things this suite is actually for.**

1. *The route must not run the chain.* ``get_conn`` holds ONE transaction for the whole request and
   a compile is bounded by ``COMPILE_BUDGET_SECONDS`` plus the deployment's L0 timeout. So the
   proof is structural (:func:`test_the_route_module_never_NAMES_the_chain`) as well as behavioural
   (a 202 leaves a ``requested`` request, a ``ready`` queue row and an EMPTY control plane).
2. *One switch, not two.* The route imports Task 9's :func:`materialization_enabled` — the same
   function object, over the same truthy set. A route testing ``== "1"`` while the worker accepts
   ``"true"`` is a deployment that 404s at the API while the worker drains, and
   :func:`test_the_route_and_the_worker_read_ONE_switch` is what makes that unshippable.
3. *A 202 is a promise that something will run.* ``record_request`` returns the EXISTING row on an
   idempotency hit and ``enqueue_checked`` returns the existing — possibly ``dead`` — queue id, so
   the naive route answers 202 over a dead-lettered job. Both halves of that trap are named tests.

**Flag off is not "the routes decline politely"** — it is a deployment that never heard of Phase G.
:func:`test_flag_OFF_both_routes_are_INDISTINGUISHABLE_from_an_absent_one` compares the refusal body
byte-for-byte against Starlette's own 404 for a path that genuinely does not exist, and
:func:`test_flag_OFF_every_existing_payload_is_BYTE_IDENTICAL` compares three existing routes'
response BYTES across the switch rather than asserting the absence of a change.
"""
from __future__ import annotations

import ast
import datetime as _datetime
import inspect
import json
import pathlib

import pytest
from tests.featuregen.materialize.test_chain import _CADENCE, _GROUP, _PROMISE
from tests.featuregen.materialize.test_resolve import _seed_work_item

from featuregen.api.routes import materialization_runs
from featuregen.api.routes.materialization_runs import MAX_GROUP_MEMBERS as _CAP
from featuregen.materialize import queue_lane
from featuregen.materialize.control_plane import (
    MaterializationGeneration,
    MaterializationRunEvent,
    RunEventKind,
    append_run_event,
    record_generation,
)
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    MATERIALIZATION_HANDLER,
    MaterializationJobV1,
    decode_job,
    encode_job,
)
from featuregen.materialize.request_store import (
    RequestLifecycle,
    accept_request,
    advance_lifecycle,
    read_request,
)
from featuregen.materialize.spine import SpineSourceDeclarationV1
from featuregen.runtime.queue import (
    claim_materialization,
    complete_materialization,
    fail_materialization,
)

_PATH = "/materialization-runs"
_KEY = "trigger-key-1"


# ── the declarations a trigger carries ───────────────────────────────────────────────────────────
#
# Built by ENCODING a real job through the lane's own `encode_job`, so the body this suite posts is
# by construction the shape `decode_job` reads. A hand-written literal here would be a second
# description of the payload, and the first thing to drift.

def _declaration() -> SpineSourceDeclarationV1:
    from tests.featuregen.materialize.test_ir import DECLARATION

    return DECLARATION


def _body(work_item_ids, *, key=_KEY, group=_GROUP, **overrides) -> dict:
    payload = encode_job(MaterializationJobV1(
        request_id="req-placeholder", work_item_ids=tuple(work_item_ids),
        spine_declaration=_declaration(), cadence=_CADENCE, availability_promise=_PROMISE,
        mechanism=queue_lane.PublishMechanism.VERSIONED_POINTER, published_schema=None,
        contract_overrides=None))
    body = {
        "logical_group_name": group,
        "work_item_ids": list(work_item_ids),
        "idempotency_key": key,
        "cadence": payload["cadence"],
        "availability_promise": payload["availability_promise"],
        "mechanism": payload["mechanism"],
        "spine_declaration": payload["spine_declaration"],
        "published_schema": payload["published_schema"],
        "contract_overrides": payload["contract_overrides"],
    }
    body.update(overrides)
    return body


@pytest.fixture
def work_items(db):
    """Two durable feature identities — ``recipe_formula_shadow_work_item`` rows, written through
    the real writer. The route's pre-flight is an EXISTENCE check against exactly this table, which
    is the only durable anchor from which a member's authoring run and intent are recoverable."""
    return [_seed_work_item(db, "total_debit_amount_30d", "t8a"),
            _seed_work_item(db, "avg_credit_amount_30d", "t8b")]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")


@pytest.fixture(autouse=True)
def _flag_unset(monkeypatch):
    """Every test states the flag it is testing: a developer whose shell exports the switch must not
    be able to turn a default-OFF assertion green."""
    monkeypatch.delenv(MATERIALIZATION_FLAG, raising=False)


def _queue_row(db, request_id: str):
    return db.execute(
        "SELECT status, handler, attempts, payload FROM queue WHERE message_id = %s",
        (f"materialize:{request_id}",)).fetchone()


def _counts(db) -> tuple[int, int, int]:
    return (
        db.execute("SELECT count(*) FROM materialization_request").fetchone()[0],
        db.execute("SELECT count(*) FROM queue").fetchone()[0],
        db.execute("SELECT count(*) FROM materialization_generation").fetchone()[0],
    )


# ── the switch ───────────────────────────────────────────────────────────────────────────────────


def test_flag_OFF_both_routes_are_INDISTINGUISHABLE_from_an_absent_one(
        client, admin_headers, work_items, off) -> None:
    """Not "a polite 503" and not "403" — a deployment that never heard of Phase G. The refusal body
    is compared BYTE-FOR-BYTE against Starlette's own 404 for a path that genuinely does not exist,
    so the surface leaks nothing about whether the feature is built, only unconfigured."""
    absent = client.get("/no-such-route-at-all", headers=admin_headers)

    posted = client.post(_PATH, json=_body(work_items), headers=admin_headers)
    fetched = client.get(f"{_PATH}/req-anything", headers=admin_headers)

    assert absent.status_code == 404
    assert (posted.status_code, posted.content) == (404, absent.content)
    assert (fetched.status_code, fetched.content) == (404, absent.content)


def test_flag_OFF_the_routes_404_BEFORE_authentication_and_before_the_body(
        client, off) -> None:
    """The switch is the FIRST dependency: an unauthenticated caller with a malformed body still
    gets the absent-route answer, so a flag-off deployment opens no connection and parses no
    declaration on this path."""
    assert client.post(_PATH, json={"nonsense": True}).status_code == 404
    assert client.get(f"{_PATH}/whatever").status_code == 404


def test_flag_OFF_every_existing_payload_is_BYTE_IDENTICAL(client, admin_headers,
                                                           monkeypatch) -> None:
    """PROVED, not asserted-absent: three existing routes — one static, one DB-backed, one 404 —
    are read with the switch off and again with it on, and their response BYTES must be equal. The
    flag changes what this app DOES on two new paths and nothing else about any payload it already
    served."""
    probes = ("/health", "/metrics", "/ingestion-runs/does-not-exist")

    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
    before = [client.get(path, headers=admin_headers).content for path in probes]
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    after = [client.get(path, headers=admin_headers).content for path in probes]

    assert before == after


def test_the_route_and_the_worker_read_ONE_switch(client, admin_headers, work_items,
                                                  monkeypatch) -> None:
    """THE SPLIT-BRAIN TEST. The route consults Task 9's function OBJECT — not a second reading of
    the variable — so every value the worker drains under is a value the route serves under. A route
    written as ``== "1"`` would 404 for a ``"true"`` deployment whose worker was consuming the
    backlog, and that is precisely the state nobody would diagnose."""
    assert materialization_runs.materialization_enabled is queue_lane.materialization_enabled

    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv(MATERIALIZATION_FLAG, value)
        assert queue_lane.materialization_enabled() is True
        assert client.post(_PATH, json=_body(work_items, key=f"key-{value.strip()}"),
                           headers=admin_headers).status_code == 202

    for value in ("0", "", "off", "2", "enabled"):
        monkeypatch.setenv(MATERIALIZATION_FLAG, value)
        assert queue_lane.materialization_enabled() is False
        assert client.post(_PATH, json=_body(work_items, key="key-off"),
                           headers=admin_headers).status_code == 404


def test_registering_the_router_costs_the_app_EXACTLY_two_paths(client, admin_headers) -> None:
    """The one thing the switch cannot hide, stated precisely rather than glossed.

    The router is included UNCONDITIONALLY — a boot-time flag read would capture the switch at
    import and make flipping it something no running process could observe — so ``/openapi.json``
    describes the two new paths whatever the flag says. This test pins that cost to EXACTLY those
    two: nothing else about the schema moves, and a third path appearing here is a red test rather
    than a surprise in a deployed spec."""
    schema = client.get("/openapi.json").json()

    added = {path for path in schema["paths"] if path.startswith("/materialization-runs")}
    assert added == {_PATH, _PATH + "/{request_id}"}


def test_the_route_module_reads_NO_environment_variable_of_its_own(on) -> None:
    """The other half of "one switch": the module must not contain a second reading of the
    environment at all, so no future edit can reintroduce the split by copying an idiom."""
    source = inspect.getsource(materialization_runs)
    assert "os.environ" not in source and "getenv" not in source


# ── authorization ────────────────────────────────────────────────────────────────────────────────


def test_a_NON_CONFIRMER_is_refused(client, non_admin_headers, work_items, db, on) -> None:
    """`require_confirmer` — the raw ``platform-admin`` CLAIM (the user's decision), not the
    ``feature:generate`` permission. A refused call mints nothing."""
    response = client.post(_PATH, json=_body(work_items), headers=non_admin_headers)

    assert response.status_code == 403
    assert _counts(db) == (0, 0, 0)


def test_an_UNAUTHENTICATED_call_is_refused(client, work_items, db, on) -> None:
    response = client.post(_PATH, json=_body(work_items))

    assert response.status_code == 401
    assert _counts(db) == (0, 0, 0)


def test_the_STATUS_route_is_confirmer_gated_too(client, non_admin_headers, on) -> None:
    """Who asked for a materialization, under which roles, and what its run decided is authority-only
    for this release — the same posture as ``gate.py``'s four routes."""
    assert client.get(f"{_PATH}/req-x", headers=non_admin_headers).status_code == 403


# ── the 202, and what it is a promise about ──────────────────────────────────────────────────────


def test_a_confirmer_gets_202_and_the_request_id(client, admin_headers, work_items, db, on) -> None:
    response = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert response.status_code == 202
    body = response.json()
    request_id = body["request_id"]
    assert body["logical_group_name"] == _GROUP
    assert body["lifecycle_state"] == "requested"
    assert body["duplicate"] is False
    assert response.headers[materialization_runs.REQUEST_ID_HEADER] == request_id
    assert read_request(db, request_id=request_id).requested_by == "user:priya"


def test_the_202_ENQUEUED_and_the_CHAIN_DID_NOT_RUN(client, admin_headers, work_items, db,
                                                    on) -> None:
    """The behavioural half of "the route must not run the chain": after a 202 the request is still
    ``requested`` (nobody accepted it), the queue row is ``ready`` under the lane's handler, and the
    control plane is EMPTY — no generation, no run event, no compiled artifact. A route that
    compiled inline would have advanced all four."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]

    stored = read_request(db, request_id=request_id)
    status, handler, attempts, _payload = _queue_row(db, request_id)
    assert stored.lifecycle_state is RequestLifecycle.REQUESTED
    assert (stored.generation_id, stored.run_id, stored.accepted_at) == (None, None, None)
    assert (status, handler, attempts) == ("ready", MATERIALIZATION_HANDLER, 0)
    assert db.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM materialization_run_event").fetchone()[0] == 0


def test_the_route_module_never_NAMES_the_chain() -> None:
    """The STRUCTURAL half, read off the module's own AST rather than off a call that happened not
    to fire: a compile is minutes-to-tens-of-minutes inside a request transaction, so "the route
    does not run it" must be a property of the file, not of a test's luck with a fixture."""
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(materialization_runs)).read_text())
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    named |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {alias.name.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.alias) for alias in [node]}
    assert "compile_feature_group" not in named
    assert "process_materialization_once" not in named


def test_the_payload_the_route_writes_is_the_one_the_WORKER_reads(client, admin_headers,
                                                                  work_items, db, on) -> None:
    """The queue row's payload is decoded by the LANE's own reader, and every declaration the caller
    posted comes back intact. Trigger and worker therefore share one description of a job."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]

    job = decode_job(_queue_row(db, request_id)[3])

    assert job.request_id == request_id
    assert job.work_item_ids == tuple(sorted(work_items))
    assert job.spine_declaration == _declaration()
    assert (job.cadence, job.availability_promise) == (_CADENCE, _PROMISE)


# ── idempotency, and the two ways a retry stops being a promise ──────────────────────────────────


def test_the_SAME_KEY_TWICE_is_one_request_one_queue_row_and_the_second_SAYS_SO(
        client, admin_headers, work_items, db, on) -> None:
    first = client.post(_PATH, json=_body(work_items), headers=admin_headers)
    second = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert (first.status_code, second.status_code) == (202, 202)
    assert second.json()["request_id"] == first.json()["request_id"]
    assert (first.json()["duplicate"], second.json()["duplicate"]) == (False, True)
    assert _counts(db) == (1, 1, 0)


def test_a_DEAD_LETTERED_request_is_REFUSED_rather_than_answered_202(
        client, admin_headers, work_items, db, on) -> None:
    """**THE TRAP, named.** ``record_request`` returns the stored row on an idempotency hit and
    ``enqueue_checked`` returns the stored queue id — including one the worker dead-lettered. The
    naive route answers 202 and nothing runs, forever, silently.

    The decision this asserts: a 202 is returned only when a worker can still REACH the request. A
    dead queue row is refused with 409, the state is named, and the dead row is left exactly as the
    worker wrote it — it is the operator's evidence, not litter for a route to clear."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    claim = claim_materialization(db, owner="w1", lease_seconds=60)
    fail_materialization(db, claim, error="deliberate poison", permanent=True)
    assert _queue_row(db, request_id)[0] == "dead"

    retry = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert "dead" in detail and request_id in detail and "idempotency" in detail
    assert _queue_row(db, request_id)[0] == "dead", "the dead-letter evidence was rewritten"
    assert _counts(db) == (1, 1, 0)


def test_a_FRESH_KEY_after_a_dead_letter_really_does_run(client, admin_headers, work_items, db,
                                                         on) -> None:
    """The other half of that refusal: the remedy it names actually works. A fresh idempotency key
    mints a fresh request, a fresh message id and a fresh ``ready`` queue row — so "use a new key"
    is an instruction, not a brush-off."""
    first = client.post(_PATH, json=_body(work_items), headers=admin_headers).json()["request_id"]
    claim = claim_materialization(db, owner="w1", lease_seconds=60)
    fail_materialization(db, claim, error="deliberate poison", permanent=True)

    retry = client.post(_PATH, json=_body(work_items, key="a-fresh-key"), headers=admin_headers)

    assert retry.status_code == 202
    fresh = retry.json()["request_id"]
    assert fresh != first
    assert _queue_row(db, fresh)[0] == "ready"
    assert _counts(db) == (2, 2, 0)


def test_a_TERMINAL_request_is_REFUSED_rather_than_answered_202(client, admin_headers, work_items,
                                                                db, on) -> None:
    """The same rule at the other end: a request the chain already terminalized would be REPLAYED,
    not re-run (``chain._replayed``), so answering 202 would promise a run that will never happen."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    accept_request(db, request_id=request_id, lease_seconds=60)
    advance_lifecycle(db, request_id=request_id, to_state=RequestLifecycle.FAILED)

    retry = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert retry.status_code == 409
    assert "failed" in retry.json()["detail"]


def test_a_key_reused_for_a_DIFFERENT_GROUP_is_refused(client, admin_headers, work_items, db,
                                                       on) -> None:
    """``record_request``'s own refusal, surfaced as 409 rather than as a 500: returning the stored
    row would tell the caller their request was queued when a different one was."""
    client.post(_PATH, json=_body(work_items), headers=admin_headers)

    clash = client.post(_PATH, json=_body(work_items, group="another_group"),
                        headers=admin_headers)

    assert clash.status_code == 409
    assert _counts(db) == (1, 1, 0)


def test_a_key_reused_for_DIFFERENT_MEMBERS_is_refused(client, admin_headers, work_items, db,
                                                       on) -> None:
    """The membership rides in ``resolved_input_digest``, which IS one of
    ``IDEMPOTENT_IDENTITY_FIELDS`` — so one key naming two different groups of features is refused
    by the store rather than silently answered with the first one's request."""
    client.post(_PATH, json=_body(work_items), headers=admin_headers)

    clash = client.post(_PATH, json=_body(work_items[:1]), headers=admin_headers)

    assert clash.status_code == 409
    assert _counts(db) == (1, 1, 0)


def test_a_key_reused_for_DIFFERENT_DECLARATIONS_is_refused(client, admin_headers, work_items, db,
                                                            on) -> None:
    """The declarations are not part of the request row's identity, so the guard that catches them is
    the queue row's ``payload_hash`` (``QueueIdempotencyConflict``). Surfaced as 409, never as a 500
    and never as a 202 over a job frozen with somebody else's cadence."""
    client.post(_PATH, json=_body(work_items), headers=admin_headers)

    other = _body(work_items)
    other["cadence"] = {**other["cadence"], "timezone": "UTC"}
    clash = client.post(_PATH, json=other, headers=admin_headers)

    assert clash.status_code == 409
    assert _counts(db) == (1, 1, 0)


def test_a_request_ANOTHER_WORKER_IS_COMPILING_never_advises_a_fresh_key(
        client, admin_headers, work_items, db, on) -> None:
    """A ``dead`` queue row does NOT always mean "nobody is working this".

    ``queue_lane._unclaimable`` dead-letters a delivery it may not drive while DELIBERATELY leaving
    the request non-terminal under a LIVE lease — another worker is compiling it right now. The 409
    is still right, but "re-trigger with a fresh idempotency key" would be actively harmful there:
    it would mint a SECOND request for a group already being compiled, and the two would collide on
    ``record_group_binding``'s unique logical name. So the refusal is told apart by the request's own
    lease, and this one points at the status route instead."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    accept_request(db, request_id=request_id, lease_seconds=3600)   # a worker holds it, live
    claim = claim_materialization(db, owner="w2", lease_seconds=60)
    fail_materialization(db, claim, error="this delivery may not drive a claimed request",
                         permanent=True)

    retry = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert "fresh idempotency key" not in detail
    assert f"{_PATH}/{request_id}" in detail
    assert "lease" in detail


def test_a_DRAINED_job_whose_request_is_not_terminal_points_at_the_STATUS_route(
        client, admin_headers, work_items, db, on) -> None:
    """``done`` is not ``dead`` either: the message was processed. Whatever the request's state, the
    dead-letter wording would be a lie about what happened to it."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    accept_request(db, request_id=request_id, lease_seconds=3600)
    complete_materialization(db, claim_materialization(db, owner="w2", lease_seconds=60))
    assert _queue_row(db, request_id)[0] == "done"

    retry = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert "dead-letter" not in detail
    assert f"{_PATH}/{request_id}" in detail


def test_a_CONCURRENT_UNCOMMITTED_writer_is_a_RETRY_not_a_500(client, admin_headers, work_items,
                                                              monkeypatch, on) -> None:
    """``record_request``'s one non-``ValueError`` refusal: under an isolation level stricter than
    READ COMMITTED the INSERT can conflict with a row this transaction's snapshot cannot see. That
    is a transient state the caller retries out of, so it must not surface as "the server broke"."""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("conflicted with a row this transaction's snapshot cannot see")

    monkeypatch.setattr(materialization_runs, "record_request", _boom)

    response = client.post(_PATH, json=_body(work_items), headers=admin_headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


# ── the pre-flight: cheap, durable, and BEFORE the request is minted ─────────────────────────────


def test_UNKNOWN_MEMBERS_are_refused_before_anything_is_minted(client, admin_headers, work_items,
                                                               db, on) -> None:
    """The members are the one thing the route can check cheaply and durably: nothing maps a logical
    group to its members, so they are supplied, and the pre-flight is an existence read of
    ``recipe_formula_shadow_work_item``. A member that names nothing refuses BEFORE the request is
    minted, so a typo does not leave a request row nobody will ever drive."""
    response = client.post(_PATH, json=_body([*work_items, "work-does-not-exist"]),
                           headers=admin_headers)

    assert response.status_code == 422
    assert "work-does-not-exist" in response.json()["detail"]
    assert _counts(db) == (0, 0, 0)


def test_DUPLICATE_MEMBERS_are_refused(client, admin_headers, work_items, db, on) -> None:
    """``resolve_feature_inputs`` refuses a duplicate member (two features in one Hive column), and
    it does so INSIDE the worker — where the only outcome is a dead-lettered job. Catching it at the
    trigger is what a pre-flight is for."""
    response = client.post(_PATH, json=_body([work_items[0], work_items[0]]),
                           headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


def test_an_EMPTY_group_is_refused(client, admin_headers, db, on) -> None:
    response = client.post(_PATH, json=_body([]), headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


def test_a_group_LARGER_THAN_THE_CAP_is_refused_by_the_MODEL(client, admin_headers, db,
                                                             on) -> None:
    """The body is caller-controlled and NOTHING in this app caps a request body — there is no
    size middleware and ``deploy/kind/nginx.conf`` sets no ``client_max_body_size``. The pre-flight
    runs after ``get_conn`` has opened its transaction, so an unbounded member list would hold that
    transaction open for as long as the scan took: the exact failure mode this route exists to
    avoid, reintroduced at the pre-flight. The cap is declared on the MODEL so Pydantic refuses the
    body before the handler does any work over it at all."""
    response = client.post(_PATH, json=_body([f"work-{i}" for i in range(_CAP + 1)]),
                           headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


def test_the_duplicate_check_is_LINEAR_like_the_resolver_it_mirrors(client, admin_headers, db,
                                                                    on) -> None:
    """``resolve._require_a_well_formed_group`` finds a duplicate with a ``seen`` set, in O(n). A
    membership scan written as ``ids.count(item)`` is O(n^2) — measurably seconds at ten thousand
    ids — and it would burn them inside the held transaction. A full group with ONE duplicate at the
    very end must still be refused promptly, and the module must not contain the quadratic idiom."""
    # Read off the AST, not the text: the module's own docstring EXPLAINS the quadratic idiom in
    # order to rule it out, and a substring check would flag the explanation.
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(materialization_runs)).read_text())
    assert not [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "count"]

    members = [f"work-{i}" for i in range(_CAP - 1)]
    response = client.post(_PATH, json=_body([*members, members[0]]), headers=admin_headers)

    assert response.status_code == 422
    assert "appear twice" in response.json()["detail"]
    assert _counts(db) == (0, 0, 0)


def test_an_UNKNOWN_FIELD_cannot_silently_choose_the_LENIENT_answer(client, admin_headers,
                                                                    work_items, db, on) -> None:
    """``extra="forbid"``, and it is load-bearing rather than tidy.

    ``published_schema=None`` means "no table is published yet", ``adds_feature_for`` derives §10.3's
    schema-evolution question from it, and ``select_publisher`` deliberately gives it NO default so
    that a caller must STATE what is published rather than "silently inherit the lenient answer by
    omission" (``publish.py:559-567``). Without ``extra="forbid"`` a typo — ``publishedSchema`` —
    drops the caller's answer and substitutes exactly that lenient default. The chain's signature was
    shaped to make this impossible; the route must not reintroduce it at the wire."""
    body = _body(work_items, published_schema=["cif_id", "some_existing_feature"])
    body["publishedSchema"] = body.pop("published_schema")

    response = client.post(_PATH, json=body, headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


@pytest.mark.parametrize("field", ["logical_group_name", "idempotency_key"])
def test_a_WHITESPACE_ONLY_identifier_is_422_not_409(client, admin_headers, work_items, db, field,
                                                     on) -> None:
    """``Field(min_length=1)`` admits ``" "``, and ``MaterializationRequestV1.__post_init__`` then
    refuses it as blank — a ``ValueError`` that must NOT be dressed up as a 409 Conflict. Nothing
    conflicts: the caller sent an unusable identifier, which is a 422."""
    response = client.post(_PATH, json=_body(work_items, **{field: "   "}), headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


def test_a_MALFORMED_DECLARATION_is_refused_before_anything_is_minted(client, admin_headers,
                                                                      work_items, db, on) -> None:
    """The declarations are validated by the LANE's decoder, so a cadence the worker could not read
    is a 422 at the trigger rather than a dead-lettered job discovered a tick later."""
    body = _body(work_items)
    body["cadence"] = {**body["cadence"], "period": "fortnightly"}

    response = client.post(_PATH, json=body, headers=admin_headers)

    assert response.status_code == 422
    assert _counts(db) == (0, 0, 0)


# ── the status route ─────────────────────────────────────────────────────────────────────────────


def test_GET_answers_for_a_QUEUED_request(client, admin_headers, work_items, on) -> None:
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]

    body = client.get(f"{_PATH}/{request_id}", headers=admin_headers).json()

    assert body["request_id"] == request_id
    assert body["lifecycle_state"] == "requested"
    assert body["run_id"] is None
    assert body["run_status"] is None
    assert "no run" in body["run_status_reason"]


def test_GET_answers_HONESTLY_for_a_request_that_FAILED_BEFORE_THE_PLANE(
        client, admin_headers, work_items, db, on) -> None:
    """**TRAP 3.** A pre-render refusal records no generation and no run event (there is nowhere in
    the plane for one to go before a project is sealed), and ``fold_run_status`` RAISES on an empty
    stream. This is a normal outcome, not an error: the route reports the request's own terminal
    lifecycle and says plainly that the plane holds nothing — it never folds an empty stream and it
    never 500s."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    accept_request(db, request_id=request_id, lease_seconds=60)
    advance_lifecycle(db, request_id=request_id, to_state=RequestLifecycle.FAILED)

    response = client.get(f"{_PATH}/{request_id}", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle_state"] == "failed"
    assert (body["generation_id"], body["run_id"], body["run_status"]) == (None, None, None)
    assert "control plane" in body["run_status_reason"]


def test_GET_FOLDS_THE_PLANE_once_a_run_exists(client, admin_headers, work_items, db, on) -> None:
    """And when there IS a run, the status is the plane's fold — never a second status stored on the
    request row."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]
    accept_request(db, request_id=request_id, lease_seconds=60)
    generation_id, run_id = "gen-t8", "run-t8"
    record_generation(db, MaterializationGeneration(
        generation_id=generation_id, logical_group_name=_GROUP,
        materialization_contract_hash="c" * 64, group_plan_hash="p" * 64,
        generated_project_hash="g" * 64, created_at="2026-08-03T00:00:00+00:00"))
    advance_lifecycle(db, request_id=request_id, to_state=RequestLifecycle.RUNNING,
                      generation_id=generation_id, run_id=run_id)
    append_run_event(db, MaterializationRunEvent(
        run_id=run_id, seq=0, generation_id=generation_id,
        event_kind=RunEventKind.PUBLICATION_REFUSED, occurred_at="2026-08-03T00:00:01+00:00",
        detail="CAPABILITY_UNPROVEN: no attestation"))
    advance_lifecycle(db, request_id=request_id, to_state=RequestLifecycle.COMMITTED)

    body = client.get(f"{_PATH}/{request_id}", headers=admin_headers).json()

    assert body["lifecycle_state"] == "committed"
    assert (body["generation_id"], body["run_id"]) == (generation_id, run_id)
    assert body["run_status"] == "refused"
    assert body["run_status_reason"] is None


def test_GET_404s_for_an_UNKNOWN_id(client, admin_headers, on) -> None:
    assert client.get(f"{_PATH}/req-nobody-asked-for", headers=admin_headers).status_code == 404


def test_the_status_response_serializes(client, admin_headers, work_items, on) -> None:
    """Timestamps and all — the response must be JSON a caller can read, not a dataclass FastAPI
    happens to encode differently tomorrow."""
    request_id = client.post(_PATH, json=_body(work_items),
                             headers=admin_headers).json()["request_id"]

    body = client.get(f"{_PATH}/{request_id}", headers=admin_headers).json()

    assert isinstance(json.dumps(body), str)
    assert _datetime.datetime.fromisoformat(body["requested_at"]).tzinfo is not None
