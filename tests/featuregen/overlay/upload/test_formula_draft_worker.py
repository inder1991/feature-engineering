"""The Draft-formula lane: what it never holds, what it never pays twice for, and what it reports.

Every test here is about a rule that costs money or credibility when it breaks:

1. **The lane is DEDICATED**, so the general consumer — which wraps its handler in one transaction —
   can never claim a draft and hold that transaction across two model calls.
2. **A blocked candidate never reaches a model.** No pinned catalog snapshot means nothing to author
   against, and finding that out is free.
3. **BLOCKED before authoring stays a SHORT path.** The state machine must not require four stages
   that never happened in order to record a refusal that was known at the start.
4. **READY is the exception**: it is reachable only from ADMISSION, so a ready formula whose trace
   records no critic stays impossible.
5. **A deterministic fault fails PERMANENTLY.** Retrying it would buy the same refusal again.
"""
from __future__ import annotations

import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.overlay.upload.formula_draft_store import (
    DraftStateV1,
    InvalidTransition,
    advance,
    read_draft,
    request_draft,
)
from featuregen.overlay.upload.formula_draft_worker import (
    _KIND_STATE,
    _PATH,
    _observed_state,
    _terminalize,
    _walk_to,
    process_formula_draft_once,
)
from featuregen.runtime.queue import (
    _DEDICATED_HANDLERS,
    FORMULA_DRAFT_QUEUE_HANDLERS,
    claim_formula_draft,
    claim_one,
    enqueue,
)

HANDLER = "formula_draft.author.v1"


def _request(db, draft_id="fd-1"):
    # Migration 1116 makes `considered_revision_id` a real foreign key. `seed_run_chain` inserts
    # ON CONFLICT DO NOTHING, so the tests below that mint their OWN `crev-1` — carrying the exact
    # `considered_json` they are about — keep it; this only covers the ones that never needed one.
    seed_run_chain(db, run_id="fdw", considered_revision_id="crev-1")
    return request_draft(
        db, formula_draft_id=draft_id, considered_revision_id="crev-1", option_id="opt-a",
        planning_request_hash="sha256:asked", catalog_snapshot_hash="sha256:catalog",
        authoring_config_hash="sha256:config", definition_revision="",
        requested_by="user:ops", requested_at="2026-08-17T00:00:00Z")


