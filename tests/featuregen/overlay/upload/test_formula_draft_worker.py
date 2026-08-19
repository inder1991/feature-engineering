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
