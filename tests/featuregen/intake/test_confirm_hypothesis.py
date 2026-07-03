from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    REQUESTER,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command, run_projection
from featuregen.documents.primary import (
    StagePrimaryProjection,
    current_primary,
    register_primary_selected,
)
from featuregen.events.store import load_stream
from featuregen.intake.commands import confirm_contract, open_gate1_task
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state


def _hypothesis_draft(request_id):
    d = definition_draft(request_id, intake_mode="hypothesis", risk_flags=["high_risk_use_case:credit_decisioning"])
    d["proposed_feature_name"] = "abrupt_category_shift"
    d["feature_semantics"]["target_definition"] = "higher credit risk (pinned label)"
    return d


def _ready(db, run_id):
    register_primary_selected(db)  # SP-2 bootstrap (seed_sp2) wires this durably in production
    draft_doc_id, cands = seed_validated_contract(
        db, run_id=run_id, request_id="req_" + run_id, draft_body=_hypothesis_draft("req_" + run_id), candidate_docs=3
    )
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "o"))
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        row = cur.fetchone()
    return row["task_id"], row["task_version"], cands


def _cmd(run_id, task_id, tv, **args):
    return Command(
        "confirm_contract", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv, **args}, REQUESTER, "cc",
    )


def test_hypothesis_requires_candidate_selection(db):
    task_id, tv, _ = _ready(db, "run_hyp0")
    res = confirm_contract(db, _cmd("run_hyp0", task_id, tv))  # no candidate_doc_id
    assert res.accepted is False
    assert "candidate" in res.denied_reason.lower()  # calculation_method_chosen not satisfied
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_hyp0")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED


def test_hypothesis_confirm_promotes_candidate_and_records_rejected(db):
    task_id, tv, cands = _ready(db, "run_hyp1")
    chosen = cands[1]
    res = confirm_contract(db, _cmd("run_hyp1", task_id, tv, candidate_doc_id=chosen))
    assert res.accepted is True, res.denied_reason
    # document PRIMARY_SELECTED promotion on the run — only the chosen doc is promoted (§7.1).
    # Drive the (async, checkpoint-driven) stage_primary projection synchronously to read it back.
    run_projection(db, StagePrimaryProjection())
    assert current_primary(db, "run_hyp1", "DRAFT_CONTRACT") == chosen
    stream = load_stream(db, "run", "run_hyp1")
    promos = [e for e in stream if e.type == "PRIMARY_SELECTED"]
    assert len(promos) == 1 and promos[0].payload == {"doc_id": chosen, "stage": "DRAFT_CONTRACT"}
    body = next(e for e in load_stream(db, "feature_contract", "run_hyp1") if e.type == "CONTRACT_CONFIRMED").payload["confirmed_body"]
    conf = body["confirmation"]
    assert conf["selected_candidate"] == chosen
    assert set(conf["rejected_candidates"]) == {cands[0], cands[2]}  # losers recorded HERE only (§8.3)
    assert body["requires_independent_validation"] is True  # risk-flagged use-case (§8.4 #1)
    assert body["intake_mode"] == "hypothesis"
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_hyp1")).status \
        is FeatureContractStatus.CONFIRMED


def test_hypothesis_confirm_binds_the_chosen_candidate_method_not_the_draft(db):
    """P1-a: the confirmed contract's calculation_method is the CHOSEN candidate's method — loaded from
    its durable body (F1 blob store) — NOT the original Draft's. The human's Gate #1 selection GOVERNS
    the output. (Before the fix, chosen_method stayed None and the Draft's method was used.)"""
    task_id, tv, cands = _ready(db, "run_hyp_bind")
    chosen = cands[1]  # seeded with chosen = rolling_aggregate/sum/60d — distinct from the draft's count/90d
    res = confirm_contract(db, _cmd("run_hyp_bind", task_id, tv, candidate_doc_id=chosen))
    assert res.accepted is True, res.denied_reason
    body = next(
        e for e in load_stream(db, "feature_contract", "run_hyp_bind") if e.type == "CONTRACT_CONFIRMED"
    ).payload["confirmed_body"]
    method = body["calculation_method"]["chosen"]
    # the CANDIDATE's method governs, not the draft's (draft would reshape to count/90d)
    assert method["aggregation"] == "sum" and method["window"] == "60d", method
    assert not (method["aggregation"] == "count" and method["window"] == "90d")
    # the confirmed contract derives from the chosen candidate doc (P1-a provenance)
    assert chosen in body["provenance"]["derived_from"]


