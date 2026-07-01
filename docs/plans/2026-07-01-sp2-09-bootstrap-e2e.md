# SP-2 — Phase 9 — Bootstrap + end-to-end (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative). The **Shared Contract — Key signatures**, the **Additive SP-0 surface**, and the **Global Constraints** there win over anything restated here.

---

This is the final SP-2 phase: the **production wiring** (`intake/bootstrap.py` — `register_sp2` + `seed_sp2_authz`, mirroring SP-1's `overlay/bootstrap.py`) and the **end-to-end acceptance suite** that drives the whole assembled stack (P1–P8) through the **real `PolicyAuthorizer`** with a deterministic **`FakeLLM`**. The E2E proves the four load-bearing guarantees of SP-2: (1) a definition-mode intent becomes a **CONFIRMED Feature Contract** SP-3 can consume; (2) the **request-owner guard** denies a non-owner `data_scientist` at clarify/confirm and writes the denial to the **security-audit** stream; (3) a **prohibited-class** intent is blocked (`PROHIBITED_DATA_CLASS`) with **no payload ever dispatched to the LLM**; (4) hypothesis-mode **stub candidates** promote via document `PRIMARY_SELECTED` and confirm. Implements design §2.1, §5.4, §6.5, §8, §9, and the two running examples (design Appendix B).

The Feature Contract lifecycle is SP-2's **folded `feature_contract` aggregate** (`fold_feature_contract_state`, validated inline in each handler) — **not** `state_machine/engine.py` (built-but-unused) and **not** `run_workflow_state`. This phase adds **no** new lifecycle logic; it only wires the registrations SP-0's registries and authz table need, and asserts the assembled behaviour.

**Cross-phase Consumes (built earlier; used verbatim here):**
- **SP-0 (verbatim):**
  - `featuregen.commands.api.execute_command(conn, cmd) -> CommandResult`; `featuregen.commands.registry.get_command`; `featuregen.commands.authz_seam.register_command_authorizer`.
  - `featuregen.authz.policy.seed_authz_policy(conn)` + `authorize_command(conn, cmd) -> AuthzDecision`; `featuregen.authz.authorizer.PolicyAuthorizer`.
  - `featuregen.aggregates.bootstrap.register_phase06_event_schemas()` (re-registers SP-0 event schemas into the per-test-reset `event_registry()` singleton).
  - `featuregen.aggregates.run_lifecycle.run_is_terminal(conn, run_id) -> bool`.
  - `featuregen.events.registry.event_registry()`; `featuregen.documents.registry.DocumentSchemaRegistry`; `featuregen.documents.primary.{register_primary_selected, current_primary}`.
  - `featuregen.identity.build.{build_human_identity, build_service_identity}`; `featuregen.contracts.Command`.
- **P1 (`intake/events.py` + migrations):** `register_sp2_event_types(registry)`; the twelve FC event-type schemas; migrations `0508_feature_contract_events.sql`, `0509_use_case_onboarding_gates.sql`; the `llm_call` record-store table + its checkpoint init.
- **P2 (`intake/contract.py`, `intake/banking_catalog.py`):** `register_contract_schemas(registry)` (registers `DRAFT_CONTRACT`/`ASSUMPTION_LEDGER`/`CONFIRMED_CONTRACT` content-schemas + upcasters); `load_banking_catalog(seed) -> BankingDomainCatalog`, `register_banking_catalog(catalog)`.
- **P3 (`intake/llm.py`, `intake/redaction.py`):** `FakeLLM`, `register_llm_client(client)`; `DefaultIntentRedactor`, `register_intent_redactor(redactor)`.
- **P4–P8 (`intake/commands.py`, `intake/read_model.py`):** `register_sp2_commands()` (idempotent — skips already-registered actions); the seven handlers (`submit_intent`, `answer_clarification`, `select_candidate_doc`, `open_gate1_task`, `confirm_contract`, `request_edit`, `reject_intent`); `get_contract(conn, run_id) -> ContractView`; `fold_feature_contract_state`.
- **P6 (`intake/candidates.py`):** `StubCandidateGenerator`, `register_candidate_generator(generator)`.

> **Integration seams owned by P2–P6 (consumed, not defined, here).** The DI accessors this phase binds to at bootstrap/E2E time — `register_banking_catalog`/`register_llm_client`/`register_intent_redactor`/`register_candidate_generator` (each with a `current_*()` reader used by the handlers, mirroring SP-1's `overlay/catalog.py::current_catalog_adapter`) — are the **module-global seams P2/P3/P4/P6 register their implementations through**. If an earlier phase named a seam accessor differently, reconcile the import at integration; the *behaviour* asserted here is fixed by the overview.

---

### Task 9.1: `intake/bootstrap.py` — `register_sp2` + `seed_sp2_authz`

**Files:**
- Create: `src/featuregen/intake/bootstrap.py`
- Test: `tests/featuregen/intake/test_bootstrap.py`

**Interfaces:**
- Consumes: `register_sp2_event_types` (`intake/events.py`, P1); `register_sp2_commands` (`intake/commands.py`, P4–P8, idempotent); `register_contract_schemas` (`intake/contract.py`, P2); `DocumentSchemaRegistry` (SP-0); `register_primary_selected` (SP-0 `documents/primary.py`); `event_registry` (SP-0). Authz-table shape `authz_policy(action, gate, permitted_role, actor_kind, scope)` + `projection_checkpoints(projection_name)` (SP-0).
- Produces:
  - `register_sp2(handler_registry) -> None` — the **conn-less** in-memory registrations SP-0's `append` validation needs each process/test: the twelve FC event schemas into the `event_registry()` singleton **and** the idempotent SP-2 command catalog. `handler_registry` is accepted for signature symmetry with `bootstrap_phase06`/`register_overlay`; **SP-2 registers no durable-runtime handlers** (the auditable-LLM record write and the clarification/Gate-#1 flow ride command handlers, not `HandlerRegistry` handlers — like SP-1's overlay, whose expiry poller is explicit, not a registered handler).
  - `seed_sp2_authz(conn) -> None` — the **conn-backed** seeding: the **eight** additive SP-2 `authz_policy` rows (incl. the `reject_intent` service row, §2.1 #5), the **contract content-schemas** into `document_type_registry` (`register_contract_schemas(DocumentSchemaRegistry(conn))`), the document **`PRIMARY_SELECTED`** registration (`register_primary_selected(conn)` — event schema + `stage_primary` checkpoint, for hypothesis-mode candidate promotion), and the **optional FC-status read-model** projection checkpoint (`'feature_contract'`). Idempotent throughout (`ON CONFLICT DO NOTHING`/`DO UPDATE`).
  - `_SP2_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...]` — the eight rows exactly per the overview.

> **Why the DB-backed registrations ride `seed_sp2_authz(conn)` and not `register_sp2`.** The overview groups "event schemas + contract schemas + commands + PRIMARY_SELECTED" under `register_sp2` **conceptually**, but pins its signature to `register_sp2(handler_registry)` — **no conn**. The document-schema registry and `register_primary_selected` are DB-backed, so — exactly as SP-1 keeps `register_overlay` conn-less and does all DB seeding in `seed_overlay_authz(conn)` — the conn-requiring parts physically execute in `seed_sp2_authz(conn)`. Deployment calls both; tests call both. This drifts no shared *symbol* (both signatures are honoured verbatim).

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_bootstrap.py
from psycopg.rows import dict_row

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import authorize_command, seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.commands.registry import get_command
from featuregen.contracts import Command
from featuregen.events.registry import event_registry
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz

_SP2_EVENT_TYPES = {
    "INTENT_SUBMITTED",
    "DRAFT_CONTRACT_PRODUCED",
    "CONTRACT_CRITIQUED",
    "FIELD_AUTO_RESOLVED",
    "CLARIFICATION_REQUESTED",
    "CLARIFICATION_ANSWERED",
    "CONTRACT_REFINED",
    "MINIMUM_CONTRACT_VALIDATED",
    "CONTRACT_CONFIRMED",
    "USE_CASE_ONBOARDING_REQUESTED",
    "INTENT_REJECTED",
    "LLM_CALL_RECORDED",
}
_SP2_ACTIONS = {
    "submit_intent",
    "answer_clarification",
    "select_candidate_doc",
    "open_gate1_task",
    "confirm_contract",
    "request_edit",
    "reject_intent",
}


class _Registry:
    """Stand-in HandlerRegistry; SP-2 registers no runtime handlers."""

    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def test_register_sp2_registers_fc_event_schemas_and_command_catalog():
    register_sp2(_Registry())
    registered = {t for (t, _v, _s, _o, _st) in event_registry().all_schemas()}
    assert _SP2_EVENT_TYPES <= registered
    for action in _SP2_ACTIONS:
        assert callable(get_command(action))
    # idempotent: a second call raises nothing (register_sp2_commands skips already-registered)
    register_sp2(_Registry())


def test_seed_sp2_authz_seeds_the_eight_additive_rows(db):
    seed_sp2_authz(db)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT action, permitted_role, actor_kind FROM authz_policy "
            "WHERE action = ANY(%s) ORDER BY action, permitted_role",
            (sorted(_SP2_ACTIONS),),
        )
        rows = cur.fetchall()
    got = {(r["action"], r["permitted_role"], r["actor_kind"]) for r in rows}
    assert ("submit_intent", "data_scientist", "human") in got
    assert ("submit_intent", "intake-agent", "service") in got
    assert ("answer_clarification", "data_scientist", "human") in got
    assert ("select_candidate_doc", "data_scientist", "human") in got
    assert ("open_gate1_task", "intake-agent", "service") in got
    assert ("confirm_contract", "data_scientist", "human") in got
    assert ("request_edit", "data_scientist", "human") in got
    # the ADDITIVE rejection authority: reject_intent is service-issued, NOT SP-0's validator `reject`
    assert ("reject_intent", "intake-agent", "service") in got
    assert len(got) == 8
    # idempotent
    seed_sp2_authz(db)
    n = db.execute(
        "SELECT count(*) FROM authz_policy WHERE action = ANY(%s)", (sorted(_SP2_ACTIONS),)
    ).fetchone()[0]
    assert n == 8


def test_seed_sp2_authz_registers_contract_schemas_primary_selected_and_checkpoints(db):
    register_sp2(_Registry())  # PRIMARY_SELECTED also lands in the in-memory registry
    seed_sp2_authz(db)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type_name FROM document_type_registry "
            "WHERE type_name IN ('DRAFT_CONTRACT','ASSUMPTION_LEDGER','CONFIRMED_CONTRACT') "
            "AND schema_version=1"
        )
        docs = {r["type_name"] for r in cur.fetchall()}
        assert docs == {"DRAFT_CONTRACT", "ASSUMPTION_LEDGER", "CONFIRMED_CONTRACT"}
        cur.execute(
            "SELECT 1 FROM event_type_registry WHERE type_name='PRIMARY_SELECTED'"
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT projection_name FROM projection_checkpoints "
            "WHERE projection_name IN ('stage_primary','feature_contract')"
        )
        checkpoints = {r["projection_name"] for r in cur.fetchall()}
        assert {"stage_primary", "feature_contract"} <= checkpoints


