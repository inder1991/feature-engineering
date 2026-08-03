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
    _CADENCE,
    _FEATURE,
    _GROUP,
    _L0,
    _PROMISE,
    _ROLES,
    _attest_capability,
    _authored,
    _clock,
    _inject_l0,
    _seed,
)
from tests.featuregen.materialize.test_inventory import _document, _write
from tests.featuregen.materialize.test_ir import DECLARATION, INVENTORY
from tests.featuregen.materialize.test_resolve import (  # noqa: F401 — `no_dsn` is autouse
    _seed_work_item,
    no_dsn,
)

from featuregen.materialize.compile.chain import L0Interpreter
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.control_plane import read_run_events
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
from featuregen.materialize.publish import PublishMechanism
from featuregen.materialize.queue_lane import (
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


def _job(request_id: str, work_item_ids) -> MaterializationJobV1:
    return MaterializationJobV1(
        request_id=request_id, work_item_ids=tuple(work_item_ids), spine_declaration=DECLARATION,
        cadence=_CADENCE, availability_promise=_PROMISE,
        mechanism=PublishMechanism.VERSIONED_POINTER, published_schema=None,
        contract_overrides=None)


def _config(tmp_path, *, l0=_L0) -> MaterializationLaneConfig:
    """The DEPLOYMENT's half of the configuration — what the handler resolves at the boundary."""
    return MaterializationLaneConfig(
        inventory=INVENTORY, project_root=str(tmp_path), l0=l0, assemble_nodes=assemble_nodes,
        clock=_clock)


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

def test_a_PROVEN_capability_fails_the_request_with_a_LEGIBLE_reason(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """`PublishStepMissing` is a named exception the chain raises when publication capability is
    PROVEN and G-3's publish step does not exist. It is a statement about the PLATFORM, so it must
    not crash into the generic error path: the request fails, the reason is durable, and the row is
    not retried (an operator ingesting an attestation is not a transient fault)."""
    request = _recorded(catalog, request_id="req-lane-proven")
    work_items = [_authored(catalog, monkeypatch, suffix="laneproven")]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    _attest_capability(catalog)

    outcome = _drain(catalog, _config(tmp_path))

    assert outcome.status == "publish_step_missing"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED
    status, error, _, _ = _queue_row(catalog, request.request_id)
    assert status == "dead"
    assert "publish step" in error
    assert not list(pathlib.Path(tmp_path).iterdir())


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


def test_a_deployment_with_no_configuration_fails_the_request_rather_than_the_TICK(
        enqueued, catalog, monkeypatch) -> None:
    """Configuration is resolved AFTER a claim, so an unconfigured deployment costs one idle query
    per tick and never crashes the worker. When a job DOES arrive, the deployment's own defect is
    recorded against the request instead of retried against a value that will not change."""
    request, _, _ = enqueued
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("FEATUREGEN_MATERIALIZE_INVENTORY", raising=False)

    outcome = process_materialization_once(catalog, owner="w1")

    assert outcome.status == "failed"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED
    assert "FEATUREGEN_MATERIALIZE" in _queue_row(catalog, request.request_id)[1]


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
    """The stage is registered beside the existing ones, so `run_forever` drives it."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    request, _, config = enqueued
    monkeypatch.setattr("featuregen.materialize.queue_lane.lane_config_from_env", lambda: config)

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_NOW)

    assert tick.materialization_processed == 1
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


def test_an_unconfigured_deployment_costs_the_tick_NOTHING(catalog, monkeypatch) -> None:
    """Nothing enqueued means the stage never resolves a configuration, so a deployment with no
    materialization environment is not a counted stage error once a second, forever."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    for variable in ("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", "FEATUREGEN_MATERIALIZE_INVENTORY"):
        monkeypatch.delenv(variable, raising=False)
    counters.reset()

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_NOW)

    assert tick.materialization_processed == 0
    assert counters.snapshot()["counters"].get("worker.stage_error.materialization", 0) == 0


_NOW = _datetime.datetime(2026, 8, 3, 12, 0, tzinfo=_datetime.UTC)
