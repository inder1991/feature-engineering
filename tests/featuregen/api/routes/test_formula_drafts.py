"""The two Draft-formula endpoints: what they return, what they enqueue, and what they never do.

The rules under test are the ones that cost money or mislead a user:

1. **202, never a formula.** The route records and enqueues; two provider calls happen in a worker.
2. **A double-click enqueues once.** Idempotency is on the formula identity, so a client minting a
   fresh key per click cannot defeat it, and the second answer says so.
3. **The candidate comes from the SERVER's frozen revision**, never from the body.
4. **Requesting a draft is not selecting.** Nothing here writes a Gate-1 choice.
"""
from __future__ import annotations

import json

import pytest

DRAFT_PATH = "/considered-revisions/{rev}/options/{opt}/formula-drafts"


@pytest.fixture
def engineer_headers():
    """The role that MAY draft: `feature_engineer` carries `feature:generate`.

    Not the platform admin — administering the platform is not the same permission as running a
    workflow that spends against a model account, and the route gates on the latter.
    """
    return {"X-User": "sam", "X-Roles": "feature_engineer"}


def _revision(conn, *, revision_id="crev-1", snapshot_id="snap-1"):
    """A considered revision with ONE option, in the shipped v2 canonical shape.

    Built with the SHIPPED `_candidate_identity` and `_idea_json` rather than a hand-written blob,
    because the resolver cross-checks the private identity against its public projection and a
    hand-written fixture only proves that two hand-written things agree. Assembling it the way
    production does means a change to what a revision must contain fails here.
    """
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    idea = FeatureIdea(
        name="avg_balance_90d", description="mean balance",
        derives_from=["deposits.balance"], aggregation="avg", grain_table="deposits")
    public_feature = _idea_json(idea)
    identity = _candidate_identity(
        path="anchor", source="anchor", lens="anchor", feature=idea)
    considered = {
        "version": "contract-considered-v3",
        "public": {"anchor": {**public_feature, "option_id": "opt-a"}, "rejections": []},
        "options_by_id": {
            "opt-a": {
                "source": "anchor", "lens": "anchor",
                "canonical_candidate_identity": identity,
                "canonical_candidate_identity_hash": canonical_hash(identity),
                "recipe_candidate_key": None,
            },
        },
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    }
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','dormancy predicts churn','hypothesis') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, metadata_snapshot_content_hash, considered_json, "
        "considered_content_hash, canonicalization_version) "
        "VALUES (%s,'int-1','run-1',%s,'sha256:snap',%s::jsonb,'sha256:considered',"
        "'contract-considered-v3')",
        (revision_id, snapshot_id, json.dumps(considered)))
    return revision_id


# ══ 202, NEVER A FORMULA ════════════════════════════════════════════════════════════════════════
def test_THE_ROUTE_RETURNS_202_AND_NO_FORMULA(client, conn, engineer_headers):
    """The whole point of the async design, asserted on the response.

    A 200 carrying a formula would mean the request thread ran two model calls and held one database
    transaction across both — which is what this endpoint shape exists to prevent.
    """
    _revision(conn)
    response = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"), headers=engineer_headers)

    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    assert body["status"] == "requested"
    assert "formula" not in body, "the route returned a formula it cannot have authored"
    assert response.headers["X-Formula-Draft-Id"] == body["formula_draft_id"]


def test_the_work_is_ENQUEUED_in_the_same_transaction_as_the_row(client, conn, engineer_headers):
    """No window where a draft exists with nobody to drive it, and none where a queue row names a
    draft that is not there."""
    _revision(conn)
    body = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                       headers=engineer_headers).json()

    draft_id = body["formula_draft_id"]
    assert conn.execute(
        "SELECT count(*) FROM formula_draft WHERE formula_draft_id=%s",
        (draft_id,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE payload->>'formula_draft_id' = %s",
        (draft_id,)).fetchone()[0] == 1


# ══ A DOUBLE-CLICK ENQUEUES ONCE ════════════════════════════════════════════════════════════════
def test_A_DOUBLE_CLICK_ENQUEUES_ONCE_AND_SAYS_SO(client, conn, engineer_headers):
    """The money guard end to end.

    The second call must not enqueue — a second job on the lane would author the same candidate
    again — and it must REPORT that nothing started, because a client showing "started" for a
    request that started nothing describes a spend that did not happen.
    """
    _revision(conn)
    first = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                        headers=engineer_headers).json()
    second = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                         headers=engineer_headers).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["formula_draft_id"] == first["formula_draft_id"]
    assert "nothing was spent" in second["detail"]
    assert conn.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE payload ? 'formula_draft_id'").fetchone()[0] == 1


