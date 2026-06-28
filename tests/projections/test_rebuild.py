from __future__ import annotations

from psycopg.rows import dict_row

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events.registry import event_registry
from sp0.events.store import append_event
from sp0.projections.runner import rebuild_projection, run_projection


class SumProjection:
    name = "sum"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE sum_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sum_state (global_seq, n) VALUES (%s, %s)",
                (event.global_seq, event.payload["n"]),
            )


def _seed(conn, values):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i, v in enumerate(values):
        append_event(
            conn,
            NewEvent(
                aggregate="run", aggregate_id="r", type="E", schema_version=1,
                payload={"n": v},
                actor=IdentityEnvelope(
                    subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                    role_claims=(),
                ),
                provenance=ProvenanceEnvelope(
                    artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
                ),
                run_id="r",
            ),
            expected_version=i,
            table_version=1,
        )


def test_rebuild_reproduces_identical_state(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [1, 2, 3])
    proj = SumProjection()
    run_projection(conn, proj)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq, n FROM sum_state ORDER BY global_seq")
        before = cur.fetchall()

    rebuild_projection(conn, proj)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq, n FROM sum_state ORDER BY global_seq")
        after = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM sum_state")
        assert cur.fetchone()["n"] == 3  # reset cleared duplicates, replay re-added once
    assert after == before


def test_rebuild_resets_checkpoint_then_replays(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [5, 6])
    proj = SumProjection()
    run_projection(conn, proj)
    rebuild_projection(conn, proj)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT checkpoint_seq, head_seq FROM projection_checkpoints WHERE projection_name='sum'")
        row = cur.fetchone()
    assert row["checkpoint_seq"] == row["head_seq"]  # fully caught up after rebuild
