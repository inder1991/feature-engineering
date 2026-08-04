"""Phase G T9 — the materialization FLAG, its configuration, and the kill switch.

**Why this exists before the route.** The worker drives every stage on ONE autocommit connection, so
a materialization compile holds the relay, the timers, the projections, the drift scan and the
ingestion sweep for as long as it runs — up to ``COMPILE_BUDGET_SECONDS`` plus the deployment's L0
timeout. That is inert only while nothing can enqueue. The switch that stops it therefore exists
before the surface that starts it.

**The bar these tests hold.** Flag OFF is not "the lane declines politely" — it is *the tick that
existed before the lane did*. :func:`test_flag_OFF_a_tick_issues_not_one_extra_STATEMENT` records
every SQL statement a real worker tick executes and proves the flag-off tick and the flag-on tick
differ by exactly ONE statement, the queue claim. Nothing weaker would catch a stage that quietly
costs a query per second in every deployment that will never materialize anything.
"""
from __future__ import annotations

import pathlib

import psycopg
import pytest
from tests.featuregen.materialize.test_chain import _authored, _inject_l0, _seed
from tests.featuregen.materialize.test_queue_lane import _NOW, _config, _job, _queue_row, _recorded
from tests.featuregen.materialize.test_resolve import no_dsn  # noqa: F401 — autouse

from featuregen.materialize import queue_lane
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_ENV_VARS,
    MATERIALIZATION_FLAG,
    enqueue_materialization,
    materialization_enabled,
)
from featuregen.materialize.request_store import RequestLifecycle, read_request
from featuregen.runtime.observability import counters

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ENV_EXAMPLE = _ROOT / ".env.example"
_KIND_BACKEND = _ROOT / "deploy" / "kind" / "k8s" / "20-backend.yaml"


@pytest.fixture(autouse=True)
def _flag_unset(monkeypatch):
    """Every test states the flag it is testing. A developer whose own shell exports the switch must
    not be able to turn a default-OFF assertion green."""
    monkeypatch.delenv(MATERIALIZATION_FLAG, raising=False)


@pytest.fixture
def catalog(db):
    return _seed(db)


@pytest.fixture
def l0_passes(monkeypatch):
    """L0 PASSES — the precondition of every test whose subject is not L0. Injected at
    ``chain.run_l0`` exactly as Task 6's and Task 7's suites do, which keeps this suite kedro-free."""
    _inject_l0(monkeypatch)


@pytest.fixture
def enqueued(catalog, monkeypatch, l0_passes, tmp_path):
    """One recorded request, one resolvable feature, one enqueued job — the state a route leaves
    behind. Task 7's fixture, restated here rather than imported: a pytest fixture used by NAME has
    to be defined in the module that names it, which is why every suite under this directory carries
    its own ``catalog``."""
    request = _recorded(catalog)
    work_items = [_authored(catalog, monkeypatch)]
    enqueue_materialization(catalog, request, job=_job(request.request_id, work_items))
    return request, work_items, _config(tmp_path)


# ── the flag itself ──────────────────────────────────────────────────────────────────────────────

def test_the_flag_is_OFF_when_nothing_is_set() -> None:
    """Default OFF. A deployment that never heard of Phase G never runs a compile."""
    assert materialization_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " 1 ", "  On  "])
def test_the_TRUTHY_set_is_the_platforms(monkeypatch, value) -> None:
    """Copied verbatim from ``feature_assist.feature_context_enabled`` — ``.strip().lower()`` into
    ``{"1", "true", "yes", "on"}``. It is a strict SUPERSET of the ``== "1"`` idiom the older flags
    use, so a deployment that writes the documented ``"0"``/``"1"`` behaves identically either way.
    """
    monkeypatch.setenv(MATERIALIZATION_FLAG, value)
    assert materialization_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "off", "no", "false", "2", "enabled", "y", "1;", "ON1"])
def test_a_value_OUTSIDE_the_truthy_set_leaves_the_switch_OFF(monkeypatch, value) -> None:
    """The set is CLOSED and the default is off, so every value nobody recognised — a typo, a
    truncated write, a `"2"` somebody meant as a level — fails to the safe side."""
    monkeypatch.setenv(MATERIALIZATION_FLAG, value)
    assert materialization_enabled() is False


def test_the_flag_is_RE_READ_on_every_call_and_NEVER_cached(monkeypatch) -> None:
    """The whole point of a kill switch: no import-time capture, no cached module state, so the
    first tick after this process's environment changes obeys it. (A container reads its environment
    at start, so flipping a ConfigMap still means restarting that pod — the win is what it comes
    back as: a worker running everything except materialization, rather than no worker.)"""
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    assert materialization_enabled() is True
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
    assert materialization_enabled() is False
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    assert materialization_enabled() is True


