"""Phase G T7 — the fenced queue lane: the chain becomes RUNNABLE WORK.

**What is real here.** Every test below drives the REAL chain over the REAL seeded catalog through
the REAL queue table: a job is enqueued exactly as Task 8's route will enqueue it, and
``process_materialization_once`` is the production consumer, unmocked. The two things injected are
the two Task 6 made injectable on purpose — the L0 interpreter's VERDICT (``chain.run_l0``, so no
test needs a kedro venv) and the two provider calls inside authoring (``test_resolve``'s rationale:
the audited ``llm_call`` rows the replay checkpoint reconciles exist only under a durable DSN, which
this suite deliberately does not have).

**The load-bearing tests are the fence and the idempotency-through-the-lane one.** The plane is
append-only with a one-terminal partial index, so "two workers ran one request's compile" has no
repair path — it is not a performance question. Three independent guards are asserted separately:
the queue's per-partition in-flight exclusion, the monotonic ``lease_fence`` on every terminal write,
and the lane's refusal to drive a request it did not itself accept.
"""
from __future__ import annotations

import datetime as _datetime
import pathlib
import sys

import pytest
from tests.featuregen.materialize.test_chain import (
    _ACTOR,
    _BUSINESS_DT,
    _CADENCE,
    _FEATURE,
    _GROUP,
    _L0,
    _PROMISE,
    _ROLES,
    _attest_capability,
    _authored,
    _clock,
    _G2Metastore,
    _inject_l0,
    _seed,
    _Submitter,
    _Swap,
)
from tests.featuregen.materialize.test_inventory import _document, _write
from tests.featuregen.materialize.test_ir import DECLARATION, INVENTORY
from tests.featuregen.materialize.test_metastore_sql import ENDPOINT, _Engine
from tests.featuregen.materialize.test_resolve import (  # noqa: F401 — `no_dsn` is autouse
    _seed_work_item,
    no_dsn,
)

from featuregen.materialize import queue_lane
from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.compile.chain import ChainStage, L0Interpreter
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.control_plane import read_run_events
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
from featuregen.materialize.inventory import EventTimePartition
from featuregen.materialize.metastore_sql import (
    MetastoreEndpoint,
    MetastoreSession,
    SqlMetastoreAdapter,
)
from featuregen.materialize.publish import PublishMechanism
from featuregen.materialize.publish_sql import SqlPublicationSwap
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    MATERIALIZATION_HANDLER,
    MaterializationJobV1,
    MaterializationLaneConfig,
    decode_job,
    encode_job,
    enqueue_materialization,
    lane_config_from_env,
    process_materialization_once,
)
from featuregen.materialize.request_store import (
    RequestLifecycle,
    accept_request,
    read_request,
    record_request,
)
from featuregen.materialize.runprep import PARTITION_VALUE_FORMS
from featuregen.materialize.validation import ValidationFinding, ValidationStatus
from featuregen.runtime.observability import counters
from featuregen.runtime.queue import (
    MATERIALIZATION_QUEUE_HANDLERS,
    QueueIdempotencyConflict,
    claim_materialization,
    claim_one,
    complete_materialization,
)


@pytest.fixture
def catalog(db):
    return _seed(db)


def _recorded(db, *, request_id="req-lane-1", group=_GROUP, roles=_ROLES):
    """A request at ``requested`` — the state the ROUTE leaves it in, before any worker."""
    return record_request(db, request_id=request_id, logical_group_name=group,
                          requested_by=_ACTOR, authorized_roles=roles,
                          idempotency_key=f"key-{request_id}", activation_state={"flag": "on"})


def _job(request_id: str, work_item_ids, *, business_dt=None) -> MaterializationJobV1:
    return MaterializationJobV1(
        request_id=request_id, work_item_ids=tuple(work_item_ids), spine_declaration=DECLARATION,
        cadence=_CADENCE, availability_promise=_PROMISE,
        mechanism=PublishMechanism.VERSIONED_POINTER, published_schema=None,
        contract_overrides=None, business_dt=business_dt)


def _config(tmp_path, *, l0=_L0, metastore=None, submitter=None, swap=None,
            staging_base=None) -> MaterializationLaneConfig:
    """The DEPLOYMENT's half of the configuration — what the handler resolves at the boundary.

    G-2's fields default to `None` because that is what `lane_config_from_env` produces for a
    deployment that states no EXECUTION block — which is every deployment that cannot reach a
    metastore, and still the posture of the kind cluster (there is no SQL endpoint in front of its
    sandbox metastore). A run driven by this configuration is honestly unprepared, which is the
    outcome the chain records. The tests at the foot of this file cover the other half: a
    deployment that DOES state the block gets the real adapters, and its run reaches PUBLISH."""
    return MaterializationLaneConfig(
        inventory=INVENTORY, project_root=str(tmp_path), l0=l0, assemble_nodes=assemble_nodes,
        clock=_clock, metastore=metastore, submitter=submitter, swap=swap,
        staging_base=staging_base)


@pytest.fixture
def enqueued(catalog, monkeypatch, l0_passes, tmp_path):
    """One recorded request, one resolvable feature, one enqueued job, one lane configuration."""
    request = _recorded(catalog)
    work_items = [_authored(catalog, monkeypatch)]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    return request, work_items, _config(tmp_path)


