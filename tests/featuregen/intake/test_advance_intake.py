"""SP-2 Task 9.2a — `advance_intake`, the THIN production driver connecting a Draft to
(clarification tasks | Minimum Contract Validation) → Gate #1. The self-driving pipeline-initiation
step a durable runtime dispatches after `submit_intent` / `answer_clarification`.

The self-driving CLEAR + ambiguous paths run over the REAL PolicyAuthorizer via `execute_command`
(proving the production boundary — registration + the `intake-agent` service authz row). The
already-validated / terminal / mcv_failed branches are driven directly against seeded contracts.

advance_intake duplicates NO routing / MCV logic: `refine_contract` stays the routing engine and
`_open_gate1_task` the gate opener — this suite proves the driver only *sequences* them."""
import copy

from psycopg.rows import dict_row
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.intake.banking_catalog import IntakeClassification, IntakeOutcome
from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz
from featuregen.intake.catalog import load_banking_catalog_from_seed, register_intake_catalog
from featuregen.intake.commands import (
    advance_intake,
    register_intake_classifier,
    register_intake_deps,
)
from featuregen.intake.llm import FakeLLM, FakeResponse, register_llm_client
from featuregen.intake.read_model import get_contract
from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor
from featuregen.intake.store import append_feature_contract_event as append_fc_event

RAJ = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))
AGENT = mint_test_service_identity(
    subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
)

_CATALOG_VERSION = "bdc-2026.06"
_BANKING_SEED = {
    "catalog_version": _CATALOG_VERSION,
    "entities": ["customer", "account", "card", "transaction"],
    "data_classes": ["transactions", "balances", "card_authorizations", "protected_attribute"],
    "use_cases": [
        {
            "use_case": "card_authorization",
            "status": "active",
            "target": {"name": "declined_auth"},
            "blocked_data_classes": ["protected_attribute"],
        }
    ],
}
_OBS = {
    "kind": "point_in_time",
    "as_of_field": "as_of_date",
    "rule": "use only data available strictly before as_of_date",
}
_OK_REVIEW = {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}

# A FULLY RESOLVED Draft (NO open_fields) — the Doubt Router asks nothing, MCV passes → validated.
_RESOLVED_STRUCTURE = {
    "request_id": "ECHO", "intake_mode": "definition", "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean", "assumption_ledger_ref": "doc_echo",
    "status": "NEEDS_CLARIFICATION", "provenance": {"schema_version": 1},
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": _OBS, "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization",
                     "predicate": "card_authorizations.auth_result = 'D'"}],
        "target_definition": "N/A (definition-mode feature, no target)",
    },
    "field_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.05, "confidence": 0.95, "source": "llm"},
    },
    "open_fields": [], "open_questions": [],
    "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "source": "default",
         "rationale": "point-in-time features are grained by entity x as_of_date by convention",
         "ambiguity": 0.30, "confidence": 0.72},
    ],
}

# An AMBIGUOUS Draft: one high-ambiguity open field → the Doubt Router opens a must-ask task.
_AMBIGUOUS_STRUCTURE = {
    "request_id": "ECHO", "intake_mode": "definition", "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean", "assumption_ledger_ref": "doc_echo",
    "status": "NEEDS_CLARIFICATION", "provenance": {"schema_version": 1},
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": _OBS, "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
        "target_definition": "N/A (definition-mode feature, no target)",
    },
    "field_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
    },
    "open_fields": ["filters.declined_status_encoding"],
    "open_questions": [{"field": "filters.declined_status_encoding",
                        "question": "Which column/value marks a declined authorization?",
                        "ambiguity": 0.80, "confidence": 0.40, "blocks_progress": True,
                        "routed_to": "human"}],
    "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "source": "default",
         "rationale": "point-in-time features are grained by entity x as_of_date by convention",
         "ambiguity": 0.30, "confidence": 0.72},
    ],
}


class _ScoringView:
    """The R10 merged-view scoring seam (candidate_count + metadata). One binding per concept keeps the
    deterministic cardinality doubt low — the LLM self-report drives routing."""

    def candidate_count(self, concept):
        return 1

    def metadata(self):
        return {}


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def _clear_cls(intent, *, product=None, region=None, catalog=None):
    return IntakeClassification(IntakeOutcome.CLEAR, _CATALOG_VERSION, "advance_intake: pinned CLEAR")


