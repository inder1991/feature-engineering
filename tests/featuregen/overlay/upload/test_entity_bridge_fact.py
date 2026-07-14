from __future__ import annotations

import pytest

from featuregen.overlay import facts
from featuregen.overlay.facts import FACT_VALUE_SCHEMAS, validate_fact_value
from featuregen.overlay.identity import (
    CatalogObjectRef,
    EntityBridgeRef,
    fact_key,
    join_write_error,
)


def _ref(left_source="core", right_source="crm") -> EntityBridgeRef:
    return EntityBridgeRef(
        entity_id="customer",
        left_ref=CatalogObjectRef(left_source, "column", "public", "customer_master", "customer_id"),
        right_ref=CatalogObjectRef(right_source, "column", "public", "customers", "customer_id"))


def _value(ref: EntityBridgeRef) -> dict:
    from dataclasses import asdict
    return {"entity_id": ref.entity_id, "left_ref": asdict(ref.left_ref), "right_ref": asdict(ref.right_ref)}


def test_entity_bridge_is_a_registered_data_fact_type():
    assert facts.ENTITY_BRIDGE == "entity_bridge"
    assert facts.ENTITY_BRIDGE in facts.DATA_FACT_TYPES
    assert facts.ENTITY_BRIDGE in FACT_VALUE_SCHEMAS


def test_value_schema_accepts_a_bridge_and_rejects_extras():
    ref = _ref()
    validate_fact_value("entity_bridge", _value(ref))   # no raise
    bad = _value(ref) | {"unexpected": 1}
    with pytest.raises(Exception):
        validate_fact_value("entity_bridge", bad)


def test_fact_key_is_symmetric():
    # swapping the two endpoints denotes the SAME bridge -> identical fact_key
    a = _ref()
    b = EntityBridgeRef(entity_id="customer", left_ref=a.right_ref, right_ref=a.left_ref)
    assert fact_key(a, "entity_bridge") == fact_key(b, "entity_bridge")


def test_write_gate_requires_cross_catalog():
    same = _ref(left_source="core", right_source="core")   # same catalog -> illegal for a bridge
    err = join_write_error(same, "entity_bridge", _value(same))
    assert err is not None and "distinct catalog" in err


def test_write_gate_passes_cross_catalog_and_matching_value():
    ref = _ref()
    assert join_write_error(ref, "entity_bridge", _value(ref)) is None


def test_write_gate_rejects_value_ref_mismatch():
    ref = _ref()
    other = _value(_ref(right_source="other"))   # value describes a different bridge than ref
    err = join_write_error(ref, "entity_bridge", other)
    assert err is not None and "does not match" in err