@pytest.fixture
def l0_passes(monkeypatch):
    """L0 PASSES — the precondition of every test whose subject is not L0 itself. Injected at
    ``chain.run_l0`` exactly as Task 6's suite does, which is what keeps this suite kedro-free."""
    _inject_l0(monkeypatch)


def _drain(db, config, *, owner="w1"):
    return process_materialization_once(db, owner=owner, config=config)


def _queue_row(db, request_id: str):
    return db.execute(
        "SELECT status, last_error, attempts, lease_fence FROM queue WHERE message_id = %s",
        (f"materialize:{request_id}",)).fetchone()


def _release(db, request_id: str) -> None:
    """Make a rescheduled message deliverable NOW — the backoff `fail_materialization` set is real
    time, and a test must not sleep through it."""
    db.execute("UPDATE queue SET available_at = now() - interval '1 minute' WHERE message_id = %s",
               (f"materialize:{request_id}",))


def _expire_lease(db, request_id: str) -> None:
    """The state a worker that died mid-compile leaves: claimed, leased, and the lease is past."""
    db.execute("UPDATE materialization_request SET lease_expires_at = now() - interval '1 hour' "
               "WHERE request_id = %s", (request_id,))


def _now():
    return _datetime.datetime.now(tz=_datetime.UTC)


def _boom(exc: Exception):
    """A chain call that fails the way a dropped connection does — not a governed verdict."""
    def _raise(*_args, **_kwargs):
        raise exc

    return _raise


def _commit_then_boom(exc: Exception):
    """The chain records its terminal and THEN the call fails — the state the re-read guard is for."""
    real = queue_lane.compile_feature_group

    def _run(*args, **kwargs):
        real(*args, **kwargs)
        raise exc

    return _run


def _boom_once(exc: Exception):
    """Fails the FIRST compile and runs the real chain afterwards — a transient fault, exactly.

    Deliberately not ``monkeypatch.undo()`` between the two drains: that would also revert the
    ``l0_passes`` fixture's injection and hand the second attempt a real ``run_l0`` against an
    interpreter with no kedro, so the "retry succeeded" assertion would be testing the environment
    rather than the lane.
    """
    real = queue_lane.compile_feature_group
    raised = []

    def _maybe(*args, **kwargs):
        if not raised:
            raised.append(True)
            raise exc
        return real(*args, **kwargs)

    return _maybe


def _running_before_the_write(run_id: str):
    """Stage the ONE race ``expected_from`` closes: the row moves to ``running`` after this handler
    re-read it as ``accepted`` and before its terminalizing UPDATE lands.

    Wrapping ``advance_lifecycle`` is how the window is made observable at all. In G-1 ``running``
    exists only inside ``_commit``'s transaction, which holds this row's lock for its whole life, so
    no arrangement of REAL calls can produce it here — which is precisely the reviewer's point:
    unreachable today, durable the moment G-2's ``prepare_run`` writes ``running`` from outside that
    lock. The wrapper moves the row through the real ``advance_lifecycle`` (a legal
    ``accepted → running`` edge, run id and all) and then lets the handler's own call proceed
    untouched, so what is under test is the handler's ARGUMENTS, not a stub's idea of them.
    """
    real = queue_lane.advance_lifecycle

    def _advance(conn, *, request_id, **kwargs):
        real(conn, request_id=request_id, to_state=RequestLifecycle.RUNNING, run_id=run_id)
        return real(conn, request_id=request_id, **kwargs)

    return _advance


_FINDING = ValidationFinding(
    code=ValidationFindingCode.PROJECT_DOES_NOT_BUILD, location="pipeline_registry",
    expected=None, observed=None, count=1)


# ── enqueue → drain → the chain ran, and the record says so ──────────────────────────────────────

def test_an_enqueued_request_is_compiled_by_the_lane_and_recorded_in_the_plane(
        enqueued, catalog) -> None:
    """The whole point of the task: the chain stops being a library function nothing calls."""
    request, _, config = enqueued

    outcome = _drain(catalog, config)

    assert outcome.status == "completed"
    assert outcome.request_id == request.request_id
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.COMMITTED
    assert stored.generation_id is not None and stored.run_id is not None
    events = read_run_events(catalog, stored.run_id)
    assert [e.event_kind.value for e in events] == ["PUBLICATION_REFUSED"]
    assert (pathlib.Path(config.project_root) / stored.generation_id /
            GENERATED_LOCK_FILENAME).is_file()
    assert _queue_row(catalog, request.request_id)[0] == "done"


def test_the_lane_is_idle_when_nothing_is_enqueued(catalog, tmp_path) -> None:
    assert _drain(catalog, _config(tmp_path)).status == "idle"


def test_the_general_consumer_can_never_claim_a_materialization_row(enqueued, catalog) -> None:
    """`claim_one` DLQs anything whose payload has no run-stream `event_id` (dispatch.py:111-133),
    so a general consumer that could claim this row would poison a governed request. The exclusion
    is the same single-source constant this lane claims BY."""
    assert claim_one(catalog, owner="general") is None
    assert MATERIALIZATION_QUEUE_HANDLERS == frozenset({MATERIALIZATION_HANDLER})