def _wire(db, *, fixtures):
    """Assemble the full SP-2 stack under the REAL PolicyAuthorizer + a deterministic FakeLLM, and
    register the Refinement-Loop deps `advance_intake` drives `refine_contract` with."""
    register_sp2(_Registry())
    seed_authz_policy(db)
    seed_sp2_authz(db)  # SP-2 authz rows (incl. advance_intake) + contract/critique output-schemas
    register_command_authorizer(PolicyAuthorizer())
    register_intake_catalog(load_banking_catalog_from_seed(_BANKING_SEED))
    register_intent_redactor(DefaultIntentRedactor())
    register_intake_classifier(_clear_cls)
    llm = FakeLLM(script=fixtures)
    register_llm_client(llm)
    register_intake_deps(client=llm, redactor=DefaultIntentRedactor(), catalog=_ScoringView())
    return llm


def _submit(db, ik, *, intent_text="90-day rolling count of declined card authorizations per customer"):
    res = execute_command(db, Command(
        "submit_intent", "feature_contract", None,
        {"intent_text": intent_text, "intake_mode": "definition"}, RAJ, ik,
    ))
    assert res.accepted, res.denied_reason
    return res.aggregate_id


def _advance(db, run_id, ik, *, actor=AGENT):
    return execute_command(db, Command(
        "advance_intake", "feature_contract", run_id, {"run_id": run_id}, actor, ik,
    ))


def _open_tasks(db, run_id):
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT task_id, task_version, allowed_responses FROM human_tasks "
            "WHERE run_id=%s AND status='open' ORDER BY task_id",
            (run_id,),
        )
        return cur.fetchall()


def _adv_cmd(run_id, ik="adv"):
    return Command("advance_intake", "feature_contract", run_id, {"run_id": run_id}, AGENT, ik)


# ── the self-driving CLEAR path (submit → advance → refine clean → MCV → Gate #1), via dispatch ──────
def test_clear_intent_self_drives_to_gate1_via_dispatch(db):
    _wire(db, fixtures={"structure_intent": FakeResponse(output=_RESOLVED_STRUCTURE),
                        "contract_review": FakeResponse(output=_OK_REVIEW)})
    run_id = _submit(db, "adv-clear-submit")
    assert get_contract(db, run_id).status == "NEEDS_CLARIFICATION"  # a Draft, not yet driven
    assert _open_tasks(db, run_id) == []  # CLEAR → no banking clarification opened at submit

    advanced = _advance(db, run_id, "adv-clear-advance")
    assert advanced.accepted, advanced.denied_reason
    # refine routed clean → converged to MCV; advance opened Gate #1.
    assert get_contract(db, run_id).status == "MINIMUM_CONTRACT_VALIDATED"
    tasks = _open_tasks(db, run_id)
    assert len(tasks) == 1  # exactly the Gate #1 confirm task
    assert set(tasks[0]["allowed_responses"]) == {"confirm", "edit", "reject"}


# ── the ambiguous path: advance OPENS a clarification task; advance again (task open) → no-op ────────
def test_ambiguous_intent_advance_opens_clarification_then_noops(db):
    _wire(db, fixtures={"structure_intent": FakeResponse(output=_AMBIGUOUS_STRUCTURE),
                        "contract_review": FakeResponse(output=_OK_REVIEW)})
    run_id = _submit(db, "adv-amb-submit")
    assert _open_tasks(db, run_id) == []  # nothing open yet — refine has not run

    a1 = _advance(db, run_id, "adv-amb-1")
    assert a1.accepted, a1.denied_reason
    tasks = _open_tasks(db, run_id)
    assert len(tasks) == 1  # refine opened the must-ask clarification task
    assert get_contract(db, run_id).status == "NEEDS_CLARIFICATION"
    first_task, first_ver = tasks[0]["task_id"], tasks[0]["task_version"]

    # advance AGAIN while the human has not answered → no-op (the SAME task, no churn, no new round)
    a2 = _advance(db, run_id, "adv-amb-2")
    assert a2.accepted, a2.denied_reason
    tasks2 = _open_tasks(db, run_id)
    assert len(tasks2) == 1
    assert (tasks2[0]["task_id"], tasks2[0]["task_version"]) == (first_task, first_ver)


