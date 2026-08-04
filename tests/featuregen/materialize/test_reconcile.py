"""Phase G T13 §3.3 — the RECONCILER: an abandoned run gets a VERDICT, not a retry.

**Every stranded state below is MANUFACTURED BY THE REAL LANE**, not hand-written into the two
tables. That is the whole point of the suite: a reconciler tested against rows a test author
imagined would agree with the author rather than with the producer, and the three classes exist
precisely because nobody imagined them until Task 7's review traced the producer paths.

* **P1** — a worker claimed the request and died; the redelivery found a live claim, refused it and
  dead-lettered the message (``_unclaimable``); the dead worker's lease then ran out. Produced by
  ``accept_request`` + one real drain + the lease running out.
* **P2** — the same, one step earlier: the message is dead and the claim is still LIVE. Another
  worker may be compiling it right now, so it must be left alone.
* **P3** — an unconfigured deployment whose retry budget ran out: the request never left
  ``requested``, holds no lease, and its message is dead. Produced by one real drain with the
  environment unset and the attempt budget one short.

**THE TEST THAT MATTERS MOST** is
:func:`test_a_request_in_its_RELEASE_BACKOFF_window_is_NOT_terminalized`. A released lease is
byte-for-byte an abandoned one — Task 7's retry path releases by *expiring* the lease, because
migration 1053 CHECKs that an ``accepted`` row has one — so for the whole backoff window a healthy
request awaiting redelivery is indistinguishable from an abandoned one **on the lease alone**. It is
told apart by the queue row, and the test proves the consequence rather than the mechanism: the
redelivery that follows must report ``completed``, i.e. a compile that actually happened, not
``replayed``, which is what a wrongly-terminalized request would quietly produce.
"""
from __future__ import annotations

import datetime as _datetime

import pytest
from tests.featuregen.materialize.test_chain import _authored, _inject_l0, _seed
from tests.featuregen.materialize.test_queue_lane import (
    _config,
    _drain,
    _expire_lease,
    _job,
    _now,
    _queue_row,
    _recorded,
    _release,
)
from tests.featuregen.materialize.test_resolve import no_dsn  # noqa: F401 — autouse

from featuregen.materialize import queue_lane
from featuregen.materialize.compile import chain as chain_module
from featuregen.materialize.control_plane import read_run_events
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    enqueue_materialization,
    materialization_message_id,
    process_materialization_once,
)
from featuregen.materialize.reconcile import (
    ReconciliationVerdict,
    reconcile_abandoned_requests,
)
from featuregen.materialize.request_store import (
    LEGAL_LIFECYCLE_TRANSITIONS,
    RequestLifecycle,
    accept_request,
    expired_requests,
    read_request,
)
from featuregen.runtime.observability import counters
from featuregen.runtime.queue import claim_materialization


@pytest.fixture
def catalog(db):
    return _seed(db)


@pytest.fixture
def l0_passes(monkeypatch):
    """L0 PASSES — the precondition of every test whose subject is not L0 itself."""
    _inject_l0(monkeypatch)


@pytest.fixture
def enqueued(catalog, monkeypatch, l0_passes, tmp_path):
    """One recorded request, one resolvable feature, one enqueued job — what a route leaves."""
    request = _recorded(catalog)
    work_items = [_authored(catalog, monkeypatch)]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    return request, work_items, _config(tmp_path)


def _sweep(db):
    return reconcile_abandoned_requests(db, now=_now())


def _verdict(db, request_id: str) -> ReconciliationVerdict | None:
    return _sweep(db).verdict_for(request_id)


def _dead_letter_the_message(db, config, request_id: str) -> None:
    """Drive the REAL lane into ``_unclaimable``: it finds a claim it did not make, refuses to mint
    a second generation under one durable identity, and dead-letters the delivery. This is the only
    producer of "the request is non-terminal and no delivery will ever arrive again"."""
    outcome = _drain(db, config)
    assert outcome.status == "unclaimable", outcome
    assert _queue_row(db, request_id)[0] == "dead"


# ── P2: the message is dead and the claim is still LIVE ──────────────────────────────────────────

