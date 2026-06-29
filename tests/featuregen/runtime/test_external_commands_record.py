from __future__ import annotations

import pytest

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import HighCostWithoutDedup, record_external_command


def _count(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM external_commands WHERE idempotency_key=%s", (key,))
        return cur.fetchone()[0]


def test_record_inserts_pending(conn):
    cmd = NewExternalCommand(integration="llm", idempotency_key="idem-1",
                             request_payload={"prompt": "x"})
    cid = record_external_command(conn, cmd, command_id="cmd_1", run_id="run_1")
    assert cid == "cmd_1"
    with conn.cursor() as cur:
        cur.execute("SELECT status, run_id FROM external_commands WHERE command_id='cmd_1'")
        assert cur.fetchone() == ("pending", "run_1")


def test_record_is_idempotent_caching(conn):
    cmd = NewExternalCommand(integration="llm", idempotency_key="dup",
                             request_payload={"prompt": "x"})
    a = record_external_command(conn, cmd, command_id="cmd_a")
    b = record_external_command(conn, cmd, command_id="cmd_b")  # same idempotency_key
    assert a == b == "cmd_a"
    assert _count(conn, "dup") == 1


def test_high_cost_requires_dedup_or_handle(conn):
    cmd = NewExternalCommand(integration="sandbox", idempotency_key="s1",
                             request_payload={}, dedup_supported=False, job_handle=None)
    with pytest.raises(HighCostWithoutDedup):
        record_external_command(conn, cmd, command_id="cmd_s1")


def test_high_cost_with_job_handle_ok(conn):
    cmd = NewExternalCommand(integration="sandbox", idempotency_key="s2",
                             request_payload={}, job_handle="job-42")
    assert record_external_command(conn, cmd, command_id="cmd_s2") == "cmd_s2"
