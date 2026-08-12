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
from tests.featuregen.data_agent.pilot_fixture import (
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    EXPECTED,
    TRANSACTION_TABLE,
)
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.engine import EXECUTION_ENGINE_NOT_APPROVED, AnalysisEngineV1
from featuregen.analysis.execution import plan_to_execution_ir
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.sealed_execution import execution_inputs_for_plan
from featuregen.analysis.sealed_plan import (
    SEALED_PLAN_ABSENT,
    SEALED_PLAN_STALE_ELIGIBILITY,
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    build_execution_ir_v2,
)
from featuregen.analysis.sealed_plan_store import seal_analysis_plan
from featuregen.api.deps import get_analysis_engine
from featuregen.data_agent.eligibility_store import confirm_eligibility, record_eligibility
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


def test_the_answer_DISCLOSES_that_nobody_confirmed_which_rows_count(make_client, bank, sealed):
    """PIN 7's disclosure half. "Usable before confirmation" is the product rule; passing silently
    is not — and a finding that reached the PREVIEW and not the answer is the disclosure stopping
    exactly where the number starts."""
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    eligibility = r.json()["provenance"]["eligibility"]
    assert eligibility["dataset_ref"].endswith(TRANSACTION_TABLE)
    assert eligibility["policy_hash"]
    assert eligibility["confirmed"] is False
    assert eligibility["status"] == "ELIGIBILITY_UNCONFIRMED"


def test_a_CONFIRMED_eligibility_policy_reports_no_outstanding_disclosure(make_client, bank,
                                                                          sealed):
    """The other half: a human agreeing between the seal and the run is not drift, and the answer
    says so rather than carrying a finding nobody owes anything about."""
    confirm_eligibility(bank, catalog_source=SRC, table=TRANSACTION_TABLE, actor="user:priya",
                        now=datetime.now(UTC))
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["provenance"]["eligibility"] == {
        **r.json()["provenance"]["eligibility"], "confirmed": True, "status": ""}


def test_a_RE_DECLARED_definition_of_which_rows_count_refuses_the_sealed_plan(make_client, bank,
                                                                              sealed):
    """The seventh pin, over HTTP. Before it existed this ran clean and answered under the OLD
    definition of "a transaction that counts" — while a re-declared TEMPORAL policy refused."""
    record_eligibility(bank, catalog_source=SRC, table=TRANSACTION_TABLE,
                       policy=_eligibility(included_status_values=("POSTED", "PENDING")),
                       proposed_by="user:sam")
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 409
    refusal = r.json()["refusal"]
    assert refusal["code"] == SEALED_PLAN_STALE_ELIGIBILITY
    assert refusal["sealed"] != refusal["current"]
    # NO RESTATEMENT CLAIM, as with every other pin: this is about THIS run.
    assert "correct" not in r.text.lower()


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


# ── read scope precedes every 409 ────────────────────────────────────────────────────────────────
#
# The order used to be flag -> replay -> engine connection (409 NAMING the event dataset) ->
# provider (409) -> scope, so a caller holding a valid plan hash learned that a sealed plan over a
# catalog they were never granted exists here — twice, before any scope was read.


def _hide(bank, table: str) -> None:
    """`visible_requires` is GENERATED (migration 1032) from the sensitivity tag, so the TAG is what
    a test sets; a direct write to the enforcement column is refused by the database."""
    bank.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'", (SRC, table))


