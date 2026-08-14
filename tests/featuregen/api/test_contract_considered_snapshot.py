"""Delivery C0 Task 5 — the metadata snapshot lineage over the HTTP contract flow.

Two route-level guarantees on top of the builder unit tests (test_considered_set_snapshot.py):
  * /contract/draft + /contract/confirm reload the SERVER-persisted snapshot lineage and NEVER trust a
    client-supplied snapshot id (the request models carry none) — the draft response carries the server
    value even when the request body smuggles a decoy id, and the draft→confirm flow still registers a
    contract (Slice-3 flow unbroken);
  * a projection-lagged catalog surfaces as 503 CATALOG_PROJECTION_UNAVAILABLE (feature generation
    aborts rather than proceeding on a stale projected view).

The E4 cutover (2026-08-14) changed how these are driven and what the second one can assert. The
free-form generator that used to fill a considered set from a scripted ``overlay.feature.recommend``
response is deleted, so an option to draft now has to come from the ONE engine: a real
``catalog_source`` + ``confirmed_scope``, with the option's activation blockers cleared through their
real surfaces exactly as the E0 walkthrough does — ``governed_ready_round`` in
``test_binding_confirmation`` performs that ritual and is imported here rather than re-typed. And
because every served option is now a SEMANTIC option carrying a frozen decision
row, the activation fold — not the old standalone drift check — owns snapshot freshness, and it fails
CLOSED: a considered set with NO server lineage is no longer draftable-with-a-null-snapshot, it is
refused. That is asserted below instead of the old "reports null" shape.
"""
import psycopg
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_binding_confirmation import governed_ready_round, seal_for_real
from tests.featuregen.api.test_e2e_walkthrough import (
    CHURN,
    HYPOTHESIS,
    SOURCE,
    TARGET,
    _cib,
    _fake,
)

from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)


def test_draft_reloads_server_lineage_and_ignores_client_snapshot_id(
        make_client, conn, monkeypatch):
    seal_for_real(monkeypatch)
    _cib(conn)
    client = make_client(_fake())
    body, card = governed_ready_round(client)

    # The lineage the SERVER sealed and recorded on its own considered-set row — the only lineage
    # there is. (The old version of this test overwrote it with decoy values; post-cutover the
    # activation fold re-verifies the sealed snapshot at draft time, so a forged server row would
    # simply fail closed. Reading the real row proves the same reload wiring without lying.)
    server = conn.execute(
        "SELECT generation_run_id, snapshot_id, snapshot_content_hash "
        "FROM contract_considered WHERE intent_id = %s", (body["intent_id"],)).fetchone()
    assert all(server), "generation under REPEATABLE READ seals a snapshot and records its lineage"

    # The draft request model carries no snapshot id; a smuggled decoy field is simply ignored
    # (Pydantic drops it) — the response carries the SERVER value reloaded from the considered-set row.
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"], "why": "best fit",
        "snapshot_id": "snap_CLIENT_FORGED"}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    assert dr.json()["snapshot"] == {
        "generation_run_id": server[0], "snapshot_id": server[1], "content_hash": server[2]}

    # Slice-3 flow unbroken: draft → confirm still registers a versioned contract.
    draft = dict(dr.json()["draft"])
    draft["intent_id"] = dr.json()["intent_id"]
    draft["expected_binding_hash"] = dr.json()["binding_hash"]
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1
    assert cr.json()["feature_id"].startswith("feat")


def test_draft_without_server_lineage_fails_closed_as_stale(make_client, conn):
    """A considered set that recorded NO lineage (a READ COMMITTED / pre-C0 run — here, the harness
    transaction with the isolation gates left alone) used to draft happily and report ``snapshot:
    null``. After the E4 cutover every served option is an engine option with a frozen decision row,
    so the activation fold owns snapshot freshness and it fails CLOSED on the unverifiable: the draft
    is refused with ACTIVATION_BLOCKED / SNAPSHOT_STALE_REGENERATE and a next step, rather than
    proceeding over a catalog state nobody can prove. Absence of a seal is no longer a free pass."""
    _cib(conn)
    client = make_client(_fake())
    body, card = governed_ready_round(client)
    assert conn.execute(
        "SELECT snapshot_id FROM contract_considered WHERE intent_id = %s",
        (body["intent_id"],)).fetchone()[0] is None, "no seal was taken on this connection"

    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"], "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 409, dr.text
    detail = dr.json()["detail"]
    assert detail["code"] == "ACTIVATION_BLOCKED"
    blockers = {b["code"]: b["next_step"] for b in detail["blockers"]}
    assert "SNAPSHOT_STALE_REGENERATE" in blockers, blockers
    assert blockers["SNAPSHOT_STALE_REGENERATE"]     # the refusal names what to do next
    assert conn.execute("SELECT count(*) FROM contract").fetchone()[0] == 0


def test_considered_set_projection_unavailable_returns_503(make_client, conn, monkeypatch):
    _cib(conn)
    client = make_client(_fake())

    def _lagged(*a, **k):
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            "load-bearing projection 'overlay' is LAGGED: checkpoint 0 < event head 1")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _lagged)
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn",
        "catalog_source": SOURCE, "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 503
    assert "LAGGED" in res.json()["detail"]
    # ATOMIC: nothing feature-generation was committed for this aborted request.
    assert res.json().get("intent_id") is None


def test_considered_set_serialization_failure_returns_409(make_client, conn, monkeypatch):
    """MF-2: /contract/considered-set STAYS on REPEATABLE READ (it builds the snapshot), so a concurrent
    broaden race on its ``contract_considered ... ON CONFLICT (intent_id) DO UPDATE`` can raise 40001
    SerializationFailure. The route must map that to a designed 409 (re-fetch and retry), NEVER a 500."""
    _cib(conn)
    client = make_client(_fake())

    def _conflict(*a, **k):
        raise psycopg.errors.SerializationFailure(
            "could not serialize access due to concurrent update")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _conflict)
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn",
        "catalog_source": SOURCE, "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    assert "concurrent" in res.json()["detail"]