# ══ THE LANE IS DEDICATED ═══════════════════════════════════════════════════════════════════════
def test_THE_GENERAL_CONSUMER_CANNOT_CLAIM_A_DRAFT(db):
    """The money-and-latency rule, asserted against the two claims rather than against a comment.

    `process_one` runs its handler INSIDE `with conn.transaction()`. If the general consumer could
    claim a draft, one database transaction would be held open across two provider calls — the exact
    shape the async design exists to prevent.
    """
    enqueue(db, message_id="m-1", partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": "fd-1"})

    assert claim_one(db, owner="general") is None, "the general lane claimed a draft"
    claimed = claim_formula_draft(db, owner="draft-lane")
    assert claimed is not None and claimed.handler == HANDLER


def test_the_lane_is_registered_as_dedicated():
    """The two consumers are complementary BY CONSTRUCTION — `claim_one` excludes exactly the
    dedicated set, so a handler missing from it is silently stealable."""
    assert FORMULA_DRAFT_QUEUE_HANDLERS <= _DEDICATED_HANDLERS


def test_the_handler_name_agrees_with_the_route_that_produces_it():
    """Queue-side and route-side spell the same string. `queue.py` cannot import the route module
    (that module imports it), so the two literals are checked here — a rename that lands on one side
    only would leave `claim_one` free to steal a draft."""
    from featuregen.api.routes.formula_drafts import FORMULA_DRAFT_HANDLER

    assert FORMULA_DRAFT_QUEUE_HANDLERS == frozenset({FORMULA_DRAFT_HANDLER})


# ══ A BLOCKED CANDIDATE NEVER REACHES A MODEL ═══════════════════════════════════════════════════
def test_A_CANDIDATE_WITH_NO_PINNED_SNAPSHOT_IS_BLOCKED_WITHOUT_SPENDING(db, monkeypatch):
    """No frozen catalog means no world to author against — and finding that out is free.

    The provider is replaced with something that RAISES if touched: "we return before calling it" is
    a claim about control flow, and this is the assertion that makes it a fact.
    """
    def _explode(*args, **kwargs):
        raise AssertionError("a blocked candidate must never reach a provider")

    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker.current_llm_client", _explode)

    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','churn rises with dormancy','hypothesis')")
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-1','int-1','run-1', NULL, '{}'::jsonb, 'h', 'v2')")
    _request(db)
    enqueue(db, message_id="m-1", partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": "fd-1"})

    outcome = process_formula_draft_once(db, owner="w")

    assert outcome.state == DraftStateV1.BLOCKED.value
    draft = read_draft(db, "fd-1")
    assert draft.state is DraftStateV1.BLOCKED
    assert draft.blockers[0]["code"] == "CATALOG_SNAPSHOT_UNPINNED"
    # The reason names the remedy and whose it is, rather than restating the code.
    assert "regenerate the candidates" in draft.blockers[0]["reason"]


def test_A_STALE_REVISION_IS_A_BLOCKER_NOT_AN_OUTAGE(db, monkeypatch):
    """A revision that cannot resolve exact option identity is a fact about what was STORED.

    The route already makes this judgement — it answers 409, not 500. The worker must agree: FAILED
    means "the platform broke", and it pages whoever is on call. This pages nobody, names the
    candidate, and leaves the remedy with whoever asked for the candidate set.
    """
    def _explode(*args, **kwargs):
        raise AssertionError("an unresolvable candidate must never reach a provider")

    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker.current_llm_client", _explode)

    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','churn rises with dormancy','hypothesis')")
    # A snapshot IS pinned, so the snapshot branch cannot be what answers — the revision's own
    # unversioned `considered_json` is.
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-1','int-1','run-1','snap-1', '{}'::jsonb, 'h', "
        "'contract-considered-v1')")
    _request(db)
    enqueue(db, message_id="m-1", partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": "fd-1"})

    process_formula_draft_once(db, owner="w")

    draft = read_draft(db, "fd-1")
    assert draft.state is DraftStateV1.BLOCKED, "a stale revision was reported as an outage"
    assert draft.blockers[0]["code"] == "CANDIDATE_UNRESOLVABLE"
    assert draft.failure_reason is None, "a blocker is not a failure and must not carry one"


def test_AN_UNRESOLVED_REQUESTER_IS_A_BLOCKER_NOT_AN_OUTAGE(db, monkeypatch):
    """The platform declining to read a catalog for a principal it cannot vouch for is the CHECK
    WORKING, not a fault.

    The shipped recipe-formula shadow lane already makes exactly this judgement: it records the same
    condition on its AUTHORIZATION axis with `technical_axis: "OK"` and `authoring_axis: "NOT_RUN"`.
    Two lanes disagreeing about whether the same condition is an incident is how one of them starts
    paging the wrong person.

    FOUND LIVE: a draft requested under a header identity with no local account came back FAILED,
    which reads as "the platform broke" and sends an operator hunting an outage that is not
    happening. The remedy belongs to whoever administers accounts.
    """
    def _explode(*args, **kwargs):
        raise AssertionError("an unverifiable requester must never reach a provider")

    monkeypatch.setattr(
        "featuregen.overlay.upload.formula_draft_worker.current_llm_client", _explode)

    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-1','h','hypothesis')")
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-1','int-1','run-1','snap-1', %s::jsonb, 'h', "
        "'contract-considered-v3')", (_one_option_revision(),))
    _request(db)
    enqueue(db, message_id="m-1", partition_key="p-1", handler=HANDLER,
            payload={"formula_draft_id": "fd-1"})

    outcome = process_formula_draft_once(db, owner="w")

    draft = read_draft(db, "fd-1")
    assert draft.state is DraftStateV1.BLOCKED, "an unverifiable requester was reported as an outage"
    assert draft.blockers[0]["code"].startswith("REQUESTER_")
    assert draft.failure_reason is None, "a governed refusal is not a failure"
    # The queue message is DONE, not dead: there is nothing to retry and nothing to page about.
    assert db.execute("SELECT status FROM queue WHERE message_id='m-1'").fetchone()[0] == "done"
    assert outcome.status == "ok"


def _one_option_revision():
    """A minimal v2 revision whose single option grounds on one ref, built with the shipped
    helpers so the resolver's cross-check passes."""
    import json

    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    idea = FeatureIdea(name="f", description="d", derives_from=["public.t.c"],
                       derives_pairs=(("src", "public.t.c"),),
                       aggregation="sum", grain_table="t",
                       # A DECLARED GRAIN, so this candidate reaches the check the test is about.
                       # Without one the worker blocks on GRAIN_NOT_RESOLVED first — correctly, and
                       # that ordering has its own test.
                       operation_kind="sum", measure_refs=(("src", "public.t.c"),),
                       grain_refs=(("src", "public.t.k"),))
    identity = _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea)
    return json.dumps({
        "version": "contract-considered-v3",
        "public": {"anchor": {**_idea_json(idea), "option_id": "opt-a"}, "rejections": []},
        "options_by_id": {"opt-a": {
            "source": "anchor", "lens": "anchor",
            "canonical_candidate_identity": identity,
            "canonical_candidate_identity_hash": canonical_hash(identity),
            "recipe_candidate_key": None}},
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    })