def test_a_caller_who_may_not_read_the_plans_data_gets_a_404_INDISTINGUISHABLE_from_absence(
        make_client, bank, sealed):
    """Byte-identical bodies, not merely the same status: a difference anywhere in the payload is
    the oracle rebuilt in a field nobody looked at."""
    client = _client(make_client, bank)
    absent = client.post("/analysis/execute", json={"plan_hash": "f" * 64}, headers=_h())

    _hide(bank, TRANSACTION_TABLE)
    hidden = client.post("/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())

    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == {**absent.json(), "refusal": {
        **absent.json()["refusal"], "subjects": [sealed.plan_hash]}}
    # The plan's own datasets appear NOWHERE — the hash the caller supplied is all that comes back.
    assert TRANSACTION_TABLE not in hidden.text
    assert "customer_master" not in hidden.text


def test_read_scope_is_checked_BEFORE_the_engine_connection_409(make_client, bank, sealed):
    """The ordering, pinned at the seam that used to leak. With the catalog engine withdrawn the
    privileged path 409s; the unprivileged one must still get the not-found, because the 409 is a
    statement about a plan they may not know exists."""
    bank.execute("DELETE FROM catalog_engine WHERE catalog_source = %s", (SRC,))
    bank.execute("DELETE FROM physical_dataset_binding WHERE catalog_source = %s", (SRC,))
    client = _client(make_client, bank)

    privileged = client.post("/analysis/execute", json={"plan_hash": sealed.plan_hash},
                             headers=_h())
    assert privileged.status_code == 409
    # …and it names no dataset: "which table" adds nothing an operator acts on here.
    assert TRANSACTION_TABLE not in privileged.text
    assert "a sealed dataset can no longer be addressed" in privileged.text

    _hide(bank, TRANSACTION_TABLE)
    unprivileged = client.post("/analysis/execute", json={"plan_hash": sealed.plan_hash},
                               headers=_h())
    assert unprivileged.status_code == 404


def test_read_scope_is_checked_BEFORE_the_engine_provider_409(make_client, bank, sealed):
    """The other 409 that used to run first. EXECUTION_ENGINE_NOT_APPROVED is a true statement
    about this deployment and still an existence oracle when the asker may not see the plan."""
    _hide(bank, TRANSACTION_TABLE)
    r = _client(make_client, bank, engine=False).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert r.status_code == 404
    assert EXECUTION_ENGINE_NOT_APPROVED not in r.text


def test_a_PRIVILEGED_caller_keeps_every_one_of_the_409s(make_client, bank, sealed):
    """The check narrows nothing for a caller who may read the data — the drift refusal, the
    approval refusal and the successful run all still reach them."""
    client = _client(make_client, bank)
    assert client.post("/analysis/execute", json={"plan_hash": sealed.plan_hash},
                       headers=_h()).status_code == 200
    assert _client(make_client, bank, engine=False).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash},
        headers=_h()).status_code == 409
    publish_temporal_policy_for(bank, expected_pointer_version=1,
                                historical_selection=TemporalSelectionKind.CURRENT_RECORD)
    stale = client.post("/analysis/execute", json={"plan_hash": sealed.plan_hash}, headers=_h())
    assert stale.status_code == 409
    assert stale.json()["refusal"]["code"] == SEALED_PLAN_STALE_TEMPORAL_POLICY


# ── the planning surface hands out an identity that REPLAYS ──────────────────────────────────────
#
# Every other test here seals through the SERVICE, so nothing proved the HTTP planning route ever
# produced a `sealed_plan_hash` at all. It did not: retrieval offers the flattened
# `source::table.column` ref and the selection contracts require `source::schema.table`, so every
# LLM-planned question refused SELECTION_BINDING_MISSING at every need and the hash was always
# `None`. This is that chain, over HTTP, end to end.


