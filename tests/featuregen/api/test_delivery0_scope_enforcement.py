import psycopg
import pytest
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_1b_rollout import (
    CHURN,
    HYPOTHESIS,
    TARGET,
    _bank_multi,
    _fake,
    _recognizer,
)

from featuregen.intake.llm import FakeLLM, FakeResponse


def _body(**overrides):
    body = {
        "hypothesis": "customers churn when transaction activity declines",
        "objective": "predict churn",
        "catalog_source": "bank",
    }
    body.update(overrides)
    return body


def _generation_and_draft_llm() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_90d",
            "description": "average balance",
            "derives_from": ["public.accounts.balance"],
            "aggregation": "avg_90d",
            "grain_table": "accounts",
        }]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary",
            "reasoning": "fits the confirmed churn objective",
        }),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average balance over 90 days.",
        }),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def test_release_mode_rejects_missing_confirmed_scope(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    response = make_client().post("/contract/considered-set", json=_body(), headers=AUTH)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCOPE_CONFIRMATION_REQUIRED"


def test_release_mode_rejects_empty_confirmed_scope(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    response = make_client().post(
        "/contract/considered-set",
        json=_body(confirmed_scope={"confirmation_source": "user_confirmed"}),
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "confirmed scope requires at least one selectable use-case"


def test_release_mode_rejects_scoped_request_without_recognition_lineage(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    response = make_client().post(
        "/contract/considered-set",
        json=_body(confirmed_scope={
            "primary": "customer.relationship_attrition.churn",
            "confirmation_source": "user_confirmed",
        }),
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "reference its recognition" in response.json()["detail"]


def test_release_mode_rejects_untrusted_confirmation_source(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    response = make_client().post(
        "/contract/considered-set",
        json=_body(confirmed_scope={
            "primary": CHURN,
            "confirmation_source": "automatic",
        }),
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "user_confirmed" in response.json()["detail"]


def test_release_mode_rejects_unlinked_or_implicit_unscoped_request(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    client = make_client()

    implicit = client.post(
        "/contract/considered-set",
        json=_body(confirmed_scope={"unscoped": True, "confirmation_source": "user_confirmed"}),
        headers=AUTH,
    )
    unlinked = client.post(
        "/contract/considered-set",
        json=_body(confirmed_scope={"unscoped": True, "confirmation_source": "broaden"}),
        headers=AUTH,
    )

    assert implicit.status_code == 422
    assert "explicit broaden" in implicit.json()["detail"]
    assert unlinked.status_code == 422
    assert "reference a recognition or prior confirmed scope" in unlinked.json()["detail"]


def test_scope_mode_endpoint_reports_release_authority(make_client, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    response = make_client().get("/contract/scope-mode", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "mode": "confirmation_required",
        "confirmation_required": True,
        "configuration_valid": True,
    }


def test_release_mode_accepts_owned_recognition_then_confirmed_scope(
        make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    _bank_multi(conn)
    recognition = make_client(_recognizer()).post(
        "/contract/recognitions",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers=AUTH,
    )
    assert recognition.status_code == 200

    rec = recognition.json()
    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            "hypothesis": HYPOTHESIS,
            "objective": "predict churn",
            "catalog_source": "bank",
            "target_ref": TARGET,
            "intent_id": rec["intent_id"],
            "recognition_id": rec["recognition_id"],
            "confirmed_scope": {
                "primary": CHURN,
                "confirmation_source": "user_confirmed",
                "use_case_origins": {CHURN: "llm_proposed"},
            },
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    run_id = response.json()["generation_run_id"]
    assert run_id
    assert response.json()["in_scope_count"] > 0
    sealed = conn.execute(
        "SELECT redacted_hypothesis, redacted_prediction_goal, target_ref, "
        "recognition_input_content_hash, generation_input_content_hash "
        "FROM contract_generation_input WHERE generation_run_id = %s",
        (run_id,),
    ).fetchone()
    assert sealed is not None
    assert sealed[:3] == (HYPOTHESIS, "predict churn", TARGET)
    assert sealed[3] and sealed[4]
    # The old intent-level target remains NULL because the run-specific record is now authoritative.
    assert conn.execute(
        "SELECT target_ref FROM contract_intent WHERE intent_id = %s",
        (rec["intent_id"],),
    ).fetchone()[0] is None


def test_release_mode_rejects_changed_recognition_text_before_minting_run(
        make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    recognition = make_client(_recognizer()).post(
        "/contract/recognitions",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers=AUTH,
    ).json()
    runs_before = conn.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0]

    for changed in (
        {"hypothesis": "customers default when payments stop", "objective": "predict churn"},
        {"hypothesis": HYPOTHESIS, "objective": "predict fraud"},
    ):
        response = make_client(_fake()).post(
            "/contract/considered-set",
            json={
                **changed,
                "catalog_source": "bank",
                "intent_id": recognition["intent_id"],
                "recognition_id": recognition["recognition_id"],
                "confirmed_scope": {
                    "primary": CHURN,
                    "confirmation_source": "user_confirmed",
                },
            },
            headers=AUTH,
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "RECOGNITION_INPUT_CHANGED"

    assert conn.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0] == runs_before


def test_release_mode_draft_reads_target_from_exact_generation_run(
        make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    _bank_multi(conn)
    recognition = make_client(_recognizer()).post(
        "/contract/recognitions",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers=AUTH,
    ).json()
    client = make_client(_generation_and_draft_llm())
    generation_payload = {
        "hypothesis": HYPOTHESIS,
        "objective": "predict churn",
        "catalog_source": "bank",
        "target_ref": TARGET,
        "intent_id": recognition["intent_id"],
        "recognition_id": recognition["recognition_id"],
        "confirmed_scope": {
            "primary": CHURN,
            "confirmation_source": "user_confirmed",
        },
    }
    generated = client.post(
        "/contract/considered-set", json=generation_payload, headers=AUTH)
    assert generated.status_code == 200, generated.text
    body = generated.json()
    chosen = next(
        feature
        for feature_set in body["alternatives"]
        for feature in feature_set["features"]
    )
    newer = client.post(
        "/contract/considered-set", json=generation_payload, headers=AUTH)
    assert newer.status_code == 200, newer.text
    assert newer.json()["generation_run_id"] != body["generation_run_id"]
    assert next(
        feature
        for feature_set in newer.json()["alternatives"]
        for feature in feature_set["features"]
    )["option_id"] != chosen["option_id"]
    newer_chosen = next(
        feature
        for feature_set in newer.json()["alternatives"]
        for feature in feature_set["features"]
    )
    wrong_run = client.post(
        "/contract/draft",
        json={
            "intent_id": recognition["intent_id"],
            "chosen_source": "alternative",
            "chosen_option_id": newer_chosen["option_id"],
            "expected_generation_run_id": body["generation_run_id"],
        },
        headers=AUTH,
    )
    assert wrong_run.status_code == 409

    # Run B is now the mutable latest pointer. Drafting run A still resolves its exact option and target;
    # changing the mutable legacy intent target cannot alter run A's leakage authority either.
    conn.execute(
        "UPDATE contract_intent SET target_ref = 'public.labels.forged' WHERE intent_id = %s",
        (recognition["intent_id"],),
    )
    drafted = client.post(
        "/contract/draft",
        json={
            "intent_id": recognition["intent_id"],
            "chosen_source": "alternative",
            "chosen_option_id": chosen["option_id"],
            "expected_generation_run_id": body["generation_run_id"],
        },
        headers=AUTH,
    )
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["draft"]["target_ref"] == TARGET
    assert drafted.json()["snapshot"]["generation_run_id"] == body["generation_run_id"]
    choice_id = drafted.json()["choice_id"]
    assert choice_id
    stored_choice = conn.execute(
        "SELECT generation_run_id, option_id FROM contract_gate1_choice_revision "
        "WHERE choice_id = %s",
        (choice_id,),
    ).fetchone()
    assert stored_choice == (body["generation_run_id"], chosen["option_id"])
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute(
            "UPDATE contract_gate1_choice_revision SET why = 'changed' WHERE choice_id = %s",
            (choice_id,),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute(
            "DELETE FROM contract_gate1_choice_revision WHERE choice_id = %s",
            (choice_id,),
        )

    contract_body = {
        **drafted.json()["draft"],
        "intent_id": recognition["intent_id"],
        "choice_id": choice_id,
    }
    wrong_actor = client.post(
        "/contract/confirm",
        json=contract_body,
        headers={"X-User": "another-user", "X-Roles": "platform_admin"},
    )
    assert wrong_actor.status_code == 422
    confirmed = client.post("/contract/confirm", json=contract_body, headers=AUTH)
    assert confirmed.status_code == 200, confirmed.text
    bound_snapshot = conn.execute(
        "SELECT metadata_snapshot_id FROM contract WHERE contract_id = %s",
        (confirmed.json()["contract_id"],),
    ).fetchone()[0]
    assert bound_snapshot == drafted.json()["snapshot"]["snapshot_id"]


def test_release_mode_rejects_legacy_unsealed_recognition(
        make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    intent_id = "legacy_unsealed_intent"
    recognition_id = "legacy_unsealed_recognition"
    conn.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, redacted_hypothesis, intake_mode, actor) "
        "VALUES (%s, %s, %s, 'hypothesis', %s::jsonb)",
        (intent_id, HYPOTHESIS, HYPOTHESIS, '"user:tester"'),
    )
    conn.execute(
        "INSERT INTO intent_recognition_attempt "
        "(recognition_id, intent_id, input_hash, status, taxonomy_version, "
        "applicability_mapping_version, recognizer_model_id, prompt_version, "
        "recipe_registry_version, created_by) "
        "VALUES (%s, %s, 'legacy-hash', 'classified', 't', 'a', 'm', 'p', 'r', '{}'::jsonb)",
        (recognition_id, intent_id),
    )

    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            "hypothesis": HYPOTHESIS,
            "objective": "predict churn",
            "catalog_source": "bank",
            "intent_id": intent_id,
            "recognition_id": recognition_id,
            "confirmed_scope": {
                "primary": CHURN,
                "confirmation_source": "user_confirmed",
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "RECOGNITION_INPUT_UNAVAILABLE"


def test_release_mode_rejects_another_actors_recognition(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    recognition = make_client(_recognizer()).post(
        "/contract/recognitions",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers={"X-User": "alice", "X-Roles": "platform_admin"},
    ).json()

    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            **_body(),
            "intent_id": recognition["intent_id"],
            "recognition_id": recognition["recognition_id"],
            "confirmed_scope": {
                "primary": CHURN,
                "confirmation_source": "user_confirmed",
            },
        },
        headers={"X-User": "bob", "X-Roles": "platform_admin"},
    )

    assert response.status_code == 404