# ── advance on an MINIMUM_CONTRACT_VALIDATED contract → opens Gate #1; again → idempotent ────────────
def test_advance_on_validated_opens_gate1_and_is_idempotent(db, sp2_schemas):
    from tests.featuregen.intake.conftest import definition_draft, seed_validated_contract

    seed_validated_contract(db, run_id="run_v", request_id="req_v", draft_body=definition_draft("req_v"))
    r1 = advance_intake(db, _adv_cmd("run_v", "adv-v-1"))
    assert r1.accepted, r1.denied_reason
    tasks1 = _open_tasks(db, "run_v")
    assert len(tasks1) == 1  # the Gate #1 confirm task
    gate_task, gate_ver = tasks1[0]["task_id"], tasks1[0]["task_version"]

    # re-drive: MUST NOT cancel + recreate the live gate (no churned pair, SAME task + version).
    r2 = advance_intake(db, _adv_cmd("run_v", "adv-v-2"))
    assert r2.accepted, r2.denied_reason
    tasks2 = _open_tasks(db, "run_v")
    assert len(tasks2) == 1
    assert (tasks2[0]["task_id"], tasks2[0]["task_version"]) == (gate_task, gate_ver)


# ── advance on a CONFIRMED / terminal contract → no-op (nothing to drive) ────────────────────────────
def test_advance_on_confirmed_is_a_noop(db, sp2_schemas):
    from tests.featuregen.intake.conftest import definition_draft, seed_validated_contract

    seed_validated_contract(db, run_id="run_c", request_id="req_c", draft_body=definition_draft("req_c"))
    append_fc_event(db, run_id="run_c", type="CONTRACT_CONFIRMED",
                    payload={"confirmed_doc_id": "doc_confirmed", "confirmed_by": "user:raj",
                             "requires_independent_validation": False}, actor=AGENT)
    assert get_contract(db, "run_c").status == "CONFIRMED"

    res = advance_intake(db, _adv_cmd("run_c", "adv-c"))
    assert res.accepted, res.denied_reason  # accepted no-op
    assert _open_tasks(db, "run_c") == []  # opened NO Gate #1 task on a terminal contract
    assert get_contract(db, "run_c").status == "CONFIRMED"  # unchanged


# ── mcv_failed route → a manual review task is opened + the run parked (never left stuck) ────────────
def _mcv_failing_draft():
    from tests.featuregen.intake.conftest import definition_draft
    draft = copy.deepcopy(definition_draft("req_f"))
    draft["feature_semantics"]["observation_intent"] = {"kind": "UNKNOWN"}  # MCV #4 fails
    draft["open_fields"] = []  # no open field → the Loop cannot re-ask; advance must not strand it
    return draft


def test_mcv_failed_opens_manual_review_and_parks(db):
    _wire(db, fixtures={"contract_review": FakeResponse(output=_OK_REVIEW)})
    from tests.featuregen.intake.conftest import seed_needs_clarification

    seed_needs_clarification(db, run_id="run_f", request_id="req_f", draft_body=_mcv_failing_draft())

    res = _advance(db, "run_f", "adv-f")
    assert res.accepted, res.denied_reason  # not a stranding denial
    tasks = _open_tasks(db, "run_f")
    assert len(tasks) == 1  # a manual review CLARIFICATION task was opened
    # N4: the mcv_failed park threads state.request_id, folded from the ENVELOPE (the seed keeps NO id
    # in the payload). The CLARIFICATION_REQUESTED it emits carries the REAL request_id, never None.
    park_req_id = db.execute(
        "SELECT request_id FROM events WHERE aggregate='feature_contract' AND run_id=%s "
        "AND type='CLARIFICATION_REQUESTED' ORDER BY global_seq DESC LIMIT 1",
        ("run_f",),
    ).fetchone()[0]
    assert park_req_id == "req_f"
    # the run was parked for the human follow-up
    n_parked = db.execute(
        "SELECT count(*) FROM events WHERE aggregate='run' AND run_id=%s AND type='RUN_PARKED'",
        ("run_f",),
    ).fetchone()[0]
    assert n_parked == 1
    # re-driving now no-ops on the open manual review task (never a second park)
    res2 = _advance(db, "run_f", "adv-f-2")
    assert res2.accepted, res2.denied_reason
    assert len(_open_tasks(db, "run_f")) == 1
    n_parked2 = db.execute(
        "SELECT count(*) FROM events WHERE aggregate='run' AND run_id=%s AND type='RUN_PARKED'",
        ("run_f",),
    ).fetchone()[0]
    assert n_parked2 == 1


# ── an un-opened / unknown contract is a clean denial (no crash, nothing committed) ──────────────────
def test_advance_on_unknown_contract_is_denied(db, sp2_schemas):
    res = advance_intake(db, _adv_cmd("run_missing", "adv-missing"))
    assert res.accepted is False
    assert "unknown feature_contract" in res.denied_reason
