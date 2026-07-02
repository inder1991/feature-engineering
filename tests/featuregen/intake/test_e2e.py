"""SP-2 acceptance E2E (Task 9.2) — the definition-mode milestone: a data scientist's intent is
driven end-to-end, over the ASSEMBLED P1–P8 stack under the REAL PolicyAuthorizer + audit, from
`submit_intent` all the way to a **CONFIRMED** Feature Contract that `get_contract` serves to SP-3.

Hermetic: the only non-deterministic collaborator — the LLM — is the P3 `FakeLLM` scripted per
`task` (R19 `FakeLLM(script={task_key: FakeResponse(...)})`). The four intake seams
(`register_intake_catalog` / `register_intent_redactor` / `register_llm_client` /
`register_candidate_generator`) + Task-9.1's `register_sp2`/`seed_sp2_authz` are wired by `_wire`.

Flow (definition mode, CLEAR) — the SELF-DRIVING pipeline, all via real dispatch (Task 9.2a):
  execute_command(submit_intent)  → FakeLLM.structure_intent → frozen Draft + Assumption Ledger
  execute_command(advance_intent) → the thin production driver folds the Draft, runs the first-pass
                                    route through refine_contract (score → critique → doubt-router;
                                    clean → MINIMUM_CONTRACT_VALIDATED), then opens Human Gate #1
  execute_command(confirm_contract)→ CONTRACT_CONFIRMED → CONFIRMED
  get_contract(conn, run_id)      → the frozen, servable CONFIRMED_CONTRACT body (SP-3 hand-off)

The E2E proves the self-driving pipeline: nothing manually calls `run_minimum_contract_validation`
or `open_gate1_task` — `advance_intake` sequences the routing engine + the gate opener over real
`execute_command` dispatch (submit → advance → gate → confirm). The Draft is scripted fully resolved
so the Doubt Router asks nothing and MCV passes on the first refine pass — this sidesteps the
per-field clarification round (Tasks 9.3+ exercise that path).
The scenario closes with the X4 CAS / stale-append guard proven end-to-end (SP-1 capstone C2): a
replayed `confirm_contract` at the now-stale gate-task version is DENIED and never double-applies.
"""
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.banking_catalog import IntakeClassification, IntakeOutcome
from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz
from featuregen.intake.candidates import StubCandidateGenerator, register_candidate_generator
from featuregen.intake.catalog import load_banking_catalog_from_seed, register_intake_catalog
from featuregen.intake.commands import register_intake_classifier, register_intake_deps
from featuregen.intake.llm import FakeLLM, FakeResponse, register_llm_client
from featuregen.intake.read_model import get_contract
from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor

# ── the SP-0-governed read-only BankingDomainCatalog seed (§4.5) ──────────────────────────────────
# Shaped so the REAL deterministic classify_intent (which the §8.4 confirmation-time re-screen runs,
# NOT the pinned intake classifier) resolves the Draft's `card authorization` concept in-scope → CLEAR
# at confirmation. The `card_authorization` use-case is unscoped (no product/region required).
_BANKING_SEED = {
    "catalog_version": "bdc-2026.06",
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
_CATALOG_VERSION = _BANKING_SEED["catalog_version"]

_OBS = {
    "kind": "point_in_time",
    "as_of_field": "as_of_date",
    "rule": "use only data available strictly before as_of_date",
}

# ── definition-mode FakeLLM fixture: a FULLY RESOLVED Draft (NO open_fields) ───────────────────────
# structure_intent returns a schema-valid DRAFT_CONTRACT body whose only platform-inferred field
# (entity_grain, source=default) is accounted in the Assumption Ledger and whose remaining fields are
# low-ambiguity verbatim readings — so §5.3 (no silent assumption) holds, the Doubt Router opens no
# clarification, and the pre-gate MCV checklist passes directly.
_STRUCTURE_OUTPUT = {
    # echoed envelope — platform-authoritative fields are re-derived by assemble_draft_body (discarded)
    "request_id": "ECHO",
    "intake_mode": "definition",
    "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean",
    "assumption_ledger_ref": "doc_echo",
    "status": "NEEDS_CLARIFICATION",
    "provenance": {"schema_version": 1},
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": _OBS,
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [
            {"concept": "declined card authorization",
             "predicate": "card_authorizations.auth_result = 'D'"}
        ],
        "target_definition": "N/A (definition-mode feature, no target)",
    },
    "field_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.05, "confidence": 0.95, "source": "llm"},
    },
    "open_fields": [],
    "open_questions": [],
    "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "source": "default",
         "rationale": "point-in-time features are grained by entity x as_of_date by convention",
         "ambiguity": 0.30, "confidence": 0.72},
    ],
}
# R19 — FakeLLM(script={task_key: FakeResponse(...)}): wrap each task's deterministic output. The
# self-driving route runs refine_contract, whose challenger critique needs a scripted `contract_review`
# (an OK, finding-free verdict on the already-resolved Draft → the Loop converges straight to MCV).
_OK_REVIEW = {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}
_DEF_FIXTURES = {
    "structure_intent": FakeResponse(output=_STRUCTURE_OUTPUT),
    "contract_review": FakeResponse(output=_OK_REVIEW),
}

