from __future__ import annotations

import pytest
from psycopg.rows import dict_row

from featuregen.contracts import (
    IdentityEnvelope,
    NewEvent,
    ProjectionApplyError,
    ProvenanceEnvelope,
)
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import projection_lag, run_projection


def _append(conn, run_id, version, payload):
    return append_event(
        conn,
        NewEvent(
            aggregate="run",
            aggregate_id=run_id,
            type="E",
            schema_version=1,
            payload=payload,
            actor=IdentityEnvelope(
                subject="u",
                actor_kind="human",
                authenticated=True,
                auth_method="oidc",
                role_claims=(),
            ),
            provenance=ProvenanceEnvelope(
                artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
            ),
            run_id=run_id,
        ),
        expected_version=version,
        table_version=1,
    )


class FailClosedProjection:
    name = "fc"
    is_analytics = False

    def __init__(self, poison_seq: int) -> None:
        self.poison_seq = poison_seq

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE fc_applied")
            cur.execute("TRUNCATE fc_degraded")

    def apply(self, conn, event) -> None:
        if event.global_seq == self.poison_seq:
            # Write PARTIAL projection state (into BOTH temp tables), THEN signal fail-closed.
            # Under the runner's SAVEPOINT wrapping these writes MUST be discarded by ROLLBACK
            # TO SAVEPOINT — only the runner's projection_degraded marker may survive.
            with conn.cursor() as cur:
                cur.execute("INSERT INTO fc_applied (global_seq) VALUES (%s)", (event.global_seq,))
                cur.execute(
                    "INSERT INTO fc_degraded (run_id, reason, at_seq) VALUES (%s, %s, %s)",
                    (event.run_id, "unappliable", event.global_seq),
                )
            raise ProjectionApplyError("run", event.run_id, "unappliable")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO fc_applied (global_seq) VALUES (%s)", (event.global_seq,))


class AnalyticsProjection:
    name = "an"
    is_analytics = True

    def __init__(self, poison_seq: int) -> None:
        self.poison_seq = poison_seq

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE an_applied")

    def apply(self, conn, event) -> None:
        if event.global_seq == self.poison_seq:
            raise ProjectionApplyError("run", event.run_id, "skip me")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO an_applied (global_seq) VALUES (%s)", (event.global_seq,))


