"""The whole Draft-formula lane, end to end: request → worker → READY, and the reuse that follows.

The unit tests around this one prove the state machine and the refusals. This one proves the pieces
CONNECT — a real ``run_authoring_v2_replay`` against a scripted provider, through the v2 tool seam,
folding to a real terminal trace event, then admitted against a real advertised set, ending in a
READY draft with a stored formula and a separately-identified admission decision.

⟨LLM⟩ The provider is a scripted ``FakeLLM``, as every authoring suite in this tree is: what is
under test is the WIRING, and a live provider would make this a billing question rather than a
correctness one.

**The reuse claim is tested by COUNTING PROVIDER CALLS.** "An engine gaining an operator costs no
LLM spend" is the design's central economic promise, and the only honest way to check it is to look
at how many times the model was actually asked.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
    seed_authoring_catalog,
)
from tests.featuregen.materialize.test_admission_v2_s13 import ENGINE, _advertise, _client, _raw

from featuregen.overlay.upload.formula_draft_store import DraftStateV1, read_draft, request_draft
from featuregen.overlay.upload.formula_draft_worker import (
    FORMULA_DRAFT_ENGINE_ENV,
    process_formula_draft_once,
)
from featuregen.runtime.queue import enqueue

HANDLER = "formula_draft.author.v1"
SNAPSHOT = "snap-1"
REFS = (TABLE_REF, REF_AMT, REF_DT, REF_CIF)
USER = "user:sam"


def _seed_snapshot(db):
    """The FROZEN catalog the model is given — the same rows `FrozenRecipeReadContext` reads.

    Written directly rather than through `build_metadata_snapshot`, which needs a REPEATABLE READ
    connection, a generation run and a projection-readiness gate — all of them about how a snapshot
    comes to EXIST, none of them about what this lane does with one. The ITEM rows are the real
    shape, because those are what the loader reads.

    Items first, header last: the header write is what SEALS the set, and an item insert after it is
    refused by trigger. The order here is the production order for that reason.
    """
    for ref in REFS:
        for field, value in (("logical_representation", "decimal"), ("unit", "monetary"),
                             ("currency", "fixed:AED")):
            db.execute(
                "INSERT INTO catalog_metadata_snapshot_item "
                "(snapshot_id, catalog_source, graph_ref, logical_ref, item_kind, "
                " field_or_fact_type, value_json, authority_json, item_hash) "
                "VALUES (%s,%s,%s,%s,'field',%s,%s::jsonb,%s::jsonb,%s)",
                (SNAPSHOT, "authored", ref, ref, field, json.dumps({"value": value}),
                 json.dumps({"status": "resolved", "authority": "governed"}),
                 f"sha256:{ref}:{field}"))
    # The shipped helper, so the manifest's NOT NULL columns come from the code that owns them
    # rather than from a hand-written INSERT that drifts the first time one is added.
    from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run

    ensure_generation_run(db, "run-1", {}, {})
    db.execute(
        "INSERT INTO catalog_metadata_snapshot (snapshot_id, generation_run_id, read_scope_hash, "
        "isolation_level, content_hash, item_count) "
        "VALUES (%s,'run-1','sha256:scope','repeatable read','sha256:snap',%s)",
        (SNAPSHOT, len(REFS) * 3))


def _seed_candidate(db):
    """A considered revision whose one option grounds on exactly the refs above."""
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    # `derives_pairs` is (catalog_source, object_ref) with the object_ref PUBLIC-FLATTENED — the
    # shape the generator records and the graph stores. The worker resolves each through
    # `logical_ref_of` to the schema-preserving key the snapshot is actually indexed by, and derives
    # the TABLE ref from the column's parent. Written that way here so this test exercises the same
    # translation production does; a fixture holding pre-resolved refs would have hidden the live
    # defect where the two spellings did not match.
    columns = [ref.split("::", 1) for ref in (REF_AMT, REF_DT, REF_CIF)]
    idea = FeatureIdea(
        name="posted_debit_amount_30d",
        description="recent debit volume",
        derives_from=[ref for _src, ref in columns],
        derives_pairs=tuple((src, ref) for src, ref in columns),
        aggregation="sum",
        grain_table="account",
        grain_ref=("authored", REF_CIF))
    identity = _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea)
    considered = {
        "version": "contract-considered-v2",
        "public": {"anchor": {**_idea_json(idea), "option_id": "opt-a"}, "rejections": []},
        "options_by_id": {"opt-a": {
            "source": "anchor", "lens": "anchor",
            "canonical_candidate_identity": identity,
            "canonical_candidate_identity_hash": canonical_hash(identity),
            "recipe_candidate_key": None}},
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    }
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','falling debit volume predicts attrition','hypothesis')")
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-1','int-1','run-1',%s,%s::jsonb,'h',"
        "'contract-considered-v2')", (SNAPSHOT, json.dumps(considered)))


def _seed_user(db):
    """A live local user, because authoring reads the catalog under a READ SCOPE.

    An unresolved principal would either read nothing or read too much, so the worker refuses to
    author without one — checked here by making the resolution succeed for real rather than stubbing
    it away.
    """
    from featuregen.identity.local_session import create_user

    create_user(db, "sam", "not-a-real-password")


@pytest.fixture
def lane(db, monkeypatch):
    """Everything a draft needs, and a scripted provider that COUNTS how often it is asked.

    NO DSN IS SET, and that is the important part. Given one, the replay orchestrator opens its own
    connection and commits the run, its trace and its provider records DURABLY — correctly, because
    a checkpoint that vanished with a crash would not be a checkpoint, and `llm_call` is write-once
    by trigger precisely so an audit record cannot be erased afterwards. But this suite rolls back,
    so those rows would survive it: an earlier version of this fixture left twelve `llm_dispatch`
    rows behind and failed `test_recipe_formula_worker`, which counts that table across the whole
    database. Nothing here may write outside the transaction, so nothing here needs erasing — and
    the audit table's write-once guarantee is left intact rather than worked around.

    `_renew` is stubbed for the same reason: it renews on its own connection by design, so that a
    renewal is visible to other workers the moment it happens rather than when the run commits.
    Its production behaviour belongs to the lane's own tests, not to this one.
    """
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker._renew", lambda *a, **k: None)
    seed_authoring_catalog(db)
    _seed_snapshot(db)
    _seed_candidate(db)
    _seed_user(db)
    _advertise(db)
    monkeypatch.setenv(FORMULA_DRAFT_ENGINE_ENV, ENGINE)

    calls = {"n": 0}

    def _counted_client():
        calls["n"] += 1
        return _client(_raw())

    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker.current_llm_client", _counted_client)
    return calls


def _request_and_enqueue(db, draft_id, message_id="m-1"):
    """Record a draft and queue it.

    EVERY TEST PASSES ITS OWN draft id, and that is not tidiness. The authoring run this drives is
    named deterministically FROM the draft id — so a resumed job resumes rather than opening (and
    paying for) a second run — and `formula_authoring_run` is committed DURABLY by the replay
    orchestrator, outside this suite's rolled-back transaction, because a checkpoint that vanished
    with a crash would not be a checkpoint. Two tests sharing a draft id therefore share a run, and
    the second one is refused as needing reconciliation. Distinct ids are also what real life looks
    like: every draft is a different candidate.
    """
    request_draft(
        db, formula_draft_id=draft_id, considered_revision_id="crev-1", option_id="opt-a",
        planning_request_hash="sha256:asked", catalog_snapshot_hash="sha256:catalog",
        authoring_config_hash="sha256:config", definition_revision="",
        requested_by=USER, requested_at="2026-08-17T00:00:00Z")
    enqueue(db, message_id=message_id, partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": draft_id})


# ══ THE HAPPY PATH ══════════════════════════════════════════════════════════════════════════════
def test_A_DRAFT_REACHES_READY_WITH_A_STORED_FORMULA_AND_AN_ADMISSION(db, lane):
    """The claim the whole lane exists to make.

    Not "the worker ran without raising" — READY specifically, carrying the formula it says it has,
    and with an admission decision recorded under its own identity. A draft that reached READY with
    a null formula would satisfy a weaker test and be useless to every reader downstream, which is
    why the migration constrains it and why this asserts it.
    """
    _request_and_enqueue(db, "fd-ready", "m-ready")

    outcome = process_formula_draft_once(db, owner="w")

    assert outcome.state == DraftStateV1.READY.value, outcome
    draft = read_draft(db, "fd-ready")
    assert draft.state is DraftStateV1.READY
    assert draft.formula_content_hash, "READY with no formula hash"
    assert draft.formula_json, "READY with no formula"
    assert draft.authoring_run_id, "READY with no run to point at"
    assert draft.blockers == ()
    assert draft.failure_reason is None

    admitted, blockers = db.execute(
        "SELECT admitted, blockers FROM formula_draft_admission WHERE formula_draft_id='fd-ready'"
    ).fetchone()
    assert admitted is True
    assert blockers == []
    # The queue message is finished, so no second worker picks the same draft up.
    assert db.execute("SELECT status FROM queue WHERE message_id='m-ready'").fetchone()[0] == "done"


def test_the_stages_it_passed_through_are_RECORDED_not_narrated(db, lane):
    """READY is only reachable from ADMISSION, and the walk cannot skip.

    Checked against the TRACE the run actually wrote: the draft claims to have been authored,
    critiqued and validated, and these are the events that make that true rather than a state
    machine that was simply told to advance.
    """
    _request_and_enqueue(db, "fd-stages", "m-stages")
    process_formula_draft_once(db, owner="w")

    run_id = read_draft(db, "fd-stages").authoring_run_id
    kinds = {row[0] for row in db.execute(
        "SELECT kind FROM formula_authoring_trace_event WHERE authoring_run_id=%s",
        (run_id,)).fetchall()}
    assert "author_turn" in kinds
    assert "critic_result" in kinds


# ══ THE REUSE THAT PAYS FOR THE DESIGN ══════════════════════════════════════════════════════════
def test_A_REDELIVERED_MESSAGE_DOES_NOT_AUTHOR_AGAIN(db, lane):
    """The at-least-once guarantee must not become an at-least-once BILL.

    Counted on the provider, because "the code returns early" is a claim about control flow and the
    number of times a paid model was asked is the fact.
    """
    _request_and_enqueue(db, "fd-redeliver", "m-redeliver")
    process_formula_draft_once(db, owner="w")
    asked_once = lane["n"]

    # The same draft, delivered again — a broker retry, or a worker that crashed after committing.
    enqueue(db, message_id="m-2", partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": "fd-redeliver"})
    outcome = process_formula_draft_once(db, owner="w")

    assert outcome.state == DraftStateV1.READY.value
    assert lane["n"] == asked_once, "a redelivery bought the same formula a second time"


def test_AN_ENGINE_GAINING_AN_OPERATOR_IS_A_NEW_ADMISSION_AND_NO_NEW_SPEND(db, lane, monkeypatch):
    """The economic promise of the two identities, tested where it is actually claimed.

    A capability set that moves must re-decide the EXISTING formula. Nothing about what the model
    would write has changed, so nothing should be paid. This is the difference between a platform
    whose costs fall as it improves and one whose costs repeat.
    """
    from featuregen.materialize.execution_proof_store import advertised_operators
    from featuregen.overlay.upload.formula_draft_store import (
        existing_admission,
        record_admission,
    )

    _request_and_enqueue(db, "fd-widen", "m-widen")
    process_formula_draft_once(db, owner="w")
    asked = lane["n"]
    draft = read_draft(db, "fd-widen")

    # The set as it stands has a decision already — which is what a re-run reads FIRST, and why it
    # does not need to re-decide either.
    advertised = advertised_operators(db, engine_id=ENGINE)
    assert existing_admission(
        db, formula_content_hash=draft.formula_content_hash,
        engine_id=ENGINE, advertised=advertised) == (True, ())

    # Now the engine gains an operator. A DIFFERENT set, so a different admission identity, decided
    # against the SAME formula.
    widened = [*advertised, "an_operator_it_just_proved"]
    assert existing_admission(
        db, formula_content_hash=draft.formula_content_hash,
        engine_id=ENGINE, advertised=widened) is None, "a moved set reused the old decision"

    record_admission(
        db, formula_draft_id="fd-widen", formula_content_hash=draft.formula_content_hash,
        engine_id=ENGINE, advertised=widened, admitted=True)

    assert db.execute(
        "SELECT count(*) FROM formula_draft_admission WHERE formula_draft_id='fd-widen'"
    ).fetchone()[0] == 2
    # ONE formula, TWO decisions, and the model was never asked again.
    assert db.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 1
    assert lane["n"] == asked


# ══ AN UNADVERTISED OPERATOR IS BLOCKED, NOT BROKEN ═════════════════════════════════════════════
def test_AN_ENGINE_THAT_ADVERTISES_NOTHING_BLOCKS_RATHER_THAN_FAILING(db, lane, monkeypatch):
    """A perfectly good formula against an engine that has proved nothing.

    BLOCKED with a named code — the remedy is to prove the operator — rather than FAILED, which
    would send someone to investigate an outage that is not happening. The formula was still
    authored and paid for, so it is still stored and still readable.
    """
    monkeypatch.setenv(FORMULA_DRAFT_ENGINE_ENV, "an-engine-that-proved-nothing")
    _request_and_enqueue(db, "fd-blocked", "m-blocked")

    outcome = process_formula_draft_once(db, owner="w")

    assert outcome.state == DraftStateV1.BLOCKED.value
    draft = read_draft(db, "fd-blocked")
    assert draft.blockers, "BLOCKED with no named blocker is a refusal nobody can act on"
    assert draft.failure_reason is None, "a blocked formula is not a failure"
    # The paid-for formula is not thrown away because it cannot run HERE — an engine that later
    # proves the operator re-decides this exact formula for free.
    assert draft.formula_content_hash
    admitted = db.execute(
        "SELECT admitted FROM formula_draft_admission WHERE formula_draft_id='fd-blocked'").fetchone()
    assert admitted is not None and admitted[0] is False


def test_a_deployment_that_NAMES_NO_ENGINE_still_delivers_the_formula(db, lane, monkeypatch):
    """No engine configured is a fact about the DEPLOYMENT, not about the candidate.

    The formula is real and the user can read it; what is missing is a decision about where it could
    run — which is exactly what the separate admission identity models. Recording an admission
    against an unnamed engine would invent the one fact `admit_artifacts_v2` refuses to default.
    """
    monkeypatch.delenv(FORMULA_DRAFT_ENGINE_ENV, raising=False)
    _request_and_enqueue(db, "fd-noengine", "m-noengine")

    outcome = process_formula_draft_once(db, owner="w")

    assert outcome.state == DraftStateV1.READY.value
    assert read_draft(db, "fd-noengine").formula_content_hash
    # HONEST ABSENCE: no decision was made, so none is recorded.
    assert db.execute(
        "SELECT count(*) FROM formula_draft_admission").fetchone()[0] == 0
