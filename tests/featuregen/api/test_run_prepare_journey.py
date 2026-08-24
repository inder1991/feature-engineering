"""ONE run, walked end to end the way an operator walks it (spec §R4.4.5).

NEVER `conn.commit()` here — the shared test conn rolls back.

Every other suite in this stream proves one seam: the rail's honesty, the trigger's refusals, the
retry's governance, the two readings of the authoring history. This one proves they are the SAME
RUN. It is deliberately the only test here that drives the real workers, because the defects it
exists to catch are the ones that live BETWEEN the parts and that no unit test can see:

* the page offering a control the entrance refuses (or refusing one it would have accepted);
* the rail folding a stage from one set of rows while the history beside it renders another;
* the jobs list disagreeing with the gesture that just added to it;
* an attempt that failed shadowing the answer that replaced it, or the reverse.

**SEEDED LIKE PRODUCTION.** What a route could not have written is seeded — the creation chain, the
identity row, the frozen catalog and snapshot, the target reading and the selections — and every ACT
goes through the door a person or a worker actually uses: the declaration through
`POST /code-generation-jobs`, the gesture through `POST /feature-runs/{id}/prepare-code`, the drafts
through the coordinator, the relay and the draft worker, the approval through
`POST /formula-drafts/{id}/regeneration-exceptions` behind governance headers, the retry through
`POST /feature-runs/{id}/authoring-retries`. The seeded rows carry the values production would carry
rather than plausible-looking ones: a selection gets the candidate's REAL `planning_request_hash`,
because 1101's binding refuses a selection whose plan hash disagrees with the draft pinned to it,
and a fixture hash would have blocked the build on a disagreement the fixture itself invented.

**ASSERTED LIKE AN OPERATOR.** Codes by their exact server string (or the imported constant);
`remedy` by presence and type only — the store's sentence is its own to change, and a test that
pinned its words would fail on an improvement.

⟨LLM⟩ The provider is a scripted `FakeLLM`, as every authoring suite in this tree is: what is under
test is the WIRING between the run page and the lanes, and a live provider would make this a
billing question rather than a correctness one.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.api.test_feature_run_retry import (
    _CEILING,
    _GOVERNANCE,
    _assert_page_and_entrance_agree,
    _hdr,
    _row,
)
from tests.featuregen.api.test_feature_run_trigger import _APPROVAL, _declare
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
    seed_authoring_catalog,
)
from tests.featuregen.materialize.test_admission_v2_s13 import ENGINE, _advertise, _client, _raw_v3
from tests.featuregen.runs._chain import considered_json, seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.code_generation_coordinator import process_code_generation_once
from featuregen.overlay.upload.code_generation_job_store import JobStatusV1, read_job
from featuregen.overlay.upload.formula_draft_service import (
    FORMULA_DRAFT_HANDLER,
    FORMULA_DRAFT_TOPIC,
)
from featuregen.overlay.upload.formula_draft_worker import (
    FORMULA_DRAFT_ENGINE_ENV,
    process_formula_draft_once,
)
from featuregen.runtime.outbox import make_queue_publisher, relay_publish_batch

PREPARE = "/feature-runs/{run}/prepare-code"
RETRY = "/feature-runs/{run}/authoring-retries"
DRAFT = "/considered-revisions/{rev}/options/{opt}/formula-drafts"
APPROVE = "/formula-drafts/{draft}/regeneration-exceptions"

#: The one person in this journey. The header identity (`_hdr`'s default) resolves to this subject,
#: the run is owned by it, and the draft worker re-resolves it against local IAM before it will read
#: the catalog on its behalf — which is why `_seed` creates the account rather than assuming one.
SUBJECT = "user:priya"
USERNAME = "priya"

#: The snapshot fields the frozen catalog carries for every ref the candidates ground on. Three,
#: because that is what `FrozenRecipeReadContext` reads and what the monetary facts need.
_SNAPSHOT_FIELDS = (("logical_representation", "decimal"), ("unit", "monetary"),
                    ("currency", "fixed:AED"))

#: What the rail's five sockets read in a test deployment: two derived from switches that are OFF,
#: two derived from a production policy that authorizes nothing yet, one literal. Pinned in the
#: projection's own suite; quoted here so the journey notices if a stage starts claiming otherwise.
_SOCKETS = {"EXECUTE_SANDBOX": "MATERIALIZATION_DISABLED",
            "PUBLISH_SANDBOX": "WORKER_NOT_IMPLEMENTED",
            "MATERIALIZE_PRODUCTION": "ACTION_UNAVAILABLE",
            "PUBLISH_PRODUCTION": "ACTION_UNAVAILABLE",
            "TRAIN_MODEL": "SUBSYSTEM_NOT_BUILT"}


@pytest.fixture
def deployment(monkeypatch):
    """The deployment this journey runs in, and the scripted provider standing in for the model.

    `FEATUREGEN_DSN` is deliberately UNSET: given one, the replay orchestrator opens its own
    connection and commits the authoring run durably — correct in production, and rows that would
    survive this suite's rollback. `_renew` is stubbed for the same reason (it renews on its own
    connection by design). Both follow `test_formula_draft_end_to_end`'s lane fixture, which is
    where those behaviours are actually under test.

    Returns the provider CALL COUNT, so the journey can state where money was and was not spent.
    """
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")
    monkeypatch.setenv(FORMULA_DRAFT_ENGINE_ENV, ENGINE)
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker._renew", lambda *a, **k: None)
    asked = {"n": 0}

    def _counted_client():
        asked["n"] += 1
        return _client(_raw_v3())

    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker.current_llm_client", _counted_client)
    return asked


def _idea(name: str):
    """One candidate the LLM lane can actually author.

    Grounded on the fixture catalog's REAL columns and carrying the grain it is computed per:
    `formula_draft_worker._frozen_facts` blocks a candidate with no `grain_refs` and no
    `derives_pairs`, and no fallback guesses either — so `_chain.feature_idea`, which carries
    neither, is not the shape this journey needs.
    """
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    columns = [ref.split("::", 1) for ref in (REF_AMT, REF_DT, REF_CIF)]
    return FeatureIdea(
        name=name, description="recent debit volume",
        derives_from=[ref for _source, ref in columns],
        derives_pairs=tuple((source, ref) for source, ref in columns),
        aggregation="sum", grain_table="account", grain_refs=(("authored", REF_CIF),))


def _seed_snapshot_items(conn, snapshot_id: str) -> None:
    """The FROZEN catalog the model reads — the rows `FrozenRecipeReadContext.load` looks for.

    Written BEFORE the snapshot header, which is the production order: the header write SEALS the
    set and a later item insert is refused by trigger. That is why this runs ahead of
    `seed_run_chain`, which is what writes the header.
    """
    for ref in (TABLE_REF, REF_AMT, REF_DT, REF_CIF):
        for field, value in _SNAPSHOT_FIELDS:
            conn.execute(
                "INSERT INTO catalog_metadata_snapshot_item (snapshot_id, catalog_source, "
                "graph_ref, logical_ref, item_kind, field_or_fact_type, value_json, "
                "authority_json, item_hash) "
                "VALUES (%s,%s,%s,%s,'field',%s,%s::jsonb,%s::jsonb,%s)",
                (snapshot_id, "authored", ref, ref, field, json.dumps({"value": value}),
                 json.dumps({"status": "resolved", "authority": "governed"}),
                 f"sha256:{snapshot_id}:{ref}:{field}"))


def _seed(conn, *, run_id: str, options=("opt-a", "opt-b"), with_identity: bool = True) -> dict:
    """One spine run whose candidates can be authored for real: the governed catalog, the frozen
    snapshot, the creation chain, the identity row, an advertised engine, the requester's account,
    and one `feature_selection_revision` per candidate.

    ▲ THE SELECTION CARRIES THE CANDIDATE'S OWN `planning_request_hash`, read back from the frozen
    revision rather than invented. 1101's binding refuses a selection and a draft that disagree
    about the planning request, so a fixture hash would block the build stage on a disagreement the
    fixture created — and the journey would be proving something about its own seeding.
    """
    from featuregen.identity.local_session import create_user
    from featuregen.overlay.upload.formula_draft_service import frozen_candidate
    from featuregen.runs.run_identity import record_run_identity

    seed_authoring_catalog(conn)
    _advertise(conn)
    create_user(conn, USERNAME, "not-a-real-password")

    snapshot_id = f"{run_id}-snap"
    _seed_snapshot_items(conn, snapshot_id)
    chain = seed_run_chain(
        conn, run_id=run_id, subject=SUBJECT, snapshot_id=snapshot_id,
        considered_json=considered_json(
            [(o, _idea(f"debit_volume_{o.replace('-', '_')}")) for o in options]))
    if with_identity:
        record_run_identity(conn, run_id, IdentityEnvelope(
            subject=SUBJECT, actor_kind="human", authenticated=True, auth_method="test",
            role_claims=("feature_engineer",)))

    reading = f"{run_id}-trr"
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s, %s, 'exploration', 'h') ON CONFLICT DO NOTHING",
        (reading, chain["intent_id"]))
    selections = {}
    for option_id in options:
        selection = f"{run_id}-sel-{option_id}"
        candidate = frozen_candidate(conn, chain["considered_revision_id"], option_id)
        conn.execute(
            "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
            "considered_revision_id, option_id, decision_id, planning_request_hash, "
            "binding_plan_hash, content_hash) VALUES (%s,%s,%s,%s,%s,%s,'sha256:plan',%s) "
            "ON CONFLICT DO NOTHING",
            (selection, reading, chain["considered_revision_id"], option_id,
             f"dec-{selection}", candidate.planning_request_hash, f"ch-{selection}"))
        selections[option_id] = selection
    return {**chain, "reading": reading, "selections": selections}


def _relay(conn) -> int:
    """The outbox → queue hop, through the PRODUCTION route pair.

    A draft's work is enqueued transactionally with the draft (`insert_outbox_message_checked`), and
    the relay is what turns that row into a queue message the draft worker can claim. Enqueuing
    directly would skip the one step that was actually missing when this lane shipped: a topic with
    no route was marked `sent` and the draft sat at REQUESTED for ever.
    """
    return relay_publish_batch(
        conn, make_queue_publisher({FORMULA_DRAFT_TOPIC: FORMULA_DRAFT_HANDLER}), owner="w-relay")


def _run_the_workers(conn, run_id: str, *, ticks: int = 20) -> None:
    """Tick every worker this journey needs until the run's jobs stop moving.

    In the order a deployment runs them: the coordinator drives ONE job one stage per claim, the
    relay publishes what that stage enqueued, and the draft worker drains the drafts it published.
    Bounded rather than `while True` — a lane that stops making progress must fail this test
    loudly instead of hanging the suite.
    """
    settled = {JobStatusV1.GENERATING_PREVIEW, *(s for s in JobStatusV1 if s.is_terminal)}
    for _ in range(ticks):
        if all(read_job(conn, job_id).status in settled for job_id in _job_ids(conn, run_id)):
            return
        process_code_generation_once(conn, worker_id="w-jobs")
        _relay(conn)
        while process_formula_draft_once(conn, owner="w-drafts").status != "idle":
            pass
    raise AssertionError("the lane never settled — a worker is not making progress")


def _job_ids(conn, run_id: str) -> list[str]:
    """This run's jobs by the projection's own bridge, so the loop watches what the page shows."""
    from featuregen.runs.projection import run_jobs

    return [job["job_id"] for job in run_jobs(conn, run_id)]


def _detail(client, run_id: str) -> dict:
    return client.get(f"/feature-runs/{run_id}", headers=_hdr()).json()


def _rail(detail: dict) -> dict:
    return {stage["stage"]: stage for stage in detail["rail"]}


def _drafts_by_option(detail: dict) -> dict:
    """The CURRENT reading, keyed by candidate — where each feature stands, per §R4.4.1."""
    return {row["option_id"]: row for row in detail["authoring"]["current"]}


# ══ before anything has happened: the page says so, and offers nothing it cannot do ═════════════
def test_the_run_page_is_honest_before_the_journey_starts(client, conn, deployment):
    """Step 2 of the journey, taken on its own because it is the state a person MEETS the run in.

    Nothing has been authored, nothing declared. Every claim on the page is either a fact about
    this run or a named absence, and the one control it could offer is refused with the remedy that
    belongs to a person rather than to this run.
    """
    _seed(conn, run_id="jny-fresh")

    detail = _detail(client, "jny-fresh")

    assert detail["pre_spine"] is False and detail["identity"]["considered_revision_id"]
    assert detail["owner_subject"] == SUBJECT
    assert detail["authoring"] == {"current": [], "history": []}
    assert detail["jobs"] == []
    by_stage = _rail(detail)
    # CHOOSE_CANDIDATES is NOT_STARTED because no Gate-1 choice exists — the world the platform is
    # actually in (`contract_gate1_choice_revision` holds zero live rows), and the projection's own
    # documented case rather than a gap in this fixture.
    assert by_stage["CHOOSE_CANDIDATES"]["state"] == "NOT_STARTED"
    assert by_stage["AUTHOR_FORMULA"]["state"] == "NOT_STARTED"
    assert by_stage["BIND_SELECTIONS"]["state"] == "NOT_STARTED"
    # The one stage whose availability moves with the deployment: 1101 is applied here and the
    # switch is on, so the entrance is open and NOT_STARTED is the honest word.
    assert by_stage["GENERATE_PREVIEW"] == {
        "stage": "GENERATE_PREVIEW", "state": "NOT_STARTED", "reason_code": None}
    assert {s: by_stage[s]["reason_code"] for s in _SOCKETS} == _SOCKETS
    assert all(by_stage[s]["state"] == "UNAVAILABLE" for s in _SOCKETS)

    # ▲ THE CONTROL AND THE ENTRANCE, FROM ONE ANSWER. Nobody has declared a build for this run, so
    # the page greys the gesture out — and the route refuses the same click with the same code and
    # the same sentence.
    gate = detail["prepare_code"]
    assert gate["available"] is False
    assert gate["reason_code"] == "RUN_BUILD_DECLARATION_ABSENT"
    refusal = client.post(PREPARE.format(run="jny-fresh"), json={"option_ids": ["opt-a"]},
                          headers=_hdr())
    assert refusal.status_code == 409, refusal.text
    assert refusal.json()["detail"]["code"] == gate["reason_code"]
    assert refusal.json()["detail"]["detail"] == gate["detail"]


def test_a_PRE_SPINE_run_says_so_rather_than_offering_a_journey_it_cannot_start(
        client, conn, deployment):
    """The honest gap (spec §7): a run recorded before the identity spine has no frozen considered
    set, so there is nothing a build could be a build OF. The page says PRE_SPINE and the gesture
    refuses in the same word — a gap in the RECORD, never a failure of the run."""
    _seed(conn, run_id="jny-pre", with_identity=False)

    detail = _detail(client, "jny-pre")

    assert detail["pre_spine"] is True and detail["identity"] is None
    assert detail["prepare_code"]["reason_code"] == "PRE_SPINE_NOT_ACTIONABLE"
    refusal = client.post(PREPARE.format(run="jny-pre"), json={"option_ids": ["opt-a"]},
                          headers=_hdr())
    assert refusal.status_code == 409, refusal.text
    assert refusal.json()["detail"]["code"] == "PRE_SPINE_NOT_ACTIONABLE"


# ══ the whole journey ═══════════════════════════════════════════════════════════════════════════
def test_THE_WHOLE_JOURNEY_from_prepare_code_through_a_governed_retry(client, conn, deployment):
    """Steps 3-7 of §R4.4.5, in one transaction, on one run.

    A person declares a build, prepares code for two candidates, watches both formulas get
    authored, meets a failure, is refused the free re-click, has a governance approval recorded,
    retries from the run page, and sees both attempts on the record with the new one standing for
    the candidate. Every reading in between comes from `GET /feature-runs/{id}` — the page itself.
    """
    run = _seed(conn, run_id="jny-go")
    declaring_job = _declare(client, run, options=("opt-a",))

    # ── 3. the gesture: two candidates, one job, and the run page lists it ───────────────────────
    gesture = client.post(PREPARE.format(run="jny-go"),
                          json={"option_ids": ["opt-a", "opt-b"], "spend_approval": _APPROVAL},
                          headers=_hdr())

    assert gesture.status_code == 202, gesture.text
    body = gesture.json()
    assert body["created"] is True and body["job_id"] != declaring_job
    # The reuse is NAMED: this build continues the declaration somebody made through the other door.
    assert body["declaration_source_job_id"] == declaring_job
    job_id = body["job_id"]
    # The answer already carried the refreshed run, and a re-read of the page agrees with it — one
    # derivation, not an echo the store might not have accepted.
    assert [j["job_id"] for j in body["run"]["jobs"]] == \
        [j["job_id"] for j in _detail(client, "jny-go")["jobs"]]
    assert {j["job_id"] for j in body["run"]["jobs"]} == {job_id, declaring_job}
    fresh = next(j for j in body["run"]["jobs"] if j["job_id"] == job_id)
    assert fresh["status"] == "REQUESTED"
    assert [a["action"] for a in fresh["actions"]] == ["AUTHOR_FORMULA", "GENERATE_PREVIEW"]

    # ── 4. the workers, driven for real, until both candidates carry a formula ───────────────────
    _run_the_workers(conn, "jny-go")
    # TWO candidates, TWO paid runs — counted on the provider, because "the code returned early" is
    # a claim about control flow and the number of times a paid model was asked is the fact. THREE
    # draft requests were made (the declaring job asked for opt-a, and the gesture asked for both),
    # so this is also the money guard's dedup: the second request for opt-a landed on the draft that
    # already existed instead of buying the same answer again.
    assert deployment["n"] == 2, "the LLM lane bought a formula it already had"

    detail = _detail(client, "jny-go")
    current = _drafts_by_option(detail)
    assert sorted(current) == ["opt-a", "opt-b"]
    assert [row["state"] for row in current.values()] == ["READY", "READY"]
    assert all(row["resolved"] is True for row in current.values())
    # A READY draft is an ANSWER: it holds the identity slot, so "retry" is not the question there
    # — false with NO blockers, which is the ABSENCE of the question rather than a refusal.
    assert all(row["retryable"] is False and row["retry_blockers"] == []
               for row in current.values())
    # Every attempt is on the record, and with one attempt each the two readings agree.
    assert [row["formula_draft_id"] for row in detail["authoring"]["history"]] == \
        [row["formula_draft_id"] for row in current.values()]
    assert _rail(detail)["AUTHOR_FORMULA"]["state"] == "SUCCEEDED"

    # ── the milestone fold, and the jobs list moving with the store that owns it ─────────────────
    # ONE derivation, two surfaces: the count on the rail and the list under it are the same read of
    # the same pins, so a person reading "2 bound" can see WHICH two. IN_PROGRESS rather than
    # SUCCEEDED is the honest word here, and the projection says why: with no Gate-1 choice on the
    # record there is no denominator, and a stage cannot claim to be finished against nothing.
    bind = _rail(detail)["BIND_SELECTIONS"]
    assert bind["state"] == "IN_PROGRESS" and bind["detail"] == "2 bound"
    assert {b["option_id"] for b in detail["milestones"]["bind_selections"]} == {"opt-a", "opt-b"}
    assert {b["formula_draft_id"] for b in detail["milestones"]["bind_selections"]} == \
        {row["formula_draft_id"] for row in current.values()}, \
        "a binding pins the formula the page shows, not some other draft for the candidate"
    # The page reports the JOB STORE's own word, verbatim: both journeys walked from REQUESTED to
    # the generation the lane performs, and each act they performed is named.
    assert [j["status"] for j in detail["jobs"]] == ["GENERATING_PREVIEW", "GENERATING_PREVIEW"]
    assert all(a["state"] == "PERFORMED" for j in detail["jobs"] for a in j["actions"])

    # ── 5. a failure, and the free re-click that is refused by name ──────────────────────────────
    failed_draft = current["opt-b"]["formula_draft_id"]
    conn.execute(
        "UPDATE formula_draft SET state = 'FAILED', failure_reason = 'the provider refused' "
        "WHERE formula_draft_id = %s", (failed_draft,))

    refusal = client.post(
        DRAFT.format(rev=run["considered_revision_id"], opt="opt-b"), headers=_hdr())

    assert refusal.status_code == 409, refusal.text
    assert refusal.json()["detail"]["code"] == "FORMULA_DRAFT_NOT_AN_ANSWER"
    # The remedy is asserted by PRESENCE and TYPE only: the sentence belongs to the store that
    # writes it, and pinning its words here would fail the day somebody improves it.
    assert isinstance(refusal.json()["detail"]["remedy"], str)
    assert refusal.json()["detail"]["remedy"]
    assert conn.execute(
        "SELECT COUNT(*) FROM formula_draft WHERE option_id = 'opt-b'").fetchone() == (1,), \
        "the refused re-request bought nothing"

    # ── and the PAGE says the same thing the door said ───────────────────────────────────────────
    failed_row = _row(client, "jny-go", failed_draft)
    assert failed_row["retryable"] is False
    assert [b["code"] for b in failed_row["retry_blockers"]] == ["FORMULA_DRAFT_NOT_AN_ANSWER"]
    assert failed_row["retry_blockers"][0]["detail"], "a code with no sentence is a dead end"
    # ▲ AND THE RUN PAGE'S OWN DOOR REFUSES IDENTICALLY. A control the page greys out over an
    # entrance that would have accepted — or offers over one that refuses — is §7 [R3.1]'s false
    # rail with a click in it, so the row's first blocker and the 409 are the same code AND the
    # same sentence rather than two copies somebody maintains.
    _assert_page_and_entrance_agree(
        client, "jny-go", failed_draft,
        client.post(RETRY.format(run="jny-go"), json={"formula_draft_id": failed_draft},
                    headers=_hdr()))
    # The candidate that is FINE is unaffected: one failure does not fold the whole run.
    assert _drafts_by_option(_detail(client, "jny-go"))["opt-a"]["state"] == "READY"
    assert _rail(_detail(client, "jny-go"))["AUTHOR_FORMULA"]["state"] == "FAILED"

    # ── 6. the governance approval, then the retry from the run page ─────────────────────────────
    # A FRESH ceiling, through the production approval route behind governance headers: re-buying a
    # failed answer is somebody taking responsibility for the spend, not the engineer's own click.
    approved = client.post(APPROVE.format(draft=failed_draft), json=_CEILING, headers=_GOVERNANCE)
    assert approved.status_code == 201, approved.text

    offered = _row(client, "jny-go", failed_draft)
    assert offered["retryable"] is True and offered["retry_blockers"] == []

    asked_before = deployment["n"]
    retry = client.post(RETRY.format(run="jny-go"), json={"formula_draft_id": failed_draft},
                        headers=_hdr())

    assert retry.status_code == 202, retry.text
    retried = retry.json()
    assert retried["created"] is True and retried["formula_draft_id"] != failed_draft
    # 202 means REQUESTED, not authored: the click queues the work and the worker pays for it.
    assert deployment["n"] == asked_before, "the retry click bought a provider call inline"
    # The coupon was consumed exactly once, by the mint it authorized.
    assert conn.execute(
        "SELECT uses_consumed, max_uses FROM formula_draft_regeneration_exception "
        "WHERE exception_id = %s", (approved.json()["exception_id"],)).fetchone() == (1, 1)

    # ── 7. the two readings, and one answer between the rail and the history ─────────────────────
    after = retried["run"]
    assert after == _detail(client, "jny-go"), \
        "the answer's run and a re-read of the page are one derivation, not two"
    history = [row["formula_draft_id"] for row in after["authoring"]["history"]
               if row["option_id"] == "opt-b"]
    assert history == [failed_draft, retried["formula_draft_id"]], \
        "both attempts, in the order they happened"
    current = _drafts_by_option(after)
    assert current["opt-b"]["formula_draft_id"] == retried["formula_draft_id"]
    assert current["opt-b"]["state"] == "REQUESTED" and current["opt-b"]["resolved"] is True
    assert current["opt-a"]["state"] == "READY", "the answered candidate did not move"
    # ▲ THE FOLD IS OVER THE CURRENT READINGS, NEVER THE HISTORY. Folding every attempt would let
    # the failure that has just been retried past shadow the run: IN_PROGRESS is the honest word
    # while one candidate is being bought again and the other already has its formula.
    assert _rail(after)["AUTHOR_FORMULA"]["state"] == "IN_PROGRESS"
    # The jobs list did not move under the retry — a retry buys a DRAFT, never a build.
    assert [j["job_id"] for j in after["jobs"]] == [j["job_id"] for j in body["run"]["jobs"]]
    # ...and the gesture's job is still the one a person can watch, with the acts it performs.
    assert set(_job_ids(conn, "jny-go")) == {job_id, declaring_job}
    # The gesture remains available: the run has a declaration, so the page keeps offering the one
    # control it can honestly offer.
    assert after["prepare_code"] == {"available": True, "reason_code": None, "detail": None}