# ── flag OFF: the tick that existed before the lane did ──────────────────────────────────────────


class _RecordingCursor(psycopg.Cursor):
    """Every SQL statement a tick executes, in order. ``conn.cursor_factory`` is psycopg's own seam,
    so this records the REAL statements the production code path issues — not a count of calls to a
    function a test chose to watch."""

    sql: list[str] = []

    def execute(self, query, params=None, **kwargs):  # type: ignore[override]
        _RecordingCursor.sql.append(query if isinstance(query, str) else str(query))
        return super().execute(query, params, **kwargs)


def _tick(db, monkeypatch, *, flag: str | None) -> list[str]:
    """One real worker tick, returning every statement it executed."""
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    if flag is None:
        monkeypatch.delenv(MATERIALIZATION_FLAG, raising=False)
    else:
        monkeypatch.setenv(MATERIALIZATION_FLAG, flag)
    db.cursor_factory = _RecordingCursor
    _RecordingCursor.sql = []
    run_worker_once(db, HandlerRegistry(), [], owner="w1", now=_NOW)
    return list(_RecordingCursor.sql)


#: Every statement the materialization stages add to a tick, in order, with what each one is. The
#: list is EXHAUSTIVE and the test below asserts it exhaustively: a stage that quietly starts issuing
#: a fourth query per second in every enabled deployment fails here rather than shipping.
#:
#: T13 added the second and third. §3.3's reconciler needs TWO queries because the class it exists
#: to find — a request stranded at ``requested`` behind a dead message — holds no lease and is
#: structurally invisible to ``expired_requests``. Both are indexed reads over the non-terminal
#: requests only (1053's partial index), and both are inside the same kill switch, which is what
#: keeps the flag-OFF tick byte-identical to the tick that existed before any of this.
_MATERIALIZATION_STATEMENTS = (
    ("the lane's fenced claim", ("FROM queue", "status='leased'", "lease_fence")),
    ("the reconciler's expired-lease query",
     ("FROM materialization_request", "lease_expires_at IS NOT NULL")),
    ("the reconciler's unreachable-message query",
     ("FROM materialization_request", "NOT EXISTS", "queue.message_id")),
)


def test_flag_OFF_a_tick_issues_not_one_extra_STATEMENT(db, monkeypatch) -> None:
    """THE byte-identity proof.

    Three ticks over the same empty queue: a warm-up (so no first-call effect is mistaken for the
    flag), one with the switch OFF, one with it ON. The off-tick must be REPRODUCIBLE, and the
    on-tick must differ from it by exactly the statements :data:`_MATERIALIZATION_STATEMENTS` names
    — contiguously, because the two stages run back to back. Removing them from the on-tick must
    yield the off-tick back, byte for byte and in order.

    A weaker "no crash" assertion would pass for a stage that costs every deployment on the platform
    one query per second forever.
    """
    _tick(db, monkeypatch, flag=None)               # warm-up, discarded
    off = _tick(db, monkeypatch, flag="0")
    off_again = _tick(db, monkeypatch, flag=None)
    on = _tick(db, monkeypatch, flag="1")

    assert off == off_again, "the flag-off tick is not reproducible; the comparison below is void"
    extra = len(_MATERIALIZATION_STATEMENTS)
    assert len(on) == len(off) + extra
    differs = next(i for i, (a, b) in enumerate(zip(on, off)) if a != b)
    assert on[:differs] + on[differs + extra:] == off
    for statement, (what, fragments) in zip(on[differs:differs + extra],
                                            _MATERIALIZATION_STATEMENTS, strict=True):
        assert all(fragment in statement for fragment in fragments), (what, statement)


def test_flag_OFF_the_stage_never_reaches_the_LANE_at_all(db, monkeypatch) -> None:
    """The gate is at the STAGE, above ``process_materialization_once`` — so flag-off is not a lane
    that returns early, it is a lane that is never entered. Nothing can read a payload, resolve a
    configuration or touch a request."""
    from featuregen.runtime import worker

    calls: list[str] = []
    monkeypatch.setattr(worker, "process_materialization_once",
                        lambda *a, **k: calls.append("called"))
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")

    tick = _run(db)

    assert calls == []
    assert tick.materialization_processed == 0


def test_flag_OFF_the_stage_is_SILENT(enqueued, catalog, monkeypatch, capsys) -> None:
    """Skip SILENTLY, not loudly — argued in the report. A tick is a second long: a stage that logs
    "disabled" every tick writes ~86 000 lines a day saying nothing happened, which is exactly the
    noise the next real signal has to be found in. The evidence an operator needs is already
    truthful and already emitted: the `queue.depth` gauge counts the undrained backlog, and the
    `materialize.lane.*` counters simply stop advancing."""
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
    counters.reset()
    capsys.readouterr()

    _run(catalog)

    snapshot = counters.snapshot()
    assert [k for k in snapshot["counters"] if "materializ" in k] == []
    assert [k for k in snapshot["gauges"] if "materializ" in k] == []
    assert "materializ" not in capsys.readouterr().err
    assert snapshot["gauges"]["queue.depth"] == 1, "the backlog is what an operator watches instead"