# ── clarification-round FakeLLM fixture (Task 9.3 SoD E2E): an AMBIGUOUS Draft with ONE must-ask open
#    field so the SELF-DRIVING pipeline opens a REAL clarification task. structure_intent returns the
#    same resolved envelope but with a high-ambiguity `filters` open field (predicate UNKNOWN) → the
#    Doubt Router routes it to a human and advance_intake → refine_contract opens the clarification task
#    the non-owner then tries (and fails) to answer. The owner's answer drives a `renormalize` round
#    that resolves the field back to the fully-resolved semantics (open_fields=[]) → MCV → Gate #1.
_AMBIGUOUS_STRUCTURE = {
    **_STRUCTURE_OUTPUT,
    "feature_semantics": {
        **_STRUCTURE_OUTPUT["feature_semantics"],
        "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
    },
    "field_scores": {
        **_STRUCTURE_OUTPUT["field_scores"],
        "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
    },
    "open_fields": ["filters.declined_status_encoding"],
    "open_questions": [{"field": "filters.declined_status_encoding",
                        "question": "Which column/value marks a declined authorization?",
                        "ambiguity": 0.80, "confidence": 0.40, "blocks_progress": True,
                        "routed_to": "human"}],
}
# The owner's answer targets the still-open `filters` field → refine_contract runs a `renormalize`
# round; this scripts it to resolve to the fully-resolved semantics (open_fields=[]) so the Loop
# converges to MINIMUM_CONTRACT_VALIDATED and the next advance_intake opens Human Gate #1.
_RENORMALIZE_RESOLVED = FakeResponse(
    output={"feature_semantics": _STRUCTURE_OUTPUT["feature_semantics"], "open_fields": []},
    self_reported_scores=_STRUCTURE_OUTPUT["field_scores"],
)
_CLARIFY_FIXTURES = {
    "structure_intent": FakeResponse(output=_AMBIGUOUS_STRUCTURE),
    "contract_review": FakeResponse(output=_OK_REVIEW),
    "renormalize": _RENORMALIZE_RESOLVED,
}


class _ScoringView:
    """The R10 merged-view scoring seam refine_contract re-scores against (candidate_count + metadata).
    One binding per concept keeps the deterministic cardinality doubt low — the resolved Draft's own
    low-ambiguity self-report drives routing, so nothing re-opens."""

    def candidate_count(self, concept):
        return 1

    def metadata(self):
        return {}


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def _clear_classification(intent, *, product=None, region=None, catalog=None):
    """A pinned CLEAR intake classification (deterministic, hermetic). The confirmation-time §8.4
    re-screen uses the REAL classify_intent over the registered catalog — this only pins the intake
    boundary so submit_intent normalizes into a Draft rather than exercising the classifier heuristics
    (those are unit-covered)."""
    return IntakeClassification(
        IntakeOutcome.CLEAR, _CATALOG_VERSION, "e2e: pinned in-scope (CLEAR)"
    )


