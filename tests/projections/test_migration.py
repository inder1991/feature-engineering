from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.migration import migrate_projection, resolve_projection, set_alias
from featuregen.projections.runner import projection_lag, run_projection


class V1Projection:
    name = "report_v1"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE report_v1_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO report_v1_state (n) VALUES (%s)", (event.payload["n"],))


class V2Projection:
    """New shape: stores doubled values."""

    name = "report_v2"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE report_v2_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO report_v2_state (n2) VALUES (%s)", (event.payload["n"] * 2,))


def _seed(conn, values):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i, v in enumerate(values):
        append_event(
            conn,
            NewEvent(
                aggregate="run", aggregate_id="r", type="E", schema_version=1, payload={"n": v},
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


def test_migrate_builds_in_parallel_then_switches_alias(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE report_v1_state (n int)")
        cur.execute("CREATE TEMP TABLE report_v2_state (n2 int)")
    _seed(conn, [1, 2, 3])

    set_alias(conn, "report", "report_v1")
    run_projection(conn, V1Projection())
    assert resolve_projection(conn, "report") == "report_v1"

    migrate_projection(conn, "report", V2Projection())

    # alias switched only after v2 caught up; v1 data still intact (parallel build).
    assert resolve_projection(conn, "report") == "report_v2"
    assert projection_lag(conn, "report_v2") == 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT n2 FROM report_v2_state ORDER BY n2")
        assert [r["n2"] for r in cur.fetchall()] == [2, 4, 6]
        cur.execute("SELECT count(*) AS n FROM report_v1_state")
        assert cur.fetchone()["n"] == 3  # old projection untouched during migration


def test_resolve_unknown_alias_returns_alias_itself(conn):
    assert resolve_projection(conn, "missing") == "missing"
