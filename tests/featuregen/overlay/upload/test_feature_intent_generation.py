"""SE-6 — abstract intent generation: physically blind in, strictly parsed out, per-item honest."""
from __future__ import annotations

import json

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_intent_generation import (
    INTENT_GENERATION_UNAVAILABLE,
    INTENT_MODEL_SPEC_NOT_OFFERED,
    INTENT_OBJECTIVE_OUT_OF_SCOPE,
    INTENT_REJECTED_PARSE,
    INTENT_VOCABULARY_GAP,
    generate_feature_intents,
    semantic_capability_inventory,
)
from featuregen.overlay.upload.generation_semantic_context import (
    build_generation_semantic_context,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

SOURCE = "intentbank"
EXEMPLAR = v2_recipe_by_id("net_transaction_flow")
SCOPE = frozenset({EXEMPLAR.primary_objective})


def _seed(db) -> None:
    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when booked"), "event_timestamp"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _wire_intent(**over) -> dict:
    base = {
        "display_name": "Net transaction flow (model)",
        "business_definition": "Signed net of inflows and outflows over the window.",
        "primary_objective": EXEMPLAR.primary_objective,
        "computation_kind": "deterministic_formula",
        "operation_class": "sum",
        "output_grain_entity": "account",
        "source_grain": "transaction",
        "output": {
            "output_id": "net_flow_model", "display_label": "Net transaction flow",
            "output_type": "numeric", "additivity": "additive", "unit_kind": "monetary",
            "currency_policy": "account reporting currency via governed conversion",
            "null_input_policy": "null amounts are excluded and counted",
            "empty_population_policy": "zero with populated flag",
        },
        "operands": [
            {"role": "account", "concept": "account_id", "operand_class": "entity_key"},
            {"role": "amount", "concept": "monetary_flow", "operand_class": "measure"},
            {"role": "event_ts", "concept": "event_timestamp",
             "operand_class": "event_timestamp"},
        ],
        "temporal": {"anchor_kind": "event", "window_basis": "event time",
                     "window_unit": "days", "cutoff_inclusivity": "inclusive"},
        "rationale": "declining net flow precedes dormancy",
    }
    base.update(over)
    return base


def _run(db, script_output, **kwargs):
    _seed(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    client = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=script_output)})
    return generate_feature_intents(
        db, client, context=context, scope_leaves=SCOPE,
        confirmed_scope_hash="scope-hash-test",
        redacted_hypothesis="declining activity precedes dormancy", **kwargs), context


def test_a_valid_intent_parses_with_our_provenance_never_the_models(db):
    result, context = _run(db, {"intents": [_wire_intent()]})
    assert result.rejections == ()
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.operation_class == "sum"
    # Provenance is the CALL's, tied to the frozen context — whatever the model wrote is gone.
    # B3: the scope hash is the SCOPE's identity; the catalog context hash is its own key.
    assert intent.generation_provenance.confirmed_scope_hash == "scope-hash-test"
    assert intent.generation_provenance.semantic_context_hash == context.context_hash()
    assert intent.generation_provenance.output_schema_version == "feature_intents@2"


def test_a_malformed_sibling_never_fails_the_batch(db):
    bad = _wire_intent(operands=[
        {"role": "amount", "concept": "definitely_not_a_concept",
         "operand_class": "measure"}])
    result, _ = _run(db, {"intents": [bad, _wire_intent()]})
    assert len(result.intents) == 1
    assert len(result.rejections) == 1
    assert result.rejections[0]["code"] == INTENT_REJECTED_PARSE
    assert result.rejections[0]["index"] == 0


def test_an_out_of_scope_objective_is_rejected_before_anything_binds(db):
    stray = _wire_intent(primary_objective="fraud.transaction_fraud_detection")
    result, _ = _run(db, {"intents": [stray]})
    assert result.intents == ()
    assert result.rejections[0]["code"] == INTENT_OBJECTIVE_OUT_OF_SCOPE


def test_an_uninvited_model_spec_is_rejected_as_ungrounded(db):
    invented = _wire_intent(
        computation_kind="governed_model_output", operation_class="",
        model_feature_ref="totally_new_model")
    result, _ = _run(db, {"intents": [invented]}, model_feature_refs=("churn_probability",))
    assert result.intents == ()
    assert result.rejections[0]["code"] == INTENT_MODEL_SPEC_NOT_OFFERED