def test_seeded_rows_admit_the_owner_and_the_service_at_the_authz_layer(db):
    seed_authz_policy(db)  # SP-0 base rows
    seed_sp2_authz(db)
    raj = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    svc = build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="deploy-sig"
    )
    submit = Command("submit_intent", "feature_contract", None, {}, raj, "ik-a")
    reject = Command("reject_intent", "feature_contract", "run_x", {}, svc, "ik-b")
    assert authorize_command(db, submit).allowed is True
    assert authorize_command(db, reject).allowed is True
    # a role that is NOT data_scientist is refused at the authz layer
    analyst = build_human_identity(subject="user:mallory", role_claims=("analyst",))
    assert authorize_command(db, Command("submit_intent", "feature_contract", None, {}, analyst, "ik-c")).allowed is False


def test_unauthorized_submit_intent_is_denied_and_audited(db):
    register_sp2(_Registry())
    seed_authz_policy(db)
    seed_sp2_authz(db)
    register_command_authorizer(PolicyAuthorizer())
    analyst = build_human_identity(subject="user:mallory", role_claims=("analyst",))
    res = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {"request_id": "r1", "intent_text": "x", "intake_mode": "definition"},
            analyst,
            "ik-deny",
        ),
    )
    assert res.accepted is False
    assert res.denied_reason == "no matching authz policy"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE event_type='COMMAND_DENIED' AND attempted_action='submit_intent'"
        )
        assert cur.fetchone()["n"] == 1
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_bootstrap.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.bootstrap'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/intake/bootstrap.py
from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.documents.primary import register_primary_selected
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.events.registry import event_registry
from featuregen.intake.commands import register_sp2_commands
from featuregen.intake.contract import register_contract_schemas
from featuregen.intake.events import register_sp2_event_types

