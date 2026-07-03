from psycopg.rows import dict_row
from tests.featuregen.intake.conftest import (
    INTAKE_SVC,
    REQUESTER,
    _Cls,
    _StubCatalog,
    definition_draft,
    seed_validated_contract,
)

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.intake.banking_catalog import IntakeOutcome
from featuregen.intake.commands import confirm_contract, open_gate1_task
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state


def _ready(db, run_id, draft):
    seed_validated_contract(db, run_id=run_id, request_id="req_" + run_id, draft_body=draft)
    open_gate1_task(db, Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, INTAKE_SVC, "o"))
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        row = cur.fetchone()
    return row["task_id"], row["task_version"]


def _cmd(run_id, task_id, tv):
    return Command(
        "confirm_contract", "feature_contract", run_id,
        {"run_id": run_id, "task_id": task_id, "expected_task_version": tv}, REQUESTER, "cc",
    )


def test_prohibited_data_class_blocks_confirmation(db, monkeypatch):
    import featuregen.intake.commands as C
    monkeypatch.setattr(
        C, "classify_intent",
        lambda text, *, product=None, region=None, catalog: _Cls(
            IntakeOutcome.PROHIBITED_DATA_CLASS, catalog.version, matched_class="protected_attribute:race"
        ),
    )
    task_id, tv = _ready(db, "run_pdc", definition_draft("req_pdc"))
    res = confirm_contract(db, _cmd("run_pdc", task_id, tv))
    assert res.accepted is False
    assert "prohibited data class" in res.denied_reason
    assert "protected_attribute:race" in res.denied_reason  # matched class recorded (§8.4)
    assert "bdc-2026.06" in res.denied_reason  # catalog version recorded
    # cannot be CONFIRMED while the finding stands; the gate stays open for edit/withdraw
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_pdc")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"


def test_confirm_rescreens_the_raw_intent_not_only_the_lossy_draft(db, monkeypatch):
    """F6/P2-b: the confirm-time §8.4 backstop re-screens the ORIGINAL raw intent (resolved from the F1
    write-once blob store), not only the lossy structured Draft. A prohibited phrase present in the raw
    intent but dropped/softened during structuring — so ABSENT from the Draft screen text — still blocks."""
    import featuregen.intake.commands as C
    from featuregen.intake.blobs import write_blob

    def _text_sensitive(text, *, product=None, region=None, catalog):
        # the Draft screen text (feature name / target / filter concepts) has no "race" → CLEAR; only the
        # RAW intent does → PROHIBITED. So a block here can ONLY come from the raw re-screen.
        if "race" in text.lower():
            return _Cls(IntakeOutcome.PROHIBITED_DATA_CLASS, catalog.version,
                        matched_class="protected_attribute:race")
        return _Cls(IntakeOutcome.CLEAR, catalog.version)

    monkeypatch.setattr(C, "classify_intent", _text_sensitive)
    draft = definition_draft("req_raw")  # raw_input_ref = "blob_raw_def"; the Draft screen text is CLEAR
    task_id, tv = _ready(db, "run_raw", draft)
    write_blob(db, draft["raw_input_ref"], {"raw_input": "predict credit risk using the customer's race"})
    res = confirm_contract(db, _cmd("run_raw", task_id, tv))
    assert res.accepted is False
    assert "prohibited data class" in res.denied_reason
    assert "protected_attribute:race" in res.denied_reason
    # the gate stays open (not consumed) — the run can edit/withdraw
    assert fold_feature_contract_state(load_stream(db, "feature_contract", "run_raw")).status \
        is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED


def test_sensitive_proxy_routes_to_clarification_not_block(db, monkeypatch):
    import featuregen.intake.commands as C
    monkeypatch.setattr(
        C, "classify_intent",
        lambda text, *, product=None, region=None, catalog: _Cls(IntakeOutcome.SENSITIVE_PROXY_CLARIFY, catalog.version),
    )
    task_id, tv = _ready(db, "run_prox", definition_draft("req_prox"))
    res = confirm_contract(db, _cmd("run_prox", task_id, tv))
    assert res.accepted is False
    assert "clarification" in res.denied_reason.lower() or "review" in res.denied_reason.lower()


def test_version_drift_flip_reclarifies(db, monkeypatch):
    """The intake-time classification was CLEAR under bdc-2026.06; the CURRENT catalog is a newer
    version that now flips to prohibited — the confirmation must not ride the stale result (§8.4(d))."""
    import featuregen.intake.commands as C
    monkeypatch.setattr(C, "current_intake_catalog", lambda: _StubCatalog("bdc-2026.09"))
    monkeypatch.setattr(
        C, "classify_intent",
        lambda text, *, product=None, region=None, catalog: _Cls(
            IntakeOutcome.PROHIBITED_DATA_CLASS, catalog.version, matched_class="blocked:new_rule"
        ),
    )
    task_id, tv = _ready(db, "run_drift", definition_draft("req_drift"))
    res = confirm_contract(db, _cmd("run_drift", task_id, tv))
    assert res.accepted is False
    assert "bdc-2026.09" in res.denied_reason  # re-checked against the CURRENT catalog


def test_unavailable_catalog_fails_closed(db, monkeypatch):
    import featuregen.intake.commands as C
    monkeypatch.setattr(C, "current_intake_catalog", lambda: None)
    task_id, tv = _ready(db, "run_nocat", definition_draft("req_nocat"))
    res = confirm_contract(db, _cmd("run_nocat", task_id, tv))
    assert res.accepted is False
    assert "catalog" in res.denied_reason.lower()
