"""``request_store`` — the mutable coordination record that anchors a run (Phase G §3.2).

What these tests hold the store to:

* a request is durable the moment somebody asks, before any event, generation or run exists;
* two identical requests are ONE request — the duplicate returns the existing row, never a second;
* the lifecycle moves only along the legal edges, and the illegal ones are refused **exhaustively**
  (every (from, to) pair is attempted, so an edge quietly added to the transition table fails here);
* a lease is granted by acceptance and renewable only while the request is being worked;
* the reconciler's query returns expired, non-terminal requests and nothing else, in a stable order.
"""
from __future__ import annotations

import dataclasses
import datetime as _datetime
import itertools

import pytest

from featuregen.materialize.request_store import (
    # The column list the reads are positional against.
    _COLUMNS,
    # The EXACT statement the reconciler issues — EXPLAINed below rather than re-typed, because a
    # look-alike query would prove nothing about the one that actually runs.
    _EXPIRED_REQUESTS_SQL,
    LEGAL_LIFECYCLE_TRANSITIONS,
    MaterializationRequestV1,
    RequestLifecycle,
    accept_request,
    advance_lifecycle,
    expired_requests,
    read_request,
    record_request,
    renew_lease,
)

GEN = "gen-req-store"
RUN = "run-req-store"
NOW = "2026-08-03T10:00:00+00:00"
ACTIVATION = {"OVERLAY_PASS_C": True, "materialization_enabled": True}


def _generation(conn, generation_id: str = GEN) -> str:
    conn.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, 'cif_daily', 'ct-hash', 'gp-hash', 'proj-hash', %s)",
        (generation_id, NOW))
    return generation_id


def _record(conn, suffix: str = "a", **overrides) -> MaterializationRequestV1:
    kwargs = {
        "request_id": f"req-{suffix}",
        "logical_group_name": "cif_daily",
        "requested_by": "analyst@bank.example",
        "authorized_roles": ("feature_engineer",),
        "idempotency_key": f"idem-{suffix}",
        "activation_state": dict(ACTIVATION),
        "resolved_input_digest": "sha256:deadbeef",
    }
    kwargs.update(overrides)
    return record_request(conn, **kwargs)


def _in_state(conn, state: RequestLifecycle, suffix: str) -> MaterializationRequestV1:
    """Drive a fresh request to ``state`` along the LEGAL path only — so the transition-matrix test
    below never depends on the very SQL it is measuring being bypassable."""
    request = _record(conn, suffix)
    if state is RequestLifecycle.REQUESTED:
        return request
    accept_request(conn, request_id=request.request_id, lease_seconds=300)
    if state is RequestLifecycle.ACCEPTED:
        return read_request(conn, request_id=request.request_id)
    advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING,
                      run_id=f"{RUN}-{suffix}")
    if state is RequestLifecycle.RUNNING:
        return read_request(conn, request_id=request.request_id)
    advance_lifecycle(conn, request_id=request.request_id, to_state=state)
    return read_request(conn, request_id=request.request_id)


# ── 1. the record is durable before anything else exists ─────────────────────────────────────────


def test_a_request_round_trips_through_the_database(conn) -> None:
    """The frozen record read back equals the record written — including the roles snapshot and the
    activation state, which are the two fields a lossy round trip would quietly flatten."""
    written = _record(conn)
    assert read_request(conn, request_id="req-a") == written
    assert written.lifecycle_state is RequestLifecycle.REQUESTED
    assert written.authorized_roles == ("feature_engineer",)
    assert written.activation_state == ACTIVATION
    assert written.generation_id is None and written.run_id is None
    assert written.accepted_at is None and written.lease_expires_at is None


def test_the_record_mirrors_the_selected_columns_field_for_field(conn) -> None:
    """Every read is ``MaterializationRequestV1(*row)``, so the two lists are one list written
    twice. A column added to the SELECT without a field (or the reverse) would shift every value
    after it into the wrong field — types alone would not always catch it, since five of the
    fourteen columns are text."""
    assert [field.name for field in dataclasses.fields(MaterializationRequestV1)] == \
        [column.strip() for column in _COLUMNS.split(",")]