def _intent_output(**over) -> dict:
    """What the model returns, in the refs RETRIEVAL offers — flattened, exactly as the real one
    would, because it may only name refs from the offered set."""
    out = {
        "entity": "customer",
        # The column that IDENTIFIES a customer, chosen from the offered set — the transaction
        # table's `cif_id` is not offered (it carries no governed grain and matches no lexeme in
        # this question), and the model may only name what it was given.
        "entity_ref": f"{SRC}::{CUSTOMER_TABLE}.cif_id",
        "base_table_ref": f"{SRC}::{TRANSACTION_TABLE}",
        "measure": {"op": "count", "logical_ref": ""},
        "windows": [
            {"label": "current", "anchor_ref": f"{SRC}::{TRANSACTION_TABLE}.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0},
            {"label": "previous", "anchor_ref": f"{SRC}::{TRANSACTION_TABLE}.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 1}],
        "dimensions": [{"logical_ref": f"{SRC}::{DIMENSION_TABLE}.segment"}],
        "comparison": "decrease", "unresolved": [],
    }
    out.update(over)
    return out


@pytest.fixture
def planning_bank(bank):
    """The pilot bank plus the freshness watermark retrieval reads. Without it every column reads
    as stale and the question matches nothing."""
    bank.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id) "
        "VALUES (%s, %s, 'r1') ON CONFLICT (catalog_source) DO UPDATE "
        "  SET last_completed_at = EXCLUDED.last_completed_at", (SRC, datetime.now(UTC)))
    return bank


def _planning_client(make_client, bank):
    from featuregen.analysis.intent import TASK
    from featuregen.intake.llm import FakeLLM, FakeResponse

    client = make_client(llm_client=FakeLLM(script={TASK: FakeResponse(output=_intent_output())}))
    client.app.dependency_overrides[get_analysis_engine] = lambda: (
        lambda _connection: AnalysisEngineV1(conn=bank, dialect=PostgresDialect(),
                                             engine="postgres"))
    return client


def test_the_PLANNING_route_returns_a_sealed_plan_hash_and_it_replays(make_client, planning_bank):
    """Plan -> the population clarification -> answer it -> a sealed hash that REPLAYS and RUNS.

    The population is asked for rather than supplied, and that is the doctrine working, not a
    detour: `extract_intent` raises `population` on every comparison question because choosing among
    look-alike population tables is inference, so `/analysis/plan` can only seal once a PERSON has
    named one. The route asserts the hash is offered at both steps and is `None` until then.
    """
    from featuregen.analysis.sealed_plan_store import replay_sealed_plan

    client = _planning_client(make_client, planning_bank)
    planned = client.post("/analysis/plan", json={"question": QUESTION}, headers=_h())
    assert planned.status_code == 200, planned.text
    # The KEY is always offered; it is `None` while nobody has declared the population.
    assert planned.json()["sealed_plan_hash"] is None
    assert "population" in {c["code"] for c in planned.json()["clarifications"]}

    answered = client.post("/analysis/clarify", headers=_h(), json={
        "question": QUESTION, "code": "population",
        "chosen": [f"{SRC}::{CUSTOMER_TABLE}.cif_id"]})
    assert answered.status_code == 200, answered.text
    plan_hash = answered.json()["sealed_plan_hash"]
    assert plan_hash, answered.json()["preview"]["blocked_by"]

    # IT REPLAYS: the identity the caller was handed addresses the one canonical sealed plan.
    replayed = replay_sealed_plan(planning_bank, plan_hash)
    assert replayed is not None
    assert replayed.plan_hash == plan_hash
    assert {s.need_role for s in replayed.sources} == {"population", "event_source",
                                                       "dimension_source"}
    # …and the seventh pin is on it, sealed by the route rather than by a test helper.
    assert replayed.eligibility is not None


def test_the_hash_the_PLANNING_route_returns_is_the_one_execute_accepts(make_client,
                                                                        planning_bank):
    """The two surfaces meet. A hash that planned but could not execute would be a governance
    handle nobody can use."""
    client = _planning_client(make_client, planning_bank)
    client.post("/analysis/plan", json={"question": QUESTION}, headers=_h())
    plan_hash = client.post("/analysis/clarify", headers=_h(), json={
        "question": QUESTION, "code": "population",
        "chosen": [f"{SRC}::{CUSTOMER_TABLE}.cif_id"]}).json()["sealed_plan_hash"]

    run = client.post("/analysis/execute", json={"plan_hash": plan_hash}, headers=_h())
    assert run.status_code == 200, run.text
    assert run.json()["plan_hash"] == plan_hash
    assert run.json()["provenance"]["contract_version"] == 2


def test_execution_requires_the_feature_generate_permission(make_client, bank, sealed):
    r = _client(make_client, bank).post(
        "/analysis/execute", json={"plan_hash": sealed.plan_hash},
        headers=_h("catalog_viewer"))
    assert r.status_code == 403