def test_P2_a_dead_message_with_a_LIVE_claim_is_LEFT_ALONE(enqueued, catalog) -> None:
    """A worker may be compiling it right now. The queue row says nobody will redeliver; the lease
    says somebody is working. Terminalizing on the first fact alone would race a live compile onto
    a plane whose terminal events cannot be retracted."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.LEASED
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED


# ── P1: the claim expired too, and nothing will ever deliver the message ─────────────────────────

def test_P1_an_abandoned_claim_behind_a_DEAD_message_is_FAILED(enqueued, catalog) -> None:
    """The one class with an honest verdict available in G-1. Every write the chain makes is inside
    ``_commit``'s single transaction, so a durably-visible ``accepted`` request PROVES no generation,
    no artifact, no report and no run event exist — the absence IS the evidence, and ``failed`` is
    what ``RequestLifecycle.FAILED`` already documents ("including reconciled abandonment")."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.FAILED
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.FAILED
    assert stored.generation_id is None and stored.run_id is None


# ── P3: invisible to expired_requests, and with no legal terminal ────────────────────────────────

def _stranded_at_requested(catalog, monkeypatch, request_id: str) -> None:
    """The REAL producer: a deployment that never configured materialization, whose retry budget
    runs out. The lane refuses to accept before it has a configuration (accepting first would burn
    every arriving request into a terminal no fix could recover), so the request never leaves
    ``requested`` — and when the budget is gone the message dies with nobody holding it."""
    for variable in ("FEATUREGEN_MATERIALIZE_PROJECT_ROOT", "FEATUREGEN_MATERIALIZE_INVENTORY"):
        monkeypatch.delenv(variable, raising=False)
    catalog.execute("UPDATE queue SET attempts = max_attempts - 1 WHERE message_id = %s",
                    (materialization_message_id(request_id),))
    outcome = process_materialization_once(catalog, owner="w1")
    assert outcome.status == "dead_letter", outcome


def test_P3_is_INVISIBLE_to_expired_requests_and_the_sweep_still_finds_it(
        enqueued, catalog, monkeypatch) -> None:
    """The structural blind spot. ``expired_requests`` requires ``lease_expires_at IS NOT NULL`` and
    a ``requested`` row holds no lease, so a reconciler built on that one query never sees this
    class at all. Two queries, because one cannot answer both questions."""
    request, _, _ = enqueued
    _stranded_at_requested(catalog, monkeypatch, request.request_id)

    assert expired_requests(catalog, now=_now()) == ()          # the blind spot, demonstrated

    considered = {row.request_id for row in _sweep(catalog).considered}
    assert request.request_id in considered


def test_P3_has_NO_legal_terminal_and_the_reconciler_refuses_to_invent_one(
        enqueued, catalog, monkeypatch) -> None:
    """``requested → failed`` is not an edge of the shipped state machine (§3.2), and adding one —
    directly, or by laundering the request through ``accepted`` to reach a terminal — would record
    that a worker claimed work nobody ever claimed. The reconciler reports the class and leaves the
    row exactly as it found it."""
    request, _, _ = enqueued
    _stranded_at_requested(catalog, monkeypatch, request.request_id)

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.NO_LEGAL_TERMINAL
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.REQUESTED
    assert stored.accepted_at is None and stored.lease_expires_at is None


def test_the_reconciler_adds_NO_edge_to_the_shipped_state_machine() -> None:
    """The refusal above is only worth anything if the set it appeals to is the shipped one. A
    ``requested → failed`` edge added to make P3 fit would fail here rather than ship quietly."""
    assert LEGAL_LIFECYCLE_TRANSITIONS[RequestLifecycle.REQUESTED] == frozenset(
        {RequestLifecycle.ACCEPTED})


# ── the false-verdict trap ───────────────────────────────────────────────────────────────────────

