from psycopg.rows import dict_row

from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.request_aggregate import create_request_command, create_run_command
from featuregen.contracts import Command
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.primary import register_primary_selected
from featuregen.documents.store import append_document
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
