from __future__ import annotations

import jsonschema
import pytest

from sp0.aggregates.events import EVENT_SCHEMAS, register_phase06_event_types


class _RecordingRegistry:
    def __init__(self):
        self.registered = {}

    def register_schema(self, type_name, schema_version, json_schema, owner, *, status="active"):
        self.registered[(type_name, schema_version)] = (json_schema, owner, status)


def test_registers_every_type_at_v1_with_owner():
    reg = _RecordingRegistry()
    register_phase06_event_types(reg)
    assert set(EVENT_SCHEMAS) <= {t for (t, v) in reg.registered}
    for (type_name, version), (_schema, owner, status) in reg.registered.items():
        assert version == 1 and owner == "sp0-aggregates" and status == "active"


def test_core_types_present():
    for t in ["REQUEST_CREATED", "CANDIDATE_ADDED", "CANDIDATE_SELECTED", "FEATURE_CREATED",
              "VERSION_MINTED", "VERSION_ACTIVATED", "ACTIVATION_CONFLICT", "ACTIVATION_REQUESTED",
              "VERSION_QUIESCED", "RUN_CREATED", "RUN_REJECTED", "FACT_CONFIRMED_RESUME",
              "SOURCE_CHANGED_REVALIDATE"]:
        assert t in EVENT_SCHEMAS


def test_sample_payload_validates_and_missing_required_fails():
    schema = EVENT_SCHEMAS["VERSION_ACTIVATED"]
    jsonschema.validate(
        {"feature_id": "feat_1", "feature_version_id": "fv_1",
         "use_case": "fraud", "activation_state": "PRODUCTION"},
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"feature_id": "feat_1"}, schema)
