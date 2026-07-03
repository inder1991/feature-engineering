from psycopg.rows import dict_row

import featuregen.intake.commands as cmds
from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.request_aggregate import create_request_command, create_run_command
from featuregen.contracts import Command, ConcurrencyError
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.contracts.envelopes import GateTaskSpec
from featuregen.documents.primary import register_primary_selected
from featuregen.documents.store import append_document
from featuregen.gates.tasks import open_task
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.idgen import mint_id
from featuregen.intake.commands import select_candidate_doc
from featuregen.intake.store import append_feature_contract_event

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
STRANGER = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
SERVICE = build_service_identity(
    subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
)


def _open_run(db, owner, concept):
    """A real requester-owned run: create_request + create_run, then open the `feature_contract`
    aggregate with an INTENT_SUBMITTED event acted by `owner` (R1 store seam). R4's fold sets
    `state.requester` from THAT event's `actor.subject`, so the state-based request-owner guard
    resolves `owner` as the run's requester."""
    req = create_request_command(
        db,
        Command("create_request", "request", None,
                {"feature_concept": concept, "intake_mode": "hypothesis"}, owner, mint_id("ik")),
    )
    run = create_run_command(
        db,
        Command("create_run", "request", None, {"request_id": req.aggregate_id}, owner, mint_id("ik")),
    )
    # R1/R4: open the feature_contract stream so the fold has a requester. The fold reads the EVENT
    # actor.subject for `state.requester` — the payload content does not set ownership.
    append_feature_contract_event(
        db,
        run_id=run.aggregate_id,
        type="INTENT_SUBMITTED",
        payload={
            "intake_mode": "hypothesis",
            "raw_input_ref": mint_id("blob"),
            "raw_input_classification": "clean",
            "classification": {"outcome": "IN_SCOPE", "catalog_version": "v0", "matched_class": None},
        },
        actor=owner,
        request_id=req.aggregate_id,
    )
    # N5: candidate selection is a Gate #1 action — the run must be MCV-validated (gate-ready) with an
    # OPEN Gate #1 task. Seed both so select_candidate_doc's lifecycle guards are exercised, not bypassed.
    append_feature_contract_event(
        db, run_id=run.aggregate_id, type="MINIMUM_CONTRACT_VALIDATED",
        payload={"run_id": run.aggregate_id}, actor=SERVICE, request_id=req.aggregate_id,
    )
    open_task(
        db,
        GateTaskSpec(
            gate="CLARIFICATION", required_inputs=(),
            eligible_assignees={"role": "data_scientist", "subject": owner.subject},
            allowed_responses=("confirm", "edit", "reject"),
            run_id=run.aggregate_id, delegation_allowed=False,
        ),
        owner,
    )
    return req.aggregate_id, run.aggregate_id


def _candidate_doc(db, run_id, request_id, *, branch_role="candidate"):
    doc_id = mint_id("doc")
    append_document(
        db,
        NewDocument(
            doc_id=doc_id,
            stage=Stage.DRAFT_CONTRACT.value,
            schema_version=1,
            branch_role=branch_role,
            content_hash="sha256:c",
            body_classification="governance-retained",
            provenance=provenance_for(artifact_type="DRAFT_CONTRACT"),
            body_ref=mint_id("blob"),
        ),
        run_id=run_id,
        request_id=request_id,
        actor=OWNER,
    )
    return doc_id


def _cmd(run_id, doc_id, actor):
    return Command(
        "select_candidate_doc", "run", None,
        {"run_id": run_id, "candidate_doc_id": doc_id, "stage": "DRAFT_CONTRACT"}, actor, mint_id("ik")
    )


def test_select_denied_before_mcv_no_open_gate(db):
    """N5: a candidate cannot be promoted before the run reaches Gate #1. A pre-MCV (NEEDS_CLARIFICATION)
    run with NO open gate is DENIED — not silently promoted at the wrong lifecycle point."""
    register_primary_selected(db)
    req = create_request_command(db, Command("create_request", "request", None,
        {"feature_concept": "x", "intake_mode": "hypothesis"}, OWNER, mint_id("ik")))
    run = create_run_command(db, Command("create_run", "request", None,
        {"request_id": req.aggregate_id}, OWNER, mint_id("ik")))
    append_feature_contract_event(db, run_id=run.aggregate_id, type="INTENT_SUBMITTED",
        payload={"intake_mode": "hypothesis", "raw_input_ref": mint_id("blob"),
                 "raw_input_classification": "clean",
                 "classification": {"outcome": "IN_SCOPE", "catalog_version": "v0", "matched_class": None}},
        actor=OWNER, request_id=req.aggregate_id)  # NO MCV, NO gate task → not at Gate #1
    doc = _candidate_doc(db, run.aggregate_id, req.aggregate_id)
    res = select_candidate_doc(db, _cmd(run.aggregate_id, doc, OWNER))
    assert res.accepted is False
    assert "Gate #1" in res.denied_reason
    # nothing promoted (the run is untouched on the RUN aggregate)
    from featuregen.events.store import load_stream
    assert not any(e.type == "PRIMARY_SELECTED" for e in load_stream(db, "run", run.aggregate_id))