def test_a_message_with_no_subject_dies_rather_than_retrying(db):
    """A message naming no draft cannot be retried into one — retrying it would burn the attempt
    budget on a fault that is identical every time."""
    enqueue(db, message_id="m-1", partition_key="p-1", handler=HANDLER, payload={})
    outcome = process_formula_draft_once(db, owner="w")
    assert outcome.status == "permanent"
    assert db.execute("SELECT status FROM queue WHERE message_id='m-1'").fetchone()[0] == "dead"


# ══ BLOCKED IS SHORT; READY IS NOT ══════════════════════════════════════════════════════════════
def test_BLOCKED_BEFORE_AUTHORING_DOES_NOT_INVENT_FOUR_STAGES(db):
    """A refusal known at REQUESTED is recorded there.

    The alternative — walking AUTHORING → CRITIC_REVIEW → VALIDATING → ADMISSION to reach a legal
    BLOCKED — would write four stages that never happened, and a user reading "Critic review…" for a
    draft no model ever saw is being told something false.
    """
    _request(db)
    _terminalize(db, "fd-1", DraftStateV1.BLOCKED,
                 blockers=[{"code": "CATALOG_SNAPSHOT_UNPINNED", "reason": "no snapshot"}])

    assert read_draft(db, "fd-1").state is DraftStateV1.BLOCKED
    # No authoring run was ever attached, which is the durable evidence that no stage was faked.
    assert read_draft(db, "fd-1").authoring_run_id is None


def test_READY_IS_REACHABLE_ONLY_FROM_ADMISSION(db):
    """The asymmetry that makes the short BLOCKED path safe.

    A refusal may be discovered early; a formula may only be declared ready after the run that
    produced it. Were READY reachable from anywhere too, a lane that skipped straight to it would
    produce a ready draft whose trace records no critic and nothing downstream could tell.
    """
    _request(db)
    for early in (DraftStateV1.REQUESTED, DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW,
                  DraftStateV1.VALIDATING):
        if early is not DraftStateV1.REQUESTED:
            advance(db, "fd-1", early)
        with pytest.raises(InvalidTransition):
            advance(db, "fd-1", DraftStateV1.READY,
                    formula_content_hash="sha256:f", formula_json={})


def test_the_walk_reaches_admission_without_skipping(db):
    """`advance` refuses a skip on purpose, so the caller walks. Each state on the path is visited,
    and the result payload rides the LAST step — a crash part-way must not leave a row claiming a
    formula it has not stored."""
    _request(db)
    _walk_to(db, "fd-1", DraftStateV1.ADMISSION,
             authoring_run_id="far-1", formula_content_hash="sha256:f", formula_json={"body": {}})

    draft = read_draft(db, "fd-1")
    assert draft.state is DraftStateV1.ADMISSION
    assert draft.formula_content_hash == "sha256:f"


def test_the_walk_stops_at_a_terminal_it_finds(db):
    """A draft a user cancelled mid-flight is not dragged onwards by a worker that had already
    started: the stored terminal wins."""
    _request(db)
    advance(db, "fd-1", DraftStateV1.CANCELLED)
    assert _walk_to(db, "fd-1", DraftStateV1.ADMISSION) is DraftStateV1.CANCELLED


