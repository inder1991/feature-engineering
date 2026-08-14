"""Four banking objectives walked from a confirmed scope to an exact draft, through the ONE engine.

The E4 cutover (2026-08-14) made the SEMANTIC ENGINE the sole candidate source and the V2 recipe
registry the disposition universe. Two things follow for this file:

* the per-objective fixture is now built from the V2 recipe's OPERANDS (concept + operand class),
  not from the legacy template's ``needs`` — those two registries overlap but do not agree, and a
  catalog shaped for the wrong one leaves the hero honestly unbound;
* reaching a DRAFT is no longer a matter of picking a card. The activation fold owns
  ``create_contract``, so each objective must clear the two preconditions a real deployment
  clears: the operand concepts are human-confirmed (not AI-proposed) and the recipe carries a
  current review across the required roles and two distinct identities. That ceremony is the
  point of a coverage test — it proves all four objectives can actually be governed, not just
  displayed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id
from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK
from featuregen.overlay.upload.templates import ALL_TEMPLATES

SCOPE_FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
RANK_FLAG = "FEATUREGEN_INTENT_RANKING"
MODE_FLAG = "FEATUREGEN_SCOPE_EXECUTION_MODE"

#: Review validity requires >= 2 distinct identities across the required roles.
AUTH_2 = {"X-User": "ravi", "X-Roles": "platform_admin"}

#: The ranker's signal bundle (family, explainability, PIT completeness) is authored on the LEGACY
#: template registry, so only a recipe that exists in BOTH registries can be ordered. All four
#: heroes below do; the family map is read for the cap assertion only.
_FAMILY_BY_ID = {template.id: template.family for template in ALL_TEMPLATES}

_SCENARIOS = (
    (
        "credit.monitoring.obligor",
        "obligor_facility_count",
        "monitor obligor exposure across active lending facilities",
        "rank obligors by concentrated facility exposure",
        "fraud.merchant_fraud",
    ),
    (
        "fraud.merchant_fraud",
        "merchant_mcc_diversity",
        "detect merchants whose category activity indicates merchant fraud",
        "predict merchant fraud risk",
        "credit.monitoring.obligor",
    ),
    (
        "treasury_alm.deposit_runoff_forecasting",
        "contractual_deposit_maturity_profile",
        "forecast contractual deposit balances running off by maturity bucket",
        "forecast deposit run-off",
        "treasury_alm.net_interest_margin",
    ),
    (
        "treasury_alm.net_interest_margin",
        "lagged_net_interest_flow",
        "forecast net interest margin from lagged interest income and expense",
        "forecast net interest margin",
        "treasury_alm.deposit_runoff_forecasting",
    ),
)


def _data_type(group: str, pit_role: str) -> str:
    if pit_role != "none":
        return "timestamp"
    if group in {"monetary", "quantity_risk", "regulatory_capital", "accounting"}:
        return "numeric"
    if group == "flag":
        return "boolean"
    return "text"


def _catalog_for_recipe(conn, recipe_id: str, *, omit_role: str | None = None) -> tuple[str, str]:
    """One column per V2 operand of ``recipe_id``, with the concept HUMAN-CONFIRMED.

    The shape comes from the V2 recipe because the V2 recipe is what the engine plans: its
    operands name the concept AND the operand class the binder must satisfy. The confirmations are
    recorded directly rather than through the funnel routes — the funnel has its own end-to-end
    proof in test_e2e_walkthrough, and here the authority floor is a precondition, not the subject.
    """
    recipe = v2_recipe_by_id(recipe_id)
    source = f"objective_{recipe_id}_{omit_role or 'complete'}"
    rows: list[CanonicalRow] = []
    concepts: dict[str, str] = {}
    entity_role = next(
        (operand.role for operand in recipe.operands
         if operand.operand_class == "entity_key"), None)
    for index, operand in enumerate(recipe.operands):
        if operand.role == omit_role:
            continue
        metadata = concept(operand.concept)
        assert metadata is not None, operand.concept
        row = CanonicalRow(
            source=source,
            table="fixture",
            column=f"{index:02d}_{operand.role}_{operand.concept}",
            type=_data_type(metadata.group, metadata.pit_role),
            is_grain=operand.role == entity_role,
            as_of=metadata.pit_role == "as_of",
            additivity=(
                "" if metadata.additivity == "n/a" else metadata.additivity
            ),
            currency="USD" if metadata.group == "monetary" else "",
            entity=metadata.entity_link or "",
        )
        rows.append(row)
        concepts[content_hash(row)] = operand.concept
    if not any(row.as_of for row in rows):
        as_of = CanonicalRow(
            source, "fixture", "decision_as_of", "timestamp", as_of=True)
        rows.append(as_of)
        concepts[content_hash(as_of)] = "as_of_date"
    target = CanonicalRow(
        source, "fixture", "target_outcome", "boolean")
    rows.append(target)
    concepts[content_hash(target)] = "outcome_label"
    build_graph(conn, source, rows, concepts=concepts)
    for row in rows:
        ref = normalize_ref(source, None, row.table, row.column)
        value = concepts[content_hash(row)]
        record_field_evidence(
            conn, logical_ref=ref, field_name="concept", proposed_value=value,
            producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
            producer_ref="human:test", source_snapshot_id="snapshot:test",
            input_hash=field_input_hash(
                logical_ref=ref, field_name="concept", material=value))
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark "
        "(catalog_source,last_completed_at,last_run_id,head_seq) "
        "VALUES (%s,%s,%s,0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at=EXCLUDED.last_completed_at",
        (source, now, f"run-{source}"),
    )
    return source, "public.fixture.target_outcome"


def _record_reviews(client, recipe_id: str) -> None:
    """The recipe-review precondition, through its real route: every required role, at the recipe's
    CURRENT revision hash, across two distinct identities (a single-identity approval is a
    governance violation by design)."""
    recipe = v2_recipe_by_id(recipe_id)
    live_hash = canonical_recipe_v2_hash(recipe)
    identities = (AUTH, AUTH_2)
    for index, role in enumerate(required_reviewer_roles(recipe)):
        response = client.post(
            f"/recipes/{recipe_id}/reviews", headers=identities[index % 2], json={
                "decision": "approved", "reviewer_role": role,
                "reviewed_revision_hash": live_hash,
                "rationale": "the definition matches the banking meaning"})
        assert response.status_code == 201, response.text


def _seal(monkeypatch) -> None:
    """The route harness shares ONE READ COMMITTED transaction, so the catalog seal would skip
    snapshotting and every draft would fail closed with SNAPSHOT_STALE_REGENERATE. Stub the two
    isolation gates so generation seals for real, exactly as the REPEATABLE READ production path
    does (its own suite: test_feature_gen_isolation)."""
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._on_repeatable_read", lambda conn: True)
    monkeypatch.setattr(
        "featuregen.overlay.upload.feature_metadata_snapshot._assert_repeatable_read",
        lambda conn: "repeatable read")


def _client_for(make_client, objective: str, hypothesis: str):
    """The tasks this flow legitimately dispatches.

    ``overlay.feature.recommend`` is deliberately absent — the E4 cutover deleted the free-form
    generator, so an unscripted entry is the proof that nothing dispatches it (FakeLLM raises on an
    unscripted task). ``overlay.feature.intents`` is left unscripted too, for a different reason:
    these four scenarios are about the RECIPE half of the engine, and the intent half fails soft
    (logged, never served), so each objective's fixture stays deterministic.
    """
    return make_client(FakeLLM(script={
        RECOGNIZER_TASK: FakeResponse(output={
            "status": "classified",
            "candidates": [{
                "use_case_id": objective,
                "relationship": "primary",
                "confidence": "high",
                "evidence_spans": [hypothesis],
                "rationale": "the stated banking objective is explicit",
            }],
            "ambiguity_note": None,
        }),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates",
            "reasoning": "the confirmed objective has governed recipes",
        }),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Governed feature derived from the selected recipe bindings.",
        }),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    }))


def _generate(
    client,
    *,
    source: str,
    target_ref: str,
    objective: str,
    hypothesis: str,
    prediction_goal: str,
) -> dict:
    recognition = client.post(
        "/contract/recognitions",
        json={"hypothesis": hypothesis, "objective": prediction_goal},
        headers=AUTH,
    )
    assert recognition.status_code == 200, recognition.text
    recognized = recognition.json()
    assert recognized["candidates"][0]["use_case_id"] == objective

    response = client.post(
        "/contract/considered-set",
        json={
            "hypothesis": hypothesis,
            "objective": prediction_goal,
            "catalog_source": source,
            "target_ref": target_ref,
            "intent_id": recognized["intent_id"],
            "recognition_id": recognized["recognition_id"],
            "confirmed_scope": {
                "primary": objective,
                "confirmation_source": "user_confirmed",
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _recipe_option(body: dict, recipe_id: str) -> dict:
    return next(
        feature
        for group in body["alternatives"]
        for feature in group["features"]
        if (feature.get("recipe_id") or "").split("@")[0] == recipe_id
    )


@pytest.mark.parametrize(
    "objective,recipe_id,hypothesis,prediction_goal,unrelated_objective",
    _SCENARIOS,
)
def test_four_objectives_complete_confirmed_scope_to_exact_draft(
    make_client,
    conn,
    monkeypatch,
    objective,
    recipe_id,
    hypothesis,
    prediction_goal,
    unrelated_objective,
) -> None:
    monkeypatch.setenv(MODE_FLAG, "confirmation_required")
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _seal(monkeypatch)
    source, target_ref = _catalog_for_recipe(conn, recipe_id)
    client = _client_for(make_client, objective, hypothesis)
    _record_reviews(client, recipe_id)

    body = _generate(
        client,
        source=source,
        target_ref=target_ref,
        objective=objective,
        hypothesis=hypothesis,
        prediction_goal=prediction_goal,
    )
    dispositions = {item["recipe_id"]: item for item in body["dispositions"]}
    assert dispositions[recipe_id]["final_disposition"] == "eligible", json.dumps(
        dispositions[recipe_id], indent=2)
    assert dispositions[recipe_id]["relevance_tier"] == "primary"
    # The unrelated objective's recipes are read from the V2 registry — the universe the engine
    # actually planned and the disposition lens actually folds.
    unrelated_ids = {
        recipe.recipe_id
        for recipe in V2_RECIPES
        if recipe.primary_objective == unrelated_objective
    }
    assert unrelated_ids
    assert all(
        dispositions[unrelated]["final_disposition"] == "out_of_scope"
        for unrelated in unrelated_ids
    )

    option = _recipe_option(body, recipe_id)
    assert option["option_id"].startswith("opt_")
    draft = client.post(
        "/contract/draft",
        json={
            "intent_id": body["intent_id"],
            "chosen_source": "alternative",
            "chosen_option_id": option["option_id"],
            "expected_generation_run_id": body["generation_run_id"],
            "why": "governed objective anchor",
        },
        headers=AUTH,
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["snapshot"]["generation_run_id"] == body["generation_run_id"]
    stored = conn.execute(
        "SELECT option_id,generation_run_id FROM contract_gate1_choice_revision "
        "WHERE choice_id=%s",
        (draft.json()["choice_id"],),
    ).fetchone()
    assert stored == (option["option_id"], body["generation_run_id"])

    selected = [
        item for item in body["ranking"] if item["selected_for_initial_view"]]
    assert len(selected) <= 15
    family_counts = {}
    for item in selected:
        family = _FAMILY_BY_ID[item["recipe_id"]]
        family_counts[family] = family_counts.get(family, 0) + 1
    assert max(family_counts.values(), default=0) <= 3
    # No option is served twice. The identity of a served option is its option_id (and the
    # source_definition_id it was minted from) — NOT the (name, aggregation, derives) tuple this
    # once used. The engine serves every authored parameterisation of a recipe as its own card, so
    # the 90-day and 180-day variants legitimately share a name, an aggregation and their columns,
    # and differ only in the window carried on the definition id.
    option_ids = [feature["option_id"]
                  for group in body["alternatives"] for feature in group["features"]]
    assert option_ids and len(option_ids) == len(set(option_ids))
    definition_ids = [feature["source_definition_id"]
                      for group in body["alternatives"] for feature in group["features"]]
    assert len(definition_ids) == len(set(definition_ids))


@pytest.mark.parametrize(
    "objective,recipe_id,hypothesis,prediction_goal,_unrelated",
    _SCENARIOS,
)
def test_four_objective_anchor_names_the_missing_operand_and_is_never_ranked(
    make_client,
    conn,
    monkeypatch,
    objective,
    recipe_id,
    hypothesis,
    prediction_goal,
    _unrelated,
) -> None:
    """Omit one REQUIRED operand and the objective's hero is refused BY NAME.

    Before the E4 cutover the legacy grounding pass reported this as ``unbuildable`` — a verdict
    with nothing in it. The engine's typed gauntlet refuses it as ``safety_rejected`` carrying
    REQUIRED_OPERAND_MISSING, which is the same decision plus the reason a human can act on. Either
    way the recipe never reaches the ranking.
    """
    monkeypatch.setenv(MODE_FLAG, "confirmation_required")
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    recipe = v2_recipe_by_id(recipe_id)
    required = next(operand for operand in recipe.operands if operand.required)
    source, target_ref = _catalog_for_recipe(
        conn, recipe_id, omit_role=required.role)
    body = _generate(
        _client_for(make_client, objective, hypothesis),
        source=source,
        target_ref=target_ref,
        objective=objective,
        hypothesis=hypothesis,
        prediction_goal=prediction_goal,
    )
    disposition = next(
        item for item in body["dispositions"] if item["recipe_id"] == recipe_id)
    assert disposition["final_disposition"] == "safety_rejected", json.dumps(
        disposition, indent=2)
    assert "REQUIRED_OPERAND_MISSING" in disposition["safety"]["reason_codes"]
    assert recipe_id not in {item["recipe_id"] for item in body["ranking"]}