def test_a_request_in_its_RELEASE_BACKOFF_window_is_NOT_terminalized(
        enqueued, catalog, monkeypatch) -> None:
    """**THE TEST THAT MATTERS MOST.**

    A transient fault makes the lane RELEASE its claim — and the only release migration 1053 permits
    is expiring the lease, because an ``accepted`` row must have one. So the request now looks
    exactly like P1 on the lease alone: ``accepted``, lease in the past. It is not abandoned at all;
    its message is ``ready`` on a backoff and the redelivery is coming.

    The sweep's ``now`` is taken from PAST that released lease deliberately — the released lease is
    a millisecond long and a real backoff is seconds to minutes, so this is the ordinary case, and
    it makes the trap maximal: the lease is demonstrably expired and the request must survive anyway.

    The assertion that matters is not "the row was left alone" but what the redelivery then reports.
    A terminalized request makes the chain short-circuit, and the lane reports ``replayed`` — "this
    was already done" — for a compile that never happened. So the test demands ``completed``.
    """
    from tests.featuregen.materialize.test_queue_lane import _boom_once

    request, _, config = enqueued
    monkeypatch.setattr(queue_lane, "compile_feature_group", _boom_once(ConnectionError("gone")))
    assert _drain(catalog, config).status == "retryable"
    released = read_request(catalog, request_id=request.request_id)
    assert released.lifecycle_state is RequestLifecycle.ACCEPTED
    # RELEASED, not held: a lease this worker still owned would be `config.lease_seconds` — minutes.
    assert released.lease_expires_at - _now() < _datetime.timedelta(seconds=1)
    assert config.lease_seconds > 60.0
    assert _queue_row(catalog, request.request_id)[0] == "ready"

    after_the_lease = released.lease_expires_at + _datetime.timedelta(seconds=1)
    sweep = reconcile_abandoned_requests(catalog, now=after_the_lease)

    assert sweep.verdict_for(request.request_id) is ReconciliationVerdict.OWNED
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED

    _release(catalog, request.request_id)
    again = _drain(catalog, config)

    assert again.status == "completed", "a compile that never happened was reported as done"
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


def test_a_LIVE_leased_message_is_left_alone_however_expired_the_lease_looks(
        enqueued, catalog) -> None:
    """The other half: a worker holds the message right now. While a compile is in flight its queue
    row is ``leased`` for the whole duration, which is also what keeps this sweep from ever blocking
    on the row lock that compile's transaction holds."""
    request, _, _ = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _expire_lease(catalog, request.request_id)
    assert claim_materialization(catalog, owner="w2", lease_seconds=300) is not None
    assert _queue_row(catalog, request.request_id)[0] == "leased"

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.OWNED
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED


# ── what the reconciler must never do ────────────────────────────────────────────────────────────

def test_the_reconciler_never_re_drives_a_COMPILE(enqueued, catalog, monkeypatch) -> None:
    """§3.3: a re-run is a NEW request. A verdict comes from evidence, never from running the work
    again — the plane is append-only with one terminal per run and no repair path."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)
    calls: list[object] = []
    monkeypatch.setattr(chain_module, "compile_feature_group",
                        lambda *a, **k: calls.append("called"))
    monkeypatch.setattr(queue_lane, "compile_feature_group",
                        lambda *a, **k: calls.append("called"))
    before = _queue_row(catalog, request.request_id)

    _sweep(catalog)

    assert calls == []
    assert catalog.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 0
    assert catalog.execute("SELECT count(*) FROM materialization_run_event").fetchone()[0] == 0
    assert _queue_row(catalog, request.request_id) == before   # no re-queue, no extra attempt


def test_reconciling_TWICE_changes_nothing_the_second_time(enqueued, catalog, monkeypatch) -> None:
    """Idempotency, over all three classes at once: a terminalized request leaves both queries, and
    a class with no honest verdict is REPORTED again rather than acted on again."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)
    stranded = _recorded(catalog, request_id="req-recon-p3")
    enqueue_materialization(catalog, stranded, job=_job(stranded.request_id, ["wi-none"]))
    _stranded_at_requested(catalog, monkeypatch, stranded.request_id)

    first = _sweep(catalog)
    snapshot = _rows(catalog)
    second = _sweep(catalog)

    assert first.terminalized == 1
    assert second.terminalized == 0
    assert second.verdict_for(request.request_id) is None      # terminal: out of both queries
    assert second.verdict_for(stranded.request_id) is ReconciliationVerdict.NO_LEGAL_TERMINAL
    assert _rows(catalog) == snapshot


def _rows(db):
    return db.execute(
        "SELECT request_id, lifecycle_state, generation_id, run_id, accepted_at, lease_expires_at "
        "FROM materialization_request ORDER BY request_id").fetchall()


# ── a run that reached the plane ─────────────────────────────────────────────────────────────────

