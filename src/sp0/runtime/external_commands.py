from __future__ import annotations

import logging
from typing import Optional

from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, NewExternalCommand

_log = logging.getLogger("sp0.external_commands")


class HighCostWithoutDedup(Exception):
    """A high-cost integration was recorded without a dedup guarantee or job handle (§5.4)."""


def record_external_command(
    conn: DbConn,
    cmd: NewExternalCommand,
    *,
    command_id: str,
    run_id: Optional[str] = None,
    require_dedup: frozenset[str] = frozenset({"sandbox"}),
) -> str:
    """Record a side-effecting command in the caller's §5.1 transaction (status='pending').
    Idempotent on idempotency_key (result caching: a duplicate returns the ORIGINAL
    command_id). High-cost integrations in `require_dedup` MUST carry dedup_supported or a
    job_handle, else HighCostWithoutDedup — no false exactly-once claim (§5.4)."""
    if cmd.integration in require_dedup and not cmd.dedup_supported and cmd.job_handle is None:
        raise HighCostWithoutDedup(
            f"{cmd.integration} requires dedup_supported or job_handle (§5.4)"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO external_commands
                (command_id, idempotency_key, run_id, integration, request_payload,
                 expected_run_id, expected_stream_version, expected_task_id,
                 job_handle, dedup_supported)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING command_id
            """,
            (command_id, cmd.idempotency_key, run_id, cmd.integration,
             Jsonb(dict(cmd.request_payload)), cmd.expected_run_id,
             cmd.expected_stream_version, cmd.expected_task_id, cmd.job_handle,
             cmd.dedup_supported),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "SELECT command_id FROM external_commands WHERE idempotency_key = %s",
            (cmd.idempotency_key,),
        )
        return cur.fetchone()[0]