# ── the fence: one writer per request, proved three ways ─────────────────────────────────────────

def test_a_second_worker_cannot_claim_a_leased_request(enqueued, catalog) -> None:
    """The per-partition in-flight exclusion. While one worker holds the lease, a second claim on
    the same partition matches nothing — this is what stops two compiles of one group."""
    first = claim_materialization(catalog, owner="w1", lease_seconds=300)

    assert first is not None
    assert claim_materialization(catalog, owner="w2", lease_seconds=300) is None


def test_a_stale_fence_write_is_REFUSED(enqueued, catalog) -> None:
    """`lease_fence` is monotonic per claim, so a worker whose lease was reclaimed cannot complete
    or fail the row a later worker now owns — its UPDATE matches no row and returns False."""
    stale = claim_materialization(catalog, owner="w1", lease_seconds=300)
    catalog.execute("UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
                    "WHERE id=%s", (stale.id,))
    fresh = claim_materialization(catalog, owner="w2", lease_seconds=300)

    assert fresh.lease_fence > stale.lease_fence
    assert complete_materialization(catalog, stale) is False
    assert complete_materialization(catalog, fresh) is True


def test_the_lane_refuses_to_drive_a_request_it_did_not_itself_ACCEPT(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """The third guard, and the one the other two cannot make. If a worker died mid-compile its
    queue row is reclaimable, and a second worker would otherwise re-drive a request that already
    holds a lease — minting a second generation under one durable identity. `accept_request` is a
    conditional UPDATE from `requested`, so a request in any other state was claimed by somebody
    else; the lane refuses it and leaves the row for §3.3's reconciler rather than deciding, with
    no evidence, that the first attempt died."""
    request = _recorded(catalog, request_id="req-lane-taken")
    work_items = [_authored(catalog, monkeypatch, suffix="taken")]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    accept_request(catalog, request_id=request.request_id, lease_seconds=300)

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "unclaimable"
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.ACCEPTED   # untouched, still the reconciler's
    assert stored.generation_id is None
    status, error, _, _ = _queue_row(catalog, request.request_id)
    assert status == "dead"
    assert "reconciler" in error


# ── idempotency, end to end through the lane ─────────────────────────────────────────────────────

def test_a_REDELIVERED_job_yields_ONE_generation_and_ONE_terminal_event(enqueued, catalog) -> None:
    """Task 3 makes re-entry structurally safe; this proves it survives the lane. A worker that
    dies AFTER the chain's commit and BEFORE completing its queue row leaves exactly this state:
    a terminal request and a reclaimable row. The redelivery must replay, not re-run."""
    request, _, config = enqueued
    first = _drain(catalog, config)
    catalog.execute("UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
                    "WHERE message_id=%s", (f"materialize:{request.request_id}",))

    again = _drain(catalog, config)

    assert again.status == "replayed"
    assert again.generation_id == first.generation_id
    assert again.run_id == first.run_id
    assert catalog.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 1
    assert len(read_run_events(catalog, first.run_id)) == 1
    assert _queue_row(catalog, request.request_id)[0] == "done"


def test_enqueueing_one_request_twice_is_ONE_work_item(enqueued, catalog) -> None:
    request, work_items, _ = enqueued

    again = enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))

    assert again == catalog.execute(
        "SELECT id FROM queue WHERE message_id=%s",
        (f"materialize:{request.request_id}",)).fetchone()[0]
    assert catalog.execute("SELECT count(*) FROM queue").fetchone()[0] == 1


def test_one_message_id_naming_DIFFERENT_work_is_refused(enqueued, catalog) -> None:
    """The queue row IS this lane's frozen work item (there is no work-item table), so its content
    hash is what stops one request id from being re-enqueued for a different group of features."""
    request, _, _ = enqueued

    with pytest.raises(QueueIdempotencyConflict):
        enqueue_materialization(catalog, request, job=_job(request.request_id, ["work-other"]))


def test_the_job_a_worker_reads_back_is_the_job_that_was_frozen(catalog) -> None:
    """The payload is the freeze. A codec that lost the population's snapshot policy, the cadence's
    zone or a declared tightening would compile a DIFFERENT group under the same request id."""
    job = _job("req-codec", ("work-a", "work-b"))

    assert decode_job(encode_job(job)) == job


# ── failures: each one legible, none of them half a state ────────────────────────────────────────