def test_owner_promotes_only_the_chosen_candidate(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift A")
    chosen = _candidate_doc(db, run_id, request_id)
    loser = _candidate_doc(db, run_id, request_id)

    res = select_candidate_doc(db, _cmd(run_id, chosen, OWNER))
    assert res.accepted is True, res.denied_reason

    # exactly one PRIMARY_SELECTED, for the CHOSEN doc, on the run aggregate
    rows = db.execute(
        "SELECT payload->>'doc_id' FROM events "
        "WHERE aggregate='run' AND aggregate_id=%s AND type='PRIMARY_SELECTED'",
        (run_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [chosen]
    # both candidate docs remain (write-once); the loser is UNTOUCHED — no per-doc reject event
    n = db.execute("SELECT count(*) FROM documents WHERE doc_id = ANY(%s)", ([chosen, loser],)).fetchone()[0]
    assert n == 2
    loser_promotions = db.execute(
        "SELECT count(*) FROM events WHERE type='PRIMARY_SELECTED' AND payload->>'doc_id'=%s", (loser,)
    ).fetchone()[0]
    assert loser_promotions == 0


def test_non_owner_is_denied_and_security_audited(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift B")
    chosen = _candidate_doc(db, run_id, request_id)

    res = select_candidate_doc(db, _cmd(run_id, chosen, STRANGER))
    assert res.accepted is False
    assert "owner" in res.denied_reason
    # nothing promoted
    assert db.execute(
        "SELECT count(*) FROM events WHERE aggregate_id=%s AND type='PRIMARY_SELECTED'", (run_id,)
    ).fetchone()[0] == 0
    # the denial is recorded on the tamper-evident security-audit stream (§8.2)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE attempted_action='select_candidate_doc' AND decision='denied'"
        )
        assert cur.fetchone()["n"] == 1


def test_service_principal_cannot_select(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift C")
    chosen = _candidate_doc(db, run_id, request_id)
    res = select_candidate_doc(db, _cmd(run_id, chosen, SERVICE))
    assert res.accepted is False
    assert "human" in res.denied_reason
    # nothing promoted
    assert db.execute(
        "SELECT count(*) FROM events WHERE aggregate_id=%s AND type='PRIMARY_SELECTED'", (run_id,)
    ).fetchone()[0] == 0
    # R15: a non-human attempting the human-only Gate #1 is security-audited (the escalation signal),
    # traceable to the run (aggregate_id=run_id) — not a plain unaudited denial.
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE attempted_action='select_candidate_doc' AND decision='denied' AND aggregate_id=%s",
            (run_id,),
        )
        assert cur.fetchone()["n"] == 1


def test_non_candidate_doc_is_rejected_fail_closed(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift D")
    primary = _candidate_doc(db, run_id, request_id, branch_role="primary")  # the Draft, not a candidate
    res = select_candidate_doc(db, _cmd(run_id, primary, OWNER))
    assert res.accepted is False
    assert "candidate" in res.denied_reason


def test_unknown_doc_for_run_is_rejected(db):
    register_primary_selected(db)
    _request_id, run_id = _open_run(db, OWNER, "abrupt category shift E")
    res = select_candidate_doc(db, _cmd(run_id, "doc_does_not_exist", OWNER))
    assert res.accepted is False
    assert "unknown" in res.denied_reason


def test_concurrent_run_append_denies_stale(db, monkeypatch):
    """The RUN-aggregate PRIMARY_SELECTED append is CAS-guarded on the run stream's OCC head; a
    concurrent run-aggregate write raises ConcurrencyError. execute_command does NOT catch it, so the
    handler must — fail closed as `stale` (mirrors submit_intent / refine_contract / answer_clarification)
    rather than raising uncaught + leaking the idempotency claim."""
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift F")
    chosen = _candidate_doc(db, run_id, request_id)

    def _boom(*a, **k):
        raise ConcurrencyError("run head advanced")

    monkeypatch.setattr(cmds, "append_event", _boom)
    res = select_candidate_doc(db, _cmd(run_id, chosen, OWNER))
    assert res.accepted is False
    assert res.denied_reason == "stale"
    # nothing promoted — the raced append committed nothing
    assert db.execute(
        "SELECT count(*) FROM events WHERE aggregate_id=%s AND type='PRIMARY_SELECTED'", (run_id,)
    ).fetchone()[0] == 0


def test_cross_run_candidate_doc_is_rejected(db):
    """Cross-run ISOLATION: a real candidate doc frozen under run B may NOT be promoted under run A.
    The doc lookup is scoped by (doc_id, run_id, stage), so B's doc_id is `unknown` for run A → denied,
    no promotion. (Distinct from the nonexistent-id case — here the doc genuinely exists, just not on A.)"""
    register_primary_selected(db)
    req_a, run_a = _open_run(db, OWNER, "abrupt category shift G-A")
    req_b, run_b = _open_run(db, OWNER, "abrupt category shift G-B")
    foreign = _candidate_doc(db, run_b, req_b)  # a real candidate — but under run B

    res = select_candidate_doc(db, _cmd(run_a, foreign, OWNER))
    assert res.accepted is False
    assert "unknown" in res.denied_reason
    # neither run promoted anything (no cross-run leak)
    assert db.execute(
        "SELECT count(*) FROM events WHERE type='PRIMARY_SELECTED' AND aggregate_id = ANY(%s)",
        ([run_a, run_b],),
    ).fetchone()[0] == 0