def _wire(db, *, fixtures, catalog_seed=_BANKING_SEED, generator=False):
    """Assemble the full SP-2 stack under the real PolicyAuthorizer + a deterministic FakeLLM.
    Re-register SP-0 + SP-2 schemas into the per-test-reset event registry, seed authz + document
    schemas, then register the four intake seams (+ a pinned CLEAR classifier for a hermetic intake
    boundary)."""
    register_sp2(_Registry())  # SP-2 FC event schemas + SP-2 command catalog (idempotent)
    seed_authz_policy(db)  # SP-0 base rows (withdraw/park/open_task/etc.)
    seed_sp2_authz(db)  # SP-2 authz rows + contract doc-schemas + PRIMARY_SELECTED + checkpoints
    register_command_authorizer(PolicyAuthorizer())
    register_intake_catalog(load_banking_catalog_from_seed(catalog_seed))  # R8/R10 seam
    register_intent_redactor(DefaultIntentRedactor())
    register_intake_classifier(_clear_classification)  # deterministic intake boundary
    llm = FakeLLM(script=fixtures)  # R19 — the ONE pinned FakeLLM construction form
    register_llm_client(llm)
    # The Refinement-Loop deps advance_intake drives refine_contract with (Task 9.2a): the same FakeLLM +
    # redactor + the merged-view scoring seam. (conftest resets these per test so they never leak.)
    register_intake_deps(client=llm, redactor=DefaultIntentRedactor(), catalog=_ScoringView())
    if generator:
        register_candidate_generator(StubCandidateGenerator(llm))
    return llm


