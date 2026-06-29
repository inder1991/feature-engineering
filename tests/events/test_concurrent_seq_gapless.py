from __future__ import annotations

import threading

import psycopg

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import run_projection


class _CollectAll:
    """A non-analytics projection that records every event's global_seq."""

    name = "concurrent_gapless_probe"
    is_analytics = False

    def __init__(self) -> None:
        self.seen: list[int] = []

    def reset(self, conn) -> None:  # pragma: no cover - not exercised here
        self.seen.clear()

    def apply(self, conn, event) -> None:
        self.seen.append(event.global_seq)


def _new(agg_id: str) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=agg_id,
        type="E",
        schema_version=1,
        payload={},
        actor=IdentityEnvelope(
            subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=agg_id,
    )


def test_concurrent_cross_aggregate_appends_are_gapless(_dsn):
    """Two concurrent appends to DISTINCT aggregates must allocate global_seq in COMMIT order:
    while connection A holds an uncommitted append (and thus the seq-allocation advisory lock),
    a concurrent append B on another connection MUST block until A commits, so B can never
    commit a higher global_seq before A's lower seq is durable. A projection then sees both
    events with no skipped seq (§3.2 no-gaps / §3.6 fail-closed).

    Before the advisory-lock fix B did not block; B could commit seq N+1 before A committed
    seq N, a projection run in between would advance its checkpoint past the missing seq, and
    A's event would be PERMANENTLY skipped.
    """
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")

    conn_a = psycopg.connect(_dsn)
    conn_b = psycopg.connect(_dsn)
    conn_c = psycopg.connect(_dsn)
    try:
        # A opens a transaction and appends -> allocates global_seq + holds the advisory lock,
        # but does NOT commit yet.
        conn_a.execute("BEGIN")
        env_a = append_event(conn_a, _new("ra"), expected_version=0, table_version=1)

        b_done = threading.Event()
        b_box: dict[str, object] = {}

        def run_b() -> None:
            with conn_b.transaction():
                b_box["env"] = append_event(conn_b, _new("rb"), expected_version=0,
                                            table_version=1)
            b_done.set()

        t = threading.Thread(target=run_b, name="appender-b")
        t.start()
        try:
            # B must be BLOCKED on the seq-allocation advisory lock while A holds it uncommitted.
            assert not b_done.wait(timeout=1.0), "B did not block; seq alloc is not serialized"

            conn_a.commit()  # release the lock; B can now allocate a higher seq and commit
            assert b_done.wait(timeout=10.0), "B never completed after A committed"
        finally:
            t.join(timeout=10.0)

        env_b = b_box["env"]
        # Commit order == allocation order: B's seq is strictly greater than A's.
        assert env_b.global_seq > env_a.global_seq

        # A projection sees BOTH events; nothing skipped.
        proj = _CollectAll()
        applied = run_projection(conn_c, proj)
        conn_c.commit()
        assert env_a.global_seq in proj.seen
        assert env_b.global_seq in proj.seen
        assert applied >= 2
    finally:
        # This test commits to the shared featuregen_test DB; clean up so other tests start empty.
        for c in (conn_a, conn_b, conn_c):
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            c.close()
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            with cleanup.cursor() as cur:
                cur.execute("DELETE FROM events")
                cur.execute("DELETE FROM projection_checkpoints")
                cur.execute("DELETE FROM projection_degraded")