def test_fail_closed_halts_and_persists_degraded_marker(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE fc_applied (global_seq bigint)")
        cur.execute("CREATE TEMP TABLE fc_degraded (run_id text, reason text, at_seq bigint)")
    e1 = _append(conn, "r", 0, {})
    poison = _append(conn, "r", 1, {})
    _append(conn, "r", 2, {})  # after the poison event

    proj = FailClosedProjection(poison_seq=poison.global_seq)
    applied = run_projection(conn, proj)
    assert applied == 1  # only the pre-poison event

    with conn.cursor(row_factory=dict_row) as cur:
        # The poison apply wrote PARTIAL state into BOTH fc_applied and fc_degraded before raising;
        # the runner's SAVEPOINT + ROLLBACK TO SAVEPOINT discarded ALL of it — fc_applied holds
        # ONLY the pre-poison event and fc_degraded is empty (NO partial projection state survives).
        cur.execute("SELECT global_seq FROM fc_applied ORDER BY global_seq")
        assert [r["global_seq"] for r in cur.fetchall()] == [e1.global_seq]
        cur.execute("SELECT count(*) AS n FROM fc_degraded")
        assert cur.fetchone()["n"] == 0  # the apply body's own partial marker was rolled back
        # The ONLY surviving degraded record is the one run_projection itself wrote, in a SEPARATE
        # statement AFTER the rollback, into the generic ledger — using the CARRIED
        # ProjectionApplyError payload (aggregate/aggregate_id/reason) + the poison event.
        cur.execute(
            "SELECT aggregate, aggregate_id, reason, poison_seq FROM projection_degraded "
            "WHERE projection_name = 'fc'"
        )
        deg = cur.fetchone()
        assert (deg["aggregate"], deg["aggregate_id"], deg["reason"]) == ("run", "r", "unappliable")
        assert deg["poison_seq"] == poison.global_seq
        cur.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name='fc'")
        assert cur.fetchone()["checkpoint_seq"] == e1.global_seq  # did not advance past poison

    # stuck: a second run does not advance (lag stays > 0).
    assert run_projection(conn, proj) == 0
    assert projection_lag(conn, "fc") > 0


def test_analytics_skips_poison_and_continues(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE an_applied (global_seq bigint)")
    a1 = _append(conn, "r", 0, {})
    poison = _append(conn, "r", 1, {})
    a3 = _append(conn, "r", 2, {})

    proj = AnalyticsProjection(poison_seq=poison.global_seq)
    applied = run_projection(conn, proj)
    assert applied == 2  # poison skipped, others applied

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq FROM an_applied ORDER BY global_seq")
        assert [r["global_seq"] for r in cur.fetchall()] == [a1.global_seq, a3.global_seq]
    assert projection_lag(conn, "an") == 0  # advanced to head despite the skip


@pytest.fixture
def poison_analytics_projection(conn):
    """An analytics Projection whose apply() raises ProjectionApplyError on exactly one seeded
    (poison) event. Yields (projection, poison_global_seq)."""
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE an_applied (global_seq bigint)")
    _append(conn, "r", 0, {})
    poison = _append(conn, "r", 1, {})
    _append(conn, "r", 2, {})  # after the poison event
    return AnalyticsProjection(poison_seq=poison.global_seq), poison.global_seq


def test_analytics_skip_is_recorded(conn, poison_analytics_projection) -> None:
    """An analytics projection that fail-opens past a poison event must record the skip durably
    (review MAJOR #20) — silent skips are a BCBS 239 accuracy gap."""
    proj, poison_seq = poison_analytics_projection
    run_projection(conn, proj)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_global_seq, reason FROM projection_skips WHERE projection_name=%s",
            (proj.name,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == poison_seq
    # Fail-open preserved: the checkpoint still advanced PAST the poison event (to head).
    assert projection_lag(conn, proj.name) == 0


def test_analytics_skip_increments_counter(conn, poison_analytics_projection) -> None:
    """A durable analytics skip must ALSO surface as the `projection.skip` counter so a health
    endpoint/operator can see BCBS-239 completeness gaps, not just a table row (SP-0.5 round-2)."""
    from featuregen.runtime.observability import counters

    proj, _ = poison_analytics_projection
    counters.reset()
    run_projection(conn, proj)
    assert counters.snapshot()["counters"].get("projection.skip", 0) >= 1


class HealableProjection:
    """A normal (fail-closed) projection that poisons on one event UNTIL `healed` is flipped —
    models an operator fixing the underlying cause, so a re-run can advance past the poison."""

    name = "healable"
    is_analytics = False

    def __init__(self, poison_seq: int) -> None:
        self.poison_seq = poison_seq
        self.healed = False

    def reset(self, conn) -> None:
        pass

    def apply(self, conn, event) -> None:
        if event.global_seq == self.poison_seq and not self.healed:
            raise ProjectionApplyError("run", event.run_id, "unappliable-until-healed")
        # otherwise no-op


def test_resolve_degraded_proves_health_before_clearing(conn):
    """resolve_degraded must RE-RUN the projection and only clear the marker once it advances past
    the poison — refusing (marker unchanged) while the projection is still stuck (SP-0.5 round-2)."""
    from tests.featuregen._helpers import make_cmd

    from featuregen.aggregates.run_lifecycle import resolve_degraded_command
    from featuregen.projections.runner import register_projection_for_repair

    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    _append(conn, "run_h", 0, {})
    poison = _append(conn, "run_h", 1, {})
    _append(conn, "run_h", 2, {})

    proj = HealableProjection(poison_seq=poison.global_seq)
    register_projection_for_repair("healable", proj)
    run_projection(conn, proj)  # halts at the poison -> writes projection_degraded (healable,run,run_h)
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE projection_name='healable'"
    ).fetchone()[0] == 1

    cmd = make_cmd("resolve_degraded", "run", "run_h", {})

    # A) still poisoned -> refuse, marker stays.
    res = resolve_degraded_command(conn, cmd)
    assert res.accepted is False and "advance past" in (res.denied_reason or "")
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE aggregate_id='run_h'"
    ).fetchone()[0] == 1

    # B) operator remediates -> re-run advances past the poison -> clear + audit.
    proj.healed = True
    res2 = resolve_degraded_command(conn, cmd)
    assert res2.accepted is True
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE aggregate_id='run_h'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM security_audit WHERE event_type='DEGRADED_RESOLVED'"
    ).fetchone()[0] >= 1


