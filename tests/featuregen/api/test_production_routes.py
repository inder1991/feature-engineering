"""§9's two production endpoints: honest refusal TODAY, the full machine THE DAY POLICY OPENS.

The first family proves the development posture (§0.1.0): both acts refuse as ACTION_UNAVAILABLE
through the one decision service, record nothing, and the refusal CARRIES the per-member
certificate answer — "how far is this feature from production" served rather than estimated.
The second family opens the policy the way the owner one day will (the availability set) and
proves the SAME request then records the attempt, the decision, and the certificate bindings.
"""
from __future__ import annotations

import pytest

MAT = "/feature-execution/production-materializations"
PUB = "/feature-execution/production-publications"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")


@pytest.fixture
def engineer_headers():
    return {"X-User": "sam", "X-Roles": "feature_engineer"}


def _sealed(conn, tag: str, *, with_identity: bool = True) -> str:
    from tests.featuregen.materialize.test_production_boundary import _sealed_member

    return _sealed_member(conn, tag, with_identity=with_identity)


# ══ TODAY — the development posture ═════════════════════════════════════════════════════════════
def test_MATERIALIZE_PRODUCTION_REFUSES_AS_UNAVAILABLE_and_records_nothing(
        client, conn, engineer_headers):
    artifact = _sealed(conn, "prod-un")

    response = client.post(MAT, json={
        "sealed_artifact_id": artifact, "environment_id": "hdfc-local",
        "logical_group_name": "grp-prod-un"}, headers=engineer_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "ACTION_UNAVAILABLE"
    # ▲ The refusal CARRIES the per-member answer — §21's "how far from production", served.
    member = detail["per_member"][0]
    assert member["member_name"] == "m1"
    assert [b["code"] for b in member["blockers"]] == ["METHOD_CERTIFICATE_MISSING"]
    for table in ("production_materialization_attempt", "action_decision_revision"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table


def test_PUBLISH_PRODUCTION_requires_a_SUCCEEDED_materialization_before_anything_else(
        client, conn, engineer_headers):
    response = client.post(PUB, json={"materialization_attempt_id": "pma-none"},
                           headers=engineer_headers)
    assert response.status_code == 404


def test_an_unrecorded_member_identity_surfaces_UNRECORDED_never_a_backfill(
        client, conn, engineer_headers):
    artifact = _sealed(conn, "prod-unrec", with_identity=False)
    response = client.post(MAT, json={
        "sealed_artifact_id": artifact, "environment_id": "hdfc-local",
        "logical_group_name": "grp-prod-unrec"}, headers=engineer_headers)
    member = response.json()["detail"]["per_member"][0]
    assert [b["code"] for b in member["blockers"]] == ["METHOD_IDENTITY_UNRECORDED"]


# ══ THE DAY THE POLICY OPENS — the same request, permitted ══════════════════════════════════════
@pytest.fixture
def policy_open(monkeypatch):
    """Open both production acts the way the owner one day will: by the availability set. The
    ONE function object `action_available` reads this global, and both its consumers imported
    the function — so patching the set opens authorize AND decide together."""
    from featuregen.materialize import action_authorization

    monkeypatch.setattr(action_authorization, "_PRODUCTION_ACTIONS", frozenset())


def _certify(conn, tag: str) -> None:
    conn.execute(
        "INSERT INTO method_certificate_revision (certificate_revision_id, certificate_kind, "
        "subject_identity_kind, subject_identity_hash, contract_hash, corpus_hash, outcome) "
        "VALUES (%s, 'AUTHORING_METHOD', 'AUTHORING_METHOD', %s, 'ch', 'co', 'CERTIFIED')",
        (f"mcr-{tag}", f"mih-{tag}"))


def test_THE_SAME_REQUEST_RECORDS_EVERYTHING_once_permitted(
        client, conn, engineer_headers, policy_open):
    artifact = _sealed(conn, "prod-go")
    _certify(conn, "prod-go")

    response = client.post(MAT, json={
        "sealed_artifact_id": artifact, "environment_id": "hdfc-local",
        "logical_group_name": "grp-prod-go"}, headers=engineer_headers)

    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    assert response.json()["created"] is True

    row = conn.execute(
        "SELECT status, action_decision_revision_id FROM production_materialization_attempt "
        "WHERE attempt_id = %s", (attempt_id,)).fetchone()
    assert row[0] == "REQUESTED" and row[1]
    # ▲ §10.3: the certificate BINDING recorded on the attempt — publication compares against
    # THIS row, never a re-derivation.
    binding = conn.execute(
        "SELECT certificate_revision_id, subject_identity_hash, method_artifact_id "
        "FROM production_attempt_member_certificate WHERE attempt_id = %s",
        (attempt_id,)).fetchone()
    assert binding == ("mcr-prod-go", "mih-prod-go", artifact)


def test_a_member_without_a_certificate_still_REFUSES_once_permitted(
        client, conn, engineer_headers, policy_open):
    """Opening the policy does not weaken the boundary: the §9 hard-block holds through the
    member facts — absence never became permission."""
    artifact = _sealed(conn, "prod-nocert")
    response = client.post(MAT, json={
        "sealed_artifact_id": artifact, "environment_id": "hdfc-local",
        "logical_group_name": "grp-prod-nocert"}, headers=engineer_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ACTION_REFUSED"
    assert conn.execute(
        "SELECT COUNT(*) FROM production_materialization_attempt").fetchone() == (0,)


def test_publication_resolves_the_output_SERVER_SIDE_and_publishes_nothing_yet(
        client, conn, engineer_headers, policy_open):
    """The request names the ATTEMPT; the output comes from the composite FK's parent. The
    worker (step-0b substrate) is what would drive it to PUBLISHED — recording is not swapping."""
    from featuregen.overlay.upload.production_attempt_store import (
        MaterializationStatusV1,
        advance_materialization,
        current_active_revision,
        record_output_revision,
    )

    artifact = _sealed(conn, "prod-pub")
    _certify(conn, "prod-pub")
    accepted = client.post(MAT, json={
        "sealed_artifact_id": artifact, "environment_id": "hdfc-local",
        "logical_group_name": "grp-prod-pub"}, headers=engineer_headers).json()
    attempt_id = accepted["attempt_id"]
    for move in ((MaterializationStatusV1.REQUESTED, MaterializationStatusV1.CLAIMED),
                 (MaterializationStatusV1.CLAIMED, MaterializationStatusV1.RUNNING),
                 (MaterializationStatusV1.RUNNING, MaterializationStatusV1.STAGED),
                 (MaterializationStatusV1.STAGED, MaterializationStatusV1.SUCCEEDED)):
        advance_materialization(conn, attempt_id, *move,
                                staging_path="staging/x/1/"
                                if move[1] is MaterializationStatusV1.STAGED else None)
    output = record_output_revision(conn, attempt_id=attempt_id,
                                    manifest={"t": "prod-pub"}, row_count=3)

    response = client.post(PUB, json={"materialization_attempt_id": attempt_id},
                           headers=engineer_headers)

    assert response.status_code == 202, response.text
    publication = conn.execute(
        "SELECT output_revision_id, status FROM production_publication_attempt "
        "WHERE attempt_id = %s", (response.json()["attempt_id"],)).fetchone()
    assert publication == (output, "REQUESTED")
    assert current_active_revision(
        conn, environment_id="hdfc-local", logical_group_name="grp-prod-pub") is None, (
        "recording an attempt is not swapping the pointer — the worker's CAS is")


def test_what_is_out_there_right_now_answers_honestly_absent(client, engineer_headers):
    response = client.get("/feature-execution/production-active",
                          params={"environment_id": "hdfc-local",
                                  "logical_group_name": "grp-empty"},
                          headers=engineer_headers)
    assert response.status_code == 200
    assert response.json()["active"] is None