def test_a_request_exists_before_any_generation_run_or_event(conn) -> None:
    """The hole this table closes: a crash between "we decided to run" and the first appended event
    used to leave ZERO trace, because a run had no identity until an event named it."""
    _record(conn)
    assert conn.execute("SELECT count(*) FROM materialization_run_event").fetchone()[0] == 0
    assert read_request(conn, request_id="req-a") is not None


def test_an_unknown_request_reads_as_None(conn) -> None:
    assert read_request(conn, request_id="never-asked") is None


def test_a_duplicate_idempotency_key_returns_the_EXISTING_row(conn) -> None:
    """Two identical requests are one request. Returning the existing row (rather than raising) is
    what makes a retried HTTP call safe — and the count proves no second run was minted."""
    first = _record(conn, "a")
    identical = _record(conn, "a")            # the ordinary retry: same id, same key
    second = _record(conn, "a", request_id="req-a-retry")   # a retry that re-minted its id
    assert identical == first
    assert second == first
    assert second.request_id == "req-a"
    assert conn.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 1


def test_an_idempotency_key_reused_for_a_DIFFERENT_request_is_refused(conn) -> None:
    """Returning the stored row here would tell the caller their request was queued when a different
    one was — the silent wrong answer, which is worse than the duplicate it prevents."""
    _record(conn, "a")
    with pytest.raises(ValueError, match="idempotency key"):
        _record(conn, "a", request_id="req-b", logical_group_name="card_daily")


def test_a_blank_actor_is_refused_before_it_reaches_the_database(conn) -> None:
    with pytest.raises(ValueError, match="requested_by"):
        _record(conn, "a", requested_by="  ")


def test_an_empty_roles_snapshot_is_refused(conn) -> None:
    """The run is judged against the scope its requester actually held; an empty snapshot judges
    nothing."""
    with pytest.raises(ValueError, match="authorized_roles"):
        _record(conn, "a", authorized_roles=())


# ── 2. acceptance and the lease ──────────────────────────────────────────────────────────────────


def test_accepting_stamps_the_acceptance_and_the_lease(conn) -> None:
    accepted = accept_request(conn, request_id=_record(conn).request_id, lease_seconds=300)
    assert accepted.lifecycle_state is RequestLifecycle.ACCEPTED
    assert accepted.accepted_at is not None
    assert accepted.lease_expires_at is not None
    assert (accepted.lease_expires_at - accepted.accepted_at) == _datetime.timedelta(seconds=300)
    assert read_request(conn, request_id="req-a") == accepted


def test_a_second_acceptance_is_refused(conn) -> None:
    """Acceptance is the claim on the work. A second one would hand the same request to two
    workers, and the append-only plane has no repair path for two writers on one run."""
    _record(conn)
    accept_request(conn, request_id="req-a", lease_seconds=300)
    with pytest.raises(ValueError, match="accepted"):
        accept_request(conn, request_id="req-a", lease_seconds=300)


def test_accepting_an_unknown_request_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="no materialization request"):
        accept_request(conn, request_id="never-asked", lease_seconds=300)


@pytest.mark.parametrize("lease_seconds", [0, -1])
def test_a_lease_must_have_a_positive_duration(conn, lease_seconds: int) -> None:
    """A zero-second lease is expired the instant it is granted, so the reconciler would adopt a
    request whose worker had not yet drawn breath."""
    _record(conn)
    with pytest.raises(ValueError, match="lease_seconds"):
        accept_request(conn, request_id="req-a", lease_seconds=lease_seconds)


@pytest.mark.parametrize("state", [RequestLifecycle.ACCEPTED, RequestLifecycle.RUNNING])
def test_the_lease_renews_while_the_request_is_being_worked(conn, state: RequestLifecycle) -> None:
    before = _in_state(conn, state, suffix=state.value)
    renewed = renew_lease(conn, request_id=before.request_id, lease_seconds=900)
    assert renewed.lifecycle_state is state
    assert renewed.lease_expires_at > before.lease_expires_at


@pytest.mark.parametrize("state", [RequestLifecycle.REQUESTED, RequestLifecycle.COMMITTED,
                                   RequestLifecycle.FAILED])