def test_terminalizing_an_already_terminal_draft_keeps_the_stored_verdict(db):
    """A redelivery must not replace a user's cancel with a READY."""
    _request(db)
    advance(db, "fd-1", DraftStateV1.CANCELLED)
    assert _terminalize(db, "fd-1", DraftStateV1.READY) is DraftStateV1.CANCELLED


# ══ PROGRESS IS OBSERVED, NOT NARRATED ══════════════════════════════════════════════════════════
def test_the_reported_stage_comes_from_TRACE_EVENTS(db):
    """Every mapped kind is a kind the trace table actually permits.

    Checked against the migration's own CHECK constraint rather than against a copy of it: a lane
    reporting progress from a vocabulary the writer does not use would report no progress at all,
    silently, and the screen would sit on "Queued" for the whole run.
    """
    permitted = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'formula_authoring_trace_event'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%%kind%%'").fetchone()[0]
    for kind in _KIND_STATE:
        assert f"'{kind}'" in permitted, f"{kind} is not a kind the trace writer may write"


def test_progress_only_moves_FORWARD(db):
    """A multi-turn run genuinely revisits the author after a validation. The state must not follow
    it back — "critic review has begun" stays true once it is."""
    assert _PATH.index(_KIND_STATE["author_turn"]) < _PATH.index(_KIND_STATE["critic_result"])
    assert _PATH.index(_KIND_STATE["critic_result"]) < _PATH.index(_KIND_STATE["validation_result"])


def test_an_unstarted_run_reports_nothing_rather_than_a_default(db):
    """Honest absence: a run with no events yet has reached no stage, and reporting AUTHORING for it
    would claim a model was called before one was."""
    assert _observed_state(db, "far-never-opened") is None


def test_the_terminal_kinds_are_deliberately_unmapped():
    """What a finished run MEANS is decided from the folded result and from admission — never from
    the fact that it stopped. A `completed` event does not make a draft READY, because admission has
    not been asked yet."""
    assert "completed" not in _KIND_STATE
    assert "failed" not in _KIND_STATE


# ══ THE REVIEWED LANE'S REBUILD (owner ruling 2026-08-23 item 2) ═══════════════════════════════
def _reviewed_plan(db, *, draft_id="fd-rev-1", pinned_hash=None):
    """A draft whose PLAN pinned the shipped recipe's reviewed blueprint."""
    from tests.featuregen.materialize.provenance_fixtures import BLUEPRINT_RECIPE

    from featuregen.overlay.upload.recipe_formula_blueprint_derivation import derive_blueprint_v2
    from featuregen.overlay.upload.recipe_formula_contracts_v2 import expectation_content_hash_v2
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    real_hash = expectation_content_hash_v2(derive_blueprint_v2(v2_recipe_by_id(BLUEPRINT_RECIPE)))
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES ('int-rev','h','hypothesis') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, considered_json, considered_content_hash, canonicalization_version) "
        "VALUES ('crev-rev','int-rev','run-rev','{}'::jsonb,'sha256:c','contract-considered-v3') "
        "ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, requested_by, requested_at) VALUES "
        "(%s,'crev-rev','opt-rev','h1','h2','cfg-rev','r', %s,'REQUESTED','user:s','t') "
        "ON CONFLICT DO NOTHING", (draft_id, f"ident-{draft_id}"))
    db.execute(
        "INSERT INTO formula_draft_authoring_plan (formula_draft_id, candidate_origin, "
        "formula_strategy, strategy_identity_hash, recipe_id, expectation_ref, "
        "expectation_generation, reviewed_blueprint_revision, reviewed_blueprint_hash) "
        "VALUES (%s, 'recipe', 'REVIEWED_RECIPE_BLUEPRINT', 'sih-1', %s, %s, 'v2', %s, %s)",
        (draft_id, BLUEPRINT_RECIPE, BLUEPRINT_RECIPE, BLUEPRINT_RECIPE,
         pinned_hash or real_hash))
    return draft_id, real_hash


