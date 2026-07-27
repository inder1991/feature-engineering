from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK
from featuregen.overlay.upload.templates import ALL_TEMPLATES

SCOPE_FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
RANK_FLAG = "FEATUREGEN_INTENT_RANKING"
MODE_FLAG = "FEATUREGEN_SCOPE_EXECUTION_MODE"

_BY_ID = {template.id: template for template in ALL_TEMPLATES}
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
    template = _BY_ID[recipe_id]
    source = f"objective_{recipe_id}_{omit_role or 'complete'}"
    rows: list[CanonicalRow] = []
    concepts = {}
    for index, need in enumerate(template.needs):
        if need.role == omit_role:
            continue
        metadata = concept(need.concept)
        assert metadata is not None
        row = CanonicalRow(
            source=source,
            table="fixture",
            column=f"{index:02d}_{need.role}_{need.concept}",
            type=_data_type(metadata.group, metadata.pit_role),
            is_grain=bool(
                metadata.entity_link
                and need.role == template.source_entity_need_role
            ),
            as_of=metadata.pit_role == "as_of",
            additivity=(
                "" if metadata.additivity == "n/a" else metadata.additivity
            ),
            currency="USD" if metadata.group == "monetary" else "",
            entity=metadata.entity_link or "",
        )
        rows.append(row)
        concepts[content_hash(row)] = need.concept
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
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark "
        "(catalog_source,last_completed_at,last_run_id,head_seq) "
        "VALUES (%s,%s,%s,0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at=EXCLUDED.last_completed_at",
        (source, now, f"run-{source}"),
    )
    return source, "public.fixture.target_outcome"


def _client_for(make_client, objective: str, hypothesis: str):
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
        "overlay.feature.recommend": FakeResponse(output={"features": []}),
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
        if feature.get("recipe_id") == recipe_id
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
    source, target_ref = _catalog_for_recipe(conn, recipe_id)
    client = _client_for(make_client, objective, hypothesis)

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
    unrelated_ids = {
        template.id
        for template in ALL_TEMPLATES
        if template.primary_objective == unrelated_objective
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
    semantic_keys = [
        (
            feature["name"],
            feature["aggregation"],
            tuple(feature["derives_from"]),
        )
        for group in body["alternatives"]
        for feature in group["features"]
    ]
    assert len(semantic_keys) == len(set(semantic_keys))


@pytest.mark.parametrize(
    "objective,recipe_id,hypothesis,prediction_goal,_unrelated",
    _SCENARIOS,
)
def test_four_objective_anchor_is_unbuildable_when_required_concept_is_missing(
    make_client,
    conn,
    monkeypatch,
    objective,
    recipe_id,
    hypothesis,
    prediction_goal,
    _unrelated,
) -> None:
    monkeypatch.setenv(MODE_FLAG, "confirmation_required")
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    template = _BY_ID[recipe_id]
    required = next(need for need in template.needs if not need.optional)
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
    assert disposition["final_disposition"] == "unbuildable"
    assert recipe_id not in {item["recipe_id"] for item in body["ranking"]}
