"""Slice 3A-iv Task 3: explicit v1/v2 FeatureIdea serializers.

Flag-OFF (v1) output must be BYTE-IDENTICAL to the pre-Slice-3 dataclass serialization even when the
new fields carry non-default values — the new fields must NOT leak (spec §8)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.api.feature_serialize import (
    serialize_feature_idea,
    serialize_feature_idea_v1,
    serialize_feature_idea_v2,
)
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement

# The exact key order FastAPI's jsonable_encoder produced for the pre-Slice-3 dataclass (field order:
# name, description, derives_from, aggregation, grain_table, derives_pairs, verification,
# critic_note, rationale). Byte-identity is asserted against this reference.
_PRE_SLICE3_KEYS = ["name", "description", "derives_from", "aggregation", "grain_table",
                    "derives_pairs", "verification", "critic_note", "rationale"]


def _idea_with_new_fields() -> FeatureIdea:
    # A fully-populated idea: new fields set to NON-default values so a leak would be visible.
    return FeatureIdea(
        name="avg_balance", description="average balance per account",
        derives_from=["public.accounts.balance"], aggregation="avg", grain_table="accounts",
        derives_pairs=(("deposits", "public.accounts.balance"),),
        verification="DESIGN-CHECKED", critic_note="note", rationale="why",
        operation_kind="avg", measure_refs=(("deposits", "public.accounts.balance"),),
        grain_ref=("deposits", "public.accounts.id"), time_ref=None, window="30d",
        grouping_refs=(("deposits", "public.accounts.cust_id"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("deposits", "public.accounts.balance"),
                                  "verify"),))


def test_v1_is_byte_identical_to_pre_slice3_reference():
    idea = _idea_with_new_fields()
    reference = {
        "name": "avg_balance", "description": "average balance per account",
        "derives_from": ["public.accounts.balance"], "aggregation": "avg",
        "grain_table": "accounts",
        "derives_pairs": [["deposits", "public.accounts.balance"]],
        "verification": "DESIGN-CHECKED", "critic_note": "note", "rationale": "why",
    }
    out = serialize_feature_idea_v1(idea)
    assert list(out.keys()) == _PRE_SLICE3_KEYS
    # Byte-for-byte: the serializer output serializes identically to the pre-Slice-3 shape.
    assert json.dumps(out) == json.dumps(reference)
    # No new-field key leaks even though the idea carries non-default new-field values.
    for leaked in ("operation_kind", "measure_refs", "grain_ref", "time_ref", "window",
                   "grouping_refs", "validation_status", "requirements"):
        assert leaked not in out


def test_v2_carries_the_new_fields():
    out = serialize_feature_idea_v2(_idea_with_new_fields())
    assert out["operation_kind"] == "avg"
    assert out["measure_refs"] == [["deposits", "public.accounts.balance"]]
    assert out["grain_ref"] == ["deposits", "public.accounts.id"]
    assert out["time_ref"] is None
    assert out["window"] == "30d"
    assert out["grouping_refs"] == [["deposits", "public.accounts.cust_id"]]
    assert out["validation_status"] == "NEEDS_EXTERNAL_VALIDATION"
    assert out["requirements"] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["deposits", "public.accounts.balance"],
         "detail": "verify"}]
    # v2 is a strict superset of v1 (same v1 keys, same values).
    v1 = serialize_feature_idea_v1(_idea_with_new_fields())
    for k, v in v1.items():
        assert out[k] == v


def test_dispatch_matches_flag():
    idea = _idea_with_new_fields()
    assert serialize_feature_idea(idea, feature_context=False) == serialize_feature_idea_v1(idea)
    assert serialize_feature_idea(idea, feature_context=True) == serialize_feature_idea_v2(idea)


def _recipe_idea() -> FeatureIdea:
    from featuregen.overlay.upload.feature_assist import RoleBinding
    return FeatureIdea(
        name="balance_trend_90d", description="", derives_from=["public.accounts.balance"],
        aggregation="trend_90d", grain_table="accounts",
        generation_source="recipe", recipe_id="retail_churn.balance_trend",
        candidate_status="considered",
        input_role_bindings=(RoleBinding(role="entity", ref=("bank", "public.accounts.customer_id"),
                                         authority="governed"),),
        planner_applicability="not_applicable_single_catalog")


def test_v1_never_leaks_h1a_fields_even_when_set():
    # H1a metadata must NEVER appear in the flag-OFF (v1) response — byte-identity depends on it.
    out = serialize_feature_idea_v1(_recipe_idea())
    assert list(out.keys()) == _PRE_SLICE3_KEYS
    for leaked in ("generation_source", "recipe_id", "candidate_status", "input_role_bindings",
                   "planner_applicability", "physical_plan_id", "metadata_snapshot_id"):
        assert leaked not in out


def test_v2_carries_h1a_fields_only_when_non_default():
    # A plain idea's v2 carries NO H1a keys (byte-stable); a recipe idea surfaces its server labels.
    plain = serialize_feature_idea_v2(_idea_with_new_fields())
    for k in ("generation_source", "recipe_id", "candidate_status", "input_role_bindings",
              "planner_applicability", "physical_plan_id"):
        assert k not in plain
    recipe = serialize_feature_idea_v2(_recipe_idea())
    assert recipe["generation_source"] == "recipe"
    assert recipe["recipe_id"] == "retail_churn.balance_trend"
    assert recipe["candidate_status"] == "considered"
    assert recipe["planner_applicability"] == "not_applicable_single_catalog"
    assert recipe["input_role_bindings"] == [
        {"role": "entity", "authority": "governed", "ref": ["bank", "public.accounts.customer_id"]}]


def _recommend_fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance"],
            "aggregation": "avg", "grain_table": "customers"}]}),
    })


def test_recommend_response_has_no_new_field_markers_when_flag_off(make_client, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    client: TestClient = make_client(llm_client=_recommend_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend",
                      json={"objective": "predict churn", "catalog_source": "deposits"},
                      headers=AUTH)
    assert res.status_code == 200
    proposals = res.json()["proposals"]
    assert len(proposals) == 1
    assert sorted(proposals[0].keys()) == sorted(_PRE_SLICE3_KEYS)
    # The new field names never appear anywhere in the raw response bytes.
    for marker in (b"validation_status", b"operation_kind", b"measure_refs", b"requirements",
                   b"grouping_refs"):
        assert marker not in res.content, marker
