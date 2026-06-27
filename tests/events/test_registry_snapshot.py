from __future__ import annotations

import pytest
from psycopg.rows import dict_row

from sp0.contracts import SchemaValidationError
from sp0.events.registry import (
    EventSchemaRegistry,
    load_registry_snapshot,
    persist_event_schemas,
    persist_registry_snapshot,
)


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_schema("A", 1, {"type": "object"}, owner="o")
    reg.register_schema("A", 2, {"type": "object"}, owner="o")
    reg.register_schema("B", 1, {"type": "object"}, owner="o")
    return reg


def test_snapshot_id_is_content_addressed_and_deterministic():
    sid = _reg().snapshot_version()
    assert sid.startswith("events@")
    # Deterministic: the same registry state yields the same id, every call and across
    # independently-built registries.
    assert _reg().snapshot_version() == sid


def test_distinct_states_get_distinct_ids_no_collision():
    # {A@1, A@2} (max-active {A:2}) vs {A@1, B@1} (max-active {A:1, B:1}) MUST differ —
    # the exact collision the old len()-based id produced.
    r1 = EventSchemaRegistry()
    r1.register_schema("A", 1, {"type": "object"}, owner="o")
    r1.register_schema("A", 2, {"type": "object"}, owner="o")
    r2 = EventSchemaRegistry()
    r2.register_schema("A", 1, {"type": "object"}, owner="o")
    r2.register_schema("B", 1, {"type": "object"}, owner="o")
    assert r1.snapshot_version() != r2.snapshot_version()


def test_withdrawing_a_version_changes_the_snapshot_id():
    reg = _reg()  # max-active {A:2, B:1}
    before = reg.snapshot_version()
    reg.set_status("A", 2, "withdrawn")  # max-active becomes {A:1, B:1}
    assert reg.snapshot_version() != before


def test_persist_event_schemas_writes_rows(conn):
    persist_event_schemas(conn, _reg())
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM event_type_registry")
        assert cur.fetchone()["n"] == 3


def test_persist_event_schemas_rejects_breaking_bump_without_upcaster(conn):
    reg = EventSchemaRegistry()
    reg.register_schema(
        "T", 1, {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
        owner="o",
    )
    reg.register_schema(
        "T", 2,
        {"type": "object", "required": ["a", "b"],
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},  # breaking
        owner="o",
    )
    with pytest.raises(SchemaValidationError):
        persist_event_schemas(conn, reg)  # assert_evolution_complete fires before any write
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM event_type_registry WHERE type_name='T'")
        assert cur.fetchone()["n"] == 0  # rejected before durable write


def test_persist_snapshot_records_max_active_version(conn):
    reg = _reg()
    reg.set_status("A", 2, "withdrawn")  # excluded from max-active
    snapshot_id = persist_registry_snapshot(conn, reg)
    assert snapshot_id == reg.snapshot_version()
    assert snapshot_id.startswith("events@")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT registry, contents FROM registry_snapshots WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
    assert row["registry"] == "events"
    assert row["contents"] == {"A": 1, "B": 1}


def test_snapshot_round_trips_to_type_version_map(conn):
    # The pinned-snapshot READ path: persist then resolve the id back to {type: version}.
    reg = _reg()  # max-active {A:2, B:1}
    snapshot_id = persist_registry_snapshot(conn, reg)
    assert load_registry_snapshot(conn, snapshot_id) == {"A": 2, "B": 1}


def test_load_unknown_snapshot_raises(conn):
    with pytest.raises(SchemaValidationError):
        load_registry_snapshot(conn, "events@deadbeefdeadbeef")
