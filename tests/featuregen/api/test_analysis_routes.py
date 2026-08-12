"""`POST /analysis/plan` and `/analysis/clarify` — the data agent's PLANNING surface.

Seven read models existed with no route. A read model nobody can call is the same inert mechanism
this programme has found six times, so these tests care most about the seams between the pieces:
that the caller's read scope reaches retrieval, that a hallucinated ref cannot survive the round
trip, and that a plan which cannot execute says so rather than pretending.

Nothing in THESE routes executes a statement. That is still true of `/analysis/plan` and
`/analysis/clarify` by design — running a sealed plan is `/analysis/execute`, which has its own
suite (`test_analysis_execute_route.py`) and its own approval gate.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse

#: REAL time, not a pinned date. These tests drive the HTTP route, and the route stamps its own
#: `datetime.now(UTC)` — so a hardcoded watermark falls outside the freshness window the moment the
#: clock passes it, and every column reads as stale. Pinning it broke this file at midnight.
_NOW = datetime.now(UTC)
_QUESTION = "which customers had fewer transactions this month than last"
#: Matches NO readable column in the fixture catalog (neither lexeme appears in any definition,
#: column name or table name), yet every word is CONTROLLED platform vocabulary — so the refusal is
#: reached AND it has something recordable to say.
_UNANSWERABLE = "counterparty exposure"


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


# ── the learning gap, at the PRODUCTION caller ───────────────────────────────────────────────────

def _gap_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT analysis_request_id, gap_key, dependency_snapshot_id "
        "FROM analysis_learning_event WHERE kind = 'gap' ORDER BY created_at").fetchall()


def test_three_identical_questions_record_ONE_gap(make_client, catalog):
    """`record_gap` dedupes on (request, gap, snapshot) — and the route defeated it by minting a
    fresh `areq-{uuid4}` per call, so the SAME unanswered question asked three times filled the
    learning store with three identical rows and `GET /learning/gaps` reported demand 3 for one
    thing to decide."""
    client = _client(make_client)
    for _ in range(3):
        r = client.post("/analysis/plan", json={"question": _UNANSWERABLE}, headers=_h())
        assert r.status_code == 422, r.text
    rows = _gap_rows(catalog)
    assert len(rows) == 1, rows
    assert rows[0][0].startswith("areq-")


def test_a_CHANGED_catalog_state_records_a_SECOND_gap(make_client, catalog):
    """A stable id must not become a permanent mute: the same question after an upload IS new
    information, because the gap may have been resolved. The id is derived from the dependency
    snapshot as well as the question, so a moved drift watermark re-records."""
    client = _client(make_client)
    assert client.post("/analysis/plan", json={"question": _UNANSWERABLE},
                       headers=_h()).status_code == 422
    catalog.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s",
                    (_NOW - timedelta(minutes=7),))
    assert client.post("/analysis/plan", json={"question": _UNANSWERABLE},
                       headers=_h()).status_code == 422
    rows = _gap_rows(catalog)
    assert len(rows) == 2, rows
    # Same THING TO DECIDE, different request id and different snapshot.
    assert rows[0][1] == rows[1][1]
    assert rows[0][0] != rows[1][0]
    assert rows[0][2] != rows[1][2]


def test_the_same_question_from_a_DIFFERENT_asker_still_dedupes(make_client, catalog):
    """The id is derived from the question and the catalog state, never from the session — two
    analysts blocked by one missing term are one gap, and `open_gaps` counts demand by DISTINCT
    request id, so a per-session id would have inflated it."""
    client = _client(make_client)
    client.post("/analysis/plan", json={"question": _UNANSWERABLE}, headers=_h())
    client.post("/analysis/plan", json={"question": _UNANSWERABLE},
                headers={"X-User": "someone-else", "X-Roles": "feature_engineer"})
    assert len(_gap_rows(catalog)) == 1


def test_the_refusal_COMMITS_the_learning_write_instead_of_rolling_it_back(conn, catalog):
    """`get_conn` rolls the request transaction back on ANY raised exception — and a raised
    `HTTPException` is one. So the 422 this route answers an unanswerable question with DISCARDED
    the gap written moments earlier, which is the entire reason that write exists: the store had no
    production producer at all. The refusal is RETURNED as a response, byte-identical to what the
    handler would have produced, so the transaction reaches its commit."""
    from fastapi.testclient import TestClient

    from featuregen.analysis.intent import TASK
    from featuregen.api.app import create_app
    from featuregen.api.deps import get_conn, get_feature_gen_conn

    disposition: list[str] = []

    def _probe_conn():
        try:
            yield conn
            disposition.append("commit")
        except Exception:
            disposition.append("rollback")
            raise

    app = create_app(llm_client=FakeLLM(script={TASK: FakeResponse(output=_intent_output())}))
    app.dependency_overrides[get_conn] = _probe_conn
    app.dependency_overrides[get_feature_gen_conn] = _probe_conn
    with TestClient(app) as client:
        r = client.post("/analysis/plan", json={"question": _UNANSWERABLE}, headers=_h())

    assert r.status_code == 422
    assert r.json()["detail"], "the refusal must still carry its reason"
    assert disposition == ["commit"], disposition
    assert len(_gap_rows(conn)) == 1


# ── the SELECTION, on the production path (Release-B Task 8 review) ─────────────────────────────
#
# The review's probe: a HISTORICAL plan grounded through the real route came back with
# `row_selections=()`, `refusals=()` and `resolved=True` — the row rule raised, a blanket except
# swallowed it, and the agent had no row rule while claiming resolution. The invariant these pin is
# that the two can never happen together again: either the decisions are there, or their absence is
# on the response.


@pytest.fixture
def selecting(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")


def test_the_selection_is_never_EMPTY_AND_RESOLVED(make_client, catalog, selecting):
    """THE PROBE. "Decreased this month vs last" is historical by construction, so it has a row
    rule to resolve — and if nothing could be decided, the response says which needs were not."""
    _bind(catalog)
    body = _client(make_client).post(
        "/analysis/plan", json={"question": _QUESTION}, headers=_h()).json()
    selection = body["selection"]
    assert selection is not None, "the flag is on; the decisions must be visible"
    assert selection["rows"] or selection["refusals"], selection
    assert not (selection["resolved"] and not selection["rows"]), selection


def test_a_selection_refusal_is_RENDERED_as_a_question(make_client, catalog, selecting):
    """Every typed refusal becomes a clarification when interactive (plan rule 10). Reaching the
    person is the whole point of returning it instead of raising it."""
    _bind(catalog)
    body = _client(make_client).post(
        "/analysis/plan", json={"question": _QUESTION}, headers=_h()).json()
    codes = {c["code"] for c in body["clarifications"]}
    assert {r["code"] for r in body["selection"]["refusals"]} & codes, body["clarifications"]
    # The population is asked ONCE, whichever half of the system noticed it is undeclared.
    assert [c["code"] for c in body["clarifications"]].count("population") == 1


def test_with_the_flag_OFF_the_response_carries_no_selection_at_all(make_client, catalog,
                                                                   monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    _bind(catalog)
    body = _client(make_client).post(
        "/analysis/plan", json={"question": _QUESTION}, headers=_h()).json()
    assert body["selection"] is None


def test_a_route_level_refusal_writes_ONE_learning_gap(make_client, catalog, selecting):
    """`record_selection_gaps` had NO production caller — the same inert-mechanism shape this
    programme has found repeatedly. This is it, at the surface a refusal reaches a person."""
    _bind(catalog)
    client = _client(make_client)
    assert client.post("/analysis/plan", json={"question": _QUESTION},
                       headers=_h()).status_code == 200
    gaps = {row[1] for row in _gap_rows(catalog)}
    assert gaps, "a visible refusal recorded nothing to decide"
    assert len(_gap_rows(catalog)) == len(gaps), "one row per thing to decide"


def test_asking_the_SAME_blocked_question_twice_is_one_thing_to_decide(make_client, catalog,
                                                                      selecting):
    """The derived request id, at the route: `record_gap` dedupes on (request, gap, snapshot), and
    a minted id would make each retry a new row and read as demand 2 for one decision."""
    _bind(catalog)
    client = _client(make_client)
    for _ in range(2):
        client.post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    rows = _gap_rows(catalog)
    assert len(rows) == len({(row[0], row[1], row[2]) for row in rows})
    assert len({row[0] for row in rows}) == 1, "two askings, one request id"


def test_a_model_that_cannot_express_the_question_is_422_not_500(make_client, catalog):
    """The question could not be expressed — that is about the request, not a fault in the service."""
    from featuregen.analysis.intent import TASK
    from featuregen.intake.llm import PROVIDER_REFUSAL

    client = make_client(llm_client=FakeLLM(
        script={TASK: FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)}))
    r = client.post("/analysis/plan", json={"question": _QUESTION}, headers=_h())
    assert r.status_code == 422
