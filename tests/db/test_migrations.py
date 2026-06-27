from __future__ import annotations


def test_events_table_and_constraints_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        assert cur.fetchone()[0] == "events"
        cur.execute("SELECT to_regclass('public.event_type_registry')")
        assert cur.fetchone()[0] == "event_type_registry"
        cur.execute("SELECT to_regclass('public.registry_snapshots')")
        assert cur.fetchone()[0] == "registry_snapshots"
        cur.execute("SELECT to_regclass('public.projection_checkpoints')")
        assert cur.fetchone()[0] == "projection_checkpoints"
        cur.execute("SELECT to_regclass('public.projection_active_alias')")
        assert cur.fetchone()[0] == "projection_active_alias"
        cur.execute("SELECT to_regclass('public.projection_degraded')")
        assert cur.fetchone()[0] == "projection_degraded"
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conname = 'events_optimistic_concurrency'"
        )
        assert cur.fetchone()[0] == "events_optimistic_concurrency"


def test_global_seq_sequence_is_monotonic(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('global_seq_seq')")
        a = cur.fetchone()[0]
        cur.execute("SELECT nextval('global_seq_seq')")
        b = cur.fetchone()[0]
    assert b > a


def test_aggregate_id_consistency_check_rejects_mismatch(conn):
    import psycopg

    # `events_aggregate_id_consistent` is a non-deferrable CHECK: Postgres raises
    # CheckViolation at the INSERT (execute), NOT at commit. Wrap the INSERT itself in
    # a savepoint so the violation is caught here and the connection stays usable.
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                                        type, schema_version, table_version, actor, payload,
                                        provenance, occurred_at)
                    VALUES ('evt_bad', 'run', 'run_1', 1, 'run_2', 'X', 1, 1, '{}'::jsonb,
                            '{}'::jsonb, '{}'::jsonb, now())
                    """
                )
        raised = False
    except psycopg.errors.CheckViolation:
        # run aggregate with aggregate_id != run_id violates the CHECK at INSERT time.
        raised = True
    assert raised
