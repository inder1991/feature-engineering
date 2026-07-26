from __future__ import annotations

from types import SimpleNamespace

import pytest

from featuregen.formula import recipe_egress as egress_module
from featuregen.formula.recipe_egress import (
    FORMULA_PROSE_POLICY_VERSION,
    RecipeEgressViolation,
    build_recipe_authoring_egress,
    project_recipe_tool_result,
    validate_recipe_provider_payload,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    content_hash,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundedNeedBinding,
    SourceEntityRoleResolution,
)


def _binding(role: str, column: str, concept: str) -> GroundedNeedBinding:
    ref = f"bank::public.txn.{column}"
    return GroundedNeedBinding(
        role=role,
        catalog_source="bank",
        logical_ref=ref,
        graph_object_ref=f"public.txn.{column}",
        expected_concept=concept,
        optional=False,
        join_role=None,
        temporal_role=None,
        distinct_binding_group=None,
        binding_resolution=BindingResolution.UNIQUE,
        tied_candidate_logical_refs=(ref,),
        tied_candidate_set_hash="set-hash",
    )


def _context_for(recipe_id: str) -> RecipeGroundingContextV1:
    definition = {"version": "test"}
    parameters = (("window", 90),)
    if recipe_id != "merchant_mcc_diversity":
        raise AssertionError("test helper only defines merchant_mcc_diversity")
    return RecipeGroundingContextV1(
        recipe_candidate_key="candidate",
        recipe_id=recipe_id,
        source_entity_need_role="merchant",
        source_entity_role_resolution=SourceEntityRoleResolution.EXPLICIT,
        need_bindings=(
            _binding("merchant", "merchant_id", "merchant_id"),
            _binding("mcc", "mcc", "mcc"),
            _binding("event_ts", "event_ts", "event_timestamp"),
        ),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(recipe_id, parameters),
        template_definition=definition,
        template_content_hash=content_hash(definition),
    )


def _bound():
    from featuregen.overlay.upload.recipe_formula_contracts import bind_formula_expectation

    recipe_id = "merchant_mcc_diversity"
    return bind_formula_expectation(
        _context_for(recipe_id), RECIPE_FORMULA_EXPECTATIONS[recipe_id])


def _egress(recipe_id: str = "merchant_mcc_diversity"):
    if recipe_id != "merchant_mcc_diversity":
        raise AssertionError("test helper only defines merchant_mcc_diversity")
    return build_recipe_authoring_egress(
        hypothesis="Merchant category breadth can indicate unusual behavior.",
        prediction_goal="Predict merchant fraud risk.",
        expectation=_bound(),
    )


def test_recipe_egress_excludes_internal_identity_and_replay_fields():
    egress = _egress()
    payload = egress.provider_payload()
    serialized = str(payload)
    assert "recipe_candidate_key" not in serialized
    assert "template_content_hash" not in serialized
    assert "semantic_parameter_binding_hash" not in serialized
    assert "evidence_id" not in serialized
    assert egress.content_hash


def test_recipe_egress_rejects_unknown_and_nested_forbidden_fields():
    payload = _egress().provider_payload()
    with pytest.raises(RecipeEgressViolation, match="unknown"):
        validate_recipe_provider_payload({**payload, "actor": {"subject": "user:x"}})
    poisoned = dict(payload)
    poisoned["formula_expectation"] = {
        **payload["formula_expectation"],
        "evidence_ids": ["ev-1"],
    }
    with pytest.raises(RecipeEgressViolation):
        validate_recipe_provider_payload(poisoned)


def test_recipe_egress_rejects_unbounded_prose():
    egress = _egress()
    with pytest.raises(RecipeEgressViolation, match="bounded string"):
        build_recipe_authoring_egress(
            hypothesis="x" * 4_001,
            prediction_goal=egress.prediction_goal,
            expectation=_bound(),
        )


