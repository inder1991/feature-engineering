from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.events.store import load_stream
from featuregen.identity.build import build_human_identity
from featuregen.intake.banking_catalog import IntakeOutcome
from featuregen.intake.commands import submit_intent
from featuregen.intake.events import (
    CLARIFICATION_REQUESTED,
    DRAFT_CONTRACT_PRODUCED,
    USE_CASE_ONBOARDING_REQUESTED,
)

ALICE = build_human_identity(subject="user:alice", role_claims=("data_scientist",))


def _cmd():
    return Command("submit_intent", "feature_contract", None,
                   {"intent_text": "a brand-new banking use case", "intake_mode": "definition",
                    "raw_input_classification": "clean"}, ALICE, "k1")


def _run_events(db, run_id, typ):
    return [e for e in load_stream(db, "run", run_id) if e.type == typ]


def test_unknown_use_case_parks_and_opens_onboarding_gate(db, intake_env):
    intake_env.pin(IntakeOutcome.NEEDS_USE_CASE_ONBOARDING)
    res = submit_intent(db, _cmd())
    assert res.accepted is True, res.denied_reason
    run_id = res.aggregate_id
    fc_types = [e.type for e in load_stream(db, "feature_contract", run_id)]
    assert USE_CASE_ONBOARDING_REQUESTED in fc_types
    assert DRAFT_CONTRACT_PRODUCED not in fc_types
    assert len(_run_events(db, run_id, "RUN_PARKED")) == 1
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT gate, status FROM human_tasks WHERE run_id=%s", (run_id,))
        row = cur.fetchone()
    assert row["gate"] == "USE_CASE_ONBOARDING"
    assert row["status"] == "open"


def test_catalog_unavailable_fails_closed_into_a_park(db, intake_env):
    intake_env.drop_catalog()  # unavailable / unversioned
    res = submit_intent(db, _cmd())
    assert res.accepted is True, res.denied_reason
    run_id = res.aggregate_id
    fc_types = [e.type for e in load_stream(db, "feature_contract", run_id)]
    assert CLARIFICATION_REQUESTED in fc_types
    assert DRAFT_CONTRACT_PRODUCED not in fc_types  # never auto-passes an absent classification
    assert len(_run_events(db, run_id, "RUN_PARKED")) == 1
