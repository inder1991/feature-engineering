from __future__ import annotations

import psycopg
import pytest


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_outbox_has_contract_columns(conn) -> None:
    assert {
        "id", "message_id", "partition_key", "topic", "payload", "caused_by_event",
        "status", "lease_owner", "lease_expires_at", "attempts", "max_attempts",
        "next_attempt_at", "last_error", "created_at", "sent_at",
    } <= _columns(conn, "outbox")


def test_queue_has_contract_columns(conn) -> None:
    assert {
        "id", "message_id", "partition_key", "handler", "payload", "status",
        "lease_owner", "lease_expires_at", "attempts", "max_attempts",
        "available_at", "priority", "last_error", "created_at",
    } <= _columns(conn, "queue")


def test_processed_messages_has_contract_columns(conn) -> None:
    assert {
        "message_id", "aggregate", "aggregate_id", "result_event_id",
        "processed_seq", "processed_at",
    } <= _columns(conn, "processed_messages")


def test_message_id_unique_on_outbox(conn) -> None:
    ins = (
        "INSERT INTO outbox (message_id, partition_key, topic, payload) "
        "VALUES ('m1', 'run:r1', 'T', '{}'::jsonb)"
    )
    with conn.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)


def test_outbox_status_check_rejects_unknown(conn) -> None:
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status) "
            "VALUES ('m2', 'run:r1', 'T', '{}'::jsonb, 'bogus')"
        )


def test_queue_one_inflight_per_partition(conn) -> None:
    base = (
        "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
        "lease_owner, lease_expires_at) VALUES (%s, 'run:r1', 'h', '{}'::jsonb, "
        "'leased', 'w1', now())"
    )
    with conn.cursor() as cur:
        cur.execute(base, ("q1",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(base, ("q2",))


def test_processed_messages_pk_is_message_id(conn) -> None:
    ins = (
        "INSERT INTO processed_messages (message_id, aggregate, aggregate_id, "
        "processed_seq) VALUES ('m3', 'run', 'r1', 5)"
    )
    with conn.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)
