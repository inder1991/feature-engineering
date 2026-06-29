from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import projection_lag, read_as_of, run_projection


class CountingProjection:
    """A simple state-bearing projection that counts events into a temp table."""

    name = "counter"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE counter_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO counter_state (global_seq) VALUES (%s)", (event.global_seq,)
            )


def _seed(conn, n: int) -> None:
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i in range(n):
        append_event(
            conn,
            NewEvent(
                aggregate="run",
                aggregate_id="r",
                type="E",
                schema_version=1,
                payload={"i": i},
                actor=IdentityEnvelope(
                    subject="u", actor_kind="human", authenticated=True,
                    auth_method="oidc", role_claims=(),
                ),
                provenance=ProvenanceEnvelope(
                    artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
                ),
                run_id="r",
            ),
            expected_version=i,
            table_version=1,
        )


def _make_counter_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE counter_state (global_seq bigint)")


def test_run_projection_applies_and_advances_checkpoint(conn):
    _make_counter_table(conn)
    _seed(conn, 3)
    applied = run_projection(conn, CountingProjection())
    assert applied == 3
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM counter_state")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name='counter'")
        assert cur.fetchone()["checkpoint_seq"] > 0


def test_second_run_applies_only_new_events(conn):
    _make_counter_table(conn)
    _seed(conn, 2)
    assert run_projection(conn, CountingProjection()) == 2
    assert run_projection(conn, CountingProjection()) == 0  # nothing new


def test_lag_and_as_of_track_checkpoint(conn):
    _make_counter_table(conn)
    _seed(conn, 2)
    proj = CountingProjection()
    run_projection(conn, proj)
    assert projection_lag(conn, "counter") == 0
    assert read_as_of(conn, "counter") > 0
    # append more without projecting -> lag grows.
    _seed_more = append_event(
        conn,
        NewEvent(
            aggregate="run", aggregate_id="r", type="E", schema_version=1, payload={},
            actor=IdentityEnvelope(
                subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                role_claims=(),
            ),
            provenance=ProvenanceEnvelope(
                artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
            ),
            run_id="r",
        ),
        expected_version=2,
        table_version=1,
    )
    assert projection_lag(conn, "counter") == 1
