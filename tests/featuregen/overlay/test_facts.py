import pytest

from featuregen.contracts import SchemaValidationError  # event-registry error (registry tests only)
from featuregen.events.registry import event_registry
from featuregen.overlay import facts
from featuregen.overlay.facts import FactValidationError  # overlay value-shape error (pin 9)


def test_availability_time_schema_accepts_good_and_rejects_malformed():
    facts.validate_fact_value(
        facts.AVAILABILITY_TIME, {"column": "posted_at", "basis": "posted_at"}
    )
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.AVAILABILITY_TIME, {"column": "posted_at", "basis": "nope"})


def test_grain_schema_accepts_good_and_rejects_malformed():
    facts.validate_fact_value(facts.GRAIN, {"columns": ["id"], "is_unique": True})
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.GRAIN, {"columns": "id", "is_unique": True})


def test_scd_schema_accepts_good_and_rejects_malformed():
    facts.validate_fact_value(
        facts.SCD_EFFECTIVE_DATING, {"valid_from": "eff_from", "valid_to": None}
    )
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.SCD_EFFECTIVE_DATING, {"valid_to": "eff_to"})


def test_approved_join_schema_accepts_good_and_rejects_malformed():
    facts.validate_fact_value(
        facts.APPROVED_JOIN,
        {
            "from_ref": {
                "catalog_source": "pg:core",
                "object_kind": "table",
                "schema": "core",
                "table": "transactions",
            },
            "to_ref": {
                "catalog_source": "pg:core",
                "object_kind": "table",
                "schema": "core",
                "table": "customers",
            },
            "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
            "cardinality": "N:1",
        },
    )
    with pytest.raises(FactValidationError):  # missing from_ref/to_ref + bad cardinality
        facts.validate_fact_value(
            facts.APPROVED_JOIN,
            {"column_pairs": [{"from_col": "customer_id", "to_col": "id"}], "cardinality": "many"},
        )


def test_policy_tag_schema_accepts_good_and_rejects_malformed():
    facts.validate_fact_value(facts.POLICY_TAG, {"decision": "deny", "basis": "PII"}, "fraud")
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.POLICY_TAG, {"decision": "maybe", "basis": "PII"}, "fraud")


def test_policy_tag_requires_use_case():
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.POLICY_TAG, {"decision": "deny", "basis": "PII"})


def test_data_fact_rejects_use_case():
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.GRAIN, {"columns": ["id"], "is_unique": True}, "fraud")


def test_availability_time_requires_lag_hours_for_event_time_plus_lag():
    # F11(a): valid when lag_hours is present
    facts.validate_fact_value(
        facts.AVAILABILITY_TIME,
        {"column": "posted_at", "basis": "event_time_plus_lag", "lag_hours": 6},
    )
    # gap (a): event_time_plus_lag without lag_hours must be rejected
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(
            facts.AVAILABILITY_TIME,
            {"column": "posted_at", "basis": "event_time_plus_lag"},
        )


def test_grain_columns_reject_duplicates():
    # F11(b): valid distinct columns still accepted
    facts.validate_fact_value(facts.GRAIN, {"columns": ["id", "region"], "is_unique": True})
    # gap (b): duplicate grain columns must be rejected
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(facts.GRAIN, {"columns": ["id", "id"], "is_unique": True})


def test_approved_join_column_pairs_reject_duplicates():
    # gap (c): duplicate column_pairs must be rejected
    with pytest.raises(FactValidationError):
        facts.validate_fact_value(
            facts.APPROVED_JOIN,
            {
                "from_ref": {"catalog_source": "pg:core", "object_kind": "table",
                             "schema": "core", "table": "transactions"},
                "to_ref": {"catalog_source": "pg:core", "object_kind": "table",
                           "schema": "core", "table": "customers"},
                "column_pairs": [
                    {"from_col": "customer_id", "to_col": "id"},
                    {"from_col": "customer_id", "to_col": "id"},
                ],
                "cardinality": "N:1",
            },
        )


def test_all_six_event_types_register_and_validate():
    reg = event_registry()  # already populated by the autouse fixture
    for type_name in (
        facts.OVERLAY_FACT_PROPOSED,
        facts.OVERLAY_FACT_PARTIALLY_CONFIRMED,
        facts.OVERLAY_FACT_CONFIRMED,
        facts.OVERLAY_FACT_REJECTED,
        facts.OVERLAY_FACT_EXPIRED,
        facts.OVERLAY_FACT_STALED,
    ):
        reg.assert_writable(type_name, facts.OVERLAY_EVENT_SCHEMA_VERSION)
    reg.validate(
        facts.OVERLAY_FACT_CONFIRMED,
        facts.OVERLAY_EVENT_SCHEMA_VERSION,
        {
            "value": {"columns": ["id"]},
            "confirmers": [{"subject": "u", "role": "data_owner"}],
            "expires_at": "2026-12-31T00:00:00+00:00",
            "confirms_event_id": "evt_1",
        },
    )


def test_confirmed_event_schema_rejects_missing_required_field():
    reg = event_registry()
    with pytest.raises(SchemaValidationError):
        reg.validate(
            facts.OVERLAY_FACT_CONFIRMED,
            facts.OVERLAY_EVENT_SCHEMA_VERSION,
            {"confirmers": [], "expires_at": None, "confirms_event_id": "evt_1"},  # no `value`
        )


def test_rejected_event_schema_category_optional():
    """Task 5 addendum: `category` is a first-class OPTIONAL nullable property on REJECTED —
    a reliable analytics key alongside the polymorphic free-text `reason`. NOT required:
    pre-existing REJECTED events without the key still validate (backward-compat)."""
    reg = event_registry()
    base = {
        "rejected_by": "u",
        "reason": "free text",
        "target_event_id": "evt_1",
        "retired_fingerprint": None,
    }
    # backward-compat: an event with NO `category` key at all still validates
    reg.validate(facts.OVERLAY_FACT_REJECTED, facts.OVERLAY_EVENT_SCHEMA_VERSION, base)
    # new shape: `category` present as a string, or explicitly null
    reg.validate(
        facts.OVERLAY_FACT_REJECTED,
        facts.OVERLAY_EVENT_SCHEMA_VERSION,
        {**base, "category": "different_entity"},
    )
    reg.validate(
        facts.OVERLAY_FACT_REJECTED,
        facts.OVERLAY_EVENT_SCHEMA_VERSION,
        {**base, "category": None},
    )