def test_confirm_stale_cas_rolls_back_no_committed_side_effects(db, monkeypatch):
    """F3/P1-c: a stale CONTRACT_CONFIRMED CAS rolls the WHOLE Gate #1 write savepoint back — the task
    stays OPEN, no PRIMARY_SELECTED promotion, no CONFIRMED event — never a stranded run. (Before the fix
    the task-consume + promotion + frozen doc committed BEFORE the append and survived the stale deny.)"""
    import featuregen.intake.commands as cmds
    from featuregen.contracts.errors import ConcurrencyError

    task_id, tv, cands = _ready(db, "run_hyp_strand")
    chosen = cands[1]
    real_append = cmds.append_fc_event

    def _fail_on_confirmed(*a, **k):
        if k.get("type") == cmds.CONTRACT_CONFIRMED:
            raise ConcurrencyError("simulated concurrent transition")
        return real_append(*a, **k)

    monkeypatch.setattr(cmds, "append_fc_event", _fail_on_confirmed)
    res = confirm_contract(db, _cmd("run_hyp_strand", task_id, tv, candidate_doc_id=chosen))
    assert res.accepted is False and "stale" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"  # task-consume rolled back (not 'answered')
    run_projection(db, StagePrimaryProjection())
    assert current_primary(db, "run_hyp_strand", "DRAFT_CONTRACT") is None  # promotion rolled back
    stream = load_stream(db, "feature_contract", "run_hyp_strand")
    assert not any(e.type == "CONTRACT_CONFIRMED" for e in stream)  # no folded transition
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED


def _assert_denied_no_writes(db, run_id, res, reason_substr):
    """A candidate-guard denial: fail-closed, decided BEFORE the task OCC + promotion — so NO
    PRIMARY_SELECTED, NO CONTRACT_CONFIRMED, and the contract status is UNCHANGED (still MCV-validated)."""
    assert res.accepted is False
    assert reason_substr in res.denied_reason.lower()
    assert db.execute(
        "SELECT count(*) FROM events WHERE aggregate='run' AND aggregate_id=%s AND type='PRIMARY_SELECTED'",
        (run_id,),
    ).fetchone()[0] == 0
    stream = load_stream(db, "feature_contract", run_id)
    assert not any(e.type == "CONTRACT_CONFIRMED" for e in stream)
    assert fold_feature_contract_state(stream).status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED


def test_hypothesis_confirm_foreign_candidate_is_denied(db):
    """Integrity (§7.1): a REAL candidate doc frozen under ANOTHER run may NOT be promoted here. The
    guard is scoped by (doc_id, run_id, DRAFT_CONTRACT), so run B's candidate is `unknown` for run A →
    fail-closed AUDITED deny, no promotion, no CONFIRMED, status unchanged (mirrors select_candidate_doc's
    cross-run test)."""
    task_id, tv, _ = _ready(db, "run_hypF")
    _tb, _vb, foreign_cands = _ready(db, "run_hypF_other")  # real candidates — but under a DIFFERENT run
    res = confirm_contract(db, _cmd("run_hypF", task_id, tv, candidate_doc_id=foreign_cands[0]))
    _assert_denied_no_writes(db, "run_hypF", res, "unknown")
    # the spoofed-candidate confirm is recorded on the tamper-evident security-audit stream (§8.2)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE attempted_action='confirm_contract' AND decision='denied' AND aggregate_id=%s",
            ("run_hypF",),
        )
        assert cur.fetchone()["n"] == 1


def test_hypothesis_confirm_unknown_candidate_is_denied(db):
    """A nonexistent candidate_doc_id → fail-closed AUDITED deny (no promotion / no CONFIRMED)."""
    task_id, tv, _ = _ready(db, "run_hypU")
    res = confirm_contract(db, _cmd("run_hypU", task_id, tv, candidate_doc_id="doc_does_not_exist"))
    _assert_denied_no_writes(db, "run_hypU", res, "unknown")


def test_hypothesis_confirm_non_candidate_doc_is_denied(db):
    """A REAL doc of THIS run but branch_role!='candidate' (the primary Draft) → fail-closed AUDITED deny
    (no promotion / no CONFIRMED). Mirrors select_candidate_doc's non-candidate guard."""
    task_id, tv, _ = _ready(db, "run_hypN")
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT doc_id FROM documents "
            "WHERE run_id=%s AND stage='DRAFT_CONTRACT' AND branch_role='primary'",
            ("run_hypN",),
        )
        primary_doc = cur.fetchone()["doc_id"]
    res = confirm_contract(db, _cmd("run_hypN", task_id, tv, candidate_doc_id=primary_doc))
    _assert_denied_no_writes(db, "run_hypN", res, "not a candidate")
