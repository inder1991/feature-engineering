"""Delivery C0 Task 5 — the metadata snapshot lineage over the HTTP contract flow.

Two route-level guarantees on top of the builder unit tests (test_considered_set_snapshot.py):
  * /contract/draft + /contract/confirm reload the SERVER-persisted snapshot lineage and NEVER trust a
    client-supplied snapshot id (the request models carry none) — the draft response carries the server
    value even when the request body smuggles a decoy id, and the draft→confirm flow still registers a
    contract (Slice-3 flow unbroken);
  * a projection-lagged catalog surfaces as 503 CATALOG_PROJECTION_UNAVAILABLE (feature generation
    aborts rather than proceeding on a stale projected view).

The shared API test conn is READ COMMITTED, so the builder itself takes no snapshot here (covered under
REPEATABLE READ in the builder suite); the server considered-set row is seeded with a lineage to prove
the draft/confirm RELOAD wiring reads the server value.
"""
import psycopg
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_90d", "description": "avg balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
            "grain_table": "accounts"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits the hypothesis"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average 90-day end-of-day ledger balance per account."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def _intent_id(client) -> str:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "definition": "90-day average balance per account",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    return res.json()["intent_id"]


def test_draft_reloads_server_lineage_and_ignores_client_snapshot_id(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    # Seed the SERVER considered-set row with a C0 snapshot lineage (as an RR feature-gen run would).
    conn.execute(
        "UPDATE contract_considered SET generation_run_id = %s, snapshot_id = %s, "
        "snapshot_content_hash = %s WHERE intent_id = %s",
        ("fgr_server", "snap_server", "sha256:server", intent_id))

    # The draft request model carries no snapshot id; a smuggled decoy field is simply ignored (Pydantic
    # drops it) — the response carries the SERVER value reloaded from the considered-set row.
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": "best fit",
        "snapshot_id": "snap_CLIENT_FORGED"}, headers=AUTH)
    assert dr.status_code == 200
    assert dr.json()["snapshot"] == {
        "generation_run_id": "fgr_server", "snapshot_id": "snap_server",
        "content_hash": "sha256:server"}

    # Slice-3 flow unbroken: draft → confirm still registers a versioned contract.
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200
    assert cr.json()["version"] == 1
    assert cr.json()["feature_id"].startswith("feat")


def test_draft_snapshot_is_null_when_no_server_lineage(make_client):
    # A pre-C0 / READ COMMITTED considered set records no lineage — draft honestly reports null (additive).
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200
    assert dr.json()["snapshot"] is None


def test_considered_set_projection_unavailable_returns_503(make_client, monkeypatch):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)

    def _lagged(*a, **k):
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            "load-bearing projection 'overlay' is LAGGED: checkpoint 0 < event head 1")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _lagged)
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 503
    assert "LAGGED" in res.json()["detail"]
    # ATOMIC: nothing feature-generation was committed for this aborted request.
    assert res.json().get("intent_id") is None


def test_considered_set_serialization_failure_returns_409(make_client, monkeypatch):
    """MF-2: /contract/considered-set STAYS on REPEATABLE READ (it builds the snapshot), so a concurrent
    broaden race on its ``contract_considered ... ON CONFLICT (intent_id) DO UPDATE`` can raise 40001
    SerializationFailure. The route must map that to a designed 409 (re-fetch and retry), NEVER a 500."""
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)

    def _conflict(*a, **k):
        raise psycopg.errors.SerializationFailure(
            "could not serialize access due to concurrent update")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _conflict)
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 409, res.text
    assert "concurrent" in res.json()["detail"]