def _confirmed_primary_doc(db, run_id):
    """The single frozen, PRIMARY-role CONFIRMED_CONTRACT document — the SP-3 hand-off artifact
    confirm_contract freezes (§8.5). (Definition mode records no PRIMARY_SELECTED, so the promotion
    is the branch_role='primary' document itself, not the hypothesis-mode stage_primary projection.)"""
    rows = db.execute(
        "SELECT doc_id FROM documents WHERE run_id=%s AND stage='CONFIRMED_CONTRACT' "
        "AND branch_role='primary'",
        (run_id,),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _only_open_task(db, run_id):
    """The single OPEN human task for a run (here: the Gate #1 confirm task after open_gate1_task)."""
    rows = db.execute(
        "SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open' "
        "ORDER BY task_id",
        (run_id,),
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one open task, got {len(rows)}"
    return rows[0][0], rows[0][1]


def _data_scientist(subject):
    return build_human_identity(subject=subject, role_claims=("data_scientist",))


def _intake_agent():
    return build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="deploy-sig"
    )


def test_definition_intent_reaches_confirmed_contract_for_sp3(db):
    agent = _intake_agent()
    _wire(db, fixtures=_DEF_FIXTURES)
    raj = _data_scientist("user:raj")

    # ── intake: submit_intent (definition, CLEAR) → frozen Draft, fully resolved ────────────────────
    submitted = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {
                "request_id": "req-decl-1",
                "intent_text": "90-day rolling count of declined card authorizations per customer",
                "intake_mode": "definition",
                "product": "card_payments",
                "region": "US",
            },
            raj,
            "ik-def-submit",
        ),
    )
    assert submitted.accepted, submitted.denied_reason
    run_id = submitted.aggregate_id  # P4 returns the run_id as the aggregate_id

    view = get_contract(db, run_id)
    assert view.status == "NEEDS_CLARIFICATION"  # a Draft is produced, not yet servable to SP-3
    assert view.open_fields == ()  # scripted fully resolved → Doubt Router opens no clarification
    assert view.reason_if_unavailable is not None  # fail-closed: a Draft is never servable

    # ── advance_intake: the thin production driver self-drives Draft → refine (clean) → MCV → Gate #1,
    #    all via real dispatch. Nothing here manually calls run_minimum_contract_validation /
    #    open_gate1_task — advance_intake sequences the routing engine + the gate opener (Task 9.2a). ──
    advanced = execute_command(
        db,
        Command("advance_intake", "feature_contract", run_id, {"run_id": run_id}, agent, "ik-def-advance"),
    )
    assert advanced.accepted, advanced.denied_reason
    assert get_contract(db, run_id).status == "MINIMUM_CONTRACT_VALIDATED"
    # the dedicated Human Gate #1 confirmation task is now open (the requester then confirms)
    gate_task, gv = _only_open_task(db, run_id)

    confirmed = execute_command(
        db,
        Command(
            "confirm_contract",
            "feature_contract",
            run_id,
            {"run_id": run_id, "task_id": gate_task, "expected_task_version": gv},
            raj,
            "ik-def-confirm",
        ),
    )
    assert confirmed.accepted, confirmed.denied_reason

    # ── SP-3 hand-off: the CONFIRMED contract get_contract serves ───────────────────────────────────
    view = get_contract(db, run_id)
    assert view.status == "CONFIRMED"
    c = view["confirmed"]
    assert c["feature_name"] == "declined_card_auth_count_90d"
    assert c["intake_mode"] == "definition"
    assert c["entity"] == "customer"
    assert c["entity_key"] == "customer_id"  # Draft entity_grain -> feature_grain + derived entity_key
    assert c["feature_grain"] == ["customer_id", "as_of_date"]
    method = c["calculation_method"]["chosen"]
    assert method["kind"] == "rolling_aggregate"  # SP-3 switches on chosen.kind
    assert method["aggregation"] == "count"
    assert method["window"] == "90d"
    assert "auth_result" in method["filter"]["predicate"]
    assert c["requires_independent_validation"] is False
    assert c["status"] == "CONFIRMED"

    # the CONFIRMED_CONTRACT document is the frozen SP-3 hand-off artifact
    assert _confirmed_primary_doc(db, run_id) is not None
    # a run in a healthy CONFIRMED lifecycle is NOT a terminal-rejected run
    assert run_is_terminal(db, run_id) is False

    # auditable-LLM boundary: every call event-sourced; the store keeps the REDACTED input (no raw PII)
    n_calls = db.execute(
        "SELECT count(*) FROM events WHERE run_id=%s AND type='LLM_CALL_RECORDED'", (run_id,)
    ).fetchone()[0]
    assert n_calls >= 1  # structure_intent
    stored = db.execute(
        "SELECT redacted_input, raw_output FROM llm_call WHERE run_id=%s", (run_id,)
    ).fetchall()
    assert stored, "llm_call records must be replayable (redacted_input stored, not hash-only)"

    # ── X4 CAS / stale-append guard (SP-1 capstone C2), proven end-to-end ────────────────────────────
    # Replay confirm_contract at the NOW-STALE gate-task version with a FRESH idempotency key (so the
    # command genuinely re-runs the guards rather than returning the cached success). The folded
    # feature_contract head is already CONFIRMED (a no-regression-locked terminal), so the stale
    # confirm must NOT commit a second transition.
    stale = execute_command(
        db,
        Command(
            "confirm_contract",
            "feature_contract",
            run_id,
            {"run_id": run_id, "task_id": gate_task, "expected_task_version": gv},
            raj,
            "ik-def-confirm-stale",  # distinct key => NOT an idempotent replay; the guards actually run
        ),
    )
    assert stale.accepted is False, "a stale/replayed confirm must be DENIED, never double-applied (X4)"
    # the transition was applied EXACTLY ONCE — the stale re-append never committed
    assert (
        db.execute(
            "SELECT count(*) FROM events WHERE run_id=%s AND type='CONTRACT_CONFIRMED'", (run_id,)
        ).fetchone()[0]
        == 1
    )
    assert get_contract(db, run_id).status == "CONFIRMED"  # unchanged — no regression, no re-advance
    assert _confirmed_primary_doc(db, run_id) is not None  # still the ONE frozen artifact


