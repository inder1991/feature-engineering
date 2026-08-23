"""The retry gesture on the run page (spec §R4.2): `POST /feature-runs/{run_id}/authoring-retries`.

NEVER `conn.commit()` here — the shared test conn rolls back.

Retry is the one gesture that can BUY something twice, so what is under test is the governance
around it rather than the button:

1. **§11 first**, exactly as on the trigger: a caller who may not see the run gets the same body
   for retrying an attempt of it as for a run that does not exist.
2. **One answer, both surfaces.** The run page's `retryable` / `retry_blockers` and this route's
   409 are the SAME derivation — §7 [R3.1]'s rule applied to a gesture, the way `prepare_code` and
   its entrance already work.
3. **The LLM lane pays.** A recorded failure is not an answer, so re-buying it needs an approved,
   cost-confirmed regeneration exception; the deterministic lane needs none, by the owner's Option 2
   ruling, because it provably spends nothing.
4. **A withdrawal refuses BOTH lanes** — governance keeps its teeth exactly where a human decided
   something, and a free retry is not a free override.
5. **The money guard is not click-proofed away**: while a live attempt holds the identity slot
   (1107's partial index) a second retry mints nothing, and the page says so instead of offering a
   click that would change nothing.

Placed in a file of its own rather than in `test_feature_runs.py` for `test_feature_run_trigger`'s
reason: that file is the run READS, and a gesture that spends against a model account is a
different subject with a different seeding chain.
"""
from __future__ import annotations

import pytest
from tests.featuregen.runs._chain import considered_json, feature_idea, seed_run_chain

from featuregen.contracts.envelopes import IdentityEnvelope

RETRY = "/feature-runs/{run}/authoring-retries"
DRAFT = "/considered-revisions/{rev}/options/{opt}/formula-drafts"

#: The approver's cost confirmation — the six keys `RegenerationApprovalIn` demands, and the ONE
#: per-draft envelope the enforcement itself quotes.
_CEILING = {"max_calls": 45, "max_tokens": 250_000, "max_cost": "25.00", "currency": "USD",
            "pricing_version": "regen@1", "expires_at": "2026-12-31T09:00:00Z"}

#: Approving a re-spend after a recorded failure is somebody taking responsibility for buying the
#: answer again, so it is a governance act and not the engineer's own click.
_GOVERNANCE = {"X-User": "owner", "X-Roles": "platform_admin"}


def _hdr(user="priya", roles="feature_engineer"):
    return {"X-User": user, "X-Roles": roles}


def _seed(conn, *, run_id, subject="user:priya", options=("opt-a",)):
    """One spine run whose candidates can actually be authored: the full creation chain, a real
    canonical v3 considered payload, and the identity row a post-spine run carries."""
    from featuregen.runs.run_identity import record_run_identity

    chain = seed_run_chain(
        conn, run_id=run_id, subject=subject,
        considered_json=considered_json(
            [(o, feature_idea(f"avg_balance_{o.replace('-', '_')}")) for o in options]))
    record_run_identity(conn, run_id, IdentityEnvelope(
        subject=subject, actor_kind="human", authenticated=True, auth_method="test",
        role_claims=("feature_engineer",)))
    return chain


def _attempt(client, conn, chain, *, option_id="opt-a", fail=True):
    """One recorded attempt for a candidate of this run, minted the ONLY way a draft can be — the
    governed service seam — and then failed the way a provider refusal fails it."""
    response = client.post(
        DRAFT.format(rev=chain["considered_revision_id"], opt=option_id), headers=_hdr())
    assert response.status_code == 202, response.text
    draft_id = response.json()["formula_draft_id"]
    if fail:
        conn.execute(
            "UPDATE formula_draft SET state = 'FAILED', failure_reason = 'boom' "
            "WHERE formula_draft_id = %s", (draft_id,))
    return draft_id


def _row(client, run_id, draft_id):
    """The attempt AS THE RUN PAGE SEES IT — the derivation the control is drawn from."""
    body = client.get(f"/feature-runs/{run_id}", headers=_hdr()).json()
    return next(r for r in body["authoring"]["history"] if r["formula_draft_id"] == draft_id)