# §2.1 SP-2 command-capability rows (coarse capability only; fine authority — the request-owner
# guard, `confirmer_is_requester_human`, delegation-off — lives in intake/mcv.py + the handlers,
# NOT here, mirroring SP-1). The `reject_intent` service row is the ONE additive rejection
# authority (§2.1 #5): SP-0's `reject` stays validator-only and is untouched; requester
# abandonment reuses SP-0's `withdraw` (also untouched). No onboarding-answer row (deferred, §14).
_SP2_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("submit_intent", "", "data_scientist", "human", None),
    ("submit_intent", "", "intake-agent", "service", None),
    ("answer_clarification", "", "data_scientist", "human", None),
    ("select_candidate_doc", "", "data_scientist", "human", None),
    ("open_gate1_task", "", "intake-agent", "service", None),
    ("confirm_contract", "", "data_scientist", "human", None),
    ("request_edit", "", "data_scientist", "human", None),
    ("reject_intent", "", "intake-agent", "service", None),
)


def register_sp2(handler_registry) -> None:
    """Conn-less, in-memory registrations SP-0's `append` validation needs every process/test:
    the twelve `feature_contract` FC event schemas into the `event_registry()` singleton + the
    idempotent SP-2 command catalog. SP-2 registers NO durable-runtime handlers, so nothing is
    put into `handler_registry` (accepted only for signature symmetry with the SP-0/SP-1
    bootstraps). The DB-backed contract-schema + PRIMARY_SELECTED registrations ride the
    conn-taking `seed_sp2_authz` below (register_sp2 has no conn — same split as SP-1's
    conn-less `register_overlay` + conn-backed `seed_overlay_authz`)."""
    del handler_registry
    register_sp2_event_types(event_registry())
    register_sp2_commands()  # idempotent: skips already-registered actions


def seed_sp2_authz(conn: DbConn) -> None:
    """Idempotently seed the eight additive SP-2 authz rows, register the contract content-schemas
    + the document `PRIMARY_SELECTED` promotion primitive, and init the FC-status projection
    checkpoint. All inserts additive/backward-compatible — no existing SP-0 row is rewritten."""
    for action, gate, role, kind, scope in _SP2_POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )
    # Contract content-schemas (DRAFT/ASSUMPTION_LEDGER/CONFIRMED) into SP-0's document registry.
    register_contract_schemas(DocumentSchemaRegistry(conn))
    # Document PRIMARY_SELECTED: event schema (DB + in-memory) + stage_primary checkpoint, used for
    # hypothesis-mode candidate promotion (§7.1). Idempotent.
    register_primary_selected(conn)
    # Optional fail-closed FC-status read-model projection checkpoint (secondary to the fold —
    # queries only, never a command decision, §4.6/§11). Harmless if the projection is unwired.
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) VALUES ('feature_contract') "
        "ON CONFLICT (projection_name) DO NOTHING"
    )
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_bootstrap.py -v`
  - Expected: PASS (6 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/bootstrap.py tests/featuregen/intake/test_bootstrap.py && git commit -m "feat(intake): register_sp2 + seed_sp2_authz bootstrap wiring"`

