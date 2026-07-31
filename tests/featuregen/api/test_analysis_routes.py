"""`POST /analysis/plan` and `/analysis/clarify` — the data agent's only surface.

Seven read models existed with no route. A read model nobody can call is the same inert mechanism
this programme has found six times, so these tests care most about the seams between the pieces:
that the caller's read scope reaches retrieval, that a hallucinated ref cannot survive the round
trip, and that a plan which cannot execute says so rather than pretending.

Nothing here executes a statement — see the route module for why that is a design decision and not
caution.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse

#: REAL time, not a pinned date. These tests drive the HTTP route, and the route stamps its own
#: `datetime.now(UTC)` — so a hardcoded watermark falls outside the freshness window the moment the
#: clock passes it, and every column reads as stale. Pinning it broke this file at midnight.
_NOW = datetime.now(UTC)
_QUESTION = "which customers had fewer transactions this month than last"


def _h(roles: str = "feature_engineer") -> dict:
    return {"X-User": "u", "X-Roles": roles}


def _intent_output(**over) -> dict:
    out = {
        "entity": "customer",
        "entity_ref": "ftr::tran_repos.cif_id",
        "base_table_ref": "ftr::tran_repos",
        "measure": {"op": "count", "logical_ref": ""},
        "windows": [
            {"label": "current", "anchor_ref": "ftr::tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0},
            {"label": "previous", "anchor_ref": "ftr::tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 1}],
        "dimensions": [], "comparison": "decrease", "unresolved": [],
    }
    out.update(over)
    return out


@pytest.fixture
def catalog(conn):
    """A transaction table with a governed grain and as-of, plus a restricted column."""
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id) "
        "VALUES ('ftr', %s, 'r1') ON CONFLICT (catalog_source) DO UPDATE "
        "  SET last_completed_at = EXCLUDED.last_completed_at", (_NOW,))
    rows = [
        ("public.tran_repos.cif_id", "cif_id", "party identifier on the posting", True, False,
         None, None),
        ("public.tran_repos.tran_month", "tran_month", "posting period partition", False, True,
         None, None),
        ("public.tran_repos.tran_amt", "tran_amt", "value of the transaction posted", False, False,
         None, None),
        ("public.tran_repos.emirates_id", "emirates_id", "national identity number of the customer",
         True, False, None, "restricted"),
    ]
    for ref, column, definition, grain, as_of, sens, restriction in rows:
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "  definition, is_grain, is_as_of, sensitivity, effective_restriction, search_doc) "
            "VALUES ('ftr',%s,'column','tran_repos',%s,%s,%s,%s,%s,%s, "
            "        setweight(to_tsvector('english', coalesce(%s,'')),'A') || "
            "        setweight(to_tsvector('english', coalesce(%s,'')),'B')) "
            "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
            (ref, column, definition, grain, as_of, sens, restriction, column, definition))
    return conn


def _client(make_client, output=None, **kw):
    from featuregen.analysis.intent import TASK
    return make_client(llm_client=FakeLLM(
        script={TASK: FakeResponse(output=output or _intent_output(**kw))}))


# ── the plan surface ─────────────────────────────────────────────────────────────────────────────

def test_a_question_becomes_a_previewed_plan(make_client, catalog):
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    assert r.status_code == 200, r.text
    view = r.json()["preview"]
    assert view["entity"] == "customer"
    assert view["measure"] == "count of rows"
    assert view["comparison"] == "decrease"


def test_an_UNBOUND_table_reports_an_operator_task(make_client, catalog):
    """With no binding, the catalog cannot supply a physical address at all — nothing to run against
    and nothing to decide. Naming it as a binding gap points at the person who can fix it."""
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    view = r.json()["preview"]
    assert view["runnable"] is False
    assert view["blocked_by"]["code"] == "PHYSICAL_BINDING_ABSENT"
    assert view["blocked_by"]["subject"] == "ftr::tran_repos"
    assert view["sql"] == ""


def _bind(conn, *, allowed="dpl_eib"):
    from featuregen.data_agent.binding_store import record_binding, record_connection
    from featuregen.data_agent.connection import DataSourceConnectionV1
    from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1

    record_connection(conn, DataSourceConnectionV1(
        connection_id="c1", environment_id="uat", kind="hive", host="h", port=10000,
        auth_mechanism="kerberos", secret_ref="vault://x", execution_principal="svc_ro",
        allowed_schemas=frozenset({allowed}), active=True))
    record_binding(conn, PhysicalDatasetBindingV1(
        binding_id="b1", catalog_logical_ref="ftr::dpl_eib.tran_repos", connection_id="c1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="wh", schema="dpl_eib",
                                          table="tran_repos", object_kind="table")))


def _set_policy(conn, *, confirmed_by=None):
    from featuregen.data_agent.eligibility import (
        NullBehavior,
        ReversalMode,
        TransactionEligibilityPolicyV1,
    )
    from featuregen.data_agent.eligibility_store import record_eligibility

    record_eligibility(
        conn, catalog_source="ftr", table="tran_repos", proposed_by="llm",
        confirmed_by=confirmed_by, now=_NOW,
        policy=TransactionEligibilityPolicyV1(
            status_column="tran_status", included_status_values=("POSTED",),
            reversal_mode=ReversalMode.BOOLEAN_OR_CODE_COLUMN, reversal_column="reversal_flag",
            non_reversed_values=("N",), null_behavior=NullBehavior.EXCLUDE))


def test_a_BOUND_table_next_needs_the_population_DECLARED(make_client, catalog):
    """The registry has a real consumer, asserted. Once the address exists the next gap is not
    configuration at all: a person must say which table is the population, and no store may choose
    for them — the clarification asking exactly that is on the same response."""
    _bind(catalog)
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    body = r.json()
    assert body["preview"]["blocked_by"]["code"] == "POPULATION_UNDECLARED"
    assert "population" in {c["code"] for c in body["clarifications"]}


def test_the_route_reports_gaps_it_does_not_know_about_itself(make_client, catalog):
    """This route used to keep its own short list of gaps, and the list was WRONG — four codes, with
    attribution and join evidence missing. POPULATION_UNDECLARED is a code the route never mentions;
    surfacing it proves the enumeration comes from `analysis.assembly`, which is the only place that
    can stay complete as stores land."""
    from featuregen.api.routes import analysis as route_module

    _bind(catalog)
    _set_policy(catalog)
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    code = r.json()["preview"]["blocked_by"]["code"]
    assert code == "POPULATION_UNDECLARED"
    assert code not in route_module.BLOCKED_ROUTE_CODES


def test_an_UNCONFIRMED_policy_is_disclosed_as_a_finding(make_client, catalog):
    """Usable before confirmation is the product rule; passing SILENTLY is not. "Which rows count" is
    the definition every number rests on, so an unconfirmed one is a finding for the same reason an
    unconfirmed join identity is."""
    _bind(catalog)
    _set_policy(catalog)
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    view = r.json()["preview"]
    codes = [f["code"] for f in view["findings"]]
    assert "ELIGIBILITY_UNCONFIRMED" in codes
    assert view["rests_on_unconfirmed_facts"] is True


def test_a_CONFIRMED_policy_raises_no_finding(make_client, catalog):
    _bind(catalog)
    _set_policy(catalog, confirmed_by="priya")
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    codes = [f["code"] for f in r.json()["preview"]["findings"]]
    assert "ELIGIBILITY_UNCONFIRMED" not in codes


def test_a_REVOKED_grant_does_not_read_as_unconfigured(make_client, catalog):
    """"Nobody bound this" and "somebody revoked it" call for different people. Collapsing the second
    into the first would hide a withdrawn permission behind a setup task."""
    from featuregen.data_agent.binding_store import record_binding, record_connection
    from featuregen.data_agent.connection import DataSourceConnectionV1
    from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1

    record_connection(catalog, DataSourceConnectionV1(
        connection_id="c1", environment_id="uat", kind="hive", host="h", port=10000,
        auth_mechanism="kerberos", secret_ref="vault://x", execution_principal="svc_ro",
        allowed_schemas=frozenset({"nothing_here"}), active=True))
    record_binding(catalog, PhysicalDatasetBindingV1(
        binding_id="b1", catalog_logical_ref="ftr::dpl_eib.tran_repos", connection_id="c1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="wh", schema="dpl_eib",
                                          table="tran_repos", object_kind="table")))

    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    assert r.json()["preview"]["blocked_by"]["code"] != "PHYSICAL_BINDING_ABSENT"


def test_truncation_is_reported_to_the_caller(make_client, catalog):
    r = _client(make_client).post(
        "/analysis/plan", json={"question": _QUESTION, "max_columns": 1}, headers=_h())
    assert r.json()["retrieval"]["dropped_columns"] > 0


def test_a_caller_cannot_ask_for_the_whole_catalog(make_client, catalog):
    """The bound is a deployment judgement, but it is still a bound."""
    r = _client(make_client).post(
        "/analysis/plan", json={"question": _QUESTION, "max_columns": 100000}, headers=_h())
    assert r.status_code == 422


# ── read scope reaches the prompt ────────────────────────────────────────────────────────────────

def test_a_restricted_column_is_never_offered_to_the_model(make_client, catalog):
    """`emirates_id` carries sensitivity NULL and a cascade-derived restriction — the shape a raw-tag
    filter misses. It is also a GRAIN column, so it would arrive through the structural leg. Failing
    this means a national ID reaches an LLM prompt."""
    captured: list = []

    class _Capture:
        def call(self, request):
            captured.append(request)
            return FakeLLM(script={request.task: FakeResponse(output=_intent_output())}).call(request)

    client = make_client(llm_client=_Capture())
    # The question must also match a PERMITTED column, or retrieval returns nothing, the model is
    # never called, and the test would pass without observing the exclusion at all.
    client.post("/analysis/plan",
                json={"question": "identity of the customer on each transaction"}, headers=_h())
    assert captured, "the model was never called — the question matched nothing readable"
    offered = captured[0].inputs["catalog_metadata"]["column_refs"]
    assert any("cif_id" in ref for ref in offered), offered      # the structural leg did run
    assert not any("emirates_id" in ref for ref in offered), offered


# ── access ───────────────────────────────────────────────────────────────────────────────────────

def test_planning_requires_feature_generate(make_client, catalog):
    """Planning dispatches an LLM call on the caller's behalf — the same class of action as the
    feature-generation routes, not a plain catalog read."""
    r = _client(make_client).post("/analysis/plan", json={"question": _QUESTION},
                                  headers=_h("catalog_viewer"))
    assert r.status_code == 403


# ── clarification round trip ─────────────────────────────────────────────────────────────────────

def test_an_abstention_comes_back_as_an_answerable_question(make_client, catalog):
    r = _client(make_client, unresolved=["entity"], entity_ref="").post(
        "/analysis/plan", json={"question": _QUESTION}, headers=_h())
    clars = {c["code"]: c for c in r.json()["clarifications"]}
    # `population` rides along on every comparison; the entity question is the one under test.
    assert "population" in clars
    clar = clars["entity"]
    assert {o["value"] for o in clar["options"]} == {"ftr::tran_repos.cif_id"}


def test_answering_a_clarification_updates_the_plan(make_client, catalog):
    client = _client(make_client, unresolved=["entity"], entity_ref="")
    r = client.post("/analysis/clarify",
                    json={"question": _QUESTION, "code": "entity",
                          "chosen": ["ftr::tran_repos.cif_id"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["preview"]["entity"] == "customer"


def test_an_answer_naming_a_column_that_was_never_offered_is_REFUSED(make_client, catalog):
    """The round trip must not be a way to smuggle a ref past retrieval's read scope."""
    r = _client(make_client).post(
        "/analysis/clarify",
        json={"question": _QUESTION, "code": "entity",
              "chosen": ["ftr::tran_repos.emirates_id"]}, headers=_h())
    assert r.status_code == 422
    assert "was not offered" in r.json()["detail"]


def test_the_answered_question_is_not_asked_again(make_client, catalog):
    client = _client(make_client, unresolved=["entity"], entity_ref="")
    body = {"question": _QUESTION, "code": "entity", "chosen": ["ftr::tran_repos.cif_id"]}
    remaining = [c["code"] for c in client.post("/analysis/clarify", json=body,
                                                headers=_h()).json()["clarifications"]]
    assert "entity" not in remaining
    assert remaining == ["population"], "the declaration must keep being asked until it is made"


# ── failure shapes ───────────────────────────────────────────────────────────────────────────────

def test_a_question_matching_nothing_is_422_not_500(make_client, catalog):
    r = _client(make_client).post("/analysis/plan",
                                  json={"question": "zzzz nonexistent terminology"}, headers=_h())
    assert r.status_code == 422
    assert "matched" in r.json()["detail"]


def test_a_model_that_cannot_express_the_question_is_422_not_500(make_client, catalog):
    """The question could not be expressed — that is about the request, not a fault in the service."""
    from featuregen.analysis.intent import TASK
    from featuregen.intake.llm import PROVIDER_REFUSAL

    client = make_client(llm_client=FakeLLM(
        script={TASK: FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)}))
    r = client.post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    assert r.status_code == 422