def test_flag_OFF_the_QUEUED_WORK_IS_PRESERVED_not_consumed(enqueued, catalog, monkeypatch) -> None:
    """Stop is not discard. Five ticks with the switch off leave the message exactly as the producer
    wrote it — still ``ready``, still on attempt zero, still at fence zero — and the request still
    ``requested``. An operator who flips the switch off mid-incident loses nothing but time, so the
    decision to flip it costs nothing to reverse."""
    request, _, _ = enqueued
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")

    for _ in range(5):
        _run(catalog)

    status, last_error, attempts, fence = _queue_row(catalog, request.request_id)
    assert (status, last_error, attempts, fence) == ("ready", None, 0, 0)
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.REQUESTED


def test_flipping_the_flag_ON_drains_the_work_the_OFF_ticks_left(
        enqueued, catalog, monkeypatch) -> None:
    """The other half of "preserved": the request the off-ticks declined is compiled by the first
    tick after the switch comes back on. Nothing had to be re-enqueued."""
    request, _, config = enqueued
    monkeypatch.setattr("featuregen.materialize.queue_lane.lane_config_from_env", lambda: config)
    monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
    _run(catalog)
    assert _queue_row(catalog, request.request_id)[0] == "ready"

    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    tick = _run(catalog)

    assert tick.materialization_processed == 1
    assert read_request(catalog, request_id=request.request_id).lifecycle_state is \
        RequestLifecycle.COMMITTED


# ── mid-flight: what the switch does NOT do ──────────────────────────────────────────────────────

def test_a_compile_ALREADY_CLAIMED_runs_to_completion_when_the_flag_flips_off(
        enqueued, catalog, monkeypatch) -> None:
    """NAMED BEHAVIOUR: the switch stops the NEXT claim; it never interrupts a claim in flight.

    A message that a worker has already leased cannot be un-claimed, and the compile behind it has
    already moved the request to ``accepted`` and may be inside ``_commit``'s single transaction.
    Abandoning it there would leave a leased queue row, a non-terminal request and a half-written
    project tree — the exact invisible work the lane's every other guard exists to prevent — and it
    would do so to save the seconds until the transaction ends anyway.

    Here the flag goes off from INSIDE the chain call, which is the only way a tick can observe it:
    the stage reads the environment once, before the claim, and the tick is single-threaded.
    """
    request, _, config = enqueued
    monkeypatch.setattr("featuregen.materialize.queue_lane.lane_config_from_env", lambda: config)
    real = queue_lane.compile_feature_group

    def _flip_then_compile(*args, **kwargs):
        monkeypatch.setenv(MATERIALIZATION_FLAG, "0")
        return real(*args, **kwargs)

    monkeypatch.setattr(queue_lane, "compile_feature_group", _flip_then_compile)
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")

    tick = _run(catalog)

    assert tick.materialization_processed == 1
    stored = read_request(catalog, request_id=request.request_id)
    assert stored.lifecycle_state is RequestLifecycle.COMMITTED
    assert _queue_row(catalog, request.request_id)[0] == "done"
    assert materialization_enabled() is False
    assert _run(catalog).materialization_processed == 0


# ── the deployment files ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [_ENV_EXAMPLE, _KIND_BACKEND])
@pytest.mark.parametrize("variable", MATERIALIZATION_ENV_VARS)
def test_every_variable_the_lane_reads_is_DOCUMENTED_in_both_deployment_files(
        path: pathlib.Path, variable: str) -> None:
    """Drift fails CI, not a deployment. ``MATERIALIZATION_ENV_VARS`` is the code's own list, so
    adding a fifth variable to the lane without telling the two files a deployer actually edits is
    a red test rather than a worker that dead-letters on the first real request."""
    assert variable in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "expected"),
    [(_ENV_EXAMPLE, f"{MATERIALIZATION_FLAG}=0"), (_KIND_BACKEND, f'{MATERIALIZATION_FLAG}: "0"')])
def test_both_deployment_files_ship_the_flag_OFF(path: pathlib.Path, expected: str) -> None:
    """Default OFF is a property of the REPOSITORY, not only of the code. Turning materialization on
    in a checked-in deployment file is a deliberate act, and this test is the thing it has to move.
    """
    assert expected in path.read_text(encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────

def _run(db):
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import run_worker_once

    return run_worker_once(db, HandlerRegistry(), [], owner="w1", now=_NOW)