def test_the_inventory_is_physically_blind_and_bounded(db):
    _seed(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    inventory = semantic_capability_inventory(
        context, scope_leaves=SCOPE, model_feature_refs=("churn_probability",))
    serialized = json.dumps(inventory)
    assert "public." not in serialized                        # no object refs, ever
    assert "transactions" not in serialized                   # no table names either
    assert {c["concept"] for c in inventory["concepts"]} == {
        "account_id", "monetary_flow", "event_timestamp"}
    assert inventory["objectives"] == [EXEMPLAR.primary_objective]
    assert "sum" in inventory["operation_classes"]
    assert inventory["concepts_truncated"] == 0


def test_no_validated_output_is_an_honest_unavailable_never_a_crash(db):
    result, _ = _run(db, {"wrong_shape": True})               # schema-invalid: repair exhausts
    assert result.intents == ()
    assert result.rejections[0]["code"] == INTENT_GENERATION_UNAVAILABLE


def test_a_malformed_output_spec_rejects_the_item_never_the_batch(db):
    """SE-6 wire-up regression: the typed spec constructors raise RecipeContractError (not the
    PlanningContractError base) — a malformed output on ONE proposed intent must become that
    item's INTENT_REJECTED_PARSE rejection while a valid sibling still parses.

    T1 moved which defect proves it. An off-vocabulary `unit_kind` is now a NAMED vocabulary gap
    (below), so the malformed case is a CROSS-FIELD one instead: every vocabulary is in order and
    a monetary output still owes a currency policy — exactly the class no JSON Schema can
    express, which is why the per-item parse seam still has to be the one that says so."""
    bad = _wire_intent(output={
        "output_id": "unpriced", "display_label": "Unpriced amount", "output_type": "numeric",
        "additivity": "additive", "unit_kind": "monetary",     # …with no currency policy
        "null_input_policy": "nulls excluded and counted",
        "empty_population_policy": "zero with populated flag",
    })
    result, _ = _run(db, {"intents": [bad, _wire_intent()]})
    assert len(result.intents) == 1                            # the valid sibling survived
    assert len(result.rejections) == 1
    assert result.rejections[0]["code"] == "INTENT_REJECTED_PARSE"
    assert "currency policy" in result.rejections[0]["detail"]


# ── T1: the vocabulary contract at the seam ────────────────────────────────────────────────────

def test_a_recorded_misspelling_is_repaired_and_the_repair_is_recorded(db):
    """`unit_kind: days` was one of the eight refusals on the 2026-08-24 AML run. The v2 wire
    schema stops a compliant provider spelling it again; the seam still recovers it — and says so,
    because an intent served differently from what the model wrote must be able to show where."""
    recorded = _wire_intent(output={
        "output_id": "tenure", "display_label": "Relationship tenure", "output_type": "numeric",
        "additivity": "non_additive", "unit_kind": "days",
        "null_input_policy": "unknown origination returns null",
        "empty_population_policy": "no relationship history returns null",
    }, operation_class="recency")
    result, _ = _run(db, {"intents": [recorded]})
    assert result.rejections == (), result.rejections
    assert result.intents[0].output.unit_kind == "duration_days"
    assert result.normalizations == ({"index": 0, "field": "output.unit_kind", "from": "days",
                                      "to": "duration_days",
                                      "reason": "the governed spelling of this unit"},)


def test_the_lens_hands_the_repairs_on_beside_the_rejections(db):
    """The trace must LEAVE the seam. `llm_intent_candidates` is what gate1 and assist call, so
    if the repairs stop here nobody downstream can ever say a served card's wording was edited.
    Rendering it is T9's call; being ABLE to is this one's."""
    from featuregen.overlay.upload.recipe_planning_lens import llm_intent_candidates

    _seed(db)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    recorded = _wire_intent(output={
        "output_id": "tenure", "display_label": "Relationship tenure", "output_type": "numeric",
        "additivity": "non_additive", "unit_kind": "days",
        "null_input_policy": "unknown origination returns null",
        "empty_population_policy": "no relationship history returns null",
    }, operation_class="recency")
    client = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output={"intents": [recorded]})})
    candidates, rejections, normalizations = llm_intent_candidates(
        db, client, context=context, scope_leaves=SCOPE,
        confirmed_scope_hash="scope-hash-test",
        redacted_hypothesis="declining activity precedes dormancy")
    assert rejections == [], rejections
    assert len(candidates) == 1
    assert [(n["index"], n["field"], n["from"], n["to"]) for n in normalizations] == [
        (0, "output.unit_kind", "days", "duration_days")]


