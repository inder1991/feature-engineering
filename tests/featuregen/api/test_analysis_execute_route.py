"""`POST /analysis/execute` — the route that finally runs a sealed plan (Release-B Task 9).

The module next door says "nothing here executes", and for `/analysis/plan` that stays true. This
is the surface Task 9 adds, and the tests care about the four properties that make it safe:

  * FLAG-GATED — 404 while `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` is off, so the flag-off API is
    byte-identical to the one that shipped;
  * it runs a SEALED plan by identity, never a question (a question would be re-planned, and the
    caller would run something they never previewed);
  * a STALE plan refuses, names what moved, and reads nothing;
  * the shipped engine provider REFUSES — running against the bank's warehouse is a separate
    approval gate, and only the suite's FIXTURE engine (the pilot's own Postgres) is substituted.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.analysis.release_b_bank import (
    CUTOFF_REF,
    QUESTION,
    SRC,
    build_bank,
    pilot_plan,
    publish_temporal_policy_for,
)
from tests.featuregen.data_agent.pilot_fixture import EXPECTED, TRANSACTION_TABLE
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.engine import EXECUTION_ENGINE_NOT_APPROVED, AnalysisEngineV1
from featuregen.analysis.execution import plan_to_execution_ir
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.sealed_execution import execution_inputs_for_plan
from featuregen.analysis.sealed_plan import (
    SEALED_PLAN_ABSENT,
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    build_execution_ir_v2,
)
from featuregen.analysis.sealed_plan_store import seal_analysis_plan
from featuregen.api.deps import get_analysis_engine
from featuregen.data_agent.eligibility_store import record_eligibility
from featuregen.data_agent.sql_postgres import PostgresDialect
from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind

REFERENCE = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def _h(roles: str = "feature_engineer") -> dict:
    return {"X-User": "u", "X-Roles": roles}


@pytest.fixture
def bank(conn, monkeypatch):
    built = build_bank(conn, monkeypatch)
    record_eligibility(built, catalog_source=SRC, table=TRANSACTION_TABLE,
                       policy=_eligibility(), proposed_by="user:priya")
    return built


@pytest.fixture
def sealed(bank):
    """One sealed plan for the pilot question, at a PINNED reference instant.

    Sealed through the service rather than over HTTP because the route stamps its own
    `datetime.now(UTC)` and the fixture's months are 2026-05/2026-06 — which is itself the point
    the execute surface makes: execution reads the SEALED partitions and never touches a clock.
    """
    grounded = ground_analysis_plan(bank, pilot_plan(), cutoff_value_ref=CUTOFF_REF,
                                    recorded_by="task9")
    inputs = execution_inputs_for_plan(bank, grounded, reference=REFERENCE)
    assert inputs is not None
    ir = build_execution_ir_v2(plan_to_execution_ir(grounded, inputs), grounded.selections,
                               question=QUESTION)
    return seal_analysis_plan(bank, ir, sealed_by="user:priya", question=QUESTION)


def _client(make_client, bank, *, engine: bool = True):
    client = make_client()
    if engine:
        # THE FIXTURE ENGINE, injected the way the catalog connection already is. The shipped
        # provider refuses; nothing here reaches a cluster.
        client.app.dependency_overrides[get_analysis_engine] = lambda: (
            lambda _connection: AnalysisEngineV1(conn=bank, dialect=PostgresDialect(),
                                                 engine="postgres"))
    return client


# ── the flag ─────────────────────────────────────────────────────────────────────────────────────


def test_the_execute_surface_is_HIDDEN_while_the_flag_is_off(make_client, bank, sealed,
                                                             monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 404
    assert r.json()["detail"] == "not found"


# ── running one ──────────────────────────────────────────────────────────────────────────────────


def test_a_sealed_plan_runs_and_returns_the_hand_counted_answer(make_client, bank, sealed):
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_hash"] == sealed.plan_hash
    assert body["row_count"] == 6
    decreased = tuple(sorted(row["key"] for row in body["rows"] if row["decreased"]))
    assert decreased == EXPECTED["decreased_customers"]


def test_the_result_carries_the_SEALED_REFS_as_its_provenance(make_client, bank, sealed):
    """A number whose sources have to be looked up elsewhere is a number nobody can defend."""
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    provenance = r.json()["provenance"]
    assert provenance["contract_version"] == 2
    event = next(s for s in provenance["sources"] if s["need_role"] == "event_source")
    assert event["binding_revision_id"].startswith("pbr_")
    assert event["serving_policy_revision_id"].startswith("dsp_")
    assert event["dataset_profile_hash"] and event["source_selection_hash"]
    row = provenance["rows"][0]
    assert row["temporal_policy_revision_id"].startswith("dtp_")
    assert row["selection_kind"] == TemporalSelectionKind.VALID_AT_REPORT_CUTOFF.value


def test_an_UNKNOWN_plan_identity_is_a_typed_absence(make_client, bank, sealed):
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": "f" * 64}, headers=_h())
    assert r.status_code == 404
    assert r.json()["refusal"]["code"] == SEALED_PLAN_ABSENT


# ── refusing ─────────────────────────────────────────────────────────────────────────────────────


def test_a_STALE_plan_refuses_and_NAMES_what_moved(make_client, bank, sealed):
    publish_temporal_policy_for(bank, expected_pointer_version=1,
                                historical_selection=TemporalSelectionKind.CURRENT_RECORD)
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 409
    refusal = r.json()["refusal"]
    assert refusal["code"] == SEALED_PLAN_STALE_TEMPORAL_POLICY
    assert refusal["sealed"] and refusal["current"]
    assert refusal["sealed"] != refusal["current"]
    # NO RESTATEMENT CLAIM. The refusal is about this run, never about an answer already given.
    assert "correct" not in r.text.lower()


def test_with_NO_engine_wired_the_route_says_which_APPROVAL_is_outstanding(make_client, bank,
                                                                           sealed):
    """The shipped default. A 409 rather than a 503: the deployment is healthy and the plan is
    fine — what is missing is an approval, which is a state of the world rather than a fault."""
    r = _client(make_client, bank, engine=False).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 409
    assert r.json()["refusal"]["code"] == EXECUTION_ENGINE_NOT_APPROVED


def test_execution_requires_the_feature_generate_permission(make_client, bank, sealed):
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash},
        headers=_h("catalog_viewer"))
    assert r.status_code == 403
