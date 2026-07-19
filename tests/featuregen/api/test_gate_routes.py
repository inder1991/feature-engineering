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


def test_gate_cohorts_requires_platform_admin(client, non_admin_headers):
    r = client.get("/gate/cohorts", headers=non_admin_headers)
    assert r.status_code == 403


def test_gate_evaluate_window_is_utc_anchored(client, admin_headers, db):
    """The frontend sends date-only strings (YYYY-MM-DD) which Pydantic coerces to NAIVE midnight;
    the route must pin those edges to UTC so the window is reproducible across deployments. Pin a
    non-UTC session timezone (east of UTC, where a naive midnight lands BEFORE UTC midnight) and
    prove a run created late in the same UTC day is still inside the half-open [since, until)."""
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, scoped_applicability_flag,"
        " ranking_flag, applicability_version, producer_commit, compiler_versions, compiler_versions_hash,"
        " payload_schema_version, created_at) VALUES ('utc1','{}','h',0,'p',true,true,true,true,'v',"
        "'sha-utc','{}','ch','pv','2026-07-18T23:30:00+00:00')")
    db.execute("SET TIME ZONE 'Asia/Kolkata'")
    r = client.post("/gate/evaluate", json={"cohort": "sha-utc", "since": "2026-07-18",
                                            "until": "2026-07-19"}, headers=admin_headers)
    assert r.status_code == 200
    coverage = r.json()["coverage"]
    assert coverage["dispatched_in_range"] == 1
    assert coverage["qualifying"] == 1


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


def test_persist_evaluation_and_approve_enables(client, admin_headers, db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PRODUCER_COMMIT", "sha-a")
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    # persist an evaluation over an empty window → result FAIL (fail-closed), and confirm APPROVE is refused
    ev = client.post("/gate/enablement-evaluation", json={"cohort": "ghost",
                     "since": "2026-07-18T00:00:00Z", "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert ev.status_code == 200 and ev.json()["result"] == "FAIL"
    bad = client.post("/gate/activation-decision", json={"evaluation_id": ev.json()["evaluation_id"],
                      "decision": "APPROVE", "reason": "x"}, headers=admin_headers)
    assert bad.status_code == 422   # APPROVE over a FAIL is refused server-side


def test_activation_endpoints_require_platform_admin(client, non_admin_headers):
    assert client.post("/gate/enablement-evaluation", json={"cohort": "c", "since":
        "2026-07-18T00:00:00Z", "until": "2026-07-19T00:00:00Z"}, headers=non_admin_headers).status_code == 403
    assert client.post("/gate/activation-decision", json={"evaluation_id": "e", "decision": "REVOKE",
                       "reason": "x"}, headers=non_admin_headers).status_code == 403


def test_gate_e2e_collects_a_batch_and_evaluates(client, admin_headers, db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PRODUCER_COMMIT", "sha-e2e")
    # collect one qualifying shadow run (all four flags on) via the planner entrypoint the route uses
    from datetime import UTC, datetime

    from tests.featuregen.overlay.upload.planner.test_plan import _txn_template
    from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

    from featuregen.overlay.upload.planner.shadow import run_shadow_planner
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="e2e", now=now, templates=(_txn_template(),),
                       compile_contracts=True, persist=True, scoped_applicability=True, ranking=True)
    r = client.post("/gate/evaluate", json={"cohort": "sha-e2e", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["qualifying"] == 1 and body["population"]["denominator"] >= 0
    assert set(body["verdict"]) == {"passed", "gate1_capture", "gate2a_map", "gate3_gold",
                                    "gate5_stability", "gate6_drift"}
