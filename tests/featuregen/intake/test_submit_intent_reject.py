# X5 — Phase 4 owns only the INTAKE-TIME terminal append (submit_intent appends INTENT_REJECTED itself);
# the standalone `reject_intent` command is P8's and is tested in Phase 8, not here.
from tests.featuregen._helpers import mint_test_identity

from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.intake.banking_catalog import IntakeOutcome
from featuregen.intake.commands import submit_intent
from featuregen.intake.events import DRAFT_CONTRACT_PRODUCED, INTENT_REJECTED

ALICE = mint_test_identity(subject="user:alice", role_claims=("data_scientist",))


def _cmd(intent="predict tomorrow's weather", mode="definition"):
    return Command("submit_intent", "feature_contract", None,
                   {"intent_text": intent, "intake_mode": mode, "raw_input_classification": "clean"}, ALICE, "k1")


def test_out_of_scope_intent_is_rejected_no_draft(db, intake_env):
    intake_env.pin(IntakeOutcome.OUT_OF_SCOPE, reason="no banking entity or concept")
    res = submit_intent(db, _cmd())
    assert res.accepted is True, res.denied_reason
    run_id = res.aggregate_id
    fc_types = [e.type for e in load_stream(db, "feature_contract", run_id)]
    assert INTENT_REJECTED in fc_types
    assert DRAFT_CONTRACT_PRODUCED not in fc_types  # never normalized (fail-closed)
    rej = next(e for e in load_stream(db, "feature_contract", run_id) if e.type == INTENT_REJECTED)
    assert rej.payload["classification"] == "OUT_OF_SCOPE"
    assert rej.payload["catalog_version"] == "bdc-2026.1"
    assert run_is_terminal(db, run_id) is True  # SP-0 RUN_REJECTED emitted


def test_prohibited_data_class_records_matched_class(db, intake_env):
    intake_env.pin(IntakeOutcome.PROHIBITED_DATA_CLASS, matched_class="protected_attribute:race")
    res = submit_intent(db, _cmd(intent="use the applicant's race to score default risk"))
    assert res.accepted is True
    rej = next(e for e in load_stream(db, "feature_contract", res.aggregate_id) if e.type == INTENT_REJECTED)
    assert rej.payload["classification"] == "PROHIBITED_DATA_CLASS"
    assert rej.payload["matched_class"] == "protected_attribute:race"