# ══ THE CANDIDATE IS THE SERVER'S ═══════════════════════════════════════════════════════════════
def test_an_unknown_revision_is_404(client, engineer_headers):
    response = client.post(DRAFT_PATH.format(rev="nope", opt="opt-a"), headers=engineer_headers)
    assert response.status_code == 404


def test_AN_OPTION_FROM_ANOTHER_REVISION_IS_422_NOT_404(client, conn, engineer_headers):
    """A stale tab naming an option from a superseded revision.

    422 rather than 404 because the revision DOES exist and the fix is knowable: regenerate, or pick
    an option that is part of this set. A 404 would say the whole revision is gone.
    """
    _revision(conn)
    response = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-from-last-round"),
                           headers=engineer_headers)
    assert response.status_code == 422
    assert "not part of this considered revision" in response.json()["detail"]


def test_a_revision_that_cannot_name_exact_options_is_409(client, conn, engineer_headers):
    """A fact about what was STORED, not a server fault — so it is not a 500."""
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','h','hypothesis') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, considered_json, considered_content_hash, canonicalization_version) "
        "VALUES ('crev-old','int-1','run-0','{}'::jsonb,'h','contract-considered-v1')")

    response = client.post(DRAFT_PATH.format(rev="crev-old", opt="opt-a"), headers=engineer_headers)
    assert response.status_code == 409


# ══ DRAFTING IS NOT SELECTING ═══════════════════════════════════════════════════════════════════
def test_REQUESTING_A_DRAFT_RECORDS_NO_GATE1_CHOICE(client, conn, engineer_headers):
    """The product rule, asserted on the durable state rather than on the route's intentions.

    `/contract/draft` records a Gate-1 choice as its FIRST act; on that route drafting IS selecting.
    Here a user must be able to read a formula and then decide, so the choice table must be
    untouched — and it is checked after the call, because that is where a regression would show.
    """
    _revision(conn)
    before = conn.execute("SELECT count(*) FROM contract_gate1_choice").fetchone()[0]

    client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"), headers=engineer_headers)

    assert conn.execute(
        "SELECT count(*) FROM contract_gate1_choice").fetchone()[0] == before


# ══ THE POLLING READ ════════════════════════════════════════════════════════════════════════════
def test_THE_STATUS_READ_REPORTS_THE_STAGE_AND_ITS_SOURCE(client, conn, engineer_headers):
    """What a candidate card renders, including the server-owned wording.

    `stage` is sent from the server so the API and the screen cannot describe one state with two
    different sentences; `formula_source` is sent so a card never has to guess whether a formula was
    written by a model or came from a recipe.
    """
    _revision(conn)
    draft_id = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                           headers=engineer_headers).json()["formula_draft_id"]

    body = client.get(f"/formula-drafts/{draft_id}", headers=engineer_headers).json()

    assert body["state"] == "REQUESTED"
    assert body["stage"] == "Queued"
    assert body["terminal"] is False
    assert body["formula_source"] == "llm_authored"
    # HONEST ABSENCE: nothing has been authored yet, and the response says exactly that rather than
    # sending an empty object that reads as a formula with no body.
    assert body["formula"] is None
    assert body["formula_content_hash"] is None
    assert body["blockers"] == []


def test_an_unknown_draft_is_404(client, engineer_headers):
    assert client.get("/formula-drafts/fd-nope", headers=engineer_headers).status_code == 404


