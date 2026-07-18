"""Phase-3C.1 Task 5 — the authority-only gate endpoints (`POST /gate/evaluate`, `GET /gate/cohorts`).

Platform-admin only (raw `platform-admin` claim via `require_confirmer`), OFF the customer path.
The body carries ONLY a batch identifier `{cohort, since, until}`; every count/verdict is assembled
server-side from the persisted WORM stores. Fail-closed: an empty/all-excluded window is a 200 with
`verdict.passed == false` (no evidence is not a pass), never an error.
"""
from __future__ import annotations

# uses the app TestClient + identity-header fixtures from tests/featuregen/api/conftest.py


def test_gate_evaluate_requires_platform_admin(client, non_admin_headers):
    r = client.post("/gate/evaluate", json={"cohort": "sha1", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=non_admin_headers)
    assert r.status_code == 403


def test_gate_evaluate_empty_window_fails_closed(client, admin_headers):
    r = client.post("/gate/evaluate", json={"cohort": "ghost", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"]["passed"] is False           # no evidence -> fail-closed
    assert body["coverage"]["qualifying"] == 0


def test_gate_cohorts_lists_producer_commits(client, admin_headers, db):
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, scoped_applicability_flag,"
        " ranking_flag, applicability_version, producer_commit, compiler_versions, compiler_versions_hash,"
        " payload_schema_version) VALUES ('r','{}','h',0,'p',true,true,true,true,'v','sha1','{}','ch','pv')")
    r = client.get("/gate/cohorts", headers=admin_headers)
    assert r.status_code == 200 and any(c["cohort"] == "sha1" for c in r.json())
