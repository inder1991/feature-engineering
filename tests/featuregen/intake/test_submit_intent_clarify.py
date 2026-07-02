from tests.featuregen.intake.test_submit_intent_definition import _DEFINITION_OUTPUT

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.identity.build import build_human_identity
from featuregen.intake.banking_catalog import IntakeOutcome
from featuregen.intake.commands import submit_intent
from featuregen.intake.events import CLARIFICATION_REQUESTED, DRAFT_CONTRACT_PRODUCED

ALICE = build_human_identity(subject="user:alice", role_claims=("data_scientist",))


def _cmd(**args):
    base = {"intent_text": "customers who abruptly shift spending category are higher credit risk",
            "intake_mode": "definition", "raw_input_classification": "clean"}
    base.update(args)
    return Command("submit_intent", "feature_contract", None, base, ALICE, "k1")


def test_sensitive_proxy_produces_draft_and_a_clarification(db, intake_env):
    intake_env.pin(IntakeOutcome.SENSITIVE_PROXY_CLARIFY, reason="proxy for a protected attribute")
    intake_env.script_llm(_DEFINITION_OUTPUT)
    res = submit_intent(db, _cmd())
    assert res.accepted is True, res.denied_reason
    fc_types = [e.type for e in load_stream(db, "feature_contract", res.aggregate_id)]
    assert DRAFT_CONTRACT_PRODUCED in fc_types  # non-terminal — the Draft is still produced
    assert CLARIFICATION_REQUESTED in fc_types  # + compliance-review routing


def test_unscanned_fails_closed_no_llm_no_draft(db, intake_env):
    # DefaultIntentRedactor fails closed on `unscanned`; the LLM must never be reached.
    intake_env.pin(IntakeOutcome.CLEAR)
    intake_env.script_llm(_DEFINITION_OUTPUT, explode=True)  # .call raises if dispatched
    res = submit_intent(db, _cmd(raw_input_classification="unscanned"))
    assert res.accepted is True, res.denied_reason
    fc_types = [e.type for e in load_stream(db, "feature_contract", res.aggregate_id)]
    assert DRAFT_CONTRACT_PRODUCED not in fc_types  # no unsafe payload dispatched, no Draft frozen
    assert CLARIFICATION_REQUESTED in fc_types


def test_llm_failed_into_clarification_produces_no_draft(db, intake_env):
    intake_env.pin(IntakeOutcome.CLEAR)
    intake_env.script_llm(_DEFINITION_OUTPUT, status="failed_into_clarification")
    res = submit_intent(db, _cmd())
    assert res.accepted is True
    fc_types = [e.type for e in load_stream(db, "feature_contract", res.aggregate_id)]
    assert DRAFT_CONTRACT_PRODUCED not in fc_types
    assert CLARIFICATION_REQUESTED in fc_types