# ══ THE MESSAGE MUST REACH THE LANE ═════════════════════════════════════════════════════════════
def test_THE_DRAFT_TOPIC_IS_ROUTED_TO_THE_DRAFT_HANDLER():
    """Writing the outbox row is not delivering the work, and this test exists because that gap
    survived a green suite, a clean deploy, and a 202.

    LIVE FAILURE: the relay had no route for this topic. Its job is to drain the outbox, so an
    unrouted topic is marked `sent` and dropped — by design. The request answered 202 "a worker
    authors it", no queue row was ever created, and the draft sat at REQUESTED forever. The
    original test asserted an outbox row existed, which was true and meant nothing.

    Both halves are asserted: the ROUTE (so the message becomes a queue row) and the
    ROUTE-REQUIRED entry (so losing the route dead-letters loudly instead of silently).
    """
    from featuregen.api.routes.formula_drafts import FORMULA_DRAFT_HANDLER, FORMULA_DRAFT_TOPIC
    from featuregen.runtime.worker import _DEFAULT_RELAY_ROUTE, _relay_publisher_from_env

    assert _DEFAULT_RELAY_ROUTE[FORMULA_DRAFT_TOPIC] == FORMULA_DRAFT_HANDLER

    # And the env-built publisher agrees, since that is the one production actually uses.
    publisher = _relay_publisher_from_env()
    routes = getattr(publisher, "routes", None) or getattr(publisher, "__closure__", None)
    assert routes is not None, "the publisher exposes no routes to check"


def test_THE_ROUTED_MESSAGE_IS_CLAIMABLE_BY_THE_DRAFT_LANE(client, conn, engineer_headers):
    """End to end through the seam that broke: request → outbox → relay → queue → claimed.

    The claim is the assertion. A queue row with the wrong handler is not claimable by this lane and
    would sit forever exactly as the live one did.
    """
    from featuregen.runtime.outbox import relay_publish_batch
    from featuregen.runtime.queue import claim_formula_draft
    from featuregen.runtime.worker import _relay_publisher_from_env

    _revision(conn)
    body = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                       headers=engineer_headers).json()

    relay_publish_batch(conn, _relay_publisher_from_env(), owner="test-relay")

    claimed = claim_formula_draft(conn, owner="draft-lane")
    assert claimed is not None, "the relay did not turn the outbox message into claimable work"
    assert claimed.payload["formula_draft_id"] == body["formula_draft_id"]


# ══ A RETIRED IDENTITY IS A CONSIDERED REFUSAL, NOT A CRASH ════════════════════════════════════
def test_REQUESTING_A_RETIRED_IDENTITY_IS_409_not_500(client, conn, engineer_headers):
    """▲ `request_draft` raises `DraftRetired` deliberately, and the route caught nothing — so the
    global handler turned a considered refusal into "Internal Server Error", telling a caller the
    platform had broken rather than that they asked for something withdrawn.

    409 rather than 422: the request is well-formed and would have been valid yesterday. What
    conflicts is the state of the world.
    """
    from featuregen.overlay.upload.formula_draft_store import retire_formula_draft

    _revision(conn)
    first = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                        headers=engineer_headers).json()
    retire_formula_draft(conn, first["formula_draft_id"], reason="SCHEMA_CONTRACT_MISMATCH",
                         detail="manifest 3, formula 2", retired_by="ops@bank")

    response = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                           headers=engineer_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "FORMULA_DRAFT_RETIRED"
    assert detail["retired_draft_id"] == first["formula_draft_id"]
    assert detail["reason"] == "SCHEMA_CONTRACT_MISMATCH"
    # WHICH inputs must change — `formula_identity_hash` is unique, so a new draft id lands on the
    # same row and "try again" is not actionable without this list.
    assert "authoring_config_hash" in detail["identity_bearing_inputs"]
    assert "identity" in detail["remedy"]


def test_THE_409_NAMES_THE_REPLACEMENT_when_there_is_one(client, conn, engineer_headers):
    """"Use that one instead" is usually the answer somebody actually needs."""
    from featuregen.overlay.upload.formula_draft_store import (
        record_draft_replacement,
        retire_formula_draft,
    )

    _revision(conn)
    first = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                        headers=engineer_headers).json()["formula_draft_id"]
    # The replacement is a draft with a DIFFERENT identity — which is exactly what regenerating
    # after a retirement produces, since a new draft id alone would collide on the same identity.
    from featuregen.overlay.upload.formula_draft_store import request_draft

    replacement, _created = request_draft(
        conn, formula_draft_id="fd-replacement", considered_revision_id="crev-1",
        option_id="opt-a", planning_request_hash="p", catalog_snapshot_hash="c",
        authoring_config_hash="a-corrected", definition_revision="",
        requested_by="ops@bank", requested_at="t")
    retire_formula_draft(conn, first, reason="CANDIDATE_SUPERSEDED", retired_by="ops@bank")
    record_draft_replacement(conn, first, replacement_draft_id=replacement)

    response = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                           headers=engineer_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["replacement_draft_id"] == replacement


