"""Slice 3a-i Task 1 — typed computation vocabulary + Requirement + FeatureIdea operand fields.

The tri-state validator (later tasks) stamps `validation_status` (underscore vocab, VALIDATION_STATES)
and attaches typed `Requirement`s to NEEDS_EXTERNAL_VALIDATION ideas. This is a SEPARATE axis from the
existing hyphenated `verification` stamp, which stays untouched.
"""
from __future__ import annotations

from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json
from featuregen.overlay.upload.feature_assist import (
    REQUIREMENT_CODES,
    VALIDATION_STATES,
    ExternalRequirementPreview,
    FeatureIdea,
    Requirement,
    RoleBinding,
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


# ── H1a: carry-through metadata fields (additive, server-assigned, flag-off byte-preserving) ─────────

# The EXACT key set the pre-H1a _idea_json produced for a plain llm_freeform idea — byte-identity is
# asserted against this reference (no H1a key may appear unless a non-default field is set).
_PRE_H1A_IDEA_JSON_KEYS = {
    "name", "derives_from", "aggregation", "grain_table", "verification", "critic_note", "rationale",
    "validation_status", "requirements", "derives_pairs", "origin", "path_authority", "plan_envelope",
}


def test_feature_idea_h1a_fields_default():
    idea = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                       aggregation="avg", grain_table=None)
    # generation_source defaults to the server label for the LLM free-form path; it is a SEPARATE axis
    # from the 3C.2a `origin` (which stays "llm"), never a duplicate of it.
    assert idea.generation_source == "llm_freeform"
    assert idea.origin == "llm"
    assert idea.recipe_id is None
    assert idea.candidate_status == ""
    assert idea.input_role_bindings == ()
    assert idea.external_requirement_previews == ()
    assert idea.metadata_snapshot_id is None
    assert idea.metadata_input_fingerprint is None
    assert idea.binding_fact_keys == ()
    assert idea.planner_applicability == "not_applicable_nonrecipe"
    assert idea.physical_plan_id is None
    assert idea.planner_declaration_id is None


def test_role_binding_is_frozen_hashable_and_round_trips():
    b = RoleBinding(role="entity", ref=("bank", "public.accounts.customer_id"),
                    evidence_ids=("ev1",), fact_ids=("fk1", "fk2"), authority="governed",
                    confirmation_required=True)
    assert hash(b) == hash(b)                       # hashable (tuple members only)
    assert RoleBinding.from_json(b.to_json()) == b   # exact round-trip
    # a defaulted binding round-trips too
    assert RoleBinding.from_json(RoleBinding().to_json()) == RoleBinding()
    p = ExternalRequirementPreview(content="verify numeric", schema_version="v1", content_hash="ab")
    assert ExternalRequirementPreview.from_json(p.to_json()) == p


def test_idea_json_byte_identical_for_plain_idea():
    # A plain llm_freeform idea (all H1a fields default) serializes to EXACTLY the pre-H1a key set —
    # no new key leaks, so a pre-H1a persisted snapshot is byte-identical (flag-off preservation).
    idea = FeatureIdea(name="avg_balance_90d", description="d",
                       derives_from=["public.accounts.balance"], aggregation="avg_90d",
                       grain_table="accounts",
                       derives_pairs=(("bank", "public.accounts.balance"),))
    d = _idea_json(idea)
    assert set(d.keys()) == _PRE_H1A_IDEA_JSON_KEYS
    for leaked in ("generation_source", "recipe_id", "candidate_status", "input_role_bindings",
                   "external_requirement_previews", "metadata_snapshot_id", "planner_applicability",
                   "physical_plan_id", "planner_declaration_id", "binding_fact_keys"):
        assert leaked not in d


def test_idea_json_round_trips_all_h1a_fields():
    idea = FeatureIdea(
        name="churn_recipe", description="d", derives_from=["public.accounts.balance"],
        aggregation="trend_90d", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.balance"),),
        generation_source="recipe", recipe_id="retail_churn.balance_trend",
        candidate_status="considered",
        input_role_bindings=(RoleBinding(role="entity", ref=("bank", "public.accounts.customer_id"),
                                         authority="governed", confirmation_required=True),),
        external_requirement_previews=(ExternalRequirementPreview("verify", "v1", "h"),),
        metadata_snapshot_id="snap_1", metadata_input_fingerprint="fp_1",
        binding_fact_keys=("entity:Customer", "time:as_of_date"),
        planner_applicability="not_applicable_single_catalog",
        physical_plan_id="bp_1", planner_declaration_id="pd_1")
    back = _idea_from_json(_idea_json(idea))
    # recipe_id survives the (de)serializer round-trip (the Gate-1 invariant, exercised end-to-end in
    # the gate1 DB suite); every H1a field survives too.
    assert back.recipe_id == "retail_churn.balance_trend"
    assert back.generation_source == "recipe"
    assert back.candidate_status == "considered"
    assert back.input_role_bindings == idea.input_role_bindings
    assert back.external_requirement_previews == idea.external_requirement_previews
    assert back.metadata_snapshot_id == "snap_1"
    assert back.metadata_input_fingerprint == "fp_1"
    assert back.binding_fact_keys == ("entity:Customer", "time:as_of_date")
    assert back.planner_applicability == "not_applicable_single_catalog"
    assert back.physical_plan_id == "bp_1"
    assert back.planner_declaration_id == "pd_1"
