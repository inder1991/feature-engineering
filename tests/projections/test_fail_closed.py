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
