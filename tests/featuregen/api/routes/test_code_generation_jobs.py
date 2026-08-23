"""Step 5a's surface: the plan is a QUESTION, the request is THE act, and neither lies.

The rules under test are the ones that cost money or mislead a person:

1. **`/plan` writes NOTHING** — no job, no decision revision, no spend ceiling. It `ask`s.
2. **LLM work without a recorded cost confirmation is a 409**, not a smaller quiet spend (§11.2).
3. **A double-click is the same job**, reported as such.
4. **Cancellation stops future stages** and refuses to pretend twice.
5. **Flag off is a 404** — this deployment does not run V2 generation at all.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.api.routes.test_formula_drafts import _revision
from tests.featuregen.materialize.crosswalk_fixtures import build_set_declaration

from featuregen.materialize.build_set_declaration import encode_declaration

PLAN = "/code-generation-jobs/plan"
JOBS = "/code-generation-jobs"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")


@pytest.fixture
def engineer_headers():
    return {"X-User": "sam", "X-Roles": "feature_engineer"}


def _seed(conn, *, tag: str) -> dict:
    revision = _revision(conn, revision_id=f"crev-{tag}", snapshot_id=f"snap-{tag}")
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-cgr','h','hypothesis') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-cgr','exploration','h') ON CONFLICT DO NOTHING", (f"trr-{tag}",))
    selection = f"sel-{tag}"
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES (%s,%s,%s,'opt-a',%s,'sha256:ask',"
        "'sha256:plan',%s) ON CONFLICT DO NOTHING",
        (selection, f"trr-{tag}", revision, f"dec-{selection}", f"ch-{selection}"))
    return {
        "considered_revision_id": revision,
        "target_reading_revision_id": f"trr-{tag}",
        "selection_revision_ids": [selection],
        "environment_id": "hdfc-local",
        "logical_group_name": f"grp-{tag}",
        "declaration": encode_declaration(build_set_declaration()),
        "execution_parameters": {
            "engine_id": "kedro-pyspark",
            "physical_type_policy": "formula-v2/physical-types@1",
            "empty_values": {"avg_balance_90d": "0"},
            "target_mode": "exploration",
        },
    }


_APPROVAL = {"max_calls": 5, "max_tokens": 200_000, "max_cost": "12.50", "currency": "USD",
             "pricing_version": "pricing@1", "expires_at": "2026-12-31T00:00:00Z"}


# ══ the plan is a QUESTION ══════════════════════════════════════════════════════════════════════
def test_THE_PLAN_WRITES_NOTHING(client, conn, enabled, engineer_headers):
    body = _seed(conn, tag="pq")

    response = client.post(PLAN, json=body, headers=engineer_headers)

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["members"][0]["formula_strategy"] == "LLM_AUTHORED"
    assert plan["llm_members"] == 1
    assert plan["spend_approval_required"] is True
    assert plan["estimated_provider_calls"] == 5, "1 call + 2 retries + 2 repairs, quoted"
    assert plan["spend_approval"] is None, "READS an approval; never creates one"
    # ▲ Nothing durable: no job, no decision revision, no spend ceiling — `ask`, not `decide`.
    for table in ("code_generation_job", "action_decision_revision",
                  "llm_spend_authorization_revision"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table


def test_the_plan_folds_member_facts_through_the_ONE_decision_service(client, conn, enabled,
                                                                      engineer_headers):
    body = _seed(conn, tag="pf")
    plan = client.post(PLAN, json=body, headers=engineer_headers).json()
    # The resolver's route-selection warnings surface through the §5 disposition fold.
    assert plan["decision_preview"]["allowed"] is True
    assert plan["members"][0]["warnings"], "the LLM route is announced, never silent"


# ══ the request is THE act ══════════════════════════════════════════════════════════════════════
def test_LLM_WORK_WITHOUT_A_COST_CONFIRMATION_IS_REFUSED(client, conn, enabled,
                                                         engineer_headers):
    body = _seed(conn, tag="nc")
    response = client.post(JOBS, json=body, headers=engineer_headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "COST_AUTHORIZATION_MISSING"
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (0,)


def test_THE_REQUEST_RECORDS_EVERYTHING_TOGETHER(client, conn, enabled, engineer_headers):
    body = {**_seed(conn, tag="wr"), "spend_approval": _APPROVAL}

    response = client.post(JOBS, json=body, headers=engineer_headers)

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert response.json()["created"] is True

    status = client.get(f"{JOBS}/{job_id}", headers=engineer_headers).json()
    assert status["status"] == "REQUESTED"
    assert [m["member_state"] for m in status["members"]] == ["SELECTED"]
    assert {a["action"] for a in status["actions"]} == {"AUTHOR_FORMULA", "GENERATE_PREVIEW"}
    assert [e["stage"] for e in status["events"]] == ["REQUESTED"]

    # ▲ §11.2 made durable: the confirmation is a content-addressed ceiling bound to THIS job.
    identity = conn.execute(
        "SELECT content_identity_hash FROM code_generation_job WHERE job_id = %s",
        (job_id,)).fetchone()[0]
    spend = conn.execute(
        "SELECT max_calls, actor_subject FROM llm_spend_authorization_revision "
        "WHERE job_identity = %s", (identity,)).fetchone()
    assert spend == (5, "user:sam")


def test_A_DOUBLE_CLICK_IS_THE_SAME_JOB(client, conn, enabled, engineer_headers):
    body = {**_seed(conn, tag="dc"), "spend_approval": _APPROVAL}
    first = client.post(JOBS, json=body, headers=engineer_headers).json()
    second = client.post(JOBS, json=body, headers=engineer_headers).json()
    assert first["created"] is True
    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    assert conn.execute("SELECT COUNT(*) FROM llm_spend_authorization_revision").fetchone() \
        == (1,), "the idempotent ceiling did not double either"


def test_missing_execution_parameters_are_refused_BY_NAME(client, conn, enabled,
                                                          engineer_headers):
    body = {**_seed(conn, tag="xp"), "spend_approval": _APPROVAL}
    del body["execution_parameters"]["engine_id"]
    response = client.post(JOBS, json=body, headers=engineer_headers)
    assert response.status_code == 422
    assert "engine_id" in response.json()["detail"]


# ══ watching and cancelling ═════════════════════════════════════════════════════════════════════
def test_CANCEL_STOPS_FUTURE_STAGES_and_refuses_to_pretend_twice(client, conn, enabled,
                                                                 engineer_headers):
    body = {**_seed(conn, tag="cx"), "spend_approval": _APPROVAL}
    job_id = client.post(JOBS, json=body, headers=engineer_headers).json()["job_id"]

    response = client.post(f"{JOBS}/{job_id}/cancel", headers=engineer_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert "in flight" in response.json()["detail"], \
        "the answer must not claim to cancel a provider call already running"

    again = client.post(f"{JOBS}/{job_id}/cancel", headers=engineer_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_TERMINAL"


def test_an_unknown_job_is_a_404(client, enabled, engineer_headers):
    assert client.get(f"{JOBS}/cgj-none", headers=engineer_headers).status_code == 404


# ══ the switch ══════════════════════════════════════════════════════════════════════════════════
def test_FLAG_OFF_IS_A_404_for_every_endpoint(client, conn, monkeypatch, engineer_headers):
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)
    body = _seed(conn, tag="off")
    assert client.post(PLAN, json=body, headers=engineer_headers).status_code == 404
    assert client.post(JOBS, json=body, headers=engineer_headers).status_code == 404
    assert client.get(f"{JOBS}/x", headers=engineer_headers).status_code == 404


def test_a_READY_LEGACY_V1_DRAFT_refuses_costing_until_a_regeneration_is_approved(
        client, conn, enabled, engineer_headers):
    """§11.1.2's state-aware rule at the PLAN endpoint — and the bite test the first cut lacked:
    the hand-rolled exception query named a column that does not exist (`consumed_at`), so this
    exact path raised UndefinedColumn at runtime. Found by the run-spine session's mapping. The
    check now goes through `valid_exception_for`, whose contract+strategy bindings are the point."""
    from featuregen.canonical import jcs_sha256
    from featuregen.overlay.upload.formula_draft_service import frozen_candidate
    from featuregen.overlay.upload.formula_strategy import resolve_formula_strategy
    from featuregen.overlay.upload.formula_strategy_facts import (
        assemble_strategy_facts,
        current_author_contract_hash,
    )
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import retirement_scope_key

    body = _seed(conn, tag="lgr")
    candidate = frozen_candidate(conn, body["considered_revision_id"], "opt-a")
    scope_key = retirement_scope_key(
        considered_revision_id=candidate.considered_revision_id, option_id="opt-a",
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        definition_revision=candidate.definition_revision)
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, formula_content_hash, formula_json, "
        "requested_by, requested_at) VALUES ('fd-lgr', %s, 'opt-a', %s, %s, 'legacy-cfg', %s, "
        "'ident-lgr', 'READY', 'sha256:f', '{\"v\": 1}'::jsonb, 'user:sam', "
        "'2026-08-01T00:00:00Z')",
        (candidate.considered_revision_id, candidate.planning_request_hash,
         candidate.catalog_snapshot_hash, candidate.definition_revision))
    conn.execute(
        "INSERT INTO formula_draft_authoring_identity (formula_draft_id, identity_version, "
        "retirement_scope_key, config_payload_json, config_hash) "
        "VALUES ('fd-lgr', 1, %s, '{}'::jsonb, 'legacy-cfg')", (scope_key,))

    refused = client.post(PLAN, json=body, headers=engineer_headers).json()
    assert "LEGACY_REGENERATION_NOT_APPROVED" in refused["members"][0]["blockers"]

    # An APPROVED regeneration — bound to the exact V2 identity, contract and strategy the plan
    # computes — clears it. Anything looser must not.
    assembled = assemble_strategy_facts(
        conn, considered_revision_id=candidate.considered_revision_id, option_id="opt-a",
        idea=candidate.idea, catalog_snapshot_hash=candidate.catalog_snapshot_hash)
    decision = resolve_formula_strategy(assembled.facts)
    contract = current_author_contract_hash()
    identity_hash = __import__("featuregen.overlay.upload.formula_draft_store",
                               fromlist=["formula_identity"]).formula_identity(
        considered_revision_id=candidate.considered_revision_id, option_id="opt-a",
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        authoring_config_hash=jcs_sha256({
            "identity_version": 2, "formula_strategy": str(decision.strategy),
            "strategy_identity_hash": decision.strategy_identity_hash,
            "provider_contract_hash": contract}),
        definition_revision=candidate.definition_revision)
    spend = authorize_spend(
        conn, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity="job-lgr",
        member_identities=["sel-lgr"], provider_contract_hash=contract, max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    conn.execute(
        "INSERT INTO formula_draft_regeneration_exception (exception_id, "
        "target_formula_identity_hash, provider_contract_hash, strategy_identity_hash, "
        "actor_subject, overrides_tombstone, expires_at, llm_spend_authorization_id) "
        "VALUES ('exc-lgr', %s, %s, %s, 'user:owner', false, "
        "'2026-12-31T00:00:00Z', %s)",
        (identity_hash, contract, decision.strategy_identity_hash, spend))

    cleared = client.post(PLAN, json=body, headers=engineer_headers).json()
    assert "LEGACY_REGENERATION_NOT_APPROVED" not in cleared["members"][0]["blockers"]


def test_THE_WRITE_ROUTE_maps_a_bad_selection_like_the_plan_route(client, conn, enabled,
                                                                  engineer_headers):
    """Task 5 review item 1: SelectionUnavailable escaped the WRITE route as a 500 while the
    plan route answered 404/409 — same refusals, same statuses, BOTH routes."""
    body = {**_seed(conn, tag="wmap"), "spend_approval": _APPROVAL}

    unknown = client.post(JOBS, json={**body, "selection_revision_ids": ["sel-absent"]},
                          headers=engineer_headers)
    assert unknown.status_code == 404, unknown.text

    # A selection anchored to a DIFFERENT considered revision (minimal second revision — the
    # shared fixture's fixed intent/run ids cannot seed twice in one test).
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-wmap2','h','hypothesis') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-wmap2','int-wmap2','run-wmap2','{}'::jsonb,"
        "'sha256:c','contract-considered-v3')")
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES ('sel-wmap2','trr-wmap','crev-wmap2','opt-a',"
        "'dec-wmap2','sha256:a','sha256:b','ch-wmap2')")
    crossed = client.post(JOBS, json={**body, "selection_revision_ids": ["sel-wmap2"]},
                          headers=engineer_headers)
    assert crossed.status_code == 409, crossed.text
    assert "one build reads one" in crossed.json()["detail"]
