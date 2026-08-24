"""The run→job trigger (spec §R4.4.2): `POST /feature-runs/{run_id}/prepare-code`.

NEVER `conn.commit()` here — the shared test conn rolls back.

The rules under test are the ones that leak, cost money, or lie:

1. **§11 first**: a caller who may not see a run gets the SAME body for triggering it as for a run
   that does not exist. A distinguishable answer would confirm the id.
2. **PRE_SPINE is not actionable**, by its own code — a run with no frozen considered set has
   nothing a build could be a build OF.
3. **The gesture is the coordinator's**, not a second authority: the job it produces is a real
   `code_generation_job` with real members, and a replay of the same gesture lands on it rather
   than buying it again.
4. **The five declarations are never invented.** A run nobody has declared a build for is refused
   by name, and a run that HAS one has the declaration REUSED and the source job named.
5. **`feature:generate` gates the write** while `feature:read` gates the reads beside it.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import APPROVAL_EXPIRES_AT
from tests.featuregen.materialize.crosswalk_fixtures import build_set_declaration
from tests.featuregen.runs._chain import considered_json as _considered_json
from tests.featuregen.runs._chain import feature_idea as _idea
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.build_set_declaration import encode_declaration

PREPARE = "/feature-runs/{run}/prepare-code"
JOBS = "/code-generation-jobs"

#: The six approval keys the code-generation screen sends, unchanged: this route accepts the SAME
#: confirmation, because it is the same act of agreeing to a cost. The expiry is the shared
#: fixture window — `ExpiryWindow` bounds it to 168 hours, so a fixed date here would be a fixture
#: that expires (see `_helpers.APPROVAL_EXPIRES_AT`).
_APPROVAL = {"max_calls": 90, "max_tokens": 200_000, "max_cost": "12.50", "currency": "USD",
             "pricing_version": "pricing@1", "expires_at": APPROVAL_EXPIRES_AT}


@pytest.fixture
def enabled(monkeypatch):
    """The deployment switch the whole code-generation journey rides. Default OFF, so a test that
    means to exercise the journey has to say so."""
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")


def _hdr(user="priya", roles="feature_engineer"):
    return {"X-User": user, "X-Roles": roles}


def _seed_run(conn, *, run_id="trg", subject="user:priya", with_identity=True,
              options=("opt-a", "opt-b")):
    """One spine run whose candidates can actually be built: the full creation chain, the identity
    row, a target reading, and one `feature_selection_revision` per candidate."""
    from featuregen.runs.run_identity import record_run_identity

    ideas = [(o, _idea(f"avg_balance_{o.replace('-', '_')}", "deposits")) for o in options]
    chain = seed_run_chain(conn, run_id=run_id, subject=subject,
                           considered_json=_considered_json(ideas))
    if with_identity:
        record_run_identity(conn, run_id, IdentityEnvelope(
            subject=subject, actor_kind="human", authenticated=True, auth_method="test",
            role_claims=("feature_engineer",)))
    reading = f"{run_id}-trr"
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s, %s, 'exploration', 'h') ON CONFLICT DO NOTHING",
        (reading, chain["intent_id"]))
    selections = {}
    for option_id in options:
        selection = f"{run_id}-sel-{option_id}"
        conn.execute(
            "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
            "considered_revision_id, option_id, decision_id, planning_request_hash, "
            "binding_plan_hash, content_hash) VALUES (%s,%s,%s,%s,%s,'sha256:ask','sha256:plan',%s)"
            " ON CONFLICT DO NOTHING",
            (selection, reading, chain["considered_revision_id"], option_id,
             f"dec-{selection}", f"ch-{selection}"))
        selections[option_id] = selection
    return {**chain, "reading": reading, "selections": selections}


def _assert_page_and_entrance_agree(client, run_id, refusal):
    """▲ THE DRIFT CHECK. A button the page offers over an entrance that refuses is §7 [R3.1]'s
    false rail with a click in it — so the run page's `prepare_code` answer and the 409 this route
    raises must be the SAME code and the SAME sentence, not two independently maintained copies."""
    gate = client.get(f"/feature-runs/{run_id}", headers=_hdr()).json()["prepare_code"]
    assert gate["available"] is False
    assert gate["reason_code"] == refusal.json()["detail"]["code"]
    assert gate["detail"] == refusal.json()["detail"]["detail"]


def _declare(client, run, *, options=("opt-a",), group=None):
    """The FIRST job for this run, made the only way a declaration can be made: through the
    code-generation route, whose body carries the five declarations a person decides.

    This is the honest precondition of the whole gesture, not a fixture convenience — see
    `prepare_run_code`'s docstring: the run page CONTINUES a declared build, it cannot start one.
    """
    response = client.post(JOBS, headers=_hdr(), json={
        "considered_revision_id": run["considered_revision_id"],
        "target_reading_revision_id": run["reading"],
        "selection_revision_ids": [run["selections"][o] for o in options],
        "environment_id": "hdfc-local",
        "logical_group_name": group or f"grp-{run['run_id']}",
        "declaration": encode_declaration(build_set_declaration()),
        "execution_parameters": {"engine_id": "kedro-pyspark",
                                 "physical_type_policy": "formula-v2/physical-types@1",
                                 "empty_values": {}, "target_mode": "exploration"},
        "spend_approval": _APPROVAL,
    })
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


# ══ §11: the object policy, and the 404 that hides denial ═══════════════════════════════════════
def test_a_caller_who_may_not_see_the_run_cannot_tell_it_from_an_absent_one(client, conn, enabled):
    run = _seed_run(conn, run_id="trg-p", subject="user:owner")
    _declare(client, run)

    denied = client.post(PREPARE.format(run="trg-p"), json={"option_ids": ["opt-a"]},
                         headers=_hdr("stranger"))
    absent = client.post(PREPARE.format(run="no-such-run"), json={"option_ids": ["opt-a"]},
                         headers=_hdr("stranger"))

    assert denied.status_code == 404 and absent.status_code == 404
    assert denied.json() == absent.json()          # indistinguishable: no shape leak
    # ...and the denial is a REFUSAL, not a quiet success: nothing was recorded for the run.
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (1,)


def test_generate_permission_gates_the_write_while_read_gates_the_reads(client, conn, enabled):
    """`catalog_viewer` is the bite: it HAS `feature:read`, so it reads the run fine and is refused
    only by the write's own gate. `data_owner` lacks both and is refused at the router."""
    run = _seed_run(conn, run_id="trg-perm")
    _declare(client, run)

    assert client.get("/feature-runs/trg-perm",
                      headers=_hdr(roles="catalog_viewer")).status_code == 200
    assert client.post(PREPARE.format(run="trg-perm"), json={"option_ids": ["opt-a"]},
                       headers=_hdr(roles="catalog_viewer")).status_code == 403
    assert client.post(PREPARE.format(run="trg-perm"), json={"option_ids": ["opt-a"]},
                       headers=_hdr(roles="data_owner")).status_code == 403