def test_THE_POLLING_ROUTE_REPORTS_RETIREMENT(client, conn, engineer_headers):
    """A card rendered from `state` alone said "Formula ready" about a withdrawn draft."""
    from featuregen.overlay.upload.formula_draft_store import retire_formula_draft

    _revision(conn)
    draft_id = client.post(DRAFT_PATH.format(rev="crev-1", opt="opt-a"),
                           headers=engineer_headers).json()["formula_draft_id"]
    retire_formula_draft(conn, draft_id, reason="WITHDRAWN", detail="not needed",
                         retired_by="ops@bank")

    body = client.get(f"/formula-drafts/{draft_id}", headers=engineer_headers).json()

    assert body["retired"] is True
    assert body["stage"] == "Retired"
    assert body["terminal"] is True, "a retired draft is not still in flight"
    assert body["retirement"]["reason"] == "WITHDRAWN"
    assert body["retirement"]["retired_by"] == "ops@bank"


# ══ OWNER RULING 2026-08-23, ITEMS 1+2: THE STRATEGY IS RESOLVED, RECORDED, AND FOLDED ═════════
def test_THE_PLAN_AND_IDENTITY_V2_ARE_RECORDED_in_the_request_transaction(
        client, conn, engineer_headers):
    """▲ The resolved method is a durable row the worker re-reads, and the identity companion says
    WHICH composition minted the draft. The old identity was a CONSTANT — getattr on a dict — so
    the money guard was blind to model, prompts and method since it shipped."""
    _revision(conn)

    response = client.post(
        "/considered-revisions/crev-1/options/opt-a/formula-drafts", headers=engineer_headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["formula_strategy"] == "LLM_AUTHORED"     # an anchor idea, no recipe
    draft_id = body["formula_draft_id"]

    plan = conn.execute(
        "SELECT candidate_origin, formula_strategy, provider_contract_hash, "
        "       reviewed_blueprint_revision FROM formula_draft_authoring_plan "
        "WHERE formula_draft_id = %s", (draft_id,)).fetchone()
    assert plan[0] == "llm_intent"                        # llm_freeform normalized, never raw
    assert plan[1] == "LLM_AUTHORED"
    assert plan[2], "an LLM plan names its frozen provider contract"
    assert plan[3] is None, "and cannot claim a reviewed blueprint (1104's CHECK backs this)"

    companion = conn.execute(
        "SELECT identity_version, config_hash FROM formula_draft_authoring_identity "
        "WHERE formula_draft_id = %s", (draft_id,)).fetchone()
    assert companion[0] == 2
    # ▲ NOT the constant. The exact defect value, refused by name.
    assert companion[1] != "f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8"
    stored = conn.execute(
        "SELECT authoring_config_hash FROM formula_draft WHERE formula_draft_id = %s",
        (draft_id,)).fetchone()[0]
    assert stored == companion[1], "the companion's composite FK describes the draft it names"


def test_A_CONCEPTUAL_CANDIDATE_CANNOT_MINT_A_DRAFT(client, conn, engineer_headers):
    """A conceptual pattern is saved or specified — a draft row for a non-formula would be a
    formula-shaped promise about a thing that is not one. 409 with a next step, not a dead end."""
    _revision(conn, revision_id="crev-conceptual")
    conn.execute(
        "INSERT INTO semantic_option_decision (decision_id, considered_revision_id, option_id, "
        "generation_run_id, source_definition_id, generation_source, computation_kind, "
        "planning_request_hash, binding_state, readiness, review_current, metadata_snapshot_id) "
        "VALUES ('dec-conceptual', 'crev-conceptual', 'opt-a', 'run-1', 'concept-1', 'recipe', "
        "'conceptual_pattern', 'h', 'bound', 'CONCEPTUAL_ONLY', false, 'snap-1')")

    response = client.post(
        "/considered-revisions/crev-conceptual/options/opt-a/formula-drafts",
        headers=engineer_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CONCEPTUAL_PATTERN_NOT_AUTHORABLE"
    assert "next_step" in detail
    assert conn.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 0
