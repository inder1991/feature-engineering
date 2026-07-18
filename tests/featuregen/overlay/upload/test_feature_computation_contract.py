"""Slice 3a-i Task 1 — typed computation vocabulary + Requirement + FeatureIdea operand fields.

The tri-state validator (later tasks) stamps `validation_status` (underscore vocab, VALIDATION_STATES)
and attaches typed `Requirement`s to NEEDS_EXTERNAL_VALIDATION ideas. This is a SEPARATE axis from the
existing hyphenated `verification` stamp, which stays untouched.
"""
from __future__ import annotations

from featuregen.overlay.upload.feature_assist import (
    REQUIREMENT_CODES,
    VALIDATION_STATES,
    FeatureIdea,
    Requirement,
)


def test_requirement_codes_are_the_closed_vocabulary():
    assert REQUIREMENT_CODES == frozenset({
        "TYPE_IS_NUMERIC", "GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED", "TEMPORAL_LAG_BOUNDED",
        "JOIN_CONNECTIVITY", "UNIT_CONSISTENT", "CURRENCY_CONSISTENT",
        "ADDITIVITY_SUPPORTS_OPERATION",
    })


def test_validation_states_tuple():
    assert VALIDATION_STATES == ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")


def test_requirement_is_frozen_and_defaults_detail():
    r = Requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"))
    assert r.code in REQUIREMENT_CODES
    assert r.operand == ("bank", "public.accounts.balance")
    assert r.detail == ""


def test_feature_idea_new_fields_default_and_keep_verification_separate():
    idea = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                       aggregation="avg", grain_table=None)
    # existing hyphenated stamp is a SEPARATE axis, unchanged
    assert idea.verification == "DESIGN-CHECKED"
    # new tri-state axis defaults
    assert idea.validation_status == "DESIGN_CHECKED"
    assert idea.requirements == ()
    assert idea.operation_kind == ""
    assert idea.measure_refs == ()
    assert idea.grain_ref is None
    assert idea.time_ref is None
    assert idea.window is None
    assert idea.grouping_refs == ()


def test_feature_idea_carries_typed_operands_and_requirements():
    req = Requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"),
                      detail="operational type unknown; numeric declared hint")
    idea = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                       aggregation="sum", grain_table="accounts",
                       measure_refs=(("bank", "public.accounts.balance"),),
                       operation_kind="sum", validation_status="NEEDS_EXTERNAL_VALIDATION",
                       requirements=(req,))
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert idea.requirements == (req,)
    assert idea.measure_refs == (("bank", "public.accounts.balance"),)