# ══ the two facts about the run itself ══════════════════════════════════════════════════════════
def test_a_PRE_SPINE_run_is_not_actionable(client, conn, enabled):
    """No identity row means no frozen considered set — there is nothing a build could build FROM,
    and the refusal names that rather than failing somewhere deeper with a stranger message."""
    _seed_run(conn, run_id="trg-pre", with_identity=False)

    response = client.post(PREPARE.format(run="trg-pre"), json={"option_ids": ["opt-a"]},
                           headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PRE_SPINE_NOT_ACTIONABLE"
    assert "honest gap" in response.json()["detail"]["detail"]
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (0,)
    _assert_page_and_entrance_agree(client, "trg-pre", response)


def test_the_deployment_switch_refuses_in_the_rails_own_word(client, conn, monkeypatch):
    """No `enabled` fixture: with V2 generation off, the worker that would drive the job never runs,
    so a job created here would sit for ever. The rail already reads `GENERATION_DISABLED` on this
    run's `GENERATE_PREVIEW` stage; the entrance refuses in the same word."""
    monkeypatch.delenv("FEATUREGEN_GENERATION_V2_ENABLED", raising=False)
    _seed_run(conn, run_id="trg-off")

    response = client.post(PREPARE.format(run="trg-off"), json={"option_ids": ["opt-a"]},
                           headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "GENERATION_DISABLED"
    rail = client.get("/feature-runs/trg-off", headers=_hdr()).json()["rail"]
    assert next(s for s in rail if s["stage"] == "GENERATE_PREVIEW")["reason_code"] \
        == "GENERATION_DISABLED", "the stage and its entrance must not disagree"
    _assert_page_and_entrance_agree(client, "trg-off", response)


# ══ the declaration is REUSED, never invented ═══════════════════════════════════════════════════
def test_a_run_nobody_declared_a_build_for_is_refused_BY_NAME(client, conn, enabled):
    """▲ THE HONEST GAP. The population, cadence, availability promise, operand facts and policy
    realizations each decide a published number, and none is derivable from a run — so a route that
    filled them in would be choosing what gets published on behalf of whoever clicked the button
    (`POST /build-sets`'s own rule). No declaration, no job."""
    _seed_run(conn, run_id="trg-nodecl")

    response = client.post(PREPARE.format(run="trg-nodecl"), json={"option_ids": ["opt-a"]},
                           headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RUN_BUILD_DECLARATION_ABSENT"
    assert "will not choose them on your behalf" in response.json()["detail"]["detail"]
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (0,)
    _assert_page_and_entrance_agree(client, "trg-nodecl", response)


def test_THE_GESTURE_MINTS_A_REAL_JOB_and_the_run_detail_lists_it(client, conn, enabled):
    """The whole point: a person picks candidates on the run page and a real coordinator job exists
    afterwards — with the members they picked, in the order they picked them."""
    run = _seed_run(conn, run_id="trg-go")
    source_job = _declare(client, run)                      # declared for opt-a alone

    response = client.post(PREPARE.format(run="trg-go"),
                           json={"option_ids": ["opt-b", "opt-a"],
                                 "spend_approval": _APPROVAL}, headers=_hdr())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True and body["job_id"] != source_job
    # The reuse is NAMED, not silent: a person can see whose declaration this build continues.
    assert body["declaration_source_job_id"] == source_job

    job = client.get(f"{JOBS}/{body['job_id']}", headers=_hdr()).json()
    assert [m["option_id"] for m in job["members"]] == ["opt-b", "opt-a"], \
        "the caller's order is the published column order, not the query's"
    assert job["status"] == "REQUESTED"
    # ...and the declaration is the SOURCE JOB's, byte for byte — reused, never re-derived.
    assert conn.execute(
        "SELECT (SELECT declaration_json FROM code_generation_job WHERE job_id = %s) = "
        "(SELECT declaration_json FROM code_generation_job WHERE job_id = %s)",
        (body["job_id"], source_job)).fetchone() == (True,)

    # THE RUN DETAIL LISTS IT — the considered-revision bridge, newest first, with the store's own
    # status word. The answer already carried the refreshed run, and a re-read agrees with it.
    listed = [j["job_id"] for j in body["run"]["jobs"]]
    assert listed == [j["job_id"] for j in
                      client.get("/feature-runs/trg-go", headers=_hdr()).json()["jobs"]]
    assert set(listed) == {body["job_id"], source_job}
    fresh = next(j for j in body["run"]["jobs"] if j["job_id"] == body["job_id"])
    assert fresh["status"] == "REQUESTED"
    # ONE JOB PERFORMS SEVERAL ACTS (1111's own words), so the acts are a list and not one string.
    assert [a["action"] for a in fresh["actions"]] == ["AUTHOR_FORMULA", "GENERATE_PREVIEW"]


def test_A_REPLAY_OF_THE_SAME_GESTURE_BUYS_NOTHING_AGAIN(client, conn, enabled):
    """Idempotency is the coordinator's, and this route inherits it rather than re-deciding: the
    same candidates under the same declaration are the same request content, so the second gesture
    lands on the live job. A second row here would be a second purchase of one answer."""
    run = _seed_run(conn, run_id="trg-replay")
    _declare(client, run)
    gesture = {"option_ids": ["opt-a", "opt-b"], "spend_approval": _APPROVAL}

    first = client.post(PREPARE.format(run="trg-replay"), json=gesture, headers=_hdr()).json()
    second = client.post(PREPARE.format(run="trg-replay"), json=gesture, headers=_hdr()).json()

    assert first["created"] is True and second["created"] is False
    assert second["job_id"] == first["job_id"]
    assert "nothing was queued or spent again" in second["detail"]
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (2,), \
        "the declaring job and ONE gesture job — the replay minted nothing"
    assert conn.execute(
        "SELECT COUNT(*) FROM llm_spend_authorization_revision").fetchone() == (2,), \
        "the idempotent ceiling did not double either"


def test_THE_GESTURE_ATTACHES_when_the_declared_job_already_covers_the_candidates(
        client, conn, enabled):
    """`mints/attaches` (spec §R4.4.2), and the attach half is not a special case: the same
    candidates under the same declaration ARE the declaring job's own request content, so the
    gesture resolves to it. Nothing is created and nothing is spent."""
    run = _seed_run(conn, run_id="trg-attach")
    source_job = _declare(client, run, options=("opt-a",))

    response = client.post(PREPARE.format(run="trg-attach"),
                           json={"option_ids": ["opt-a"], "spend_approval": _APPROVAL},
                           headers=_hdr())

    assert response.status_code == 202, response.text
    assert response.json() == {**response.json(), "job_id": source_job, "created": False}
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (1,)


# ══ money: the ceiling is a person's, never the server's ════════════════════════════════════════
def test_LLM_WORK_WITHOUT_A_CONFIRMED_CEILING_IS_REFUSED_VERBATIM(client, conn, enabled):
    """§11.2 reaches this surface unchanged: the refusal, its code and its numbers are the
    coordinator's, so a person confirming a ceiling on the run page confirms the enforcement's own
    budget rather than a number this route invented."""
    run = _seed_run(conn, run_id="trg-money")
    _declare(client, run)

    response = client.post(PREPARE.format(run="trg-money"), json={"option_ids": ["opt-b"]},
                           headers=_hdr())

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "COST_AUTHORIZATION_MISSING"
    assert detail["llm_members"] == 1
    from featuregen.overlay.upload.formula_draft_service import PER_DRAFT_CALL_ENVELOPE

    assert detail["estimated_provider_calls"] == PER_DRAFT_CALL_ENVELOPE, \
        "the quote is the ONE per-draft bound, never a second opinion"
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (1,)


def test_THE_QUOTE_IS_PER_MEMBER_and_a_TWO_DRAFT_BUILD_says_so(client, conn, enabled):
    """▲ THE MULTIPLICATION, pinned at n=2 — where it is the only n at which it is visible.

    The quote is `llm_members × PER_DRAFT_CALL_ENVELOPE`, and every assertion of it stood at ONE
    member: at n=1 the product and the constant are the same number, so dropping the
    multiplication altogether passed. That is not an abstract mutant. The run page echoes this
    quote straight back as the CONFIRMED `max_calls` (`RunDetailScreen`'s confirm dialog sends
    `quote.estimated_provider_calls` verbatim), so a two-draft build would have recorded a
    45-call ceiling for 90 calls of work — and the second draft would die mid-authoring at the
    dispatch seam, against a ceiling its own approver believed covered it.

    The constant is IMPORTED, never spelled: a test that wrote `90` would pass on the day the
    envelope moved and the arithmetic broke.
    """
    from featuregen.overlay.upload.formula_draft_service import PER_DRAFT_CALL_ENVELOPE

    run = _seed_run(conn, run_id="trg-money2")
    _declare(client, run)

    response = client.post(PREPARE.format(run="trg-money2"),
                           json={"option_ids": ["opt-a", "opt-b"]}, headers=_hdr())

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "COST_AUTHORIZATION_MISSING"
    assert detail["llm_members"] == 2, "the arrangement is TWO LLM members or it proves nothing"
    assert detail["estimated_provider_calls"] == 2 * PER_DRAFT_CALL_ENVELOPE, \
        "the quote budgets every member's envelope — a per-JOB constant would under-fund the rest"


# ══ the candidates a caller names ═══════════════════════════════════════════════════════════════
def test_a_candidate_with_no_selection_is_refused_by_name(client, conn, enabled):
    run = _seed_run(conn, run_id="trg-nosel")
    _declare(client, run)

    response = client.post(PREPARE.format(run="trg-nosel"),
                           json={"option_ids": ["opt-a", "opt-ghost"],
                                 "spend_approval": _APPROVAL}, headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RUN_CANDIDATE_NOT_SELECTED"
    assert response.json()["detail"]["option_ids"] == ["opt-ghost"]


@pytest.mark.parametrize("typed", ["twelve", "0", "-5", "NaN", "Infinity"])
def test_a_CEILING_THAT_IS_NOT_A_POSITIVE_AMOUNT_is_a_422_not_a_500(client, conn, enabled, typed):
    """▲ `max_cost` travels as a STRING into a `numeric` column, so nothing on the way down looked
    at it and `'twelve'` came back as `InvalidTextRepresentation` — a raw 500 for a value a person
    typed into a form. Zero and negative are the same refusal for the sibling ints' reason: a
    ceiling of nothing authorizes nothing."""
    run = _seed_run(conn, run_id=f"trg-money-{abs(hash(typed))}")
    _declare(client, run)

    response = client.post(PREPARE.format(run=run["run_id"]),
                           json={"option_ids": ["opt-a"],
                                 "spend_approval": {**_APPROVAL, "max_cost": typed}},
                           headers=_hdr())

    assert response.status_code == 422, response.text
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (1,)


@pytest.mark.parametrize("typed,why", [
    ("2020-01-01T00:00:00Z", "already in the past — a ceiling dead on arrival"),
    ("soon", "not a timestamp at all — the raw-500 at the ::timestamptz cast"),
    ("2100-01-01T00:00:00Z", "74 years out, against the platform's own 168-hour rule"),
    (None, "one hour past the triage window"),
])
def test_a_CEILING_EXPIRY_OUTSIDE_THE_TRIAGE_WINDOW_is_a_TYPED_422(client, conn, enabled,
                                                                   typed, why):
    """The same hole as `max_cost`'s, in the field beside it — see the regeneration route's twin
    of this test for the three failure modes. Both approval bodies carry `expires_at` and both
    reach it through the one `ExpiryWindow` declaration, so neither can be validated a second way.

    `None` stands for "computed at run time": 169 hours must stay one hour OUTSIDE the bound for
    ever, and a literal date cannot promise that.
    """
    from tests.featuregen.api._helpers import hours_from_now

    run = _seed_run(conn, run_id=f"trg-exp-{abs(hash(why))}")
    _declare(client, run)

    response = client.post(
        PREPARE.format(run=run["run_id"]),
        json={"option_ids": ["opt-a"],
              "spend_approval": {**_APPROVAL,
                                 "expires_at": typed if typed is not None
                                 else hours_from_now(169)}},
        headers=_hdr())

    assert response.status_code == 422, f"{why}: {response.text}"
    assert response.json()["detail"][0]["loc"][-1] == "expires_at", \
        "a typed 422 names the field a person typed into, never a bare string three layers down"
    # The DECLARING job stands and nothing else was recorded — the refusal bought nothing.
    assert conn.execute("SELECT COUNT(*) FROM code_generation_job").fetchone() == (1,)


def test_a_candidate_named_twice_is_a_422_naming_it(client, conn, enabled):
    run = _seed_run(conn, run_id="trg-dup")
    _declare(client, run)

    response = client.post(PREPARE.format(run="trg-dup"),
                           json={"option_ids": ["opt-a", "opt-a"],
                                 "spend_approval": _APPROVAL}, headers=_hdr())

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["option_ids"] == ["opt-a"]


def test_the_body_carries_the_candidates_and_the_ceiling_and_NOTHING_else(client, conn, enabled):
    """A body that could name the considered revision would let one run's gesture be aimed at
    another run's frozen set, and the server would have no way to know it had been lied to."""
    run = _seed_run(conn, run_id="trg-forbid")
    _declare(client, run)

    response = client.post(PREPARE.format(run="trg-forbid"),
                           json={"option_ids": ["opt-a"],
                                 "considered_revision_id": "somebody-elses"}, headers=_hdr())

    assert response.status_code == 422, response.text
