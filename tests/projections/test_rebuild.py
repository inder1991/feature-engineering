from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import rebuild_projection, run_projection


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
                aggregate="run",
                aggregate_id="r",
                type="E",
                schema_version=1,
                payload={"n": v},
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


def test_rebuild_clears_projection_skips(conn):
    # m4 (final review): after a fix-and-replay rebuild, stale projection_skips rows would report a
    # phantom completeness gap (a BCBS 239 accuracy signal). rebuild must clear the ledger for this
    # projection (only), before replay.
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [1, 2])
    proj = SumProjection()
    run_projection(conn, proj)

    conn.execute(
        "INSERT INTO projection_skips (projection_name, event_global_seq, reason) "
        "VALUES (%s, %s, %s)",
        (proj.name, 1, "stale poison from a prior run"),
    )
    # a skip for a DIFFERENT projection must survive (the clear is scoped by projection_name)
    conn.execute(
        "INSERT INTO projection_skips (projection_name, event_global_seq, reason) "
        "VALUES (%s, %s, %s)",
        ("other_proj", 1, "unrelated"),
    )

    rebuild_projection(conn, proj)

    assert (
        conn.execute(
            "SELECT count(*) FROM projection_skips WHERE projection_name=%s", (proj.name,)
        ).fetchone()[0]
        == 0  # rebuild cleared this projection's stale skip ledger
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM projection_skips WHERE projection_name='other_proj'"
        ).fetchone()[0]
        == 1  # the other projection's ledger is untouched
    )


def test_rebuild_resets_checkpoint_then_replays(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [5, 6])
    proj = SumProjection()
    run_projection(conn, proj)
    rebuild_projection(conn, proj)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq, head_seq FROM projection_checkpoints WHERE projection_name='sum'"
        )
        row = cur.fetchone()
    assert row["checkpoint_seq"] == row["head_seq"]  # fully caught up after rebuild