---

### Task 9.2: E2E harness + definition mode → CONFIRMED contract (usable by SP-3)

**Files:**
- Create: `tests/featuregen/intake/test_e2e.py` (shared harness + the definition scenario; Tasks 9.3–9.5 append scenarios)

**Interfaces:**
- Consumes: `register_sp2`/`seed_sp2_authz` (Task 9.1); `register_phase06_event_schemas` (SP-0); `seed_authz_policy` + `PolicyAuthorizer` + `register_command_authorizer` + `execute_command` (SP-0); `get_contract` (P8); `current_primary` (SP-0); `run_is_terminal` (SP-0); the P2/P3/P6 seams `load_banking_catalog`/`register_banking_catalog`, `FakeLLM`/`register_llm_client`, `DefaultIntentRedactor`/`register_intent_redactor`, `StubCandidateGenerator`/`register_candidate_generator`.
- Produces: no new src — an **acceptance** test over the assembled P1–P8 stack. Helpers `_wire`, `_only_open_task`, `_Registry`, the `_BANKING_SEED`, and the `FakeLLM` fixture maps `_DEF_FIXTURES`/`_HYP_FIXTURES` (task-keyed deterministic outputs).

> **Acceptance-test discipline.** These E2E scenarios run over already-built code (P1–P8), so a scenario is expected to **PASS on first run**. A failure is a real integration defect — localize it to the phase that owns the failing step (the assertion messages name the step) and fix it there, not by weakening the assertion.

> **`FakeLLM` fixtures are task-keyed for determinism.** The overview keys `FakeLLM` on `(task, prompt_id, input_hash)`; within a single scenario each `task` fires once (`structure_intent`, then `renormalize`, plus `generate_candidates` in hypothesis mode), so keying the fixture map on `task` alone is unambiguous and independent of the redactor's `input_hash`. If P3's `FakeLLM` constructor differs, adapt the two-line construction in `_wire`.

- [ ] **Step 1 — write the failing test (harness + definition scenario)**

