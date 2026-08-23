"""Task S1A-5a §4 — a decision row nobody can authenticate is a 409, never a 500.

``load_frozen_option_facts`` refuses two kinds of unauthenticatable row: a plan that fails its
manifest seal (B1) and — new here — a ``governed_cross_catalog`` read set carrying an entry that
is not a qualified ref. Both raise ``OptionDecisionIntegrityError``, which is a ``RuntimeError``,
and BEFORE this task every one of its three route consumers let it reach ``app.py``'s catch-all
handler: an opaque ``Internal Server Error`` with no code, no message and no next step, for a
condition that is not a fault at all — it is a governed record disagreeing with itself, which a
person fixes by regenerating.

So each of the three call sites now translates it, with the shape the routes already use for a
typed governed conflict (``ACTIVATION_BLOCKED``'s sibling): 409, a ``code``, the error's own
message, and the ``next_step`` that actually resolves it.

**How each route is reached.** The materialization route takes the option key straight off the
request body, so its case is end to end: a REAL malformed row written by the real writer, posted
to the real route. ``/contract/draft`` and ``/contract/confirm`` take the key from a recorded
Gate-1 choice, which only a full generation run can mint — and the rows that run writes are
well-formed and, the table being append-only, unrewritable. Their cases therefore run the real
generation, the real choice and the real route, and raise the GENUINE exception object at the one
seam in between: the error is produced by the real loader over a real malformed row persisted in
the same test, never a hand-written stand-in.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_materialization_runs import _PATH, _body
from tests.featuregen.api.test_semantic_v1_serving import _bank, _fake_with_intents, _post
from tests.featuregen.materialize.test_resolve import _seed_work_item

from featuregen.materialize.queue_lane import MATERIALIZATION_FLAG
from featuregen.overlay.upload import semantic_option_decision as sod
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_planning_lens import DatasetStoryV1, V2RecipeCandidateV1
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_option_decision import (
    OptionDecisionIntegrityError,
    decision_facts_for_candidate,
    load_frozen_option_facts,
    persist_option_decisions,
)

RECIPE = "posted_debit_amount"
INTEGRITY_CODE = "OPTION_DECISION_INTEGRITY"
NEXT_STEP = "regenerate from the current considered set"

#: A governed cross-catalog plan with ONE entry that names no catalog — the exact defect the
#: strict loader exists to catch, and the realistic one: a bare ref copied out of a single-source
#: plan, where every entry inherited the plan's one catalog.
MALFORMED_GOVERNED_PLAN = {
    "plan_kind": "governed_cross_catalog",
    "population_ref": "accounts",
    "read_set": ["bank::public.accounts.acct_id", "sales.customers.cust_id"],
    "role_bindings": {"grain": "bank::public.accounts.acct_id"},
    "pit": "as-of state at the cutoff",
    "output_grain": "account",
    "window": None,
}


def _freeze_malformed_option(db, *, key: str) -> tuple[str, str]:
    """One REAL ``semantic_option_decision`` row, through the real writers, whose governed read
    set carries an unattributable entry. The manifest seal is CORRECT — this row is refused for
    the read set, not for the seal, which is what makes it a distinct case."""
    definition = v2_recipe_by_id(RECIPE)
    assert definition is not None
    request = planning_request_from_recipe(definition)
    candidate = V2RecipeCandidateV1(
        recipe_id=RECIPE, relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=(), binding_state="bound",
        readiness=definition.readiness, temporal_pit_text="pit", temporal_blocker="",
        review_current=True, review_missing_roles=(), eligibility={},
        dataset_story=DatasetStoryV1(
            population_ref="accounts", population_basis="declared_grain",
            dataset_tables=("accounts",), cross_dataset=False, codes=()),
        binding_plan=MALFORMED_GOVERNED_PLAN)
    idea = FeatureIdea(name=RECIPE, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=RECIPE)
    facts = decision_facts_for_candidate(candidate, idea, None, "ctx")
    db.execute("INSERT INTO contract_intent "
               "(intent_id,hypothesis,intake_mode,redacted_hypothesis) "
               "VALUES (%s,'h','hypothesis','h') ON CONFLICT DO NOTHING", (f"intent-{key}",))
    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES (%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING",
               (f"genrun-{key}", f"intent-{key}"))
    db.execute("INSERT INTO contract_considered_revision "
               "(considered_revision_id,intent_id,generation_run_id,considered_json,"
               "considered_content_hash,canonicalization_version) "
               "VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
               (f"rev-{key}", f"intent-{key}", f"genrun-{key}", f"hash-{key}"))
    assert persist_option_decisions(
        db, considered_revision_id=f"rev-{key}", generation_run_id=f"genrun-{key}",
        metadata_snapshot_id=None, facts_by_option_id={f"opt-{key}": facts}) == 1
    return f"rev-{key}", f"opt-{key}"


def _genuine_integrity_error(db) -> OptionDecisionIntegrityError:
    """The REAL exception, raised by the REAL loader over a REAL malformed row — so what the
    contract routes are pinned against is the message operators will actually see."""
    revision_id, option_id = _freeze_malformed_option(db, key="genuine")
    with pytest.raises(OptionDecisionIntegrityError) as excinfo:
        load_frozen_option_facts(db, considered_revision_id=revision_id, option_id=option_id)
    return excinfo.value


def _assert_integrity_conflict(response, error: str | None = None) -> None:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == INTEGRITY_CODE, detail
    assert detail["next_step"] == NEXT_STEP, detail
    assert detail["message"], detail
    if error is not None:
        assert detail["message"] == error


# ── the materialization request route (end to end) ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _materialization_on(monkeypatch):
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")


def test_the_materialization_route_answers_409_not_500(client, admin_headers, db):
    """The whole point, stated as the difference it makes: an operator gets a code they can look
    up and an action they can take, instead of ``Internal Server Error``."""
    work_items = [_seed_work_item(db, "total_debit_amount_30d", "s1a5a")]
    revision_id, option_id = _freeze_malformed_option(db, key="mat")

    response = client.post(
        _PATH, json=_body(work_items, considered_revision_id=revision_id, option_id=option_id),
        headers=admin_headers)

    _assert_integrity_conflict(response)
    assert "not a normalized logical_ref" in response.json()["detail"]["message"]
    assert f"{revision_id}/{option_id}" in response.json()["detail"]["message"]


def test_the_refused_materialization_mints_nothing(client, admin_headers, db):
    """A refusal at the integrity gate is as clean as one at the activation gate: no request
    row, no queue row. The load happens before anything durable, and it must stay there."""
    work_items = [_seed_work_item(db, "total_debit_amount_30d", "s1a5a-clean")]
    revision_id, option_id = _freeze_malformed_option(db, key="matclean")

    assert client.post(
        _PATH, json=_body(work_items, considered_revision_id=revision_id, option_id=option_id),
        headers=admin_headers).status_code == 409
    assert db.execute("SELECT count(*) FROM materialization_request").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0


# ── the two contract routes ──────────────────────────────────────────────────────────────────────


def test_contract_draft_answers_409_not_500(make_client, conn, db, monkeypatch):
    """``/contract/draft``, over a real generation and a real recorded Gate-1 choice."""
    error = _genuine_integrity_error(db)
    _bank(conn)
    client = make_client(llm_client=_fake_with_intents())
    body = _post(client)
    option = next(f for s_ in body["alternatives"] for f in s_["features"]
                  if f.get("source_definition_id"))

    def _refuse(*a, **k):
        raise error

    monkeypatch.setattr(sod, "load_frozen_option_facts", _refuse)
    response = client.post("/contract/draft", json={
        "intent_id": body["intent_id"],
        "chosen_option_id": option["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)

    _assert_integrity_conflict(response, str(error))


def test_contract_confirm_answers_409_not_500(make_client, conn, db, monkeypatch):
    """``/contract/confirm`` — the GOVERNING write, which must never be softer than the draft.
    A client that skipped the draft reaches the same refusal here."""
    error = _genuine_integrity_error(db)
    _bank(conn)
    client = make_client(llm_client=_fake_with_intents())
    body = _post(client)
    option = next(f for s_ in body["alternatives"] for f in s_["features"]
                  if f.get("source_definition_id"))
    client.post("/contract/draft", json={
        "intent_id": body["intent_id"],
        "chosen_option_id": option["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)

    def _refuse(*a, **k):
        raise error

    monkeypatch.setattr(sod, "load_frozen_option_facts", _refuse)
    response = client.post("/contract/confirm", json={
        "feature_name": option["name"],
        "definition": "hand-built definition smuggled past the draft",
        "derives_from": option.get("derives_from", []),
        "intent_id": body["intent_id"],
    }, headers=AUTH)

    _assert_integrity_conflict(response, str(error))
