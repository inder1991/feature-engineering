"""POST /contract/considered-set writes the run's identity in its own creation transaction (§6.1).

The seam is proved through the REAL route, end to end, because only the route can prove the two
halves of the chain co-exist: a sealed recognition (which is what makes ``contract_generation_input``
exist) and a sealed catalog snapshot (which is what makes the considered revision's snapshot pin
exist). Both are needed, and the harness supplies each the way the existing route suites do:

* ``confirmation_required`` + a real ``/contract/recognitions`` round → the sealed generation input;
* ``seal_for_real`` → the C0 snapshot. The whole API harness shares ONE READ COMMITTED transaction,
  so without that stub the snapshot gate legitimately skips and every run would be chain-incomplete.

The second test pins the honest half: a run whose creation never sealed a recognition gets NO
identity row and still returns 200 — it renders PRE_SPINE, and nothing is fabricated over it.
"""
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_1b_rollout import _recognizer
from tests.featuregen.api.test_binding_confirmation import seal_for_real
from tests.featuregen.api.test_e2e_walkthrough import CHURN, SOURCE, TARGET, _cib, _fake

HYPOTHESIS = "run-spine: long-tenured customers churn when their balance drops"
_SCOPE = {"primary": CHURN, "confirmation_source": "user_confirmed",
          "use_case_origins": {CHURN: "llm_proposed"}}


def _identity_rows(conn, run_id):
    return conn.execute(
        "SELECT generation_run_id, owner_subject, root_generation_run_id, parent_generation_run_id, "
        "intent_id, considered_revision_id, metadata_snapshot_id, run_identity_hash "
        "FROM feature_run_identity WHERE generation_run_id = %s", (run_id,)).fetchall()


def test_the_scoped_route_writes_one_identity_for_the_run_it_minted(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    seal_for_real(monkeypatch)
    _cib(conn)

    rec = make_client(_recognizer()).post(
        "/contract/recognitions",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn"}, headers=AUTH)
    assert rec.status_code == 200, rec.text
    res = make_client(_fake()).post(
        "/contract/considered-set",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn",
              "catalog_source": SOURCE, "target_ref": TARGET,
              "intent_id": rec.json()["intent_id"],
              "recognition_id": rec.json()["recognition_id"],
              "confirmed_scope": _SCOPE},
        headers=AUTH)
    assert res.status_code == 200, res.text
    run_id = res.json()["generation_run_id"]

    rows = _identity_rows(conn, run_id)
    assert len(rows) == 1, rows          # exactly one identity, written once
    (run, owner, root, parent, intent_id, considered_id, snapshot_id, run_hash) = rows[0]
    assert run == run_id                      # the identity belongs to the run the route minted
    assert owner == "user:tester"             # the AUTHENTICATED caller, never a client-supplied value
    assert (root, parent) == (run_id, None)   # foundation: every run is its own root, no parent
    assert intent_id == res.json()["intent_id"]
    assert run_hash
    # The links are the SAME rows the creation transaction wrote — never re-derived later.
    assert (considered_id, snapshot_id) == conn.execute(
        "SELECT considered_revision_id, metadata_snapshot_id FROM contract_considered_revision "
        "WHERE generation_run_id = %s", (run_id,)).fetchone()


def test_a_run_without_a_sealed_recognition_gets_no_identity_and_still_serves(
        make_client, conn, monkeypatch):
    """Honest absence: no sealed recognition → no generation input → no identity (PRE_SPINE).

    The request still succeeds — the spine writer never turns a servable generation into a refusal,
    and it never invents an identity over links that do not exist."""
    seal_for_real(monkeypatch)   # the snapshot half seals; the input half is what is missing
    _cib(conn)

    res = make_client(_fake()).post(
        "/contract/considered-set",
        json={"hypothesis": HYPOTHESIS, "objective": "predict churn",
              "catalog_source": SOURCE, "target_ref": TARGET, "confirmed_scope": _SCOPE},
        headers=AUTH)

    assert res.status_code == 200, res.text
    run_id = res.json()["generation_run_id"]
    assert conn.execute(
        "SELECT count(*) FROM contract_generation_input WHERE generation_run_id = %s",
        (run_id,)).fetchone()[0] == 0
    assert _identity_rows(conn, run_id) == []
