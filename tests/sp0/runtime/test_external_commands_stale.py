from __future__ import annotations

from sp0.contracts import NewExternalCommand
from sp0.runtime.external_commands import accept_result, record_external_command


def _record(conn, key, **expected):
    cmd = NewExternalCommand(integration="llm", idempotency_key=key, request_payload={},
                             expected_run_id=expected.get("run"),
                             expected_stream_version=expected.get("sv"),
                             expected_task_id=expected.get("task"))
    return record_external_command(conn, cmd, command_id=f"cmd_{key}", require_dedup=frozenset())


def _status(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM external_commands WHERE command_id=%s", (cid,))
        return cur.fetchone()[0]


def test_accepted_when_on_target(conn):
    cid = _record(conn, "a", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=5,
                        current_task_id=None)
    assert out.accepted is True and out.stale is False


def test_stale_when_run_changed(conn):
    cid = _record(conn, "b", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_2", current_stream_version=5,
                        current_task_id=None)
    assert out.stale is True and out.accepted is False
    assert _status(conn, cid) == "stale_ignored"


def test_stale_when_advanced_past_version(conn):
    cid = _record(conn, "c", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=6,
                        current_task_id=None)
    assert out.stale is True


def test_stale_when_task_changed(conn):
    cid = _record(conn, "d", task="task_1")
    out = accept_result(conn, cid, current_run_id=None, current_stream_version=None,
                        current_task_id="task_2")
    assert out.stale is True


def test_stale_idempotent_cache(conn):
    cid = _record(conn, "e", run="run_1")
    accept_result(conn, cid, current_run_id="run_2", current_stream_version=None,
                  current_task_id=None)
    out = accept_result(conn, cid, current_run_id="run_2", current_stream_version=None,
                        current_task_id=None)
    assert out.stale is True and out.cached is True


def test_applied_result_is_cached(conn, insert_stub_event):
    cid = _record(conn, "f", run="run_1", sv=5)
    insert_stub_event(conn, event_id="evt_res", run_id="run_1", type="LLM_RESULT", stream_version=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE external_commands SET result_event_id='evt_res' WHERE command_id=%s", (cid,))
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=5,
                        current_task_id=None)
    assert out.accepted is True and out.cached is True
