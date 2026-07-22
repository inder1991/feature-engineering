"""Delivery C2-C3 Task 1 — versioned ValidationRequirementSchema registry + build_requirement factory.

Each of the 8 closed REQUIREMENT_CODES is defined in a VERSIONED registry with typed subject refs,
params, result schema, unit, and default blocking behaviour. `build_requirement` is the ONLY sanctioned
way to mint a Requirement: an unknown code / version / param is a PROGRAMMER ERROR (a raised
exception), never open JSON quietly accepted. The extended `Requirement` stays frozen + hashable +
backward-compatible.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.feature_assist import REQUIREMENT_CODES, Requirement
from featuregen.overlay.upload.validation_requirements import (
    REQUIREMENT_SCHEMA_REGISTRY,
    RequirementValidationError,
    UnknownRequirement,
    ValidationRequirementSchema,
    build_requirement,
    schema_for,
)


# ── the registry ────────────────────────────────────────────────────────────────────────────────
def test_registry_covers_exactly_the_closed_vocabulary():
    assert set(REQUIREMENT_SCHEMA_REGISTRY) == set(REQUIREMENT_CODES)
    assert len(REQUIREMENT_SCHEMA_REGISTRY) == 8


def test_schema_for_returns_each_code_schema():
    for code in REQUIREMENT_CODES:
        schema = schema_for(code)
        assert isinstance(schema, ValidationRequirementSchema)
        assert schema.code == code
        assert schema.schema_version == "v1"
        assert schema.subject_kind == "column_ref"
        assert isinstance(schema.blocking, bool)


def test_schema_for_unknown_code_raises():
    with pytest.raises(UnknownRequirement):
        schema_for("NOT_A_CODE")


def test_schema_for_wrong_version_raises():
    with pytest.raises(UnknownRequirement):
        schema_for("TYPE_IS_NUMERIC", schema_version="v2")


def test_schema_is_frozen():
    schema = schema_for("TYPE_IS_NUMERIC")
    with pytest.raises(Exception):
        schema.code = "OTHER"  # type: ignore[misc]


def test_schema_mappings_are_immutable_and_do_not_leak_across_lookups():
    # M-1: a caller mutating a returned schema's mapping must NOT corrupt the global registry for every
    # subsequent validation. The mappings are read-only (MappingProxyType), so a write RAISES, and a
    # later lookup sees the pristine schema — never a stray injected/removed param.
    schema = schema_for("TYPE_IS_NUMERIC")
    with pytest.raises(TypeError):
        schema.params_schema["sneaky"] = int  # type: ignore[index]
    with pytest.raises(TypeError):
        schema.result_schema["sneaky"] = int  # type: ignore[index]
    # a fresh lookup is unaffected — no injected param leaked into the registry
    assert schema_for("TYPE_IS_NUMERIC").params_schema == {}
    assert schema_for("TYPE_IS_NUMERIC").result_schema == {"is_numeric": bool}
    # the mutation would otherwise have broken build_requirement globally — it still mints cleanly
    assert build_requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.t.c")).params == ()


def test_lag_bounded_schema_has_params_result_and_unit():
    schema = schema_for("TEMPORAL_LAG_BOUNDED")
    assert schema.params_schema == {"max_lag": int, "unit": str}
    assert schema.result_schema == {"max_observed_lag": float, "within_bound": bool}
    assert schema.unit == "days"
    assert schema.blocking is True


def test_type_is_numeric_schema_shapes():
    schema = schema_for("TYPE_IS_NUMERIC")
    assert schema.params_schema == {}
    assert schema.result_schema == {"is_numeric": bool}
    assert schema.unit is None


def test_grain_is_unique_result_schema():
    assert schema_for("GRAIN_IS_UNIQUE").result_schema == {
        "is_unique": bool,
        "duplicate_count": int,
    }


def test_currency_consistent_declares_optional_currency_ref():
    # C2-C3 Task 2: currency_ref is DECLARED (typed tuple) but OPTIONAL — `_validate_idea` mints this
    # requirement for an operand whose currency is UNKNOWN, so no reference ref is available at mint.
    schema = schema_for("CURRENCY_CONSISTENT")
    assert schema.params_schema == {"currency_ref": tuple}
    assert schema.optional_params == frozenset({"currency_ref"})


# ── build_requirement — the sanctioned factory ────────────────────────────────────────────────────
def test_build_no_param_requirement():
    r = build_requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.t.c"))
    assert isinstance(r, Requirement)
    assert r.code == "TYPE_IS_NUMERIC"
    assert r.operand == ("bank", "public.t.c")
    assert r.detail == ""
    assert r.schema_version == "v1"
    assert r.params == ()


def test_build_requirement_carries_detail():
    r = build_requirement(
        code="TYPE_IS_NUMERIC", operand=("bank", "public.t.c"), detail="numeric declared hint"
    )
    assert r.detail == "numeric declared hint"


def test_build_lag_bounded_valid_params():
    r = build_requirement(
        code="TEMPORAL_LAG_BOUNDED",
        operand=("bank", "public.t.as_of"),
        params={"max_lag": 30, "unit": "days"},
    )
    # params stored as the sorted, hashable (name, value) tuple form
    assert r.params == (("max_lag", 30), ("unit", "days"))
    assert isinstance(r.params, tuple)


def test_build_lag_bounded_missing_param_rejected():
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="TEMPORAL_LAG_BOUNDED",
            operand=("bank", "public.t.as_of"),
            params={"max_lag": 30},
        )


def test_build_lag_bounded_extra_param_rejected():
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="TEMPORAL_LAG_BOUNDED",
            operand=("bank", "public.t.as_of"),
            params={"max_lag": 30, "unit": "days", "bogus": 1},
        )


def test_build_lag_bounded_wrong_type_param_rejected():
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="TEMPORAL_LAG_BOUNDED",
            operand=("bank", "public.t.as_of"),
            params={"max_lag": "30", "unit": "days"},
        )


def test_build_bool_where_int_expected_rejected():
    # bool is an int subclass — the typed contract must not silently accept True for an int param.
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="TEMPORAL_LAG_BOUNDED",
            operand=("bank", "public.t.as_of"),
            params={"max_lag": True, "unit": "days"},
        )


def test_build_no_param_code_rejects_supplied_params():
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="TYPE_IS_NUMERIC", operand=("bank", "public.t.c"), params={"anything": 1}
        )


def test_build_unknown_code_rejected_not_open_json():
    with pytest.raises((RequirementValidationError, UnknownRequirement)):
        build_requirement(code="NOT_A_CODE", operand=("bank", "public.t.c"))


def test_build_unknown_version_rejected():
    with pytest.raises((RequirementValidationError, UnknownRequirement)):
        build_requirement(
            code="TYPE_IS_NUMERIC", operand=("bank", "public.t.c"), schema_version="v99"
        )


def test_build_currency_consistent_valid():
    r = build_requirement(
        code="CURRENCY_CONSISTENT",
        operand=("bank", "public.t.amount"),
        params={"currency_ref": ("bank", "public.t.ccy")},
    )
    assert r.params == (("currency_ref", ("bank", "public.t.ccy")),)


def test_build_currency_consistent_without_optional_ref():
    # the OPTIONAL currency_ref may be omitted (the unknown-currency mint case in _validate_idea)
    r = build_requirement(code="CURRENCY_CONSISTENT", operand=("bank", "public.t.amount"))
    assert r.code == "CURRENCY_CONSISTENT"
    assert r.params == ()
    assert r.schema_version == "v1"


def test_build_currency_consistent_wrong_type_ref_still_rejected():
    # optional does NOT mean untyped: a supplied currency_ref must still be the declared tuple type
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="CURRENCY_CONSISTENT",
            operand=("bank", "public.t.amount"),
            params={"currency_ref": "not-a-tuple"},
        )


def test_build_rejects_unhashable_nested_param_value():
    # M-2: `currency_ref` is typed `tuple`, so a tuple with a NESTED unhashable member (a list) passes
    # the isinstance(tuple) check — yet the resulting Requirement would be unhashable and blow up only
    # at a distant set/dict-key use site. build_requirement must reject it up front, never return a
    # broken (unhashable) value object.
    with pytest.raises(RequirementValidationError):
        build_requirement(
            code="CURRENCY_CONSISTENT",
            operand=("bank", "public.t.amount"),
            params={"currency_ref": (["cat"], "ref")},
        )


# ── Requirement backward-compatibility (additive extension) ────────────────────────────────────────
def test_requirement_positional_backward_compatible():
    # The pre-C3 3-positional-arg construction still constructs unchanged, with new fields defaulted.
    r = Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "detail")
    assert r.code == "TYPE_IS_NUMERIC"
    assert r.operand == ("bank", "public.accounts.balance")
    assert r.detail == "detail"
    assert r.schema_version == "v1"
    assert r.params == ()


def test_requirement_defaults_detail_and_new_fields():
    r = Requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"))
    assert r.detail == ""
    assert r.schema_version == "v1"
    assert r.params == ()


def test_requirement_still_frozen():
    r = Requirement("TYPE_IS_NUMERIC", ("bank", "public.t.c"))
    with pytest.raises(Exception):
        r.code = "OTHER"  # type: ignore[misc]


def test_requirement_still_hashable_with_params():
    r = build_requirement(
        code="TEMPORAL_LAG_BOUNDED",
        operand=("bank", "public.t.as_of"),
        params={"max_lag": 30, "unit": "days"},
    )
    # hashable => usable in a set / as a dict key despite carrying params
    assert len({r, r}) == 1
    assert r in {r}


def test_requirement_equality_unaffected_for_old_shape():
    # An old-shape requirement equals another built the same way (new fields default identically).
    a = Requirement("TYPE_IS_NUMERIC", ("bank", "public.t.c"), "d")
    b = Requirement("TYPE_IS_NUMERIC", ("bank", "public.t.c"), "d")
    assert a == b
    assert hash(a) == hash(b)
