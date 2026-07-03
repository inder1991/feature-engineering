import pytest

from featuregen.intake.critique import (
    CritiqueResult,
    apply_critique,
    contract_review,
)
from featuregen.intake.redaction import EgressViolation
from featuregen.intake.store import (
    append_feature_contract_event as append_fc_event,
)
from featuregen.intake.store import (
    load_feature_contract,
)


class ScriptedLLM:
    """A raw LLMClient double (spec §9.1). call_llm wraps it: it egress-guards, records the llm_call,
    stamps `call_ref`, emits LLM_CALL_RECORDED, and returns the LLMResult. Mirrors how SP-1 Phase-4
    tests use their own CatalogAdapter double rather than importing the Phase-3 fixture."""

    def __init__(self, output, *, self_reported_scores=None, status="ok"):
        self._output = output
        self._scores = self_reported_scores or {}
        self._status = status

    def call(self, request):
        from featuregen.intake.llm import LLMResult as _R

        return _R(output=self._output, self_reported_scores=self._scores, call_ref="", status=self._status)


def _seed_contract(db, agent, run_id="run_crit"):
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_crit", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                 "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
        actor=agent, expected_version=0,
    )
    return run_id


def test_contract_review_records_findings_and_emits_domain_shadow(db, sp2_schemas, agent):
    run_id = _seed_contract(db, agent)
    client = ScriptedLLM({
        "review_type": "CONTRACT_REVIEW", "status": "NEEDS_REVIEW",
        "findings": [{
            "severity": "HIGH", "category": "AMBIGUOUS_DEFINITION", "field": "filters",
            "evidence": "'declined' could mean issuer-declined, expired, or fraud-blocked.",
            "recommendation": "Ask the requester to confirm the declined-status encoding.",
            "blocks_progress": True,
        }],
    })
    result = contract_review(
        db, client, {"entity": "customer", "filters": [{"concept": "declined card authorization"}]},
        run_id=run_id, actor=agent,
    )
    assert isinstance(result, CritiqueResult)
    assert result.status == "NEEDS_REVIEW"
    assert result.findings[0].field == "filters"
    assert result.findings[0].blocks_progress is True
    assert result.call_ref  # call_llm stamped the llm_call reference
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_CRITIQUED" in types
    assert "LLM_CALL_RECORDED" in types  # call_llm event-sourced the call


def test_apply_critique_ors_blocking_findings_to_human():
    routing = {"filters": "auto", "windows": "auto"}
    crit = CritiqueResult(
        review_type="CONTRACT_REVIEW", status="NEEDS_REVIEW", call_ref="llmc_1",
        findings=(
            __import__("featuregen.intake.critique", fromlist=["CritiqueFinding"]).CritiqueFinding(
                severity="HIGH", category="AMBIGUOUS_DEFINITION", evidence="e",
                recommendation="r", blocks_progress=True, field="filters",
            ),
        ),
    )
    out = apply_critique(routing, crit)
    assert out["filters"] == "human"  # forced to must-ask
    assert out["windows"] == "auto"   # untouched


def test_apply_critique_never_lowers_a_doubt():
    routing = {"filters": "human"}
    crit = CritiqueResult("CONTRACT_REVIEW", "OK", (), "llmc_2")  # no findings
    assert apply_critique(routing, crit)["filters"] == "human"  # challenger can only raise doubts


# --- added coverage the Task 5.3 brief mandates beyond the three sketched cases -----------------


def test_contract_review_clean_draft_yields_no_spurious_findings(db, sp2_schemas, agent):
    """A clean draft → the challenger reports OK with NO findings; it must not invent doubts, and
    apply_critique leaves the router untouched (nothing forced to human)."""
    run_id = _seed_contract(db, agent)
    client = ScriptedLLM({"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []})
    result = contract_review(
        db, client, {"entity": "customer", "windows": [{"name": "lookback", "value": "90d"}]},
        run_id=run_id, actor=agent,
    )
    assert result.status == "OK"
    assert result.findings == ()
    assert apply_critique({"entity": "auto", "windows": "auto"}, result) == {
        "entity": "auto", "windows": "auto",
    }
    assert "CONTRACT_CRITIQUED" in [e.type for e in load_feature_contract(db, run_id)]


def test_contract_review_llm_failure_fails_closed(db, sp2_schemas, agent):
    """A critique-LLM failure (provider refusal → §9.2 fail-closed) must NOT become a fake-clean pass:
    no fabricated findings, a non-OK fail-closed status, and the failure stays fully auditable."""
    run_id = _seed_contract(db, agent)
    client = ScriptedLLM({}, status="refusal")
    result = contract_review(db, client, {"entity": "customer"}, run_id=run_id, actor=agent)
    assert result.status != "OK"                       # never a fake-clean pass
    assert result.status == "failed_into_clarification"
    assert result.findings == ()                       # no fabricated findings
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "LLM_CALL_RECORDED" in types                # the failure is recorded, not swallowed
    assert "CONTRACT_CRITIQUED" in types
    # a failed critique never silently unblocks the router
    assert apply_critique({"filters": "human"}, result)["filters"] == "human"


class _ExplodingLLM:
    """A raw LLMClient double whose .call MUST NEVER fire — proves the egress backstop fails closed
    BEFORE any model dispatch (call_llm would invoke .call on this)."""

    def call(self, request):  # pragma: no cover - firing it is the test failure
        raise AssertionError("LLM must not be called: draft_semantics carried residual PII")


def test_contract_review_fails_closed_on_residual_pii_in_draft_semantics(db, sp2_schemas, agent):
    """The PRIMARY model-facing payload (draft_semantics) must clear the no-PII egress backstop
    (§9.4): a Draft whose semantic content carries residual PII fails CLOSED (EgressViolation) and
    NEVER reaches the LLM — no LLM_CALL_RECORDED is written to the stream."""
    run_id = _seed_contract(db, agent)
    draft_with_pii = {
        "entity": "customer",
        "filters": [{"concept": "declined card", "note": "email requester alice@example.com"}],
    }
    with pytest.raises(EgressViolation):
        contract_review(db, _ExplodingLLM(), draft_with_pii, run_id=run_id, actor=agent)
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "LLM_CALL_RECORDED" not in types   # the draft never reached (or was recorded against) the LLM
    assert "CONTRACT_CRITIQUED" not in types  # no critique shadow on a fail-closed egress breach
