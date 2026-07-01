import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake import contract
from featuregen.intake.contract import register_contract_schemas


def _confirmed_body():
    return {
        "feature_name": "declined_card_auth_count_90d", "intake_mode": "definition",
        "raw_input_ref": "blob_01H", "raw_input_classification": "clean",
        "entity": "customer", "entity_key": "customer_id",
        "feature_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time"},
        "calculation_method": {"method_version": 1,
                               "chosen": {"kind": "rolling_aggregate", "aggregation": "count",
                                          "window": "90d"}, "considered": []},
        "target": None, "assumption_ledger_ref": "doc_led1",
        "requires_independent_validation": False,
        "confirmation": {"confirmed_by": "user:raj", "confirmed_at": "2026-07-01T10:22:41Z"},
        "provenance": {"derived_from": ["doc_draft1"], "schema_version": 1},
        "status": "CONFIRMED",
    }


def test_registers_all_three_content_stages_and_confirmed_is_writable(db):
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    # CONFIRMED_CONTRACT is the NEW registered type; DRAFT/LEDGER re-affirmed (additive)
    reg.assert_writable("CONFIRMED_CONTRACT", contract.CONFIRMED_CONTRACT_SCHEMA_VERSION)
    reg.assert_writable("DRAFT_CONTRACT", contract.DRAFT_CONTRACT_SCHEMA_VERSION)
    reg.assert_writable("ASSUMPTION_LEDGER", contract.ASSUMPTION_LEDGER_SCHEMA_VERSION)


def test_registered_confirmed_schema_validates_a_good_body_and_rejects_a_bad_one(db):
    from featuregen.contracts import SchemaValidationError

    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    reg.validate("CONFIRMED_CONTRACT", 1, _confirmed_body())
    bad = _confirmed_body()
    del bad["calculation_method"]
    with pytest.raises(SchemaValidationError):
        reg.validate("CONFIRMED_CONTRACT", 1, bad)


def test_register_is_idempotent(db):
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    register_contract_schemas(reg)   # must not raise (ON CONFLICT DO UPDATE, additive)
    reg.validate("CONFIRMED_CONTRACT", 1, _confirmed_body())


def test_upcaster_chain_is_total_at_v1_identity_read(db):
    # v1 is the base version — reading a v1 body "through the chain to the current version" is the
    # identity projection; the registry supports from==to. The first real upcaster ships with the
    # first schema-version bump (§4.0).
    reg = DocumentSchemaRegistry(db)
    register_contract_schemas(reg)
    body = _confirmed_body()
    assert reg.upcast("CONFIRMED_CONTRACT", body, 1, 1) == dict(body)