```python
# tests/featuregen/intake/test_e2e.py
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.documents.primary import current_primary
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.banking_catalog import load_banking_catalog, register_banking_catalog
from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz
from featuregen.intake.candidates import StubCandidateGenerator, register_candidate_generator
from featuregen.intake.llm import FakeLLM, register_llm_client
from featuregen.intake.read_model import get_contract
from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor

# ── the SP-0-governed, read-only BankingDomainCatalog seed (§4.5) ──────────────────────────
_BANKING_SEED = {
    "version": "bdc-2026-06-01",
    "owner": "governance:model-risk",
    "effective_date": "2026-06-01",
    "provenance": "seed/banking-domain-catalog.json",
    "allowed_domains": ["card_payments", "credit_risk", "transactions"],
    "allowed_use_cases": ["fraud_detection", "credit_decisioning", "transaction_monitoring"],
    "out_of_scope_examples": ["weather forecasting", "sports betting odds"],
    "blocked_data_classes": ["race", "ethnicity", "religion", "national_origin"],
    "sensitive_proxy_hints": ["credit risk", "spending category", "income", "zip code"],
    "high_risk_use_cases": ["credit_decisioning"],
    "jurisdiction_scope": {},
    "use_case_scope": {},
}

_OBS = {
    "kind": "point_in_time",
    "as_of_field": "as_of_date",
    "rule": "use only data available strictly before as_of_date",
}

# ── definition-mode FakeLLM fixtures (declined_card_auth_count_90d) ─────────────────────────
_DEF_STRUCTURE = {
    "output": {
        "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": _OBS,
            "calculation_method": "rolling_count",
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
            "target_definition": "N/A (definition-mode feature, no target)",
        },
        "open_questions": [
            {
                "field": "filters.declined_status_encoding",
                "question": "Which column/value marks a declined authorization?",
                "blocks_progress": True,
                "routed_to": "human",
            }
        ],
    },
    "self_reported_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90},
        "windows": {"ambiguity": 0.05, "confidence": 0.98},
        "filters": {"ambiguity": 0.80, "confidence": 0.40},
    },
}
_DEF_RENORMALIZE = {
    "output": {
        "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": _OBS,
            "calculation_method": "rolling_count",
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [
                {
                    "concept": "declined card authorization",
                    "predicate": "card_authorizations.auth_result = 'D'",
                }
            ],
            "target_definition": "N/A (definition-mode feature, no target)",
        },
        "open_questions": [],
    },
    "self_reported_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.97},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90},
        "windows": {"ambiguity": 0.05, "confidence": 0.98},
        "filters": {"ambiguity": 0.05, "confidence": 0.95},
    },
}
_DEF_FIXTURES = {"structure_intent": _DEF_STRUCTURE, "renormalize": _DEF_RENORMALIZE}


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def _wire(db, *, fixtures, catalog_seed=_BANKING_SEED, generator=False):
    """Assemble the full SP-2 stack under the real PolicyAuthorizer + a deterministic FakeLLM.
    Mirrors SP-1's `_wire` (Task 4.7): re-register SP-0 + SP-2 schemas into the per-test-reset
    event registry, seed authz + document schemas, then register the four intake seams."""
    register_phase06_event_schemas()  # SP-0 event schemas (guard re-registers after per-test reset)
    register_sp2(_Registry())  # SP-2 FC event schemas + SP-2 command catalog
    seed_authz_policy(db)  # SP-0 base rows (withdraw/park/open_task/etc.)
    seed_sp2_authz(db)  # SP-2 authz rows + contract doc-schemas + PRIMARY_SELECTED + checkpoints
    register_command_authorizer(PolicyAuthorizer())
    register_banking_catalog(load_banking_catalog(catalog_seed))
    register_intent_redactor(DefaultIntentRedactor())
    llm = FakeLLM(fixtures)
    register_llm_client(llm)
    if generator:
        register_candidate_generator(StubCandidateGenerator(client=llm))
    return llm


def _only_open_task(db, run_id):
    """The single OPEN human task for a run (the per-field clarification, or — after
    open_gate1_task cancels the per-field tasks — the Gate #1 confirm task)."""
    row = db.execute(
        "SELECT task_id, task_version FROM human_tasks WHERE run_id=%s AND status='open' "
        "ORDER BY task_id",
        (run_id,),
    ).fetchall()
    assert len(row) == 1, f"expected exactly one open task, got {len(row)}"
    return row[0][0], row[0][1]


def _data_scientist(subject):
    return build_human_identity(subject=subject, role_claims=("data_scientist",))


def _intake_agent():
    return build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="deploy-sig"
    )


def test_definition_intent_reaches_confirmed_contract_for_sp3(db):
    _wire(db, fixtures=_DEF_FIXTURES)
    raj = _data_scientist("user:raj")

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

    # a Draft was produced with ONE must-ask clarification (the declined-status encoding, amb 0.80)
    assert get_contract(db, run_id)["status"] == "NEEDS_CLARIFICATION"
    task_id, tv = _only_open_task(db, run_id)

    answered = execute_command(
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
            raj,
            "ik-def-answer",
        ),
    )
    assert answered.accepted, answered.denied_reason
    # refinement loop closed all open fields → MCV passed
    assert get_contract(db, run_id)["status"] == "MINIMUM_CONTRACT_VALIDATED"

    opened = execute_command(
        db,
        Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, _intake_agent(), "ik-def-gate"),
    )
    assert opened.accepted, opened.denied_reason
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

    view = get_contract(db, run_id)
    assert view["status"] == "CONFIRMED"
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
    assert current_primary(db, run_id, "CONFIRMED_CONTRACT") is not None
    # a run in a healthy CONFIRMED lifecycle is NOT a terminal-rejected run
    assert run_is_terminal(db, run_id) is False

    # auditable-LLM boundary: every call event-sourced; the store keeps the REDACTED input (no raw PII)
    n_calls = db.execute(
        "SELECT count(*) FROM events WHERE run_id=%s AND type='LLM_CALL_RECORDED'", (run_id,)
    ).fetchone()[0]
    assert n_calls >= 2  # structure_intent + renormalize
    stored = db.execute(
        "SELECT redacted_input, raw_output FROM llm_call WHERE run_id=%s", (run_id,)
    ).fetchall()
    assert stored, "llm_call records must be replayable (redacted_input stored, not hash-only)"
```

