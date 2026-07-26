from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_1b_rollout import (
    CHURN,
    HYPOTHESIS,
    TARGET,
    _bank_multi,
    _fake,
    _recognizer,
)


def _body(**overrides):
    body = {
        "hypothesis": "customers churn when transaction activity declines",
        "objective": "predict churn",
        "catalog_source": "bank",
    }
    body.update(overrides)
    return body


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
    assert response.json()["generation_run_id"]
    assert response.json()["in_scope_count"] > 0


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
