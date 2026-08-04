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

import featuregen.materialize.request_store as request_store
from featuregen.materialize.request_store import (
    # The EXACT statement the reconciler issues — EXPLAINed below rather than re-typed, because a
    # look-alike query would prove nothing about the one that actually runs.
    _EXPIRED_REQUESTS_SQL,
    _NON_IDENTITY_FIELDS,
    IDEMPOTENT_IDENTITY_FIELDS,
    LEGAL_LIFECYCLE_TRANSITIONS,
    # The column list the reads are positional against.
    REQUEST_COLUMNS,
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
        [column.strip() for column in REQUEST_COLUMNS.split(",")]


def test_the_idempotency_identity_split_partitions_the_record_exactly(conn) -> None:
    """Which fields make a retry "the same request" is a decision per field, so the two sets must
    PARTITION the record: add a column and this fails until somebody classifies it. Compared as sets
    rather than as a subset, because a subset assertion is what would let a new identity-bearing
    column be silently omitted from the comparison and quietly widen what one key can name."""
    fields = {field.name for field in dataclasses.fields(MaterializationRequestV1)}
    assert set(IDEMPOTENT_IDENTITY_FIELDS) | _NON_IDENTITY_FIELDS == fields
    assert not set(IDEMPOTENT_IDENTITY_FIELDS) & _NON_IDENTITY_FIELDS


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
    in is a caller who lost track of the run, and letting it through would hide that.

    ``accepted`` is the one target this function refuses from EVERY source, including the source the
    lifecycle table calls legal — it carries no lease to grant, and the next test says what that
    would cost.
    """
    suffix = f"{source.value}-{target.value}"
    request = _in_state(conn, source, suffix=suffix)
    run_id = f"{RUN}-{suffix}" if target is RequestLifecycle.RUNNING else None
    legal = (target in LEGAL_LIFECYCLE_TRANSITIONS[source]
             and target is not RequestLifecycle.ACCEPTED)
    expected = ("Use accept_request" if target is RequestLifecycle.ACCEPTED else source.value)
    if legal:
        moved = advance_lifecycle(conn, request_id=request.request_id, to_state=target,
                                  run_id=run_id)
        assert moved.lifecycle_state is target
        assert read_request(conn, request_id=request.request_id) == moved
    else:
        with pytest.raises(ValueError, match=expected):
            advance_lifecycle(conn, request_id=request.request_id, to_state=target, run_id=run_id)
        assert read_request(conn, request_id=request.request_id).lifecycle_state is source


def test_expected_from_NARROWS_the_write_to_the_state_the_caller_judged(conn) -> None:
    """A verdict reached from evidence about ONE state must not be applied to another.

    ``running → failed`` and ``accepted → failed`` are both legal, so an unnarrowed UPDATE matching
    every legal source would let §3.3's reconciler terminalize a request that had moved to
    ``running`` since it was judged — on a finding ("nothing is stamped, so nothing reached the
    plane") that is only true of ``accepted``. Naming the source makes the row's own state part of
    the write's precondition, so the moved request is refused and nothing is written.
    """
    request = _record(conn, "narrow")
    accept_request(conn, request_id=request.request_id, lease_seconds=60)
    advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING,
                      run_id="mrun-narrow")

    with pytest.raises(ValueError, match="moved to 'running'"):
        advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.FAILED,
                          expected_from=RequestLifecycle.ACCEPTED)
    assert read_request(conn, request_id=request.request_id).lifecycle_state \
        is RequestLifecycle.RUNNING

    # …and naming the state the row IS in writes exactly as before.
    moved = advance_lifecycle(conn, request_id=request.request_id,
                              to_state=RequestLifecycle.FAILED,
                              expected_from=RequestLifecycle.RUNNING)
    assert moved.lifecycle_state is RequestLifecycle.FAILED


def test_expected_from_names_an_edge_that_EXISTS_or_refuses(conn) -> None:
    """A narrowing to a source the target is not reachable from could never match a row, so it is a
    call assembled wrongly rather than a race — and it is refused as one, loudly, instead of being
    reported as "the request moved"."""
    request = _record(conn, "noedge")
    accept_request(conn, request_id=request.request_id, lease_seconds=60)
    # `accepted → failed` IS legal, so the generic check passes and only the narrowing can catch it.
    with pytest.raises(ValueError, match="not an edge of the shipped state machine"):
        advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.FAILED,
                          expected_from=RequestLifecycle.REQUESTED)
    assert read_request(conn, request_id=request.request_id).lifecycle_state \
        is RequestLifecycle.ACCEPTED


def test_advance_lifecycle_will_not_open_a_SECOND_lease_less_door_into_accepted(conn) -> None:
    """``requested → accepted`` is a legal lifecycle edge, and ``advance_lifecycle`` still refuses to
    walk it: it takes no ``lease_seconds``, so all it could produce is an ``accepted`` row with no
    ``accepted_at`` and no lease — non-terminal, and invisible to ``expired_requests``, which
    requires ``lease_expires_at IS NOT NULL``. That row is a run nobody works and nobody ever looks
    at again, i.e. exactly the loss this table exists to prevent; it would also undercut the
    ``accepted → failed`` edge, whose whole justification is that an accepted row carries a lease.
    """
    request = _record(conn, "door")
    with pytest.raises(ValueError, match="Use accept_request"):
        advance_lifecycle(conn, request_id=request.request_id,
                          to_state=RequestLifecycle.ACCEPTED)
    assert read_request(conn, request_id=request.request_id).lifecycle_state \
        is RequestLifecycle.REQUESTED
    # And the door that IS legal leaves the row visible to the reconciler.
    accepted = accept_request(conn, request_id=request.request_id, lease_seconds=1)
    assert accepted.accepted_at is not None and accepted.lease_expires_at is not None
    assert [found.request_id for found in expired_requests(conn, now=_later(3_600))] \
        == [request.request_id]


@pytest.mark.parametrize(
    ("missing", "refusal"),
    [("lease_expires_at", "claimed-and-leased"), ("accepted_at", "lease with no acceptance")])
def test_an_accepted_record_without_a_lease_cannot_be_constructed(
        conn, missing: str, refusal: str) -> None:
    """The same invariant one layer up, so a row assembled in Python (a hand-written read, a future
    writer) cannot express what the database refuses to store."""
    stored = accept_request(conn, request_id=_record(conn, "inv").request_id, lease_seconds=300)
    with pytest.raises(ValueError, match=refusal):
        dataclasses.replace(stored, **{missing: None})


def test_advancing_an_unknown_request_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="no materialization request"):
        advance_lifecycle(conn, request_id="never-asked", to_state=RequestLifecycle.RUNNING,
                          run_id=RUN)


def test_running_requires_the_run_it_claims_to_be(conn) -> None:
    """``running`` means a run was prepared. Without a run_id the request would claim to be running
    something nobody could look up — and the reconciler reads run evidence BY run id."""
    request = _in_state(conn, RequestLifecycle.ACCEPTED, suffix="norun")
    with pytest.raises(ValueError, match="run_id"):
        advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING)


def test_the_conditional_UPDATE_is_the_arbiter_not_the_python_precheck(conn, monkeypatch) -> None:
    """``advance_lifecycle`` reads the row, checks the edge in Python, and then UPDATEs ``WHERE
    lifecycle_state = ANY(<legal sources>)``. That WHERE is the only thing standing between two
    workers and a double transition — and because the Python check refuses every illegal move
    first, deleting the WHERE leaves the rest of this file green. So the read is stubbed to report
    a STALE state (what a racing worker's read really returns) while the row holds another: the
    pre-check passes, and only the WHERE can refuse.

    Single-session and deterministic — no second connection, no sleep, no thread. The stub is stale
    for exactly one call, which is what a real race looks like: the pre-check reads the old state,
    and the error path's re-read sees the truth.
    """
    request = _in_state(conn, RequestLifecycle.ACCEPTED, suffix="race")
    stale = read_request(conn, request_id=request.request_id)
    # The other worker gets there first.
    advance_lifecycle(conn, request_id=request.request_id, to_state=RequestLifecycle.RUNNING,
                      run_id=f"{RUN}-race")

    real_current = request_store._current
    reads = itertools.count()

    def _stale_once(conn_, request_id):
        return stale if next(reads) == 0 else real_current(conn_, request_id)

    monkeypatch.setattr(request_store, "_current", _stale_once)
    with pytest.raises(ValueError, match="moved to 'running'"):
        advance_lifecycle(conn, request_id=request.request_id,
                          to_state=RequestLifecycle.RUNNING, run_id=f"{RUN}-race")
    monkeypatch.undo()
    # Refused means "never applied twice", not "applied and then complained".
    assert read_request(conn, request_id=request.request_id).lifecycle_state \
        is RequestLifecycle.RUNNING


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


def test_expired_requests_is_ordered_by_lease_expiry_not_by_insertion(conn) -> None:
    """Two workers reconciling the same backlog must see the same order, so the ordering is the
    lease expiry — never the table's physical order. Inserted newest-lease-first so insertion order
    and expiry order disagree: an ORDER BY dropped from the query fails here."""
    for suffix, lease in (("z", 300), ("m", 100), ("a", 200)):
        _record(conn, suffix)
        accept_request(conn, request_id=f"req-{suffix}", lease_seconds=lease)
    expired = expired_requests(conn, now=_later(3_600))
    assert [request.request_id for request in expired] == ["req-m", "req-a", "req-z"]


def test_expired_requests_breaks_a_lease_TIE_by_request_id(conn) -> None:
    """The tie-break, which the store cannot produce on its own: leases are stamped from
    ``statement_timestamp()``, so two acceptances in one transaction are microseconds apart and
    never tie. Two rows are therefore given one identical expiry directly — test setup, not store
    behaviour — because a tie is exactly what two workers accepted in the same instant would look
    like, and without the tie-break their backlogs would be ordered differently."""
    for suffix in ("z", "a"):
        _record(conn, suffix)
        accept_request(conn, request_id=f"req-{suffix}", lease_seconds=60)
    conn.execute("UPDATE materialization_request "
                 "SET lease_expires_at = timestamptz '2026-08-03T10:00:00+00:00'")
    expired = expired_requests(conn, now=_later(3_600))
    assert [request.request_id for request in expired] == ["req-a", "req-z"]


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
    # The placeholders are numbered POSITIONALLY from the shipped statement rather than counted by
    # hand, so a parameter added to the query cannot leave this EXPLAINing a different one.
    prepared = _EXPIRED_REQUESTS_SQL
    for position in range(1, prepared.count("%s") + 1):
        prepared = prepared.replace("%s", f"${position}", 1)
    conn.execute(f"PREPARE reconciler_query (timestamptz, bigint) AS {prepared}")
    plan = "\n".join(
        row[0] for row in conn.execute(
            "EXPLAIN EXECUTE reconciler_query('2099-01-01T00:00:00+00'::timestamptz, 50)"
        ).fetchall())
    assert "materialization_request_expired_lease_idx" in plan, plan


def test_a_naive_now_is_refused(conn) -> None:
    """Comparing a naive datetime against ``timestamptz`` silently reads it in the session's zone,
    which would expire every lease in the world or none of them depending on where the worker
    runs."""
    with pytest.raises(ValueError, match="offset"):
        expired_requests(conn, now=_datetime.datetime(2026, 8, 3, 10, 0, 0))