def _assert_page_and_entrance_agree(client, run_id, draft_id, refusal):
    """▲ THE DRIFT CHECK, in the trigger's own shape. A control the page offers over an entrance
    that refuses is §7 [R3.1]'s false rail with a click in it — so the row's first blocker and the
    409 must be the same code and the same sentence, not two maintained copies."""
    row = _row(client, run_id, draft_id)
    assert row["retryable"] is False
    assert row["retry_blockers"][0]["code"] == refusal.json()["detail"]["code"]
    assert row["retry_blockers"][0]["detail"] == refusal.json()["detail"]["detail"]


def _deterministic_lane(monkeypatch):
    """Put this candidate in the reviewed deterministic lane, by moving the FACTS and leaving the
    RESOLVER alone.

    Nothing seeds a reviewed v2 registry entry for a fixture candidate, so the reviewed axes are
    supplied here — but the decision is still `resolve_formula_strategy`'s, which is the half these
    tests are about: a stubbed decision would prove the lane discriminator agrees with itself. The
    blueprint identity rides too, because 1104's CHECK requires a REVIEWED plan row to name its
    blueprint and to carry NO provider contract.

    TWO bindings, because they are two: `formula_draft_service` bound the name at import (so the
    MINT path needs its own patch), and `runs.projection` resolves it at call time off the source
    module. Patching one alone would put the run page and the store in different lanes, which is
    the exact disagreement these tests exist to forbid.
    """
    from dataclasses import replace

    import featuregen.overlay.upload.formula_draft_service as service
    import featuregen.overlay.upload.formula_strategy_facts as facts_module

    real = facts_module.assemble_strategy_facts

    def _reviewed(conn, **kwargs):
        assembled = real(conn, **kwargs)
        return replace(
            assembled,
            facts=replace(
                assembled.facts, recipe_id="rcp-det", recipe_revision_hash="rrh-det",
                expectation_ref="exp-det", expectation_generation="v2",
                reviewed_expectation_current=True, deterministic_lane_available=True,
                blueprint_bindable=True, reviewed_blueprint_revision="rbr-det",
                reviewed_blueprint_hash="rbh-det"),
            reviewed_blueprint_revision="rbr-det", reviewed_blueprint_hash="rbh-det")

    monkeypatch.setattr(service, "assemble_strategy_facts", _reviewed)
    monkeypatch.setattr(facts_module, "assemble_strategy_facts", _reviewed)


# ══ §11: the object policy, and the 404 that hides denial ═══════════════════════════════════════
def test_a_caller_who_may_not_see_the_run_cannot_tell_it_from_an_absent_one(client, conn):
    chain = _seed(conn, run_id="rty-p", subject="user:owner")
    draft_id = _attempt(client, conn, chain)

    denied = client.post(RETRY.format(run="rty-p"), json={"formula_draft_id": draft_id},
                         headers=_hdr("stranger"))
    absent = client.post(RETRY.format(run="no-such-run"), json={"formula_draft_id": draft_id},
                         headers=_hdr("stranger"))

    assert denied.status_code == 404 and absent.status_code == 404
    assert denied.json() == absent.json()          # indistinguishable: no shape leak
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (1,)


def test_generate_permission_gates_the_write_while_read_gates_the_reads(client, conn):
    """`catalog_viewer` is the bite: it HAS `feature:read`, so it reads the run fine and is refused
    only by the write's own gate."""
    chain = _seed(conn, run_id="rty-perm")
    draft_id = _attempt(client, conn, chain)

    assert client.get("/feature-runs/rty-perm",
                      headers=_hdr(roles="catalog_viewer")).status_code == 200
    assert client.post(RETRY.format(run="rty-perm"), json={"formula_draft_id": draft_id},
                       headers=_hdr(roles="catalog_viewer")).status_code == 403
    assert client.post(RETRY.format(run="rty-perm"), json={"formula_draft_id": draft_id},
                       headers=_hdr(roles="data_owner")).status_code == 403