def test_recipe_egress_redacts_prose_before_hashing_and_is_deterministic():
    hypothesis = (
        "Customer named Alice Johnson emailed alice.johnson@example.com about "
        "account 12345678901.\n"
        "The sample profile is ALPHA_NUMERIC, with representative values such as "
        "ARTKOM GLOBAL FZE; BRANCH01, which supports interpretation."
    )
    prediction_goal = (
        "Predict card fraud for 4111 1111 1111 1111, while preserving AML and "
        "net interest margin terminology."
    )
    first = build_recipe_authoring_egress(
        hypothesis=hypothesis,
        prediction_goal=prediction_goal,
        expectation=_bound(),
    )
    second = build_recipe_authoring_egress(
        hypothesis=hypothesis,
        prediction_goal=prediction_goal,
        expectation=_bound(),
    )
    payload = first.provider_payload()
    serialized = str(payload)
    for raw in (
        "Alice Johnson",
        "alice.johnson@example.com",
        "12345678901",
        "ARTKOM GLOBAL FZE",
        "BRANCH01",
        "4111 1111 1111 1111",
    ):
        assert raw not in serialized
    assert "[REDACTED:PERSON_NAME]" in first.hypothesis
    assert "[REDACTED:EMAIL]" in first.hypothesis
    assert "[REDACTED:ACCOUNT]" in first.hypothesis
    assert "[REDACTED:PAN]" in first.prediction_goal
    assert "AML" in first.prediction_goal
    assert "net interest margin" in first.prediction_goal
    assert first.redaction_policy_version == FORMULA_PROSE_POLICY_VERSION
    span_types = {
        span["type"]
        for field in first.input_redaction.values()
        for span in field["redacted_spans"]
    }
    assert {"PERSON_NAME", "EMAIL", "ACCOUNT", "SAMPLE_VALUE", "PAN"} <= span_types
    assert all(
        set(span) == {"type", "start", "end"}
        for field in first.input_redaction.values()
        for span in field["redacted_spans"]
    )
    assert first.provider_payload() == second.provider_payload()
    assert first.content_hash == second.content_hash


def test_recipe_egress_preserves_safe_multiline_banking_prose():
    egress = build_recipe_authoring_egress(
        hypothesis=(
            "Customer transaction velocity, balance trend,\n"
            "and channel diversity can indicate deposit attrition."
        ),
        prediction_goal="Predict churn in 90 days using governed metadata.",
        expectation=_bound(),
    )
    assert "transaction velocity" in egress.hypothesis
    assert "deposit attrition" in egress.hypothesis
    assert egress.input_redaction == {
        "hypothesis": {"redacted_spans": []},
        "prediction_goal": {"redacted_spans": []},
    }


def test_recipe_egress_redactor_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        egress_module,
        "sanitize_definition",
        lambda _text: SimpleNamespace(
            reason="pii_redaction_failed",
            clean="",
            redaction_version="default-redactor@1",
            redacted_spans=(),
        ),
    )
    with pytest.raises(RecipeEgressViolation, match="failed closed"):
        build_recipe_authoring_egress(
            hypothesis="safe hypothesis",
            prediction_goal="safe prediction goal",
            expectation=_bound(),
        )


def test_recipe_egress_validator_rejects_residual_unsafe_prose():
    payload = _egress().provider_payload()
    with pytest.raises(RecipeEgressViolation, match="residual unsafe prose"):
        validate_recipe_provider_payload({
            **payload,
            "prediction_goal": "Predict fraud for card 4111 1111 1111 1111",
        })


def test_tool_projection_strips_provenance_and_blocks_lineage():
    internal = {
        "found": True,
        "logical_ref": "ftr::public.txns.amount",
        "table": "txns",
        "column": "amount",
        "data_type": "numeric",
        "facts": {
            "additivity": {
                "value": "additive",
                "authority": "governed",
                "provenance": "evidence-secret",
            }
        },
    }
    projected = project_recipe_tool_result("get_column_metadata", internal)
    assert "provenance" not in str(projected)
    with pytest.raises(RecipeEgressViolation, match="does not expose graph lineage"):
        project_recipe_tool_result(
            "get_verified_lineage",
            {"nodes": [{"id": "secret"}], "edges": []},
        )


def test_tool_projection_rejects_unclassified_internal_fields():
    internal = {
        "found": True,
        "logical_ref": "ftr::public.txns.amount",
        "table": "txns",
        "column": "amount",
        "data_type": "numeric",
        "facts": {},
        "samples": ["123"],
    }
    with pytest.raises(RecipeEgressViolation, match="unknown"):
        project_recipe_tool_result("get_column_metadata", internal)