def test_a_RUNNING_request_MIRRORS_the_planes_own_terminal(enqueued, catalog) -> None:
    """The one verdict that is a READING rather than an absence.

    ``running`` is not durably reachable in G-1 — ``_commit`` moves ``accepted → running →
    committed|failed`` inside one transaction, so no reader ever observes the middle state. The
    evidence here is therefore produced by a real compile and the row is then wound back to the
    state a crash *between* those writes would leave if the transaction were ever split. The
    reconciler reads the plane's terminal and mirrors it: ``PUBLICATION_REFUSED`` is exactly the
    event ``_commit`` pairs with ``committed``.
    """
    request, _, config = enqueued
    assert _drain(catalog, config).status == "completed"
    run_id = read_request(catalog, request_id=request.request_id).run_id
    assert [e.event_kind.value for e in read_run_events(catalog, run_id)] == ["PUBLICATION_REFUSED"]
    catalog.execute("UPDATE materialization_request SET lifecycle_state = 'running' "
                    "WHERE request_id = %s", (request.request_id,))
    _expire_lease(catalog, request.request_id)

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.COMMITTED
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


def test_a_RUNNING_request_with_NO_terminal_event_is_NOT_judged(enqueued, catalog) -> None:
    """§3.3 decides a prepared run from its staging manifests, and G-1 submits nothing, so there are
    none to read. Writing ``failed`` here would put the coordination record in disagreement with an
    open run stream on no evidence at all — the reconciler says so instead."""
    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)
    catalog.execute("UPDATE materialization_request SET lifecycle_state = 'running', "
                    "run_id = %s WHERE request_id = %s", ("mrun_nothing", request.request_id))

    assert _verdict(catalog, request.request_id) is ReconciliationVerdict.NO_RUN_EVIDENCE
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.RUNNING


# ── the worker stage ─────────────────────────────────────────────────────────────────────────────

def test_a_worker_TICK_reconciles(enqueued, catalog, monkeypatch) -> None:
    """It runs where the lane runs. A reconciler nothing schedules is a deferral wearing a costume."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    monkeypatch.setattr("featuregen.materialize.queue_lane.lane_config_from_env", lambda: config)

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_now())

    assert tick.materialization_reconciled == 1
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.FAILED


def test_the_KILL_SWITCH_stops_the_sweep_too(enqueued, catalog, monkeypatch) -> None:
    """The switch is one switch. A deployment that turned materialization off did not ask for a
    sweep that keeps writing verdicts about its requests — and T9's "off costs nothing" property is
    a property of the TICK, not of one stage."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    request, _, config = enqueued
    accept_request(catalog, request_id=request.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, request.request_id)
    _expire_lease(catalog, request.request_id)
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
    counters.reset()

    tick = run_worker_once(catalog, HandlerRegistry(), [], owner="w1", now=_now())

    assert tick.materialization_reconciled == 0
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED
    assert [k for k in counters.snapshot()["gauges"] if "reconcile" in k] == []


# ── bounds ───────────────────────────────────────────────────────────────────────────────────────

def test_a_class_with_NO_verdict_does_not_STARVE_a_class_with_one(
        catalog, monkeypatch, l0_passes, tmp_path) -> None:
    """The bound has a failure mode, and this is it. P3 is permanent — it is reported every sweep
    and never leaves the candidate set — so an oldest-first sweep would let a standing set of
    stranded ``requested`` rows sit at the head of the budget forever and hide every abandoned claim
    behind them. Candidates a terminal edge exists for are judged first, which is derived from the
    shipped transition set rather than listed by hand."""
    config = _config(tmp_path)
    work_items = [_authored(catalog, monkeypatch)]
    for name in ("req-starve-a", "req-starve-b"):
        older = _recorded(catalog, request_id=name)
        enqueue_materialization(catalog, older, job=_job(name, work_items))
        _stranded_at_requested(catalog, monkeypatch, name)
    live = _recorded(catalog, request_id="req-starve-live")
    enqueue_materialization(catalog, live, job=_job(live.request_id, work_items))
    accept_request(catalog, request_id=live.request_id, lease_seconds=3600)
    _dead_letter_the_message(catalog, config, live.request_id)
    _expire_lease(catalog, live.request_id)

    sweep = reconcile_abandoned_requests(catalog, now=_now(), limit=1)

    assert sweep.verdict_for(live.request_id) is ReconciliationVerdict.FAILED
    assert read_request(catalog, request_id=live.request_id).lifecycle_state is \
        RequestLifecycle.FAILED


def test_the_sweep_is_BOUNDED(catalog, monkeypatch) -> None:
    """One tick's sweep is bounded, because it shares the worker's single connection with the relay,
    the timers and every poller."""
    for index in range(4):
        _recorded(catalog, request_id=f"req-bound-{index}")

    sweep = reconcile_abandoned_requests(catalog, now=_now(), limit=2)

    assert len(sweep.considered) == 2