def test_a_draft_that_is_not_this_runs_attempt_is_refused_by_name(client, conn):
    """The body names an attempt; the RUN's own history is what says whether it is one. A draft
    from somewhere else is not this run's to retry, and answering otherwise would let one run's
    gesture spend against another run's candidate."""
    mine = _seed(conn, run_id="rty-mine")
    _attempt(client, conn, mine)
    theirs = _seed(conn, run_id="rty-theirs")
    other_draft = _attempt(client, conn, theirs, option_id="opt-a")

    response = client.post(RETRY.format(run="rty-mine"),
                           json={"formula_draft_id": other_draft}, headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RUN_ATTEMPT_NOT_RECORDED"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (2,)


def test_the_body_carries_the_attempt_and_NOTHING_else(client, conn):
    chain = _seed(conn, run_id="rty-forbid")
    draft_id = _attempt(client, conn, chain)

    response = client.post(RETRY.format(run="rty-forbid"),
                           json={"formula_draft_id": draft_id, "option_id": "somebody-elses"},
                           headers=_hdr())

    assert response.status_code == 422, response.text


# ══ the question is only asked of an attempt that bought nothing ════════════════════════════════
def test_an_attempt_that_is_still_in_flight_is_not_the_retry_question(client, conn):
    """Absence of the question, not a refusal: a REQUESTED draft holds the identity slot and is
    still being bought. The row carries no blockers, because there is nothing to explain."""
    chain = _seed(conn, run_id="rty-live")
    draft_id = _attempt(client, conn, chain, fail=False)

    row = _row(client, "rty-live", draft_id)
    assert row["retryable"] is False
    assert row["retry_blockers"] == []

    response = client.post(RETRY.format(run="rty-live"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RUN_ATTEMPT_IS_NOT_A_FAILURE"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (1,)


# ══ the LLM lane: approved, priced, and only then offered ═══════════════════════════════════════
def test_a_FAILED_LLM_ATTEMPT_WITH_NO_APPROVAL_is_refused_in_the_stores_own_word(client, conn):
    """§11.1.2: a recorded failure is not an answer this platform bought, and re-authoring spends
    again — so the remedy is an approval, and the page names it rather than offering the click."""
    chain = _seed(conn, run_id="rty-noexc")
    draft_id = _attempt(client, conn, chain)

    row = _row(client, "rty-noexc", draft_id)
    assert row["retryable"] is False
    assert [b["code"] for b in row["retry_blockers"]] == ["FORMULA_DRAFT_NOT_AN_ANSWER"]
    assert row["retry_blockers"][0]["detail"], "a code with no sentence is a dead end"

    response = client.post(RETRY.format(run="rty-noexc"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "FORMULA_DRAFT_NOT_AN_ANSWER"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (1,)
    _assert_page_and_entrance_agree(client, "rty-noexc", draft_id, response)


def test_AN_APPROVED_AND_PRICED_RETRY_MINTS_A_SECOND_ATTEMPT(client, conn):
    """The whole gesture, end to end: governance approves and confirms the cost, the page then
    OFFERS the retry with no blockers, one click mints a second draft against the same candidate,
    and the refreshed detail carries both attempts with the new one as the current answer."""
    chain = _seed(conn, run_id="rty-go")
    draft_id = _attempt(client, conn, chain)

    approved = client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                           json=_CEILING, headers=_GOVERNANCE)
    assert approved.status_code == 201, approved.text

    offered = _row(client, "rty-go", draft_id)
    assert offered["retryable"] is True and offered["retry_blockers"] == []

    response = client.post(RETRY.format(run="rty-go"), json={"formula_draft_id": draft_id},
                           headers=_hdr())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True and body["formula_draft_id"] != draft_id
    # TWO READINGS (§R4.4.1): every attempt on the record, and the one the candidate stands on.
    history = body["run"]["authoring"]["history"]
    assert [r["formula_draft_id"] for r in history] == [draft_id, body["formula_draft_id"]]
    current = body["run"]["authoring"]["current"]
    assert [r["formula_draft_id"] for r in current] == [body["formula_draft_id"]]
    assert current[0]["resolved"] is True
    # ...and the coupon was consumed exactly once, by the mint it authorized.
    assert conn.execute(
        "SELECT uses_consumed, max_uses FROM formula_draft_regeneration_exception "
        "WHERE exception_id = %s", (approved.json()["exception_id"],)).fetchone() == (1, 1)


def test_A_SECOND_CLICK_WHILE_THE_NEW_ATTEMPT_IS_LIVE_MINTS_NOTHING(client, conn):
    """1107's partial index covers ANSWERS and purchases in flight, so the retry that just landed
    holds the slot. A control offered here would be a click that changes nothing — the page says
    which draft holds it, and the entrance refuses in the same word."""
    chain = _seed(conn, run_id="rty-twice")
    draft_id = _attempt(client, conn, chain)
    client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                json={**_CEILING, "max_uses": 2}, headers=_GOVERNANCE)
    first = client.post(RETRY.format(run="rty-twice"), json={"formula_draft_id": draft_id},
                        headers=_hdr())
    assert first.status_code == 202, first.text

    row = _row(client, "rty-twice", draft_id)
    assert row["retryable"] is False
    assert [b["code"] for b in row["retry_blockers"]] == ["RETRY_ATTEMPT_ALREADY_LIVE"]
    assert first.json()["formula_draft_id"] in row["retry_blockers"][0]["detail"], \
        "a refusal that will not name what holds the slot sends a person looking for it"

    second = client.post(RETRY.format(run="rty-twice"), json={"formula_draft_id": draft_id},
                         headers=_hdr())
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "RETRY_ATTEMPT_ALREADY_LIVE"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (2,), \
        "the second click bought nothing"
    _assert_page_and_entrance_agree(client, "rty-twice", draft_id, second)


def test_an_EXHAUSTED_CEILING_refuses_in_the_spend_guards_own_word(client, conn):
    """§11.2 reaches the affordance: an approval whose budget is spent authorizes no provider call,
    so offering the retry would send a person into `SpendExhausted` at the dispatch seam."""
    chain = _seed(conn, run_id="rty-broke")
    draft_id = _attempt(client, conn, chain)
    approved = client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                           json=_CEILING, headers=_GOVERNANCE)
    spend_id = approved.json()["spend_authorization_id"]
    # The ceiling, spent to the last call — reserved and settled the way the audited seam does it.
    from featuregen.overlay.upload.llm_spend import reserve_spend, settle_spend

    now = conn.execute("SELECT now()").fetchone()[0]
    reservation = reserve_spend(conn, spend_authorization_id=spend_id, calls=45, tokens=250_000,
                                cost="25.00", now=now)
    settle_spend(conn, reservation, actual_calls=45, actual_tokens=250_000, actual_cost="25.00")

    row = _row(client, "rty-broke", draft_id)
    assert row["retryable"] is False
    assert "COST_AUTHORIZATION_EXHAUSTED" in [b["code"] for b in row["retry_blockers"]]

    response = client.post(RETRY.format(run="rty-broke"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "COST_AUTHORIZATION_EXHAUSTED"


def test_THE_PAGE_AND_THE_SEAM_PICK_THE_SAME_CEILING_OUT_OF_TWO(client, conn):
    """▲ THE DRIFT-KILLER for the one query this projection COPIES.

    `runs.projection.retry_availability` reads the ceiling a retry would ride with a query that is
    byte-identical to `formula_draft_service.request_draft_for_candidate`'s — including its
    preferences: only the AUTHORIZATION's expiry filters, the coupon's own uses and expiry do not,
    and `ORDER BY e.approved_at DESC` breaks the tie. Copies do not stay identical on their own, and
    nothing else in the suite notices a divergence: two approvals for one identity is the smallest
    arrangement where the preference ORDER decides the answer, so it is the arrangement that
    defends it. Flip the copy's DESC to ASC and this dies; every other test stays green.

    The extraction of the shared read belongs to the substrate session (`formula_draft_service` is
    not this session's to edit); until it lands, this test is what stands between the two copies.
    """
    from featuregen.overlay.upload.llm_spend import remaining_spend, reserve_spend, settle_spend

    chain = _seed(conn, run_id="rty-two")
    draft_id = _attempt(client, conn, chain)

    # TWO approvals for one identity, differing only in the amount confirmed — so each mints its own
    # spend authorization and its own coupon, which is what makes the pick a real choice.
    older = client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                        json={**_CEILING, "max_cost": "25.00"}, headers=_GOVERNANCE).json()
    newer = client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                        json={**_CEILING, "max_cost": "9.00"}, headers=_GOVERNANCE).json()
    assert older["spend_authorization_id"] != newer["spend_authorization_id"]
    # The older one is aged back a day and then spent to the last call: a ceiling that authorizes
    # nothing any more, which is exactly what a wrong pick would report on.
    conn.execute(
        "UPDATE formula_draft_regeneration_exception "
        "SET approved_at = approved_at - interval '1 day' WHERE exception_id = %s",
        (older["exception_id"],))
    now = conn.execute("SELECT now()").fetchone()[0]
    settle_spend(conn, reserve_spend(
        conn, spend_authorization_id=older["spend_authorization_id"], calls=45, tokens=250_000,
        cost="25.00", now=now), actual_calls=45, actual_tokens=250_000, actual_cost="25.00")

    # THE ARRANGEMENT, asserted rather than assumed — a test whose premise silently stopped holding
    # would pass for the wrong reason for ever.
    assert remaining_spend(conn, older["spend_authorization_id"], now=now).calls == 0
    assert remaining_spend(conn, newer["spend_authorization_id"], now=now).calls > 0

    # THE PAGE picks the NEWER ceiling, so it offers the retry...
    row = _row(client, "rty-two", draft_id)
    assert row["retryable"] is True, row["retry_blockers"]
    assert row["retry_blockers"] == []

    # ...and THE SEAM agrees, which is the half a page-only assertion could never catch: a
    # projection reading one ceiling while the mint rides another is two answers to one question.
    response = client.post(RETRY.format(run="rty-two"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 202, response.text
    assert conn.execute(
        "SELECT llm_spend_authorization_id FROM formula_draft_authoring_plan "
        "WHERE formula_draft_id = %s",
        (response.json()["formula_draft_id"],)).fetchone() == (newer["spend_authorization_id"],), \
        "the draft rides the ceiling the page believed in, not the spent one"


def test_a_RACE_LOST_AT_THE_SEAM_IS_A_REFUSAL_NOT_A_REPORTED_PURCHASE(client, conn, monkeypatch):
    """The TOCTOU loser: the page's gate said yes, and somebody else's mint landed in between.

    1107's partial index then refuses this INSERT, `request_draft` returns the row that won WITHOUT
    consuming any coupon, and reporting that as a new attempt would describe a purchase that never
    happened. Unreachable through the front door — the projection's own live-row blocker catches
    every non-race case — so the concurrent winner is simulated by minting one from inside the seam.
    """
    import featuregen.overlay.upload.formula_draft_service as service

    chain = _seed(conn, run_id="rty-race")
    draft_id = _attempt(client, conn, chain)
    client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                json=_CEILING, headers=_GOVERNANCE)
    real = service.request_draft_for_candidate

    def _loses_the_race(conn, **kwargs):
        # Somebody else's retry, committed between this route's read and its write. It takes the
        # identity slot AND the one coupon — which is why the second call below neither refuses as
        # not-an-answer nor mints: it simply loses.
        real(conn, **{**kwargs, "formula_draft_id": "fd-concurrent-winner"})
        return real(conn, **kwargs)

    monkeypatch.setattr(service, "request_draft_for_candidate", _loses_the_race)

    response = client.post(RETRY.format(run="rty-race"), json={"formula_draft_id": draft_id},
                           headers=_hdr())

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RETRY_ATTEMPT_ALREADY_LIVE"
    assert response.json()["detail"]["formula_draft_id"] == "fd-concurrent-winner"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (2,), \
        "the failure and the winner — this gesture reported a refusal and minted nothing of its own"


def test_a_DATABASE_WITHOUT_THE_GOVERNED_RETRY_SUBSTRATE_DEGRADES_INSTEAD_OF_500ing(client, conn):
    """`pin_exists`'s rule, proved for 1103/1105. The live ledger stands at 1099, so this is the
    deployment the code will actually meet first: reading the approval or ceiling store there raises
    `UndefinedTable` out of a read-only projection and takes the WHOLE run page down for one failed
    draft. The stores are dropped inside the test transaction, which rolls back."""
    chain = _seed(conn, run_id="rty-pre")
    draft_id = _attempt(client, conn, chain)
    conn.execute("DROP TABLE formula_draft_regeneration_exception, "
                 "llm_spend_authorization_revision CASCADE")

    row = _row(client, "rty-pre", draft_id)
    assert row["retryable"] is False
    assert [b["code"] for b in row["retry_blockers"]] == ["RETRY_SUBSTRATE_ABSENT"]
    assert "migrations" in row["retry_blockers"][0]["detail"], \
        "the remedy is an operator's, and the sentence has to say so"

    response = client.post(RETRY.format(run="rty-pre"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RETRY_SUBSTRATE_ABSENT"


def test_an_APPROVAL_CEILING_THAT_IS_NOT_A_POSITIVE_AMOUNT_is_a_422_not_a_500(client, conn):
    """The approval this gesture rides carries the same string-into-`numeric` field, and it had the
    same raw-500 hole: refused now at the one declaration both approval bodies share."""
    chain = _seed(conn, run_id="rty-badcost")
    draft_id = _attempt(client, conn, chain)

    response = client.post(f"/formula-drafts/{draft_id}/regeneration-exceptions",
                           json={**_CEILING, "max_cost": "twelve"}, headers=_GOVERNANCE)

    assert response.status_code == 422, response.text
    assert conn.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception").fetchone() == (0,)


# ══ the deterministic lane: free by construction (owner ruling, Option 2) ═══════════════════════
def test_A_FAILED_DETERMINISTIC_ATTEMPT_IS_RETRYABLE_WITH_NO_APPROVAL_AT_ALL(
        client, conn, monkeypatch):
    """The reviewed lane calls no provider and spends nothing (1104's CHECK forbids it a provider
    contract), so the gate that exists to guard a re-spend does not apply. Nothing is approved
    here, and the retry is still offered and still mints."""
    _deterministic_lane(monkeypatch)
    chain = _seed(conn, run_id="rty-det")
    draft_id = _attempt(client, conn, chain)

    row = _row(client, "rty-det", draft_id)
    assert row["retryable"] is True, row["retry_blockers"]
    assert row["retry_blockers"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception").fetchone() == (0,), \
        "free by construction means no coupon exists — not that one was quietly minted"

    response = client.post(RETRY.format(run="rty-det"), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 202, response.text
    assert response.json()["created"] is True


# ══ a withdrawal refuses BOTH lanes ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("lane", ["llm", "deterministic"])
def test_A_WITHDRAWN_CANDIDATE_IS_REFUSED_IN_BOTH_LANES(client, conn, monkeypatch, lane):
    """Withdrawal is a decision, not a cost — so the free lane is free of the SPEND gate and not of
    governance. Under the one law a covering withdrawal must be NAMED by a valid coupon, and none
    is here."""
    if lane == "deterministic":
        _deterministic_lane(monkeypatch)
    run_id = f"rty-tomb-{lane}"
    chain = _seed(conn, run_id=run_id)
    draft_id = _attempt(client, conn, chain)
    from featuregen.overlay.upload.retirement_scope import RetirementScope, record_tombstone

    record_tombstone(conn, formula_draft_id=draft_id,
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")

    row = _row(client, run_id, draft_id)
    assert row["retryable"] is False
    assert "FORMULA_DRAFT_RETIRED" in [b["code"] for b in row["retry_blockers"]]

    response = client.post(RETRY.format(run=run_id), json={"formula_draft_id": draft_id},
                           headers=_hdr())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "FORMULA_DRAFT_RETIRED"
    assert conn.execute("SELECT COUNT(*) FROM formula_draft").fetchone() == (1,)
    _assert_page_and_entrance_agree(client, run_id, draft_id, response)