def test_non_owner_data_scientist_denied_clarify_and_confirm_and_audited(db):
    """SoD / request-owner enforcement E2E (§8.2, R4/R15): SP-0 role-authz is NECESSARY BUT NOT
    SUFFICIENT. A DIFFERENT `data_scientist` — admitted by the coarse role row, yet NOT the request
    owner — is DENIED by the in-handler owner guard at BOTH `answer_clarification`
    (`actor_is_request_owner`) and `confirm_contract` (`confirmer_is_requester_human`); each denial is
    routed to the tamper-evident security-audit stream (`record_denial` → decision='denied'), never
    counted, with NO state change — while the rightful author still succeeds at each step.

    Driven end-to-end via the production driver `advance_intake` (Task 9.2a) over the REAL
    PolicyAuthorizer + audit: the scripted Draft carries ONE high-ambiguity must-ask field, so
    advance_intake → refine_contract opens a REAL clarification task the impostor then attacks; the
    owner's answer drives the Loop to MCV and a further advance opens Human Gate #1 (NOT a manual
    open_gate1_task). Proves the coarse role row admits the impostor while the fine owner guard denies +
    audits her, and the guard blocks the impostor — never the author."""
    agent = _intake_agent()
    _wire(db, fixtures=_CLARIFY_FIXTURES)
    raj = _data_scientist("user:raj")          # the request owner (INTENT_SUBMITTED actor, REQUESTER)
    mallory = _data_scientist("user:mallory")  # a DIFFERENT data_scientist (SAME role, not the owner)

    submitted = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {
                "request_id": "req-owner-1",
                "intent_text": "90-day rolling count of declined card authorizations per customer",
                "intake_mode": "definition",
                "product": "card_payments",
                "region": "US",
            },
            raj,
            "ik-own-submit",
        ),
    )
    assert submitted.accepted, submitted.denied_reason
    run_id = submitted.aggregate_id

    # advance_intake self-drives refine_contract, which opens a REAL must-ask clarification task
    # (the scripted Draft has one high-ambiguity open field) — the state the impostor then attacks.
    adv = execute_command(
        db,
        Command("advance_intake", "feature_contract", run_id, {"run_id": run_id}, agent, "ik-own-advance-1"),
    )
    assert adv.accepted, adv.denied_reason
    task_id, tv = _only_open_task(db, run_id)

    # coarse authz admits mallory (role=data_scientist), but the request-owner guard denies her
    denied = execute_command(
        db,
        Command(
            "answer_clarification",
            "feature_contract",
            run_id,
            {
                "task_id": task_id,
                "response": "confirm",
                "expected_task_version": tv,
                "answer": "card_authorizations.auth_result = 'D'",
            },
            mallory,
            "ik-own-mallory-answer",
        ),
    )
    assert denied.accepted is False
    assert "owner" in (denied.denied_reason or "").lower()
    n = db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE attempted_action='answer_clarification' AND decision='denied'"
    ).fetchone()[0]
    assert n >= 1
    # nothing was counted — the task is UNTOUCHED and the true owner can still answer it
    task_id2, tv2 = _only_open_task(db, run_id)
    assert (task_id2, tv2) == (task_id, tv)
    ok = execute_command(
        db,
        Command(
            "answer_clarification",
            "feature_contract",
            run_id,
            {
                "task_id": task_id2,
                "response": "confirm",
                "expected_task_version": tv2,
                "answer": "card_authorizations.auth_result = 'D'",
            },
            raj,
            "ik-own-raj-answer",
        ),
    )
    assert ok.accepted, ok.denied_reason

    # the owner's answer drove the Loop to MCV; advance opens Human Gate #1 (Task 9.2a — NOT a manual
    # open_gate1_task) — then prove a NON-owner cannot confirm either.
    adv2 = execute_command(
        db,
        Command("advance_intake", "feature_contract", run_id, {"run_id": run_id}, agent, "ik-own-advance-2"),
    )
    assert adv2.accepted, adv2.denied_reason
    gate_task, gv = _only_open_task(db, run_id)
    bad_confirm = execute_command(
        db,
        Command(
            "confirm_contract",
            "feature_contract",
            run_id,
            {"run_id": run_id, "task_id": gate_task, "expected_task_version": gv},
            mallory,
            "ik-own-mallory-confirm",
        ),
    )
    assert bad_confirm.accepted is False
    assert get_contract(db, run_id).status != "CONFIRMED"
    n2 = db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE attempted_action='confirm_contract' AND decision='denied'"
    ).fetchone()[0]
    assert n2 >= 1
    # and the real owner CAN confirm (the guard blocks the impostor, not the author)
    good = execute_command(
        db,
        Command(
            "confirm_contract",
            "feature_contract",
            run_id,
            {"run_id": run_id, "task_id": gate_task, "expected_task_version": gv},
            raj,
            "ik-own-raj-confirm",
        ),
    )
    assert good.accepted, good.denied_reason
    assert get_contract(db, run_id).status == "CONFIRMED"