def test_the_lease_does_not_renew_outside_the_working_states(
        conn, state: RequestLifecycle) -> None:
    """Nothing is being worked in ``requested`` (no claim yet) or in a terminal state (the work is
    over) — a renewal there would keep a dead request out of the reconciler's reach forever."""
    request = _in_state(conn, state, suffix=state.value)
    with pytest.raises(ValueError, match=state.value):
        renew_lease(conn, request_id=request.request_id, lease_seconds=900)


# ── 3. the lifecycle, exhaustively ───────────────────────────────────────────────────────────────


def test_the_legal_transition_set_is_exactly_what_the_plan_ships(conn) -> None:
    """Pinned as a set, not described in prose: requested → accepted → running → committed|failed,
    plus the one edge the reconciler needs (accepted → failed, for a worker that died between
    claiming the request and preparing a run). Terminal is terminal; nothing moves backwards."""
    assert LEGAL_LIFECYCLE_TRANSITIONS == {
        RequestLifecycle.REQUESTED: frozenset({RequestLifecycle.ACCEPTED}),
        RequestLifecycle.ACCEPTED: frozenset({RequestLifecycle.RUNNING, RequestLifecycle.FAILED}),
        RequestLifecycle.RUNNING: frozenset({RequestLifecycle.COMMITTED, RequestLifecycle.FAILED}),
        RequestLifecycle.COMMITTED: frozenset(),
        RequestLifecycle.FAILED: frozenset(),
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [pytest.param(source, target, id=f"{source.value}->{target.value}")
     for source, target in itertools.product(RequestLifecycle, RequestLifecycle)])
def test_every_transition_is_allowed_or_refused_exactly_as_the_table_says(
        conn, source: RequestLifecycle, target: RequestLifecycle) -> None:
    """All 25 pairs, including the self-transitions: re-advancing to the state a request is already
    in is a caller who lost track of the run, and letting it through would hide that."""
    suffix = f"{source.value}-{target.value}"
    request = _in_state(conn, source, suffix=suffix)
    legal = target in LEGAL_LIFECYCLE_TRANSITIONS[source]
    run_id = f"{RUN}-{suffix}" if target is RequestLifecycle.RUNNING else None
    if legal:
        moved = advance_lifecycle(conn, request_id=request.request_id, to_state=target,
                                  run_id=run_id)
        assert moved.lifecycle_state is target
        assert read_request(conn, request_id=request.request_id) == moved
    else:
        with pytest.raises(ValueError, match=source.value):
            advance_lifecycle(conn, request_id=request.request_id, to_state=target, run_id=run_id)
        assert read_request(conn, request_id=request.request_id).lifecycle_state is source


def test_advancing_an_unknown_request_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="no materialization request"):
        advance_lifecycle(conn, request_id="never-asked", to_state=RequestLifecycle.ACCEPTED)


def test_running_requires_the_run_it_claims_to_be(conn) -> None:
    """``running`` means a run was prepared. Without a run_id the request would claim to be running
    something nobody could look up — and the reconciler reads run evidence BY run id."""
    request = _in_state(conn, RequestLifecycle.ACCEPTED, suffix="norun")
    with pytest.raises(ValueError, match="run_id"):
        advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING)


def test_the_generation_and_run_links_are_stamped_once(conn) -> None:
    _generation(conn)
    request = _in_state(conn, RequestLifecycle.ACCEPTED, suffix="link")
    moved = advance_lifecycle(conn, request_id=request.request_id,
                              to_state=RequestLifecycle.RUNNING, generation_id=GEN, run_id=RUN)
    assert (moved.generation_id, moved.run_id) == (GEN, RUN)
    committed = advance_lifecycle(conn, request_id=request.request_id,
                                  to_state=RequestLifecycle.COMMITTED)
    assert (committed.generation_id, committed.run_id) == (GEN, RUN)