def _fixture_context():
    """The hand-built grounding context the provenance fixtures already bind with."""
    from tests.featuregen.materialize.provenance_fixtures import (
        BLUEPRINT_BINDINGS,
        BLUEPRINT_RECIPE,
        _binding,
    )

    from featuregen.overlay.upload.recipe_grounding_context import (
        RecipeGroundingContextV1,
        semantic_parameter_hash,
    )
    from featuregen.overlay.upload.recipe_grounding_context import (
        content_hash as grounding_content_hash,
    )
    from featuregen.overlay.upload.templates import SourceEntityRoleResolution

    parameters = (("window", 30),)
    definition_json = {"version": "reviewed-lane-test", "window": 30}
    return RecipeGroundingContextV1(
        recipe_candidate_key="candidate-30", recipe_id=BLUEPRINT_RECIPE,
        source_entity_need_role=BLUEPRINT_BINDINGS[0][0],
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=tuple(_binding(role, ref) for role, ref in BLUEPRINT_BINDINGS),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(BLUEPRINT_RECIPE, parameters),
        template_definition=definition_json,
        template_content_hash=grounding_content_hash(definition_json))


def test_A_DRAFT_WITH_NO_PLAN_AUTHORS_BY_LLM(db):
    """Every pre-strategy draft's identity recorded the LLM method, because every draft WAS
    LLM-authored — so absence routes exactly as the identity claims."""
    from featuregen.overlay.upload.formula_draft_worker import _reviewed_blueprint_for

    assert _reviewed_blueprint_for(db, "fd-never-planned") is None


def test_A_REVIEWED_PLAN_REBUILDS_VERIFIES_AND_BINDS(db, monkeypatch):
    """▲ The deterministic lane's worker half: derive from the registry, verify the bytes against
    the PIN the plan froze, bind against the candidate's context — and hand the orchestrator the
    bound object whose bypass names exactly that blueprint. Zero provider involvement anywhere."""
    import featuregen.overlay.upload.formula_draft_worker as worker_mod

    draft_id, real_hash = _reviewed_plan(db)
    monkeypatch.setattr(worker_mod, "_BOUND_CONTEXT_LOADER",
                        lambda conn, d: _fixture_context())

    bound = worker_mod._reviewed_blueprint_for(db, draft_id)

    assert bound is not None
    assert bound.blueprint_content_hash == real_hash
    from featuregen.formula.deterministic_producer import bypass_for
    bypass = bypass_for(bound)
    assert bypass.expectation_hash == real_hash


def test_A_MOVED_BLUEPRINT_BLOCKS_BY_NAME_never_falls_back(db, monkeypatch):
    """▲ The draft's identity folded the strategy over the PINNED bytes. Authoring a newer
    blueprint under the old identity would seal a method claim about bytes nobody chose — and a
    silent LLM fallback would hide the movement entirely. Named, blocked, new request required."""
    import featuregen.overlay.upload.formula_draft_worker as worker_mod

    draft_id, _ = _reviewed_plan(db, draft_id="fd-rev-moved", pinned_hash="a" * 64)
    monkeypatch.setattr(worker_mod, "_BOUND_CONTEXT_LOADER",
                        lambda conn, d: _fixture_context())

    with pytest.raises(worker_mod._DraftBlocked) as raised:
        worker_mod._reviewed_blueprint_for(db, draft_id)
    assert raised.value.blocker["code"] == "REVIEWED_BLUEPRINT_NOT_EXECUTABLE"
    assert "moved" in raised.value.blocker["reason"]


def test_THE_SHIPPED_POSTURE_HAS_NO_LOADER_and_a_reviewed_plan_blocks_loudly(db):
    """▲ The resolver's posture routes reviewed candidates to the LLM while the context plumbing is
    unpersisted, so a REVIEWED plan reaching this worker is a posture/plan DISAGREEMENT — blocked
    loudly, never quietly LLM'd, because quiet is how a disagreement becomes a norm."""
    import featuregen.overlay.upload.formula_draft_worker as worker_mod

    draft_id, _ = _reviewed_plan(db, draft_id="fd-rev-noloader")

    assert worker_mod._BOUND_CONTEXT_LOADER is None
    with pytest.raises(worker_mod._DraftBlocked) as raised:
        worker_mod._reviewed_blueprint_for(db, draft_id)
    assert raised.value.blocker["code"] == "REVIEWED_BLUEPRINT_NOT_EXECUTABLE"
    assert "posture" in raised.value.blocker["reason"]