class MultiPoisonProjection:
    """Poisons on a SET of seqs; healing removes seqs from the set. Models a projection with a
    second-stage poison at a later seq than the one that first halted it."""

    name = "multi"
    is_analytics = False

    def __init__(self, poisons):
        self.poisons = set(poisons)

    def reset(self, conn):
        pass

    def apply(self, conn, event):
        if event.global_seq in self.poisons:
            raise ProjectionApplyError("run", event.run_id, "poison")


def test_resolve_degraded_refuses_when_a_second_stage_poison_remains(conn):
    """resolve must re-read the LIVE marker: healing the FIRST poison lets the projection advance
    only to a LATER poison, which re-marks the aggregate — resolve must still REFUSE, not clear on
    the stale pre-run snapshot (SP-0.5 round-2 review, finding 6)."""
    from tests.featuregen._helpers import make_cmd

    from featuregen.aggregates.run_lifecycle import resolve_degraded_command
    from featuregen.projections.runner import register_projection_for_repair

    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    _append(conn, "run_m", 0, {})
    p1 = _append(conn, "run_m", 1, {})
    p2 = _append(conn, "run_m", 2, {})

    proj = MultiPoisonProjection({p1.global_seq, p2.global_seq})
    register_projection_for_repair("multi", proj)
    run_projection(conn, proj)  # halts at p1 -> marker poison_seq = p1

    cmd = make_cmd("resolve_degraded", "run", "run_m", {})
    # Heal ONLY the first poison; the second (p2) still halts the projection.
    proj.poisons.discard(p1.global_seq)
    res = resolve_degraded_command(conn, cmd)
    assert res.accepted is False  # advanced past p1 but re-halted at p2 -> still degraded
    left = conn.execute(
        "SELECT poison_seq FROM projection_degraded WHERE aggregate_id='run_m'"
    ).fetchone()
    assert left is not None and left[0] == p2.global_seq  # marker moved to the second-stage poison

    # Heal the second poison too -> now fully healthy -> resolve clears.
    proj.poisons.discard(p2.global_seq)
    assert resolve_degraded_command(conn, cmd).accepted is True
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE aggregate_id='run_m'"
    ).fetchone()[0] == 0


def test_rebuild_clears_stale_degraded_only_after_clean_replay(conn):
    """A successful rebuild-to-head must clear stale projection_degraded markers so an operator who
    fixed the cause + rebuilt gets commands un-blocked WITHOUT a separate resolve_degraded — but a
    rebuild that re-halts (still poisoned) must KEEP the markers, fail-closed (SP-0.5 r2 review)."""
    from featuregen.projections.runner import rebuild_projection

    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    _append(conn, "run_rb", 0, {})
    poison = _append(conn, "run_rb", 1, {})
    _append(conn, "run_rb", 2, {})

    proj = HealableProjection(poison_seq=poison.global_seq)
    run_projection(conn, proj)  # halts -> degraded marker
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE projection_name='healable'"
    ).fetchone()[0] == 1

    # Still poisoned: rebuild re-halts (lag > 0) -> marker KEPT (fail-closed).
    rebuild_projection(conn, proj)
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE projection_name='healable'"
    ).fetchone()[0] == 1

    # Cause fixed: rebuild reaches head cleanly (lag 0) -> stale marker cleared.
    proj.healed = True
    rebuild_projection(conn, proj)
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE projection_name='healable'"
    ).fetchone()[0] == 0


def test_resolve_degraded_fails_closed_when_projection_not_registered(conn):
    """A marker naming a projection not registered for repair cannot be health-proven, so resolve
    fail-closes (accepted=False, marker unchanged) rather than blindly unblocking (SP-0.5 r2)."""
    from tests.featuregen._helpers import make_cmd

    from featuregen.aggregates.run_lifecycle import resolve_degraded_command

    conn.execute(
        "INSERT INTO projection_degraded (projection_name, aggregate, aggregate_id, reason, "
        "poison_event_id, poison_seq) VALUES ('unregistered_proj','run','run_u','boom',NULL,5)"
    )
    res = resolve_degraded_command(conn, make_cmd("resolve_degraded", "run", "run_u", {}))
    assert res.accepted is False and "not registered for repair" in (res.denied_reason or "")
    assert conn.execute(
        "SELECT count(*) FROM projection_degraded WHERE aggregate_id='run_u'"
    ).fetchone()[0] == 1  # marker unchanged
