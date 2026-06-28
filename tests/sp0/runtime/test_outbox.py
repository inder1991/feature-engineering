from __future__ import annotations

import pytest

from sp0.runtime.outbox import (
    OutboxMessage,
    insert_outbox_message,
    outbox_messages_for_events,
    partition_key_for,
)


def test_partition_key_per_aggregate(db, seed_run_event) -> None:
    ev = seed_run_event("run_p1")
    assert partition_key_for(ev) == "run:run_p1"


def test_derive_one_message_per_event(db, seed_run_event) -> None:
    ev = seed_run_event("run_d1", type="STEP_TRIGGER")
    msgs = outbox_messages_for_events([ev])
    assert len(msgs) == 1
    m = msgs[0]
    assert m.message_id == ev.event_id
    assert m.partition_key == "run:run_d1"
    assert m.topic == "STEP_TRIGGER"
    assert m.caused_by_event == ev.event_id
    assert m.payload["event_id"] == ev.event_id
    assert m.payload["run_id"] == "run_d1"


def test_insert_is_idempotent_on_message_id(db, seed_run_event) -> None:
    ev = seed_run_event("run_i1")
    (m,) = outbox_messages_for_events([ev])
    first = insert_outbox_message(db, m)
    second = insert_outbox_message(db, m)  # duplicate publish -> same row
    assert first == second
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox WHERE message_id = %s", (m.message_id,))
        assert cur.fetchone()[0] == 1


def test_partition_key_for_unknown_aggregate_raises() -> None:
    class _Fake:
        aggregate = "bogus"
        run_id = feature_id = request_id = aggregate_id = "x"

    with pytest.raises(ValueError):
        partition_key_for(_Fake())  # type: ignore[arg-type]
