from __future__ import annotations

import copy
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from featuregen.formula import recipe_egress as egress_module
from featuregen.formula.recipe_egress import (
    FORMULA_EXPECTATION_SCHEMA_V2,
    FORMULA_PROSE_POLICY_VERSION,
    MAX_OFFSET_PERIODS,
    RecipeEgressViolation,
    build_recipe_authoring_egress,
    project_recipe_tool_result,
    validate_recipe_provider_payload,
)
from featuregen.formula.schema_v2 import MAX_WINDOW_OFFSET_PERIODS
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
    derive_blueprint_v2,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    bind_formula_expectation_v2,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    content_hash,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
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


# ── task A4, increment 1: the v2 arm of the fail-close whitelist ───────────────────────────
#
# The gate is the boundary to a provider, so widening it is a governed-security change and gets
# its own increment. Two properties are asserted below and neither may be relaxed:
#   1. the v1 shape is FROZEN — same acceptances, same refusals, same bytes;
#   2. every key v2 adds carries a REAL bound, not a presence check.

#: The v1 provider payload for the reviewed merchant expectation, canonically encoded. Proved
#: equal to the pre-A4 implementation's output before this digest was written down (the old
#: module was loaded from git and run side by side over 33 payloads — 31 identical outcomes, and
#: the two differences are payloads that DECLARE a schema version, which could not exist before
#: this change and are still refused). Live work items carry these exact bytes.
V1_GOLDEN_PAYLOAD_SHA256 = (
    "09ce6764a4213cfd86778dcd6d984c27665b4e4b13220c7ef7460d50045ecf99")

#: The frozen v1 arm's own source. A byte of drift here changes what a sealed, already-enqueued
#: work item is allowed to send, so it fails CI instead of shipping.
V1_ARM_SOURCE_SHA256 = (
    "c063aad8dde97acc44e2f8d27a266435593736447ba7e2974549b992fb2020c2")


def _bound_v2():
    """The A4 exemplar: a blueprint DERIVED from the ``posted_debit_amount`` definition and bound
    by A1's binder — the exact object the plan's reproduced repro built."""
    blueprint = derive_blueprint_v2(v2_recipe_by_id("posted_debit_amount"))
    parameters = (("window", 90),)

    def binding(role: str, ref: str) -> GroundedNeedBinding:
        return GroundedNeedBinding(
            role=role, catalog_source="bank", logical_ref=ref,
            graph_object_ref=ref.replace("bank::", "public."), expected_concept=role,
            optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
            binding_resolution=BindingResolution.UNIQUE, tied_candidate_logical_refs=(ref,),
            tied_candidate_set_hash="set-hash")

    definition = {"version": "derived-probe"}
    context = RecipeGroundingContextV1(
        recipe_candidate_key="candidate", recipe_id="posted_debit_amount",
        source_entity_need_role="account",
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=(binding("account", "bank::public.txns.acct_id"),
                       binding("amount", "bank::public.txns.txn_amt"),
                       binding("event_ts", "bank::public.txns.booking_ts")),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(
            "posted_debit_amount", parameters),
        template_definition=definition,
        template_content_hash=content_hash(definition))
    return bind_formula_expectation_v2(context, blueprint)


def _egress_v2():
    return build_recipe_authoring_egress(
        hypothesis="Posted debit velocity can indicate deposit attrition.",
        prediction_goal="Predict deposit attrition in 90 days.",
        expectation=_bound_v2(),
    )


def _v2_payload() -> dict[str, Any]:
    return copy.deepcopy(_egress_v2().provider_payload())


def _refused(payload: dict[str, Any]) -> str:
    with pytest.raises(RecipeEgressViolation) as caught:
        validate_recipe_provider_payload(payload)
    return str(caught.value)