def test_a_contradicting_link_is_refused_rather_than_overwritten(conn) -> None:
    """Which compilation a request became is recorded once. Overwriting it would rewrite what a run
    was — the mutable record's one place where it must behave like evidence."""
    _generation(conn)
    _generation(conn, "gen-other")
    request = _in_state(conn, RequestLifecycle.ACCEPTED, suffix="clash")
    advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING,
                      generation_id=GEN, run_id=RUN)
    with pytest.raises(ValueError, match="generation_id"):
        advance_lifecycle(conn, request_id=request.request_id,
                          to_state=RequestLifecycle.COMMITTED, generation_id="gen-other")
    with pytest.raises(ValueError, match="run_id"):
        advance_lifecycle(conn, request_id=request.request_id,
                          to_state=RequestLifecycle.COMMITTED, run_id="run-other")
    assert read_request(conn, request_id=request.request_id).lifecycle_state \
        is RequestLifecycle.RUNNING


# ── 4. the reconciler's query ────────────────────────────────────────────────────────────────────


def _later(seconds: int) -> _datetime.datetime:
    return _datetime.datetime.now(tz=_datetime.UTC) + _datetime.timedelta(seconds=seconds)


def test_expired_requests_returns_only_expired_non_terminal_rows(conn) -> None:
    """The reconciler's ONLY real query. A live lease means somebody is still working; a terminal
    request has nothing left to reconcile; a never-accepted request holds no lease at all."""
    _record(conn, "live")
    accept_request(conn, request_id="req-live", lease_seconds=86_400)
    _record(conn, "unaccepted")
    stale = _in_state(conn, RequestLifecycle.RUNNING, suffix="stale")
    for terminal in (RequestLifecycle.COMMITTED, RequestLifecycle.FAILED):
        finished = _in_state(conn, RequestLifecycle.RUNNING, suffix=terminal.value)
        advance_lifecycle(conn, request_id=finished.request_id, to_state=terminal)

    expired = expired_requests(conn, now=_later(3_600))
    assert [request.request_id for request in expired] == [stale.request_id]


def test_expired_requests_is_deterministically_ordered(conn) -> None:
    """Two workers reconciling the same backlog must see the same order, so the ordering is by
    lease expiry with the request id as the tie-break — never the table's physical order."""
    for suffix, lease in (("z", 1), ("m", 1), ("a", 2)):
        _record(conn, suffix)
        accept_request(conn, request_id=f"req-{suffix}", lease_seconds=lease)
    expired = expired_requests(conn, now=_later(3_600))
    assert [request.request_id for request in expired] == ["req-m", "req-z", "req-a"]


def test_the_reconcilers_query_can_use_the_partial_index_under_a_GENERIC_plan(conn) -> None:
    """1053's index is partial, and PostgreSQL uses a partial index only when it can PROVE the
    query's predicate implies the index's. That proof is what fails silently: a reconciler runs the
    same statement in a loop, psycopg prepares it server-side after ``prepare_threshold`` (5)
    executions, and from then on the planner builds a GENERIC plan that sees ``$1`` rather than the
    values. Measured here (PostgreSQL 18, ``force_generic_plan``): with the terminal states bound as
    a parameter the plan is a Seq Scan; with them written as literals it is a Bitmap Index Scan on
    the partial index.

    So this test forces the generic plan rather than trusting the custom one — under a custom plan
    BOTH spellings use the index, and a test written that way would pass for the wrong reason.
    ``enable_seqscan = off`` removes the cost noise on a table with one row: what is being measured
    is whether the access path is LEGAL, not whether the planner prefers it.
    """
    _record(conn, "a")
    accept_request(conn, request_id="req-a", lease_seconds=1)
    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute("SET LOCAL plan_cache_mode = force_generic_plan")
    conn.execute("PREPARE reconciler_query (timestamptz) AS "
                 + _EXPIRED_REQUESTS_SQL.replace("%s", "$1"))
    plan = "\n".join(
        row[0] for row in conn.execute(
            "EXPLAIN EXECUTE reconciler_query('2099-01-01T00:00:00+00'::timestamptz)").fetchall())
    assert "materialization_request_expired_lease_idx" in plan, plan


def test_a_naive_now_is_refused(conn) -> None:
    """Comparing a naive datetime against ``timestamptz`` silently reads it in the session's zone,
    which would expire every lease in the world or none of them depending on where the worker
    runs."""
    with pytest.raises(ValueError, match="offset"):
        expired_requests(conn, now=_datetime.datetime(2026, 8, 3, 10, 0, 0))
