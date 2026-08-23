"""Step 5a — the coordinator SEQUENCES; every gate it passes through is everybody's gate.

The build stage runs against the REAL machinery (bindings, build set, generation authorization,
the decision service, the generation request and its queue row) — the point of the coordinator is
composition, and a test that faked all five parts would prove only that fakes compose. The
authoring stages fake exactly ONE seam: `request_draft_for_candidate`, whose own behaviour is
proved by the drafts suites.

Unique ids per test (the test_retirement_scope lesson).
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.crosswalk_fixtures import build_set_declaration
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.materialize.build_set_declaration import (
    declaration_identity,
    encode_declaration,
)
from featuregen.materialize.code_generation_coordinator import process_code_generation_once
from featuregen.overlay.upload.code_generation_job_store import (
    JobMemberSpecV1,
    JobStatusV1,
    MemberStateV1,
    advance_job,
    create_job,
    read_job,
    read_job_actions,
    read_members,
    update_member,
)

_PARAMS = {"engine_id": "kedro-pyspark",
           "physical_type_policy": "formula-v2/physical-types@1",
           "empty_values": {"opt-a": "0", "opt-b": None},
           "target_mode": "exploration"}


def _job(conn, tag: str, *, n_members: int = 2) -> str:
    considered = f"crev-{tag}"
    seed_run_chain(conn, run_id=f"cgc-{tag}", considered_revision_id=considered)
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-cgc','exploration','h') ON CONFLICT DO NOTHING", (f"trr-{tag}",))
    members = []
    for position in range(n_members):
        selection = f"sel-{tag}-{position}"
        conn.execute(
            "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
            "considered_revision_id, option_id, decision_id, planning_request_hash, "
            "binding_plan_hash, content_hash) VALUES (%s,%s,%s,%s,%s,'sha256:ask','sha256:plan',"
            "%s) ON CONFLICT DO NOTHING",
            (selection, f"trr-{tag}", considered, f"opt-{position}", f"dec-{selection}",
             f"ch-{selection}"))
        members.append(JobMemberSpecV1(
            position=position, selection_revision_id=selection,
            considered_revision_id=considered, option_id=f"opt-{position}",
            formula_strategy="llm_authored"))
    declaration = build_set_declaration()
    job_id, created = create_job(
        conn, job_id=f"cgj-{tag}", considered_revision_id=considered,
        target_reading_revision_id=f"trr-{tag}", environment_id="hdfc-local",
        logical_group_name=f"grp-{tag}", declaration=encode_declaration(declaration),
        declaration_identity=declaration_identity(declaration),
        execution_parameters=_PARAMS, members=members, requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")
    assert created
    return job_id


def _seed_draft(conn, job_id: str, position: int, *, state: str = "REQUESTED") -> str:
    """A REAL draft row for one member's candidate — the fields the binding's composite FKs
    will check against the selection."""
    member = read_members(conn, job_id)[position]
    draft_id = f"fd-{job_id}-{position}"
    ready = state == "READY"
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, formula_content_hash, formula_json, "
        "requested_by, requested_at) VALUES (%s,%s,%s,'sha256:ask','sha256:snap','sha256:cfg',"
        "'rev-1',%s,%s,%s,%s::jsonb,'user:sam','2026-08-23T00:00:00Z') "
        "ON CONFLICT (formula_draft_id) DO NOTHING",
        (draft_id, member.considered_revision_id, member.option_id,
         f"sha256:ident-{draft_id}", state,
         f"sha256:formula-{draft_id}" if ready else None,
         '{"formula_schema_version": 3, "fixture": true}' if ready else None))
    return draft_id


class _FakeRequested:
    def __init__(self, formula_draft_id: str):
        self.formula_draft_id = formula_draft_id
        self.created = True


# ══ the authoring stages ════════════════════════════════════════════════════════════════════════
def test_REQUESTED_requests_every_draft_through_the_ONE_service(db, monkeypatch):
    from featuregen.overlay.upload import formula_draft_service

    job_id = _job(db, "req")
    requested: list[tuple[str, str]] = []

    def fake(conn, *, revision_id, option_id, formula_draft_id, requested_by, now, **kwargs):
        requested.append((option_id, requested_by))
        position = int(option_id.split("-")[1])
        return _FakeRequested(_seed_draft(conn, job_id, position))

    monkeypatch.setattr(formula_draft_service, "request_draft_for_candidate", fake)
    assert process_code_generation_once(db, worker_id="w1") is True

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.AUTHORING
    assert requested == [("opt-0", "user:sam"), ("opt-1", "user:sam")]
    members = read_members(db, job_id)
    assert all(m.member_state is MemberStateV1.AUTHORING for m in members)
    assert all(m.formula_draft_id for m in members)
    actions = {a["action"]: a["state"] for a in read_job_actions(db, job_id)}
    assert actions["AUTHOR_FORMULA"] == "PERFORMED"
    # The lease came back with the stage: the next tick may claim immediately.
    assert db.execute("SELECT lease_owner FROM code_generation_job WHERE job_id = %s",
                      (job_id,)).fetchone() == (None,)


def test_MEMBER_REFUSALS_BLOCK_THE_JOB_with_one_complete_answer(db, monkeypatch):
    """No selected feature is silently dropped — and the job blocks only when every member has
    settled, so 'which selections need attention' is one answer, not a drip-feed."""
    from featuregen.overlay.upload import formula_draft_service
    from featuregen.overlay.upload.formula_draft_service import (
        FrozenCandidateV1,
        RetiredAtRequest,
    )

    job_id = _job(db, "blk")

    def fake(conn, *, revision_id, option_id, formula_draft_id, requested_by, now, **kwargs):
        raise RetiredAtRequest(
            "retired", candidate=FrozenCandidateV1(revision_id, None, "s", "p", "d"),
            config_hash="cfg")

    monkeypatch.setattr(formula_draft_service, "request_draft_for_candidate", fake)
    process_code_generation_once(db, worker_id="w1")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.BLOCKED
    assert all(m.blockers == ("FORMULA_DRAFT_RETIRED",) for m in read_members(db, job_id))
    detail = job.terminal_detail
    assert len(detail["members"]) == 2, "BOTH refused members are in the one answer"
    assert "NEW job" in detail["detail"]


def test_AUTHORING_waits_on_DURABLE_draft_states_then_advances(db, monkeypatch):
    from featuregen.overlay.upload import formula_draft_service

    job_id = _job(db, "wait")
    monkeypatch.setattr(
        formula_draft_service, "request_draft_for_candidate",
        lambda conn, *, revision_id, option_id, formula_draft_id, requested_by, now, **kw:
        _FakeRequested(_seed_draft(conn, job_id, int(option_id.split("-")[1]))))
    process_code_generation_once(db, worker_id="w1")
    assert read_job(db, job_id).status is JobStatusV1.AUTHORING

    # Drafts still REQUESTED → the job WAITS (release, no advance): providers are never polled.
    process_code_generation_once(db, worker_id="w1")
    assert read_job(db, job_id).status is JobStatusV1.AUTHORING

    # The drafts turn READY on their own lane; the next look advances on the durable state.
    for position in range(2):
        db.execute(
            "UPDATE formula_draft SET state = 'READY', formula_content_hash = %s, "
            "formula_json = '{\"formula_schema_version\": 3}'::jsonb "
            "WHERE formula_draft_id = %s",
            (f"sha256:formula-fd-{job_id}-{position}", f"fd-{job_id}-{position}"))
    process_code_generation_once(db, worker_id="w1")
    job = read_job(db, job_id)
    assert job.status is JobStatusV1.READY_TO_BUILD
    assert all(m.member_state is MemberStateV1.FORMULA_READY
               for m in read_members(db, job_id))


def test_a_BLOCKED_draft_blocks_its_member_with_the_drafts_own_blockers(db, monkeypatch):
    from featuregen.overlay.upload import formula_draft_service

    job_id = _job(db, "dblk", n_members=1)
    monkeypatch.setattr(
        formula_draft_service, "request_draft_for_candidate",
        lambda conn, *, revision_id, option_id, formula_draft_id, requested_by, now, **kw:
        _FakeRequested(_seed_draft(conn, job_id, 0)))
    process_code_generation_once(db, worker_id="w1")

    db.execute(
        "UPDATE formula_draft SET state = 'BLOCKED', blockers = %s::jsonb "
        "WHERE formula_draft_id = %s", ('["UNBOUND_OPERAND"]', f"fd-{job_id}-0"))
    process_code_generation_once(db, worker_id="w1")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.BLOCKED
    assert read_members(db, job_id)[0].blockers == ("UNBOUND_OPERAND",)


# ══ the build stage — REAL machinery ════════════════════════════════════════════════════════════
def _ready_to_build(db, tag: str) -> str:
    """A job at READY_TO_BUILD with REAL READY drafts on its members."""
    job_id = _job(db, tag)
    for position in range(2):
        draft_id = _seed_draft(db, job_id, position, state="READY")
        update_member(db, job_id, position, state=MemberStateV1.FORMULA_READY,
                      formula_draft_id=draft_id)
    advance_job(db, job_id, JobStatusV1.REQUESTED, JobStatusV1.PLANNING_FORMULAS)
    advance_job(db, job_id, JobStatusV1.PLANNING_FORMULAS, JobStatusV1.AUTHORING)
    advance_job(db, job_id, JobStatusV1.AUTHORING, JobStatusV1.READY_TO_BUILD)
    return job_id


def test_THE_BUILD_STAGE_composes_the_REAL_chain(db):
    """Bindings → build set → server-minted approval → the ONE decision service → generation
    request + queue row, all in one drive — and every link lands on the job."""
    job_id = _ready_to_build(db, "build")

    assert process_code_generation_once(db, worker_id="w1") is True

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.GENERATING_PREVIEW
    assert job.build_set_revision_id and job.generation_request_id

    members = read_members(db, job_id)
    assert all(m.member_state is MemberStateV1.BOUND for m in members)
    assert all(m.selection_formula_binding_id for m in members)

    # The build set pins the BINDINGS, in member order.
    from featuregen.overlay.upload.build_set_store import read_build_set

    build_set = read_build_set(db, job.build_set_revision_id)
    assert list(build_set.selection_formula_binding_ids) == [
        m.selection_formula_binding_id for m in members]

    # The request-time decision is durable and the action row carries it (§0.1.3).
    actions = {a["action"]: a for a in read_job_actions(db, job_id)}
    generate = actions["GENERATE_PREVIEW"]
    assert generate["state"] == "PERFORMED"
    assert generate["decision_revision_id"]
    decision_row = db.execute(
        "SELECT allowed FROM action_decision_revision WHERE decision_id = %s",
        (generate["decision_revision_id"],)).fetchone()
    assert decision_row == (True,)

    # The generation request exists with the queue row the lane will claim.
    from featuregen.overlay.upload.build_set_store import read_request

    request = read_request(db, job.generation_request_id)
    assert request is not None and request.build_set_revision_id == job.build_set_revision_id
    from featuregen.materialize.generation_lane import generation_message_id

    queued = db.execute(
        "SELECT COUNT(*) FROM queue WHERE message_id = %s",
        (generation_message_id(job.generation_request_id),)).fetchone()
    assert queued[0] == 1


def test_A_REDRIVEN_BUILD_STAGE_IS_IDEMPOTENT_nothing_doubles(db):
    """A crash after the build stage's writes but before the advance re-drives the stage; every
    write is content-addressed or live-scoped, so the world ends up identical."""
    job_id = _ready_to_build(db, "redrive")
    process_code_generation_once(db, worker_id="w1")
    first = read_job(db, job_id)

    # Simulate the crash-shaped replay: force the status back and drive again.
    db.execute("UPDATE code_generation_job SET status = 'READY_TO_BUILD', lease_owner = NULL, "
               "lease_expires_at = NULL WHERE job_id = %s", (job_id,))
    process_code_generation_once(db, worker_id="w2")
    second = read_job(db, job_id)

    assert second.build_set_revision_id == first.build_set_revision_id
    assert second.generation_request_id == first.generation_request_id, (
        "the LIVE generation attempt is the answer; a redrive must not start a second compile")


# ══ watching the generation ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("generation_status, expected_job_status", [
    ("SUCCEEDED", JobStatusV1.PREVIEW_READY),
    ("REFUSED", JobStatusV1.BLOCKED),
    ("FAILED", JobStatusV1.FAILED),
])
def test_THE_GENERATION_TERMINAL_FOLDS_ONTO_THE_JOB(db, generation_status, expected_job_status):
    job_id = _ready_to_build(db, f"gen{generation_status.lower()}")
    process_code_generation_once(db, worker_id="w1")
    job = read_job(db, job_id)
    # Terminal writes must satisfy the schema's own honesty CHECKs: a success carries its
    # (really-sealed) artifact, a refusal its refusals, a failure its reason.
    if generation_status == "SUCCEEDED":
        approval = db.execute(
            "SELECT generation_authorization_revision_id FROM generation_request "
            "WHERE request_id = %s", (job.generation_request_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO sealed_artifact_v2 (artifact_id, "
            "generation_authorization_revision_id, environment_id, logical_group_name, "
            "compilation_identity_hash, group_plan_hash, project_digest, subgraph_satisfied, "
            "triggered_requirements, subgraph_findings, sealed_at) VALUES ('seal-x', %s, "
            "'hdfc-local', %s, 'c', 'g', 'sha256:d', true, '[]'::jsonb, '[]'::jsonb, 't')",
            (approval, job.logical_group_name if hasattr(job, "logical_group_name")
             else "grp"))
    extras = {"SUCCEEDED": "sealed_artifact_id = 'seal-x'",
              "REFUSED": "refusals = '[{\"code\": \"X\"}]'::jsonb",
              "FAILED": "failure_reason = 'boom'"}[generation_status]
    db.execute(f"UPDATE generation_request SET status = %s, {extras} WHERE request_id = %s",
               (generation_status, job.generation_request_id))

    process_code_generation_once(db, worker_id="w1")
    assert read_job(db, job_id).status is expected_job_status


def test_a_still_running_generation_is_WAITED_ON_not_polled_to_death(db):
    job_id = _ready_to_build(db, "genwait")
    process_code_generation_once(db, worker_id="w1")
    process_code_generation_once(db, worker_id="w1")
    assert read_job(db, job_id).status is JobStatusV1.GENERATING_PREVIEW


# ══ crash posture ═══════════════════════════════════════════════════════════════════════════════
def test_A_COORDINATOR_CRASH_FAILS_THE_JOB_with_the_posture_named(db, monkeypatch):
    from featuregen.overlay.upload import formula_draft_service

    job_id = _job(db, "crash", n_members=1)

    def boom(conn, **kwargs):
        raise RuntimeError("provider registry exploded")

    monkeypatch.setattr(formula_draft_service, "request_draft_for_candidate", boom)
    with pytest.raises(RuntimeError, match="provider registry exploded"):
        process_code_generation_once(db, worker_id="w1")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.FAILED
    assert job.terminal_detail["failure"] == "coordinator crash"
    assert "provider registry exploded" in job.terminal_detail["traceback"]


# ══ §11.2 — the spend thread: ceiling → plan row → per-call binding ═════════════════════════════
def test_THE_JOBS_CEILING_RIDES_EVERY_MEMBERS_PLAN_ROW(db):
    """Through the REAL service: the coordinator resolves the recorded ceiling once and every
    LLM member's authoring plan carries it to the worker's dispatch seam."""
    from tests.featuregen.api.routes.test_formula_drafts import _revision

    from featuregen.overlay.upload.llm_spend import authorize_spend

    tag = "spendthread"
    revision = _revision(db, revision_id=f"crev-{tag}", snapshot_id=f"snap-{tag}")
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-1','exploration','h') ON CONFLICT DO NOTHING", (f"trr-{tag}",))
    selection = f"sel-{tag}"
    db.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES (%s,%s,%s,'opt-a',%s,'sha256:ask',"
        "'sha256:plan',%s)",
        (selection, f"trr-{tag}", revision, f"dec-{selection}", f"ch-{selection}"))
    declaration = build_set_declaration()
    job_id, _ = create_job(
        db, job_id=f"cgj-{tag}", considered_revision_id=revision,
        target_reading_revision_id=f"trr-{tag}", environment_id="hdfc-local",
        logical_group_name=f"grp-{tag}", declaration=encode_declaration(declaration),
        declaration_identity=declaration_identity(declaration),
        execution_parameters=_PARAMS,
        members=(JobMemberSpecV1(position=0, selection_revision_id=selection,
                                 considered_revision_id=revision, option_id="opt-a",
                                 formula_strategy="LLM_AUTHORED"),),
        requested_by="user:sam", requested_at="2026-08-23T00:00:00Z")
    identity = db.execute(
        "SELECT content_identity_hash FROM code_generation_job WHERE job_id = %s",
        (job_id,)).fetchone()[0]
    spend_id = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity=identity,
        member_identities=[selection], provider_contract_hash="sha256:contract",
        max_calls=5, max_tokens=100_000, currency="USD", max_cost="10.00",
        pricing_version="p@1", expires_at="2026-12-31T00:00:00Z")

    process_code_generation_once(db, worker_id="w1")   # REAL service — no fakes

    member = read_members(db, job_id)[0]
    assert member.member_state is MemberStateV1.AUTHORING
    plan = db.execute(
        "SELECT llm_spend_authorization_id FROM formula_draft_authoring_plan "
        "WHERE formula_draft_id = %s", (member.formula_draft_id,)).fetchone()
    assert plan == (spend_id,), "the ceiling rides the PLAN to the dispatch seam"

    # And the worker-side binding derives the approval's OWN per-call arithmetic.
    from featuregen.overlay.upload.formula_draft_worker import _spend_binding_for

    binding = _spend_binding_for(db, member.formula_draft_id)
    assert binding.spend_authorization_id == spend_id
    assert binding.call_tokens == 20_000          # ceil(100000 / 5)
    assert str(binding.call_cost) == "2.00"       # 10.00 / 5, Decimal arithmetic


def test_a_draft_with_no_ceiling_binds_NOTHING(db):
    from featuregen.overlay.upload.formula_draft_worker import _spend_binding_for

    assert _spend_binding_for(db, "fd-none") is None


def test_a_FAILED_PREDECESSOR_blocks_the_member_by_name_never_the_whole_job(db, monkeypatch):
    """DraftNotAnAnswer is a considered refusal (§11.1.2): the member blocks with the code, the
    job settles as BLOCKED — not FAILED as a 'coordinator crash'. Found by the run-spine session:
    the control-flow exception was uncaught in both service callers."""
    from featuregen.overlay.upload import formula_draft_service
    from featuregen.overlay.upload.formula_draft_service import NotAnAnswerAtRequest

    job_id = _job(db, "naa", n_members=1)

    def fake(conn, **kwargs):
        raise NotAnAnswerAtRequest("the existing draft records a failure")

    monkeypatch.setattr(formula_draft_service, "request_draft_for_candidate", fake)
    process_code_generation_once(db, worker_id="w1")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.BLOCKED, "a product refusal, never a platform crash"
    assert read_members(db, job_id)[0].blockers == ("FORMULA_DRAFT_NOT_AN_ANSWER",)


def test_an_ABSENT_JOB_CEILING_refuses_the_member_never_substitutes_a_dev_envelope(db):
    """Task 5 review 4b: the job's ceiling was cost-CONFIRMED at the write; if it is gone (or
    expired) at drive time, the member refuses as COST_AUTHORIZATION_MISSING — quietly minting a
    $25 development envelope would replace what a person approved with what nobody did."""
    from tests.featuregen.api.routes.test_formula_drafts import _revision

    tag = "noceil"
    revision = _revision(db, revision_id=f"crev-{tag}", snapshot_id=f"snap-{tag}")
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s,'int-1','exploration','h') ON CONFLICT DO NOTHING", (f"trr-{tag}",))
    selection = f"sel-{tag}"
    db.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES (%s,%s,%s,'opt-a',%s,'sha256:ask',"
        "'sha256:plan',%s)",
        (selection, f"trr-{tag}", revision, f"dec-{selection}", f"ch-{selection}"))
    declaration = build_set_declaration()
    job_id, _ = create_job(
        db, job_id=f"cgj-{tag}", considered_revision_id=revision,
        target_reading_revision_id=f"trr-{tag}", environment_id="hdfc-local",
        logical_group_name=f"grp-{tag}", declaration=encode_declaration(declaration),
        declaration_identity=declaration_identity(declaration),
        execution_parameters=_PARAMS,
        members=(JobMemberSpecV1(position=0, selection_revision_id=selection,
                                 considered_revision_id=revision, option_id="opt-a",
                                 formula_strategy="LLM_AUTHORED"),),
        requested_by="user:sam", requested_at="2026-08-23T00:00:00Z")

    # NO spend authorization exists for the job identity — the drive runs the REAL service.
    process_code_generation_once(db, worker_id="w1")

    job = read_job(db, job_id)
    assert job.status is JobStatusV1.BLOCKED
    assert read_members(db, job_id)[0].blockers == ("COST_AUTHORIZATION_MISSING",)
    envelopes = db.execute(
        "SELECT COUNT(*) FROM llm_spend_authorization_revision "
        "WHERE pricing_version = 'development'").fetchone()
    assert envelopes == (0,), "nothing was minted on the job path"