def test_an_invented_concept_beside_a_bad_class_is_a_gap_and_the_sibling_survives(db):
    """The population that invents an operand-class word is the population that invents concept
    names, so this pair is ordinary traffic — not an edge case. It must stay a PER-ITEM refusal:
    the registry answers None for an unknown name, the resolver reads that as "determines
    nothing", and the batch's clean sibling is served exactly as it would have been."""
    invented = _wire_intent(display_name="Invented operand", operands=[
        {"role": "who", "concept": "not_a_registered_concept", "operand_class": "attribute"}])
    result, _ = _run(db, {"intents": [invented, _wire_intent(display_name="Clean twin")]})
    assert len(result.intents) == 1
    assert result.intents[0].display_name == "Clean twin"
    assert len(result.rejections) == 1
    assert result.rejections[0]["code"] == INTENT_VOCABULARY_GAP
    assert "'attribute'" in result.rejections[0]["detail"]
    assert "not_a_registered_concept" in result.rejections[0]["detail"]


def test_a_repair_is_logged_where_an_operator_watching_a_live_run_can_see_it(db, caplog):
    """The trace on the result is for callers; this line is for whoever is reading logs while the
    run happens. Both, because a repair nobody can see is a silent edit of the model's answer."""
    import logging

    recorded = _wire_intent(output={
        "output_id": "tenure", "display_label": "Relationship tenure", "output_type": "numeric",
        "additivity": "non_additive", "unit_kind": "days",
        "null_input_policy": "unknown origination returns null",
        "empty_population_policy": "no relationship history returns null",
    }, operation_class="recency")
    with caplog.at_level(logging.INFO,
                         logger="featuregen.overlay.upload.feature_intent_generation"):
        _run(db, {"intents": [recorded]})
    logged = [r.getMessage() for r in caplog.records if "vocabulary repair" in r.getMessage()]
    assert len(logged) == 1, logged
    assert "output.unit_kind" in logged[0]
    assert "'days'" in logged[0] and "'duration_days'" in logged[0]


def test_a_vocabulary_gap_is_named_per_item_never_filed_as_malformed(db):
    """`output_type: categorical` is not a malformed answer — an ordinal rating is a real feature
    shape OUTPUT_TYPES has no entry for, and adding one is an owner's decision. The item refuses
    under its own code, naming the entry; the clean sibling is untouched."""
    gapped = _wire_intent(display_name="Risk rating snapshot", output={
        "output_id": "risk_rating", "display_label": "Risk rating", "output_type": "categorical",
        "additivity": "non_additive", "unit_kind": "rating_scale",
        "null_input_policy": "unrated customers return null",
        "empty_population_policy": "no rating history returns null",
    })
    result, _ = _run(db, {"intents": [gapped, _wire_intent()]})
    assert len(result.intents) == 1                            # the clean sibling survived
    assert result.normalizations == ()
    assert result.rejections[0]["code"] == INTENT_VOCABULARY_GAP
    assert "vocabulary gap" in result.rejections[0]["detail"]
    assert "'categorical'" in result.rejections[0]["detail"]
    assert "owner decision" in result.rejections[0]["detail"]


# ── C8: model prose naming physical objects is a per-item parse failure ────────────────────────

def test_prose_naming_a_physical_column_is_rejected_per_item(db):
    """C8's acceptance: a rationale saying "use transactions.acct_ref" names a physical
    object the physically-blind inventory never showed — that ITEM rejects; a clean sibling
    in the same batch survives."""
    dirty = _wire_intent(rationale="use transactions.acct_ref for the account leg")
    clean = _wire_intent(display_name="Clean twin")
    result, _ = _run(db, {"intents": [dirty, clean]})
    assert len(result.intents) == 1
    assert result.intents[0].display_name == "Clean twin"
    assert any(r["code"] == INTENT_REJECTED_PARSE
               and "physical objects" in r["detail"]
               and "transactions.acct_ref" in r["detail"]
               for r in result.rejections)


def test_a_bare_physical_column_name_in_the_definition_is_caught_too(db):
    dirty = _wire_intent(
        business_definition="Signed net of acct_ref grouped inflows over the window.")
    result, _ = _run(db, {"intents": [dirty]})
    assert result.intents == ()
    assert any("acct_ref" in r["detail"] for r in result.rejections)


def test_plain_english_prose_never_false_positives(db):
    """The boundedness half: table names that are ordinary words ("transactions") never fire
    on ordinary prose — only physical-looking tokens (underscore/dot) are candidates."""
    clean = _wire_intent(
        business_definition="Net of transactions in and out of the account over the window.",
        rationale="account activity and transactions volume precede dormancy")
    result, _ = _run(db, {"intents": [clean]})
    assert len(result.intents) == 1, result.rejections