def test_a_PROVEN_capability_this_deployment_cannot_EXECUTE_is_run_failed_not_publish_step_missing(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """D1 made this terminal PRECISE, and the precision is the point.

    Before G-2 was composed, ANY proven capability raised `PublishStepMissing` before the project
    was even rendered. Now the chain says the truer thing first: this deployment configures no
    metastore adapter, no submitter and no staging base, so the run was never prepared — and the
    publish step is not what is missing, because nothing ever got near it. `run_failed` is the
    lane's word for the three G-2 stages, kept apart from `refused` because there is no governed
    verdict about a feature here at all (`CompiledGroup.refusal` is `None`).

    The message was PROCESSED — the chain recorded a terminal — so the queue row is `done` rather
    than dead-lettered, and the project stays on disk as the evidence of what was built."""
    request = _recorded(catalog, request_id="req-lane-proven")
    work_items = [_authored(catalog, monkeypatch, suffix="laneproven")]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    _attest_capability(catalog)

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "run_failed"
    assert outcome.stopped_at == ChainStage.PREPARE_RUN.value
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED
    status, _error, _, _ = _queue_row(catalog, request.request_id)
    assert status == "done"
    assert "no run execution seam" in read_run_events(
        catalog, read_request(catalog, request_id=request.request_id).run_id)[-1].detail


def test_a_fully_configured_lane_PUBLISHES(catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """The landmine's other end, and what replaced it. D0's guard and D1's
    `test_the_lane_classifies_PublishStepMissing_instead_of_crashing` both existed only because a
    run that EXECUTED under a proven capability had nowhere to go; G-3 built the step, so both are
    deleted here and this is the case they were standing in for.

    The lane writes no lifecycle of its own on this path — the chain recorded the terminal — and the
    queue row is `done` because the message was processed."""
    request = _recorded(catalog, request_id="req-lane-publishes")
    work_items = [_authored(catalog, monkeypatch, suffix="lanepublishes")]
    enqueue_materialization(catalog, request, job=_job(
        request.request_id, work_items, business_dt=_BUSINESS_DT))
    _attest_capability(catalog)
    swap = _Swap()

    outcome = _drain(catalog, _config(
        tmp_path, metastore=_G2Metastore(INVENTORY), submitter=_Submitter(), swap=swap,
        staging_base="/staging"))

    assert outcome.status == "completed"
    assert outcome.stopped_at == ChainStage.PUBLISH.value
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED
    status, _error, _, _ = _queue_row(catalog, request.request_id)
    assert status == "done"
    assert len(swap.calls) == 1
    assert read_run_events(catalog, read_request(
        catalog, request_id=request.request_id).run_id)[-1].event_kind.value == "PUBLISHED"


def test_a_governed_refusal_fails_the_request_and_leaves_no_half_state(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """A work item whose authoring run never happened cannot resolve. The CHAIN records that
    verdict and moves the request to `failed`; the lane must not write the lifecycle a second time,
    and must not treat a governed verdict as a queue failure — the message WAS processed."""
    request = _recorded(catalog, request_id="req-lane-refused")
    work_item_id = _seed_work_item(catalog, _FEATURE, "lanerefused")
    enqueue_materialization(catalog, request, job=_job(request.request_id, [work_item_id]))

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "refused"
    assert outcome.stopped_at == "resolve"
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.FAILED
    assert stored.generation_id is None
    assert catalog.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 0
    assert _queue_row(catalog, request.request_id)[0] == "done"


def test_a_job_naming_a_request_nobody_recorded_is_a_permanent_failure(catalog, tmp_path) -> None:
    """The request row is minted BEFORE the job is enqueued, so a job naming none of them can only
    be a caller defect. Retrying it forever would be a hot loop against a row that will never
    appear."""
    from featuregen.runtime.queue import enqueue_checked

    enqueue_checked(catalog, message_id="materialize:req-ghost",
                    partition_key=f"materialize:{_GROUP}", handler=MATERIALIZATION_HANDLER,
                    payload=encode_job(_job("req-ghost", ("work-x",))))

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "failed"
    assert _queue_row(catalog, "req-ghost")[0] == "dead"


def test_an_undecodable_payload_fails_permanently_rather_than_looping(catalog, tmp_path) -> None:
    from featuregen.runtime.queue import enqueue_checked

    enqueue_checked(catalog, message_id="materialize:req-garbled",
                    partition_key=f"materialize:{_GROUP}", handler=MATERIALIZATION_HANDLER,
                    payload={"version": 99, "request_id": "req-garbled"})

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "failed"
    status, error, _, _ = _queue_row(catalog, "req-garbled")
    assert status == "dead"
    assert "99" in error


def test_an_UNCONFIGURED_deployment_does_not_burn_the_request(
        enqueued, catalog, monkeypatch, tmp_path) -> None:
    """The failure that must not be terminal. A missing `FEATUREGEN_MATERIALIZE_*` variable is an
    operator's five-second fix — but the request is governed and cannot be re-minted, so failing it
    would destroy work nobody could recover. Configuration is therefore resolved BEFORE the request
    is accepted, and its failure is retryable: the request stays `requested` behind a live queue
    row, and the very next delivery runs it."""
    request, _, config = enqueued
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_INVENTORY", raising=False)

    outcome = process_materialization_once(catalog, owner="w1")

    assert outcome.status == "retryable"
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.REQUESTED   # untouched, and still claimable
    assert stored.lease_expires_at is None
    status, error, _, _ = _queue_row(catalog, request.request_id)
    assert status == "ready"                                      # a live owner, not a dead letter
    assert "FEATUREGEN_MATERIALIZE" in error

    # …and once the deployment IS configured, the same message runs. This is the assertion that
    # makes "retryable" a claim about recovery rather than a label.
    _release(catalog, request.request_id)
    assert _drain(catalog, config).status == "completed"


def test_a_TRANSIENT_fault_leaves_a_request_the_retry_can_actually_take(
        enqueued, catalog, monkeypatch) -> None:
    """The other half of the same property, and the one that is easy to get wrong. A retryable
    failure that left the request `accepted` under a full-size lease would be a retry that could
    never succeed: the redelivery reads a live claim, refuses it and dead-letters the message. So a
    worker that gives up must RELEASE the lease it took."""
    request, _, config = enqueued
    monkeypatch.setattr(queue_lane, "compile_feature_group", _boom_once(ConnectionError("gone")))

    outcome = _drain(catalog, config)

    assert outcome.status == "retryable"
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.ACCEPTED
    # RELEASED: a lease this worker still held would be `config.lease_seconds` — minutes — out.
    assert stored.lease_expires_at - _now() < _datetime.timedelta(seconds=1)
    assert config.lease_seconds > 60.0
    assert _queue_row(catalog, request.request_id)[0] == "ready"

    _release(catalog, request.request_id)
    again = _drain(catalog, config)

    assert again.status == "completed"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


def test_an_EXHAUSTED_retry_budget_terminalizes_the_request(enqueued, catalog,
                                                            monkeypatch) -> None:
    """Releasing the lease is right while a redelivery is still coming. Once the queue has given up
    there is no owner left at all, and a request left adoptable by a delivery that will never
    arrive is exactly the stuck class this lane must not mint."""
    request, _, config = enqueued
    catalog.execute("UPDATE queue SET attempts = max_attempts - 1 WHERE message_id = %s",
                    (f"materialize:{request.request_id}",))
    monkeypatch.setattr(queue_lane, "compile_feature_group", _boom(ConnectionError("gone")))

    outcome = _drain(catalog, config)

    assert outcome.status == "failed"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED
    assert _queue_row(catalog, request.request_id)[0] == "dead"


def test_a_transient_fault_AFTER_the_chain_committed_does_not_raise_out_of_the_handler(
        enqueued, catalog, monkeypatch) -> None:
    """"This call raised" and "the chain recorded a terminal" are not mutually exclusive — a fault
    after `_commit`'s transaction closes is both, and so is losing a race to an adopter. Both
    `renew_lease` and `advance_lifecycle` refuse a terminal request with a ValueError, so an
    unguarded write here would replace a legible retryable failure with an uncaught exception."""
    request, _, config = enqueued
    monkeypatch.setattr(queue_lane, "compile_feature_group",
                        _commit_then_boom(ConnectionError("gone")))

    outcome = _drain(catalog, config)

    assert outcome.status == "retryable"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED     # the chain's verdict stands, unmolested by the error path


def test_an_exhausted_budget_this_worker_never_CLAIMED_is_a_dead_letter_not_a_failure(
        enqueued, catalog, monkeypatch) -> None:
    """The status must report what HAPPENED. A configuration failure whose retries run out leaves
    the message dead and the request untouched at `requested` — calling that "failed" would name a
    request nobody failed."""
    request, _, _ = enqueued
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", raising=False)
    catalog.execute("UPDATE queue SET attempts = max_attempts - 1 WHERE message_id = %s",
                    (f"materialize:{request.request_id}",))

    outcome = process_materialization_once(catalog, owner="w1")

    assert outcome.status == "dead_letter"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.REQUESTED
    assert _queue_row(catalog, request.request_id)[0] == "dead"


@pytest.mark.parametrize(("exhaust_budget", "error"), [
    (False, KeyError("a call assembled wrongly")),
    (True, ConnectionError("gone")),
], ids=["_deterministic_failure", "_retryable_with_the_budget_gone"])
def test_a_request_that_became_RUNNING_is_not_FAILED_by_a_verdict_about_ACCEPTED(
        enqueued, catalog, monkeypatch, exhaust_budget, error) -> None:
    """Both of the lane's terminalizing writes, narrowed — `reconcile.py`'s argument, applied here.

    Every verdict these two paths reach is EVIDENCE ABOUT AN `accepted` REQUEST: `_still_ours`
    literally re-reads the row and requires that state before either will write. An unnarrowed
    `advance_lifecycle` UPDATE matches every state `failed` is legal from, `running` included — so a
    request that moved on between that re-read and the write would be terminalized on a finding
    gathered about the state it had left, and `failed` is not a state anything walks back from.

    `expected_from` makes the row's own state a PRECONDITION of the UPDATE rather than a fact that
    merely preceded it. The refusal is the honest outcome: nothing is written, and the raise says
    the row moved rather than pretending this worker's verdict still applies to it. The assertion
    that matters is the last line — the request is still `running`, holding whatever the writer that
    moved it there is doing.

    Unreachable in G-1 and asserted anyway, for the reason `reconcile.py:541` gives: G-2's
    `prepare_run` writes `running` from outside `_commit`'s lock, and on that day this is a live
    hazard rather than a latent one.
    """
    request, _, config = enqueued
    if exhaust_budget:
        catalog.execute("UPDATE queue SET attempts = max_attempts - 1 WHERE message_id = %s",
                        (f"materialize:{request.request_id}",))
    monkeypatch.setattr(queue_lane, "compile_feature_group", _boom(error))
    monkeypatch.setattr(queue_lane, "advance_lifecycle", _running_before_the_write("run-moved-on"))

    with pytest.raises(ValueError, match="moved to 'running'"):
        _drain(catalog, config)

    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.RUNNING


def test_an_ABANDONED_claim_is_ADOPTED_once_its_lease_has_demonstrably_expired(
        enqueued, catalog) -> None:
    """A worker that dies mid-compile leaves `accepted` — which PROVES no run evidence exists, since
    every plane write the chain makes is inside one transaction that ends terminal. There is
    nothing for §3.3's evidence-reading reconciler to read, so adoption here is the retry the queue
    was always going to perform, not a verdict about a run."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=300)
    _expire_lease(catalog, request.request_id)

    outcome = _drain(catalog, config)

    assert outcome.status == "completed"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED
    assert _queue_row(catalog, request.request_id)[0] == "done"


def test_a_LIVE_claim_is_still_refused(enqueued, catalog) -> None:
    """The lease is the only evidence there is that another worker is alive, so a live one is
    conclusive: adoption is bounded to a lease that has demonstrably expired, and everything else
    stays the reconciler's."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)

    assert _drain(catalog, config).status == "unclaimable"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED


def test_a_run_whose_BUILD_FAILED_is_not_reported_as_a_governed_refusal(
        catalog, monkeypatch, tmp_path) -> None:
    """Opposite facts about one run. A run that stopped at L0 was compiled, rendered and sealed and
    then failed to BUILD — there is no governed refusal in it and `refusal` is None. Reporting it
    as `refused` would tell an operator the catalog rejected their feature when the project simply
    does not import."""
    request = _recorded(catalog, request_id="req-lane-nobuild")
    work_items = [_authored(catalog, monkeypatch, suffix="nobuild")]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    _inject_l0(monkeypatch, status=ValidationStatus.FAILED, findings=(_FINDING,))

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "build_unproven"
    assert outcome.stopped_at == "run_l0"
    assert outcome.detail is None                       # no governed refusal to report
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED
    assert _queue_row(catalog, request.request_id)[0] == "done"


# ── the lease: sized, not renewed ────────────────────────────────────────────────────────────────

def test_the_lease_COVERS_the_configured_build_proof(tmp_path) -> None:
    """The lease is not renewed during a compile and cannot be: the L0 subprocess runs INSIDE the
    chain's commit transaction, which holds a row lock on the request. So the bound is sized from
    the one part of a compile whose duration is configured — raise the L0 timeout and the lease
    rises with it, structurally rather than by remembering."""
    short = MaterializationLaneConfig(
        inventory=INVENTORY, project_root=str(tmp_path),
        l0=L0Interpreter(python_executable=sys.executable, timeout_seconds=60.0),
        assemble_nodes=assemble_nodes, clock=_clock)
    long = MaterializationLaneConfig(
        inventory=INVENTORY, project_root=str(tmp_path),
        l0=L0Interpreter(python_executable=sys.executable, timeout_seconds=3600.0),
        assemble_nodes=assemble_nodes, clock=_clock)

    assert short.lease_seconds > 60.0
    assert long.lease_seconds - short.lease_seconds == pytest.approx(3600.0 - 60.0)


def test_a_deployment_with_no_interpreter_still_leases_for_the_compile(tmp_path) -> None:
    """`l0=None` is "this deployment configures no interpreter", which the chain records as an
    `error` report — it does not make a compile free, so the lease still has to cover one."""
    assert _config(tmp_path, l0=None).lease_seconds > 0


# ── the boundary: what the deployment configures, and what it does NOT ───────────────────────────

def test_the_lane_configuration_comes_from_the_ENVIRONMENT_not_the_chain(monkeypatch,
                                                                        tmp_path) -> None:
    """Task 6 gave the chain no default interpreter and no default timeout on purpose — whether a
    run's build is proved is not a thing to inherit by omission. This is the boundary that owns
    it."""
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_INVENTORY", str(_write(tmp_path, _document())))
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_L0_PYTHON", sys.executable)
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS", "120")

    config = lane_config_from_env()

    assert config.l0.python_executable == sys.executable
    assert config.l0.timeout_seconds == 120.0
    assert config.inventory.environment_id == INVENTORY.environment_id
    assert config.project_root == str(tmp_path / "projects")


def test_no_interpreter_configured_is_a_STATE_not_a_missing_configuration(monkeypatch,
                                                                         tmp_path) -> None:
    """`l0=None` is a legitimate deployment posture (the chain records it as an unproven build),
    so it must not be an error here — but a timeout with no interpreter is a half-configuration."""
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_INVENTORY", str(_write(tmp_path, _document())))
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_L0_PYTHON", raising=False)

    assert lane_config_from_env().l0 is None

    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS", "120")
    with pytest.raises(ValueError, match="FEATUREGEN_MATERIALIZE_L0_PYTHON"):
        lane_config_from_env()


# ── the worker stage ─────────────────────────────────────────────────────────────────────────────

def test_a_worker_TICK_drains_the_lane(enqueued, catalog, monkeypatch) -> None:
    """The stage is registered beside the existing ones, so `run_forever` drives it.

    T9's flag is set explicitly: it defaults OFF, and `test_materialization_flag.py` owns what that
    default does. This test's subject is the stage, so it states the deployment it needs."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    request, _, config = enqueued
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    monkeypatch.setattr("featuregen.materialize.queue_lane.lane_config_from_env", lambda: config)

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_NOW)

    assert tick.materialization_processed == 1
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


def test_an_unconfigured_deployment_costs_the_tick_NOTHING(catalog, monkeypatch) -> None:
    """Nothing enqueued means the stage never resolves a configuration, so a deployment with no
    materialization environment is not a counted stage error once a second, forever.

    The flag is ON here on purpose: the subject is a deployment that ENABLED materialization and has
    not configured it, which the switch must not be allowed to mask."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    for variable in ("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", "FEATUREGEN_MATERIALIZE_INVENTORY"):
        monkeypatch.delenv(variable, raising=False)
    counters.reset()

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_NOW)

    assert tick.materialization_processed == 0
    assert counters.snapshot()["counters"].get("worker.stage_error.materialization", 0) == 0


_NOW = _datetime.datetime(2026, 8, 3, 12, 0, tzinfo=_datetime.UTC)


# ── SUCCESSOR 2: the EXECUTION block, and where "honestly unprepared" now sits ────────────────────
#
# Until this increment, `lane_config_from_env` could not fill G-2's seams at all — the adapters are
# objects and no implementation of either existed — so `metastore=None` was the only posture a
# deployment could have and EVERY deployed run stopped at PREPARE_RUN. The boundary moves here: a
# deployment that states the eight-variable execution block gets the real
# `SqlMetastoreAdapter` + `SqlPublicationSwap` over ONE session, and a run driven by that
# configuration reaches PUBLISH. Unset is still an outcome, and the tests keep both halves.

_EXECUTION_ENV = {
    "FEATUREGEN_MATERIALIZE_METASTORE_ENGINE": "hive",
    "FEATUREGEN_MATERIALIZE_METASTORE_HOST": "spark-thrift",
    "FEATUREGEN_MATERIALIZE_METASTORE_PORT": "10000",
    "FEATUREGEN_MATERIALIZE_METASTORE_AUTH": "NONE",
    "FEATUREGEN_MATERIALIZE_METASTORE_PRINCIPAL": "featuregen",
    "FEATUREGEN_MATERIALIZE_STAGING_BASE": "/warehouse/staging",
    "FEATUREGEN_MATERIALIZE_SUBMIT_PYTHON": "/opt/kedro-venv/bin/python",
    "FEATUREGEN_MATERIALIZE_SUBMIT_TIMEOUT_SECONDS": "3600",
}


def _compile_env(monkeypatch, tmp_path) -> None:
    """The four variables a deployment that only COMPILES sets, and no execution block at all."""
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_INVENTORY", str(_write(tmp_path, _document())))
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_L0_PYTHON", sys.executable)
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS", "120")
    for variable in _EXECUTION_ENV:
        monkeypatch.delenv(variable, raising=False)


def test_no_EXECUTION_block_is_a_POSTURE_and_the_run_is_honestly_unprepared(
        monkeypatch, tmp_path) -> None:
    """`l0=None`'s rule, two stages later. A deployment that cannot execute a run says so by
    setting none of the eight, and the chain records that as the run's OUTCOME."""
    _compile_env(monkeypatch, tmp_path)

    config = lane_config_from_env()

    assert (config.metastore, config.swap, config.submitter, config.staging_base) == \
        (None, None, None, None)
    assert config.execution_for(_job("req", (), business_dt=_BUSINESS_DT)) is None


@pytest.mark.parametrize("stated", ["FEATUREGEN_MATERIALIZE_METASTORE_HOST",
                                    "FEATUREGEN_MATERIALIZE_STAGING_BASE",
                                    "FEATUREGEN_MATERIALIZE_SUBMIT_PYTHON"])
def test_HALF_an_execution_block_is_refused_and_NAMES_what_is_missing(
        monkeypatch, tmp_path, stated) -> None:
    """Neither posture. `RunExecution` would refuse the half anyway ("ALL FIVE or None"), and a
    deployment that stated a metastore host and no staging base has not chosen anything — so the
    refusal names every variable that is unset rather than the first one."""
    _compile_env(monkeypatch, tmp_path)
    monkeypatch.setenv(stated, _EXECUTION_ENV[stated])

    with pytest.raises(ValueError) as raised:
        lane_config_from_env()

    message = str(raised.value)
    assert "half configured" in message
    assert all(name in message for name in _EXECUTION_ENV if name != stated)


def test_a_configured_deployment_gets_BOTH_adapters_over_ONE_session(monkeypatch,
                                                                     tmp_path) -> None:
    """One transport, structurally. L1 asks the environment which partitions exist and the swap
    then makes a generation visible in it; two sessions could answer from two worlds, so the lane
    builds one and hands it to both."""
    _compile_env(monkeypatch, tmp_path)
    for name, value in _EXECUTION_ENV.items():
        monkeypatch.setenv(name, value)

    config = lane_config_from_env()

    assert isinstance(config.metastore, SqlMetastoreAdapter)
    assert isinstance(config.swap, SqlPublicationSwap)
    assert config.metastore.session is config.swap.session
    assert config.metastore.session.endpoint == MetastoreEndpoint(
        engine="hive", host="spark-thrift", port=10000, auth_mechanism="NONE",
        principal="featuregen")
    assert config.submitter.python_executable == "/opt/kedro-venv/bin/python"
    assert config.submitter.timeout_seconds == 3600.0
    assert config.staging_base == "/warehouse/staging"

    execution = config.execution_for(_job("req", (), business_dt=_BUSINESS_DT))
    assert execution is not None and execution.metastore is config.metastore
    assert execution.swap is config.swap and execution.staging_base == "/warehouse/staging"


def test_a_configured_deployment_still_needs_the_JOB_to_declare_a_business_date(
        monkeypatch, tmp_path) -> None:
    """The one part of an execution that is NOT the deployment's. A worker-wide date would run
    every group at whatever day the pod was configured with."""
    _compile_env(monkeypatch, tmp_path)
    for name, value in _EXECUTION_ENV.items():
        monkeypatch.setenv(name, value)

    assert lane_config_from_env().execution_for(_job("req", (), business_dt=None)) is None


def _metastore_double(inventory, *, business_dt=_BUSINESS_DT, band_days=500):
    """A DB-API double stocked with what the REAL adapter asks a REAL engine, for this inventory.

    The answers are derived from the layouts the compilation was authorized against — the same
    discipline `_G2Metastore` states — so a run cannot pass L1 against a table nobody declared. The
    partitions are a band around the business date, because the exact resolved set lives inside the
    chain where no caller can reach it.
    """
    answers = {}
    anchor = _datetime.date.fromisoformat(business_dt)
    for ref, layout in inventory.tables.items():
        schema, table = ref.split(".")
        quoted = f"`{schema}`.`{table}`"
        answers[f"SHOW TABLES IN `{schema}` LIKE '{table}'"] = (("tab_name",), ((table,),))
        answers[f"DESCRIBE {quoted}"] = (
            ("col_name", "data_type", "comment"),
            tuple((name, physical_type, "")
                  for name, physical_type in (*layout.columns,
                                              *(layout.partition_columns or ()))))
        mapping = layout.partition_mapping
        rows: tuple[tuple[str], ...] = ()
        if isinstance(mapping, EventTimePartition):
            form = PARTITION_VALUE_FORMS[mapping.transform]
            rows = tuple(
                (f"{mapping.partition_column}={form(anchor + _datetime.timedelta(days=offset))}",)
                for offset in range(-band_days, band_days + 1))
        answers[f"SHOW PARTITIONS {quoted}"] = (("partition",), rows)
    answers["SHOW GRANT"] = (
        ("database", "table", "partition", "column", "principal_name", "principal_type",
         "privilege", "grant_option", "grant_time", "grantor"),
        (("", "", "", "", _ROLES[0], "ROLE", "SELECT", False, 0, "admin"),))
    return _Engine(answers)


def test_a_lane_with_the_REAL_adapters_carries_a_run_PAST_prepare_run(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """WHERE THE BOUNDARY NOW SITS. `test_a_fully_configured_lane_PUBLISHES` proves the chain
    composes through the two SEAMS using fakes defined in the tests; this proves it composes
    through the two IMPLEMENTATIONS, with only the DB-API driver faked — the real adapter really
    builds `SHOW PARTITIONS` / `SHOW TABLES` / `DESCRIBE` / `SHOW GRANT`, really parses what comes
    back, and the real swap really emits its one `CREATE OR REPLACE VIEW` and reads it back.

    The remaining fake is the SUBMITTER, and it has to be: submitting for real launches a Spark
    process, which is `l0_gate.py`'s job and not the default suite's."""
    request = _recorded(catalog, request_id="req-lane-real-adapters")
    work_items = [_authored(catalog, monkeypatch, suffix="realadapters")]
    enqueue_materialization(catalog, request, job=_job(
        request.request_id, work_items, business_dt=_BUSINESS_DT))
    _attest_capability(catalog)
    engine = _metastore_double(INVENTORY)
    session = MetastoreSession(ENDPOINT, connect=engine.connect)

    outcome = _drain(catalog, _config(
        tmp_path, metastore=SqlMetastoreAdapter(session), swap=SqlPublicationSwap(session),
        submitter=_Submitter(), staging_base="/warehouse/staging"))

    assert outcome.status == "completed"
    assert outcome.stopped_at == ChainStage.PUBLISH.value, \
        "the run did not get past PREPARE_RUN — the boundary did not move"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED

    asked = [statement.split(" `")[0] for statement in engine.statements]
    assert "SHOW GRANT ROLE" in asked, "L1 never asked whether these roles may read"
    assert "DESCRIBE" in asked and "SHOW PARTITIONS" in asked
    swapped = [statement for statement in engine.statements
               if statement.startswith("CREATE OR REPLACE VIEW")]
    assert len(swapped) == 1, "the publication was not exactly one metastore operation"
    assert "/warehouse/staging/" in swapped[0] and "/published/" in swapped[0]
    assert engine.connections == 1, "the metadata reads and the swap used two connections"
