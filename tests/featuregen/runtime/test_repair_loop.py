from __future__ import annotations

from featuregen.runtime.repair_loop import evaluate_repair_loop, route_repair_exhaustion

ATTEMPT = ("REPAIR_ATTEMPTED",)


def test_no_attempts_not_exhausted(conn):
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 0 and st.exhausted is False


def test_exhausts_at_n(conn, insert_stub_event):
    for i in range(3):
        insert_stub_event(
            conn, event_id=f"evt_{i}", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=i + 1
        )
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 3 and st.exhausted is True


def test_manual_retry_rearms(conn, insert_stub_event):
    insert_stub_event(
        conn, event_id="e1", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=1
    )
    insert_stub_event(
        conn, event_id="e2", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=2
    )
    insert_stub_event(conn, event_id="e3", run_id="run_1", type="MANUAL_RETRY", stream_version=3)
    insert_stub_event(
        conn, event_id="e4", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=4
    )
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 1 and st.exhausted is False  # only the post-rearm attempt counts


def test_exhaustion_routes_to_human_idempotently(conn, insert_stub_event):
    for i in range(3):
        insert_stub_event(
            conn, event_id=f"evt_{i}", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=i + 1
        )
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.exhausted is True
    # exhaustion -> human: enqueue exactly ONE idempotent routing message (§5.6)
    assert route_repair_exhaustion(conn, "run_1", st) is True
    assert route_repair_exhaustion(conn, "run_1", st) is False  # idempotent per episode
    with conn.cursor() as cur:
        cur.execute(
            "SELECT handler, count(*) FROM queue WHERE message_id=%s GROUP BY handler",
            (f"repair-exhausted:run_1:{st.rearm_seq}",),
        )
        handler, count = cur.fetchone()
    assert handler == "runtime.repair_exhausted" and count == 1


def test_rearm_allows_fresh_exhaustion_routing(conn, insert_stub_event):
    # First episode exhausts and routes...
    for i in range(3):
        insert_stub_event(
            conn, event_id=f"a{i}", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=i + 1
        )
    st1 = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert route_repair_exhaustion(conn, "run_1", st1) is True
    # ...a manual_retry re-arms; a fresh exhaustion routes again under a NEW episode key.
    insert_stub_event(conn, event_id="mr", run_id="run_1", type="MANUAL_RETRY", stream_version=4)
    for i in range(3):
        insert_stub_event(
            conn, event_id=f"b{i}", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=5 + i
        )
    st2 = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st2.exhausted is True and st2.rearm_seq > st1.rearm_seq
    assert route_repair_exhaustion(conn, "run_1", st2) is True  # new episode -> new message
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE handler='runtime.repair_exhausted'")
        assert cur.fetchone()[0] == 2


def test_not_exhausted_does_not_route(conn, insert_stub_event):
    insert_stub_event(
        conn, event_id="e1", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=1
    )
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.exhausted is False
    assert route_repair_exhaustion(conn, "run_1", st) is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue")
        assert cur.fetchone()[0] == 0
