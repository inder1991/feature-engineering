from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.documents.registry import DocumentSchemaRegistry

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "integer"}},
    "additionalProperties": False,
}


def test_validate_accepts_conforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    reg.validate("FEATURE_PLAN", 1, {"x": 7})  # no raise


def test_validate_rejects_nonconforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 1, {"x": "not-an-int"})


def test_validate_unregistered_type_raises(db):
    reg = DocumentSchemaRegistry(db)
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 99, {"x": 1})


def test_upcast_chains_stepwise(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    reg.register_upcaster("DQ_REPORT", 2, 3, lambda b: {**b, "v3": True})
    out = reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)
    assert out == {"v1": True, "v2": True, "v3": True}


def test_upcast_missing_step_is_poison(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    with pytest.raises(SchemaValidationError):
        reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)


def test_upcaster_must_be_stepwise():
    reg = DocumentSchemaRegistry.__new__(DocumentSchemaRegistry)
    reg._upcasters = {}
    with pytest.raises(ValueError):
        reg.register_upcaster("DQ_REPORT", 1, 3, lambda b: b)


def test_snapshot_version_is_idempotent_when_unchanged(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("RISK_ASSESSMENT", 1, {"type": "object"}, owner="sp0")
    first = reg.snapshot_version()
    assert first == reg.snapshot_version()


def test_snapshot_advances_when_active_set_changes(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("RISK_ASSESSMENT", 1, {"type": "object"}, owner="sp0")
    first = reg.snapshot_version()
    reg.register_schema("RISK_ASSESSMENT", 2, {"type": "object"}, owner="sp0")
    second = reg.snapshot_version()
    assert first != second
    assert second.startswith("docs@v")


def test_deprecated_versions_excluded_from_snapshot(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("EXPLAINABILITY", 1, {"type": "object"}, owner="sp0",
                        status="deprecated")
    reg.register_schema("MONITORING_SPEC", 1, {"type": "object"}, owner="sp0")
    snap_id = reg.snapshot_version()
    contents = db.execute(
        "SELECT contents FROM registry_snapshots WHERE snapshot_id=%s", (snap_id,)
    ).fetchone()[0]
    assert "MONITORING_SPEC" in contents
    assert "EXPLAINABILITY" not in contents


def test_snapshot_contents_has_only_type_keys(db):
    # Regression: contents must be exactly {type_name: max_active_version}.
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("MONITORING_SPEC", 3, {"type": "object"}, owner="sp0")
    snap_id = reg.snapshot_version()
    contents = db.execute(
        "SELECT contents FROM registry_snapshots WHERE snapshot_id=%s", (snap_id,)
    ).fetchone()[0]
    assert contents == {"MONITORING_SPEC": 3}
    assert all(not k.startswith("_") for k in contents)  # no '_digest' pollution


def test_active_version_is_writable(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("DQ_REPORT", 5, {"type": "object"}, owner="sp0")
    reg.assert_writable("DQ_REPORT", 5)  # no raise


def test_no_new_writes_at_deprecated_version(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("VALIDATION_REPORT", 1, {"type": "object"}, owner="sp0")
    reg.register_schema("VALIDATION_REPORT", 1, {"type": "object"}, owner="sp0",
                        status="deprecated")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("VALIDATION_REPORT", 1)


def test_no_new_writes_at_withdrawn_version(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("SANDBOX_RESULT", 1, {"type": "object"}, owner="sp0",
                        status="withdrawn")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("SANDBOX_RESULT", 1)


def test_deprecated_version_still_readable_for_inflight(db):
    # §3.3: deprecated/withdrawn versions remain READABLE (validate) for in-flight docs.
    reg = DocumentSchemaRegistry(db)
    schema = {"type": "object", "required": ["x"],
              "properties": {"x": {"type": "integer"}}}
    reg.register_schema("DQ_REPORT", 1, schema, owner="sp0", status="deprecated")
    reg.validate("DQ_REPORT", 1, {"x": 1})  # no raise — still readable


def test_withdrawn_version_reachable_via_upcast(db):
    # §3.3: a withdrawn version is reachable only via upcast (old data upcast on read).
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("DQ_REPORT", 1, {"type": "object"}, owner="sp0",
                        status="withdrawn")
    reg.register_schema("DQ_REPORT", 2, {"type": "object"}, owner="sp0")
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    out = reg.upcast("DQ_REPORT", {"v1": True}, 1, 2)
    assert out == {"v1": True, "v2": True}