- [ ] **Step 2 — run it (expect PASS end-to-end)**
  - `uv run pytest tests/featuregen/intake/test_e2e.py::test_definition_intent_reaches_confirmed_contract_for_sp3 -v`
  - Expected: PASS. A failure localizes an integration gap to the phase owning the failing step (P2 catalog/schema, P3 LLM/redaction, P4 intake, P5 clarification/MCV, P7 Gate #1, P8 read model).

- [ ] **Step 3 — commit**
  - `git add tests/featuregen/intake/test_e2e.py && git commit -m "test(intake): E2E definition-mode intent -> CONFIRMED contract (FakeLLM, real authz)"`

---

### Task 9.3: E2E — non-owner `data_scientist` DENIED clarify + confirm, security-audited

**Files:**
- Modify: `tests/featuregen/intake/test_e2e.py` (append scenario)

**Interfaces:**
- Consumes: everything from Task 9.2; the SP-2 **request-owner guard** (`actor_is_request_owner` at `answer_clarification`; `confirmer_is_requester_human` at `confirm_contract`, P5/P7) and its security-audit write (`security_audit(attempted_action, decision)`); `delegation_allowed=False` on SP-2-opened tasks.
- Produces: no new src — proves SP-0 role-authz is **necessary but not sufficient** (a *different* `data_scientist` is admitted by the coarse row yet denied by the in-handler owner guard) and that the denial is audited, never counted.

- [ ] **Step 1 — write the failing test (append)**

```python
def test_non_owner_data_scientist_denied_clarify_and_confirm_and_audited(db):
    _wire(db, fixtures=_DEF_FIXTURES)
    raj = _data_scientist("user:raj")  # the request owner
    mallory = _data_scientist("user:mallory")  # a DIFFERENT data_scientist (same role)

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
    run_id = submitted.aggregate_id
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

    # drive to Gate #1 and prove a NON-owner cannot confirm either
    execute_command(
        db,
        Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, _intake_agent(), "ik-own-gate"),
    )
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
    assert get_contract(db, run_id)["status"] != "CONFIRMED"
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
    assert get_contract(db, run_id)["status"] == "CONFIRMED"
```

- [ ] **Step 2 — run it (expect PASS)**
  - `uv run pytest tests/featuregen/intake/test_e2e.py::test_non_owner_data_scientist_denied_clarify_and_confirm_and_audited -v`
  - Expected: PASS. A failure means the request-owner guard (P5 `answer_clarification` / P7 `confirm_contract`) is missing or not writing to `security_audit`.

- [ ] **Step 3 — commit**
  - `git add tests/featuregen/intake/test_e2e.py && git commit -m "test(intake): E2E non-owner clarify/confirm denied + security-audited"`

---

### Task 9.4: E2E — prohibited-class intent blocked (`PROHIBITED_DATA_CLASS`, no LLM payload)

**Files:**
- Modify: `tests/featuregen/intake/test_e2e.py` (append scenario)

**Interfaces:**
- Consumes: the deterministic banking-boundary classifier `classify_intent` (P2, `PROHIBITED_DATA_CLASS` most-restrictive-wins, stamps matched class + catalog `version`); the platform/service-issued `reject_intent` → `INTENT_REJECTED` + SP-0 `RUN_REJECTED` (P8, runs as `service:intake-agent` under the additive authz row — **not** SP-0's validator-only `reject`); `run_is_terminal`.
- Produces: no new src — proves the block is **fail-closed BEFORE the redactor/LLM** (no `llm_call` row: an un-dispatched intent leaks nothing), terminal on the run aggregate, and carries the audit provenance (matched class + version).

- [ ] **Step 1 — write the failing test (append)**

```python
def test_prohibited_class_intent_is_blocked_before_any_llm_call(db):
    _wire(db, fixtures={})  # no LLM fixtures — a blocked intent must never reach the model
    raj = _data_scientist("user:raj")

    submitted = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {
                "request_id": "req-prohib-1",
                "intent_text": "count of declined card authorizations per customer, split by customer race",
                "intake_mode": "definition",
                "product": "card_payments",
                "region": "US",
            },
            raj,
            "ik-prohib-submit",
        ),
    )
    run_id = submitted.aggregate_id

    # the folded feature_contract status is the terminal block
    assert get_contract(db, run_id)["status"] == "PROHIBITED_DATA_CLASS"

    # platform/service-issued terminal rejection, stamping the matched class + catalog version
    rej = db.execute(
        "SELECT payload, actor FROM events WHERE run_id=%s AND type='INTENT_REJECTED'", (run_id,)
    ).fetchone()
    assert rej is not None, "a prohibited intent must emit INTENT_REJECTED"
    payload, actor = rej
    assert payload["classification"] == "PROHIBITED_DATA_CLASS"
    assert payload["matched_class"] == "race"
    assert payload["catalog_version"] == _BANKING_SEED["version"]
    assert actor["subject"] == "service:intake-agent"  # NOT the requester, NOT a validator

    # SP-0 run terminal outcome is RUN_REJECTED (reject_intent), never the validator-only `reject`
    assert run_is_terminal(db, run_id) is True
    assert (
        db.execute(
            "SELECT count(*) FROM events WHERE run_id=%s AND type='RUN_REJECTED'", (run_id,)
        ).fetchone()[0]
        == 1
    )

    # the hard no-PII boundary: no payload was ever dispatched — the llm_call store is empty
    assert (
        db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 0
    )
```

- [ ] **Step 2 — run it (expect PASS)**
  - `uv run pytest tests/featuregen/intake/test_e2e.py::test_prohibited_class_intent_is_blocked_before_any_llm_call -v`
  - Expected: PASS. A failure means the banking-boundary screen (P2/P4) is not running before redact→LLM, or `reject_intent` (P8) is misattributed.

- [ ] **Step 3 — commit**
  - `git add tests/featuregen/intake/test_e2e.py && git commit -m "test(intake): E2E prohibited-class intent blocked, no LLM payload"`

---

### Task 9.5: E2E — hypothesis mode stub candidates → `select_candidate_doc` → confirm

**Files:**
- Modify: `tests/featuregen/intake/test_e2e.py` (append the hypothesis fixtures + scenario)

**Interfaces:**
- Consumes: `StubCandidateGenerator`/`register_candidate_generator` (P6, one LLM call → 1–3 candidate-role `DRAFT_CONTRACT` docs); `confirm_contract` hypothesis path (invokes `select_candidate_doc` → document `PRIMARY_SELECTED` promotion, records `selected_candidate`/`rejected_candidates` in the confirmation record only — write-once losers untouched); the risk-flag screen (`requires_independent_validation=true` on the high-risk credit-decisioning use-case, §8.4); `current_primary`.
- Produces: no new src — proves the hypothesis flow is real end-to-end and the `CandidateGenerator` seam is generator-agnostic (SP-12 boundary holds).

- [ ] **Step 1 — write the failing test (append fixtures + scenario)**

```python
# ── hypothesis-mode FakeLLM fixtures (abrupt spending-category shift -> credit risk) ─────────
_HYP_STRUCTURE = {
    "output": {
        "proposed_feature_name": "abrupt_spending_category_shift",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": _OBS,
            "calculation_method": "see_candidate_set",  # non-UNKNOWN: resolved by the candidate set (§6.7 #2)
            "windows": [{"name": "lookback", "value": "30d"}],
            "filters": [],
            "target_definition": "UNKNOWN",  # policy-sensitive credit-risk label — must-ask
        },
        "open_questions": [
            {
                "field": "target_definition",
                "question": "Confirm the exact permitted definition of 'higher credit risk'.",
                "blocks_progress": True,
                "routed_to": "human",
            }
        ],
    },
    "self_reported_scores": {
        "entity": {"ambiguity": 0.05, "confidence": 0.95},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72},
        "target_definition": {"ambiguity": 0.70, "confidence": 0.40},
    },
}
_HYP_CANDIDATES = {
    "output": {
        "candidates": [
            {
                "candidate_id": "cand_mcc_delta",
                "definition_text": "count of distinct merchant-category codes in last 30d minus prior 30d",
                "rationale": "abrupt breadth change signals distress",
                "calculation_method": {
                    "method_version": 1,
                    "chosen": {
                        "kind": "distribution_divergence",
                        "measure": "distinct_mcc_delta",
                        "window": "30d",
                        "baseline_window": "30d",
                    },
                    "considered": [],
                },
                "signals": {"references_catalog_concepts": True, "window_sane": True},
            },
            {
                "candidate_id": "cand_top_share",
                "definition_text": "top-1 category spend share this month vs the 3-month average",
                "rationale": "concentration shift",
                "calculation_method": {
                    "method_version": 1,
                    "chosen": {
                        "kind": "ratio",
                        "numerator": "top1_share_1m",
                        "denominator": "top1_share_3m_avg",
                        "window": "1m",
                    },
                    "considered": [],
                },
                "signals": {"references_catalog_concepts": True, "window_sane": True},
            },
            {
                "candidate_id": "cand_js_div",
                "definition_text": "Jensen-Shannon divergence of category-spend vs trailing 6 months",
                "rationale": "distribution drift",
                "calculation_method": {
                    "method_version": 1,
                    "chosen": {
                        "kind": "distribution_divergence",
                        "measure": "jensen_shannon",
                        "window": "1m",
                        "baseline_window": "6m",
                    },
                    "considered": [],
                },
                "signals": {"references_catalog_concepts": True, "window_sane": True},
            },
        ]
    },
    "self_reported_scores": {},
}
_HYP_RENORMALIZE = {
    "output": {
        "proposed_feature_name": "abrupt_spending_category_shift",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": _OBS,
            "calculation_method": "see_candidate_set",
            "windows": [{"name": "lookback", "value": "30d"}],
            "filters": [],
            "target_definition": "90+ days past due within 12 months (permitted credit-performance label)",
        },
        "open_questions": [],
    },
    "self_reported_scores": {
        "target_definition": {"ambiguity": 0.05, "confidence": 0.95},
    },
}
_HYP_FIXTURES = {
    "structure_intent": _HYP_STRUCTURE,
    "generate_candidates": _HYP_CANDIDATES,
    "renormalize": _HYP_RENORMALIZE,
}


def _candidate_doc_ids(db, run_id):
    rows = db.execute(
        "SELECT doc_id FROM documents WHERE run_id=%s AND stage='DRAFT_CONTRACT' "
        "AND branch_role='candidate' ORDER BY doc_id",
        (run_id,),
    ).fetchall()
    return [r[0] for r in rows]


def test_hypothesis_stub_candidates_select_and_confirm(db):
    _wire(db, fixtures=_HYP_FIXTURES, generator=True)
    raj = _data_scientist("user:raj")

    submitted = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {
                "request_id": "req-hyp-1",
                "intent_text": "customers who abruptly shift spending category are higher credit risk",
                "intake_mode": "hypothesis",
                "product": "credit_risk",
                "region": "US",
            },
            raj,
            "ik-hyp-submit",
        ),
    )
    assert submitted.accepted, submitted.denied_reason
    run_id = submitted.aggregate_id

    # the stub made ONE call and emitted 1–3 candidate-role DRAFT_CONTRACT documents
    candidates = _candidate_doc_ids(db, run_id)
    assert 1 <= len(candidates) <= 3
    chosen = candidates[0]

    # the policy-sensitive target is must-ask → one clarification; the owner pins a permitted label
    task_id, tv = _only_open_task(db, run_id)
    answered = execute_command(
        db,
        Command(
            "answer_clarification",
            "feature_contract",
            run_id,
            {
                "task_id": task_id,
                "response": "confirm",
                "expected_task_version": tv,
                "answer": "90+ days past due within 12 months (permitted credit-performance label)",
            },
            raj,
            "ik-hyp-answer",
        ),
    )
    assert answered.accepted, answered.denied_reason
    assert get_contract(db, run_id)["status"] == "MINIMUM_CONTRACT_VALIDATED"

    execute_command(
        db,
        Command("open_gate1_task", "feature_contract", run_id, {"run_id": run_id}, _intake_agent(), "ik-hyp-gate"),
    )
    gate_task, gv = _only_open_task(db, run_id)

    confirmed = execute_command(
        db,
        Command(
            "confirm_contract",
            "feature_contract",
            run_id,
            {
                "run_id": run_id,
                "task_id": gate_task,
                "expected_task_version": gv,
                "candidate_doc_id": chosen,
            },
            raj,
            "ik-hyp-confirm",
        ),
    )
    assert confirmed.accepted, confirmed.denied_reason

    # the chosen candidate was promoted via document PRIMARY_SELECTED (write-once losers untouched)
    assert current_primary(db, run_id, "DRAFT_CONTRACT") == chosen

    view = get_contract(db, run_id)
    assert view["status"] == "CONFIRMED"
    c = view["confirmed"]
    assert c["intake_mode"] == "hypothesis"
    assert c["confirmation"]["selected_candidate"] == chosen
    assert set(c["confirmation"]["rejected_candidates"]) == set(candidates) - {chosen}
    # the credit-decisioning use-case is high-risk → the risk flag rides to Gate #2 (SP-5)
    assert c["requires_independent_validation"] is True
    # the chosen candidate's tagged calculation_method is what SP-3 consumes
    assert c["calculation_method"]["chosen"]["kind"] in (
        "rolling_aggregate",
        "point_snapshot",
        "ratio",
        "distribution_divergence",
    )
```

- [ ] **Step 2 — run it (expect PASS)**
  - `uv run pytest tests/featuregen/intake/test_e2e.py::test_hypothesis_stub_candidates_select_and_confirm -v`
  - Expected: PASS. A failure localizes to P6 (candidate docs / stub) or P7 (`confirm_contract` hypothesis promotion path).

- [ ] **Step 3 — commit**
  - `git add tests/featuregen/intake/test_e2e.py && git commit -m "test(intake): E2E hypothesis stub candidates -> PRIMARY_SELECTED -> CONFIRMED"`

---

### Task 9.6: Full SP-2 suite regression + lint

**Files:** none (verification only)

**Interfaces:**
- Consumes: the whole assembled SP-2 package (`src/featuregen/intake/`) + suite (`tests/featuregen/intake/`).
- Produces: green suite + clean lint — the SP-2 acceptance gate.

- [ ] **Step 1 — run the whole intake suite (no regression across P1–P9)**
  - `uv run pytest tests/featuregen/intake/ -v`
  - Expected: PASS (all P1–P9 intake tests green, including the four E2E scenarios).

- [ ] **Step 2 — run the full repo suite (SP-2 rewrites no SP-0/SP-1 row)**
  - `uv run pytest -q`
  - Expected: PASS — the two additive migrations (`0508`/`0509`) + the eight additive authz rows + the overwritten-in-place contract content-schemas break no existing SP-0/SP-1 test.

- [ ] **Step 3 — lint**
  - `uv run ruff check src/featuregen/intake/bootstrap.py tests/featuregen/intake/test_bootstrap.py tests/featuregen/intake/test_e2e.py`
  - Expected: no findings.

- [ ] **Step 4 — commit (docs/marker only, if the suite surfaced any fixup)**
  - `git add -A && git commit -m "chore(intake): SP-2 acceptance suite green (bootstrap + E2E)"`

---

## Phase 9 done — SP-2 acceptance criteria met

- **`register_sp2` + `seed_sp2_authz`** wire the additive SP-0 surface (FC event schemas, contract content-schemas, command catalog, `PRIMARY_SELECTED`, the eight authz rows incl. the additive `reject_intent` service row + the FC-status checkpoint) — mirroring SP-1's `register_overlay`/`seed_overlay_authz`, rewriting no existing SP-0 row.
- **Definition mode** end-to-end (`declined_card_auth_count_90d`): intent → Draft (`NEEDS_CLARIFICATION`) → one clarification → refinement → MCV → Gate #1 → **CONFIRMED** contract with the exact tagged `calculation_method`, `requires_independent_validation=false`, the frozen `CONFIRMED_CONTRACT` SP-3 consumes, and every LLM call event-sourced with a **replayable redacted input**.
- **Request-owner guard**: a *different* `data_scientist` (admitted by coarse authz) is **denied** at clarify **and** confirm and **security-audited**, never counted; the true author proceeds.
- **Prohibited class**: a `blocked_data_classes` intent is blocked as `PROHIBITED_DATA_CLASS` — platform/service-issued `reject_intent` → `RUN_REJECTED`, matched class + catalog version stamped, and **zero** payloads dispatched to the LLM.
- **Hypothesis mode**: the stub's single call yields candidate documents; the requester's Gate #1 pick promotes via document `PRIMARY_SELECTED`, records `selected_candidate`/`rejected_candidates`, and confirms with `requires_independent_validation=true`.