def test_the_v1_provider_payload_is_byte_frozen():
    """A1..A4 must not move one byte of the v1 projection: a stored work item is re-validated
    against this gate before every dispatch, and its ``provider_input_hash`` was sealed."""
    egress = _egress()
    canonical = json.dumps(
        egress.provider_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(canonical.encode()).hexdigest() == V1_GOLDEN_PAYLOAD_SHA256
    assert egress.content_hash == V1_GOLDEN_PAYLOAD_SHA256
    assert "formula_schema_version" not in egress.provider_payload()["formula_expectation"]


def test_the_v1_expectation_arm_is_source_frozen():
    source = inspect.getsource(egress_module._validate_formula_expectation_v1)
    assert hashlib.sha256(source.encode()).hexdigest() == V1_ARM_SOURCE_SHA256


def test_the_v1_key_sets_are_exactly_the_reviewed_ones():
    assert egress_module._EXPECTATION_KEYS_V1 == frozenset({
        "final_operation", "expressions", "grain_entity", "grain_key_refs", "decimal",
        "policy_version"})
    assert egress_module._EXPRESSION_KEYS_V1 == frozenset({
        "expression_path", "aggregation", "operand_ref", "source_relation_ref",
        "event_time_ref", "window_length", "window"})
    assert egress_module._WINDOW_KEYS_V1 == frozenset({
        "event_time_role", "basis", "length_parameter", "unit", "start_inclusive",
        "end_inclusive", "timezone", "empty_window", "null_input"})
    assert len(egress_module._EXPRESSION_KEYS_V2) == 12
    assert len(egress_module._WINDOW_KEYS_V2) == 10


def test_a_bound_v2_expectation_reaches_the_provider_vocabulary():
    """THE A4-a REPRO, from the plan's own correction blockquote. Before this increment it read
    ``expressions[0] keys differ: unknown=['aggregation_argument', 'authority_refs',
    'second_operand_ref', 'term_name', 'term_sign']`` and the capture wrote EGRESS_REJECTED."""
    payload = _egress_v2().provider_payload()
    validate_recipe_provider_payload(payload)
    expectation = payload["formula_expectation"]
    assert expectation["formula_schema_version"] == FORMULA_EXPECTATION_SCHEMA_V2
    assert expectation["expressions"][0]["operand_ref"] == "bank::public.txns.txn_amt"
    assert expectation["expressions"][0]["window"]["offset_periods"] == 0
    assert expectation["expressions"][0]["authority_refs"]["status_policy_ref"]


def test_the_v2_projection_keeps_the_governance_keys_server_private():
    serialized = str(_egress_v2().provider_payload())
    for private in ("expectation_ref", "recipe_candidate_key", "blueprint_content_hash",
                    "semantic_parameter_binding_hash", "allocation_policy_ref"):
        assert private not in serialized


def test_an_undeclared_v2_shaped_expectation_is_still_refused():
    """Fail-close: the v2 keys are admitted because the payload DECLARES v2, never because the
    keys are present."""
    payload = _v2_payload()
    payload["formula_expectation"].pop("formula_schema_version")
    assert "second_operand_ref" in _refused(payload)


@pytest.mark.parametrize("declared", ["formula-v1", "formula-v3", "", None, 2])
def test_an_unknown_declared_schema_version_is_refused(declared):
    payload = _v2_payload()
    payload["formula_expectation"]["formula_schema_version"] = declared
    assert "unsupported declared formula expectation schema version" in _refused(payload)


def _with_expression(payload: dict[str, Any], **fields: Any) -> dict[str, Any]:
    payload["formula_expectation"]["expressions"][0].update(fields)
    return payload


@pytest.mark.parametrize(("label", "mutate", "message"), [
    ("second operand not a ref",
     lambda p: _with_expression(p, aggregation="date_diff_avg", second_operand_ref="booking_ts"),
     "second_operand_ref is not source-qualified"),
    ("second operand on an aggregate that takes none",
     lambda p: _with_expression(p, second_operand_ref="bank::public.txns.booking_ts"),
     "second_operand_ref disagrees with sum"),
    ("argument on an aggregate that takes none",
     lambda p: _with_expression(p, aggregation_argument=95.0),
     "aggregation_argument: sum takes none"),
    ("percentile argument out of range",
     lambda p: _with_expression(p, aggregation="percentile", aggregation_argument=100),
     "strictly inside (0, 100)"),
    ("percentile argument missing",
     lambda p: _with_expression(p, aggregation="percentile"),
     "strictly inside (0, 100)"),
    ("authority refs not an object",
     lambda p: _with_expression(p, authority_refs=["policy:posted"]),
     "authority_refs must be an object or null"),
    ("authority refs with an unknown key",
     lambda p: _with_expression(p, authority_refs={"leak_policy_ref": "x"}),
     "authority_refs keys differ"),
    ("authority ref carrying prose",
     lambda p: _with_expression(p, authority_refs={
         "status_policy_ref": "posted only, per Alice in risk ops",
         "direction_policy_ref": "", "reversal_policy_ref": "", "currency_conversion_ref": ""}),
     "is not a governed policy identifier"),
    ("authority ref over the bound",
     lambda p: _with_expression(p, authority_refs={
         "status_policy_ref": "p:" + "x" * 200, "direction_policy_ref": "",
         "reversal_policy_ref": "", "currency_conversion_ref": ""}),
     "is not a bounded string"),
    ("wholly blank authority block",
     lambda p: _with_expression(p, authority_refs={
         "status_policy_ref": "", "direction_policy_ref": "", "reversal_policy_ref": "",
         "currency_conversion_ref": ""}),
     "declares no policy at all"),
    ("term name outside a signed sum",
     lambda p: _with_expression(p, term_name="inflow"),
     "names or signs a term outside a signed sum"),
    ("term sign outside a signed sum",
     lambda p: _with_expression(p, term_sign=-1),
     "names or signs a term outside a signed sum"),
    ("term name unbounded",
     lambda p: _with_expression(p, term_name="x" * 129),
     "term_name is not a bounded string"),
    ("term sign not an integer",
     lambda p: _with_expression(p, term_sign="+1"),
     "term_sign must be an integer"),
    ("term sign is a bool",
     lambda p: _with_expression(p, term_sign=True),
     "term_sign must be an integer"),
    ("offset over the bound",
     lambda p: _offset(p, MAX_OFFSET_PERIODS + 1),
     "offset_periods is outside the reviewed bound"),
    ("offset negative", lambda p: _offset(p, -1),
     "offset_periods is outside the reviewed bound"),
    ("offset not an integer", lambda p: _offset(p, "3"),
     "offset_periods is outside the reviewed bound"),
    ("offset is a bool", lambda p: _offset(p, True),
     "offset_periods is outside the reviewed bound"),
])
def test_every_new_v2_key_carries_a_real_bound(label, mutate, message):
    assert message in _refused(mutate(_v2_payload())), label


def _offset(payload: dict[str, Any], value: Any) -> dict[str, Any]:
    payload["formula_expectation"]["expressions"][0]["window"]["offset_periods"] = value
    return payload


def test_the_egress_offset_bound_is_pinned_against_the_grammar():
    """The bound is this module's OWN — stated, not imported — so a grammar that widens must
    widen the boundary deliberately. This test is where the two are reconciled."""
    assert MAX_OFFSET_PERIODS == MAX_WINDOW_OFFSET_PERIODS


@pytest.mark.parametrize(("path", "value", "message"), [
    (("expressions", 0, "aggregation"), "sum_of_squares", "aggregation is not in the closed"),
    (("final_operation",), "weighted_sum", "final_operation is not in the closed"),
    (("expressions", 0, "window", "basis"), "sliding", "window.basis is not in the closed"),
    (("expressions", 0, "window", "unit"), "minute", "window.unit is not in the closed"),
    (("expressions", 0, "window", "empty_window"), "skip",
     "window.empty_window is not in the closed"),
    (("expressions", 0, "window", "null_input"), "drop", "window.null_input is not in the closed"),
    (("expressions", 0, "window", "start_inclusive"), "open",
     "window.start_inclusive is not in the closed"),
    (("decimal", "rounding"), "bankers", "decimal.rounding is not in the closed"),
    (("decimal", "overflow"), "wrap", "decimal.overflow is not in the closed"),
])
def test_the_v2_arm_closes_every_grammar_vocabulary(path, value, message):
    payload = _v2_payload()
    target: Any = payload["formula_expectation"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert message in _refused(payload)


def test_a_signed_sum_term_carries_its_name_and_its_sign():
    payload = _v2_payload()
    expectation = payload["formula_expectation"]
    expectation["final_operation"] = "signed_sum"
    first = expectation["expressions"][0]
    second = copy.deepcopy(first)
    first.update(expression_path="body.terms[0].expr", term_name="inflow", term_sign=1)
    second.update(expression_path="body.terms[1].expr", term_name="outflow", term_sign=-1)
    expectation["expressions"] = [first, second]
    validate_recipe_provider_payload(copy.deepcopy(payload))

    unsigned = copy.deepcopy(payload)
    unsigned["formula_expectation"]["expressions"][1]["term_sign"] = 0
    assert "carries a name and a sign of +1 or -1" in _refused(unsigned)

    lonely = copy.deepcopy(payload)
    lonely["formula_expectation"]["expressions"] = [first]
    assert "at least two terms" in _refused(lonely)


def test_the_v2_arm_still_refuses_the_shared_bounds():
    """The v2 arm is a widening, never a relaxation: every v1 bound still bites."""
    assert "not source-qualified" in _refused(
        _with_expression(_v2_payload(), operand_ref="txn_amt"))
    assert "window_length is outside the reviewed bound" in _refused(
        _with_expression(_v2_payload(), window_length=0))
    payload = _v2_payload()
    payload["formula_expectation"]["grain_key_refs"] = []
    assert "grain_key_refs must be a bounded non-empty list" in _refused(payload)
    payload = _v2_payload()
    payload["formula_expectation"]["decimal"]["scale"] = 99
    assert "decimal precision/scale is invalid" in _refused(payload)
    payload = _v2_payload()
    payload["formula_expectation"]["expressions"][0]["evidence_ids"] = ["ev-1"]
    assert "forbidden recipe egress keys" in _refused(payload)


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
