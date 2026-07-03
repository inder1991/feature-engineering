"""SP-2 intake command hub (mirrors SP-1's `overlay/commands.py`): the collaborator-seam accessors
the handlers read, the R1 feature_contract append helper, and the idempotent command registrar.

R10: the LLM / redactor / catalog collaborator seams are the CANONICAL module-globals owned by P3
(`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported and
re-exported here, NEVER redefined. R1: `append_fc_event` is `intake.store.append_feature_contract_event`
imported verbatim (aliased), NOT a local redefinition. Phase 4 owns ONLY a Phase-4-local override of
P2's pure `classify_intent` (`register_intake_classifier`/`_current_classifier`/`reset_intake_seams`)
so a test can pin the banking outcome deterministically."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from featuregen.aggregates._append import (
    append,
    current_version,
    provenance_for,
    table_version_for,
)
from featuregen.aggregates.request_aggregate import (
    create_request_command,
    create_run_command,
)
from featuregen.aggregates.run_lifecycle import (
    park_command,
    reject_command,
    run_is_terminal,
    withdraw_command,
)
from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import (
    Command,
    CommandResult,
    ConcurrencyError,
    DbConn,
    EventEnvelope,
    IdentityEnvelope,
)
from featuregen.contracts.documents import Stage
from featuregen.contracts.envelopes import GateTaskSpec, NewDocument
from featuregen.documents.draft import (
    INTAKE_MODES,
    RAW_INPUT_CLASSIFICATIONS,
    UNKNOWN,
    validate_draft,
)
from featuregen.documents.primary import new_primary_selected
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.documents.store import append_document, compute_content_hash, get_document
from featuregen.events import append_event
from featuregen.events.store import load_stream
from featuregen.gates.tasks import (
    cancel_tasks_on_run_advance,
    open_task,
    submit_human_signal,
)
from featuregen.idgen import mint_id
from featuregen.intake.banking_catalog import (
    IntakeClassification,
    IntakeOutcome,
    classify_intent,
)
from featuregen.intake.blobs import read_blob, write_blob  # F1 write-once blob store (P1-b/P2-c)
from featuregen.intake.candidates import (  # R10 hypothesis seam (P6, candidates.py)
    generate_candidates_for_run,
)
from featuregen.intake.catalog import (  # R8/R10 seam (P2, catalog.py)
    IntakeCatalogNotConfigured,
    current_intake_catalog,
)
from featuregen.intake.contract import (  # R6/R7 (P2, contract.py)
    CONTRACT_SCHEMA_OWNER,
    assemble_confirmed,
    validate_semantics,
)
from featuregen.intake.critique import apply_critique, contract_review  # P5 challenger seam
from featuregen.intake.doubt_router import default_thresholds, route_draft  # P5 routing seam
from featuregen.intake.events import (
    CANDIDATES_GENERATED,
    CLARIFICATION_ANSWERED,
    CLARIFICATION_REQUESTED,
    CONTRACT_CONFIRMED,
    CONTRACT_REFINED,
    DRAFT_CONTRACT_PRODUCED,
    FIELD_AUTO_RESOLVED,
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    MINIMUM_CONTRACT_VALIDATED,
    USE_CASE_ONBOARDING_GATE,
    USE_CASE_ONBOARDING_REQUESTED,
)
from featuregen.intake.llm import (  # R10 seam (P3, llm.py)
    STATUS_FAILED,
    LLMRequest,
    call_llm,
    current_llm_client,
)
from featuregen.intake.mcv import (  # P5 pre-gate checklist (Task 5.4)
    _is_unknown,
    _latest_body,
    minimum_contract_validated,
)
from featuregen.intake.redaction import (  # R10 seam (P3, redaction.py)
    INPUT_KEY_CATALOG,
    INPUT_KEY_CLASSIFICATION,
    INPUT_KEY_REDACTION_VERSION,
    REDACTION_VERSION,
    EgressViolation,
    _first_pii,
    build_llm_inputs,
    current_intent_redactor,
)
from featuregen.intake.scoring import score_fields  # P5 per-field scoring (Task 5.1)

# R3/R4: the ONE feature_contract fold + the ONE request-owner predicate are owned by P2 (intake.state);
# refine / answer_clarification derive the request owner from state.requester (the INTENT_SUBMITTED
# actor.subject) via actor_is_request_owner — never a payload key.
from featuregen.intake.state import (
    TERMINAL_STATUSES,
    FeatureContractState,
    FeatureContractStatus,
    actor_is_request_owner,
    confirmer_is_requester_human,
    fold_feature_contract_state,
)
from featuregen.intake.store import (
    append_feature_contract_event,  # R1 seam (unaliased) — reject_intent's CAS append (X4); monkeypatch target
    load_feature_contract,
)
from featuregen.intake.store import (  # R1 seam (P1, store.py)
    append_feature_contract_event as append_fc_event,
)
from featuregen.privacy.classification import InlinePIIError, assert_no_inline_pii
from featuregen.security.audit import (
    record_denial,  # R15 — SoD/owner-guard denials → security stream
)

__all__ = [
    "IntakeError",
    "register_intake_classifier",
    "reset_intake_seams",
    "register_sp2_commands",
    # Task 4.2 pure body assemblers + their platform-owned constants.
    "DRAFT_STATUS",
    "DRAFT_SCHEMA_VERSION",
    "assemble_ledger_body",
    "assemble_draft_body",
    # Task 4.3 no-silent-assumption rule (§5.3).
    "NoSilentAssumptionError",
    "assert_no_silent_assumption",
    # Task 4.4 the first intake command handler (definition-mode CLEAR happy path).
    "submit_intent",
    # Task 5.5 — Human Clarification task + the bounded Contract Refinement Loop (§6.5, §6.6).
    "freeze_draft",
    "open_clarification_task",
    "refine_contract",
    # Task 5.6 — the human's answer to a Clarification gate task (request-owner guard → drives the loop).
    "answer_clarification",
    "RefineResult",
    "MAX_REFINEMENT_ROUNDS",
    "IntakeDeps",
    "register_intake_deps",
    "current_intake_deps",
    # re-exported collaborator seams (R10) + R1 append/load — the handlers added by later Phase-4
    # tasks read these off this module; consumers import them from here.
    "append_fc_event",
    "load_feature_contract",
    "current_llm_client",
    "current_intent_redactor",
    "current_intake_catalog",
    "classify_intent",
    "IntakeClassification",
    "append",
    "DbConn",
    "EventEnvelope",
    "IdentityEnvelope",
    # Task 8.2 — inline lifecycle guards (fold-companion predicates the SP-2 handlers run BEFORE append).
    # confirmer_is_requester_human is owned by intake.state (imported, not redefined here).
    "open_fields_empty",
    "guard_advance",
    # Task 8.3 — the standalone post-intake platform/service terminal reject (X5; wired into
    # _SP2_CATALOG in Task 8.7, not here).
    "reject_intent",
    # Task 8.4 — the requester's own abandonment reusing SP-0 `withdraw` (RUN_WITHDRAWN); wired into
    # _SP2_CATALOG + given its own ("withdraw_intent",...,"data_scientist","human") authz row in Task 9.1
    # (bootstrap._SP2_POLICY_ROWS) so a requester can dispatch it via execute_command.
    "withdraw_intent",
    # Task 9.2a — the thin production driver connecting a Draft to (clarification | MCV) → Gate #1.
    "advance_intake",
]


class IntakeError(Exception):
    """Raised on intake command misconfiguration."""


# ── Task 8.2 — inline lifecycle guards (fold-companion predicates, §8.2/§11) ───────────────────────
# Pure, deterministic functions of the FOLDED FeatureContractState that every advancing SP-2 handler
# (P8's reject_intent / withdraw_intent, etc.) evaluates BEFORE appending — the direct analogue of
# `overlay/confirmation_commands.py`'s `if state.status not in _AWAITING_CONFIRMATION: return DENY(...)`.
# The no-regression check reads P2's `FeatureContractState.is_terminal` property and composes P2's
# `actor_is_request_owner` (both imported from intake.state, R3/R4) — neither is redefined here.


def open_fields_empty(state: FeatureContractState) -> bool:
    """The Gate-#1 hard invariant (§11): a run with a non-empty open_fields can never advance."""
    return len(state.open_fields) == 0


def guard_advance(
    state: FeatureContractState,
    allowed_from: tuple[FeatureContractStatus, ...],
) -> str | None:
    """The inline lifecycle guard every advancing handler runs BEFORE appending (§11), mirroring
    `overlay/confirmation_commands.py`. Returns a deny reason, or None to proceed. Enforces (a) an
    opened contract, (b) NO-REGRESSION — a terminal/confirmed fold refuses a conflicting re-advance,
    read from the P2 `FeatureContractState.is_terminal` property — and (c) the allowed source status."""
    if state.status is None:
        return "no feature contract for this run"
    if state.is_terminal:
        return f"contract already terminal (status={state.status.value})"
    if state.status not in allowed_from:
        return f"illegal advance from {state.status.value}"
    return None


# ── Task 8.3 — reject_intent: the standalone, POST-INTAKE platform/service terminal (§5.4, §8.4, §11) ─
# The STANDALONE platform/service-issued terminal outcome for OUT_OF_SCOPE / PROHIBITED_DATA_CLASS
# (Decision D13): append INTENT_REJECTED on the feature_contract (folding the status to the matching
# terminal) then drive SP-0's RUN_REJECTED via `run_lifecycle.reject_command`. X5 — this is DISTINCT
# from submit_intent's INTAKE-TIME terminal reject (`_do_reject_intent`, which submit_intent appends
# ITSELF): reject_intent covers rejections determined AFTER intake (the §8.4 Gate-#1 re-screen after
# MCV, or a boundary call while still in clarification). P8 owns it; P4 does NOT call it. SP-2 NEVER
# touches SP-0's validator-only `reject` action.
_REJECTABLE_FROM: tuple[FeatureContractStatus, ...] = (
    FeatureContractStatus.NEEDS_CLARIFICATION,
    FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED,
)
_REJECT_CLASSIFICATIONS = ("OUT_OF_SCOPE", "PROHIBITED_DATA_CLASS")


def reject_intent(conn: DbConn, cmd: Command) -> CommandResult:
    """Standalone, POST-INTAKE platform/service rejection (§5.4, §8.4, §11; X5). Folds the FC status,
    runs the inline no-regression guard, appends INTENT_REJECTED (X4 — CAS-pinned to the folded head)
    so the fold advances to the matching terminal, then drives SP-0's RUN_REJECTED. Idempotent by
    no-regression: a re-reject of an already-terminal contract is denied (never double-charged). NOT
    called by P4 — `submit_intent` appends the intake-time INTENT_REJECTED itself."""
    args = cmd.args
    run_id = args["run_id"]
    classification = args.get("classification")
    if classification not in _REJECT_CLASSIFICATIONS:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"reject_intent classification must be one of {_REJECT_CLASSIFICATIONS}, "
                          f"got {classification!r}",
        )
    stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(stream)
    head_version = stream[-1].stream_version if stream else 0  # X4 — the folded head, captured at fold time
    deny = guard_advance(state, _REJECTABLE_FROM)
    if deny is not None:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=deny)
    # Deny-before-append (X5 discipline): `reject_command` below itself denies "run already terminal"
    # (run_lifecycle.py:18-21) when the run was made terminal OUT-OF-BAND (e.g. RUN_WITHDRAWN /
    # RUN_CANCELLED, which write NO fc event) while the FC is still non-terminal. Appending
    # INTENT_REJECTED first would then orphan an fc terminal on a differently-terminal run (the paired
    # RUN_REJECTED never fires). Hoist the run-terminality deny BEFORE the fc append so no orphan is ever
    # written — matching submit_intent / _do_reject_intent, which never deny after an append.
    if run_is_terminal(conn, run_id):
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="run already terminal")

    payload = {"run_id": run_id, "classification": classification,
               "catalog_version": args["catalog_version"]}
    if args.get("reason") is not None:
        payload["reason"] = args["reason"]
    if args.get("matched_class") is not None:
        payload["matched_class"] = args["matched_class"]
    # X4 (GLOBAL CONSTRAINT / SP-1 capstone C2): CAS the append on the folded head. SP-0's `append`
    # treats expected_version=None as "current head at append time" (aggregates/_append.py:76), so a
    # stale fold + this append could otherwise commit AFTER a concurrent transition. A raised
    # ConcurrencyError => the stream advanced between fold and append => deny `stale` (no RUN_REJECTED).
    try:
        rejected = append_feature_contract_event(
            conn, run_id=run_id, type=INTENT_REJECTED, payload=payload, actor=cmd.actor,
            expected_version=head_version,  # X4 CAS pin to the folded head (§12)
        )
    except ConcurrencyError:
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="stale: contract advanced concurrently")
    # Drive SP-0's run terminal via the existing lifecycle handler (NOT the validator-only `reject`
    # action): `reject_command` reads cmd.aggregate_id (run_id) + cmd.args["reason"] and checks run
    # terminality itself.
    run_cmd = replace(cmd, aggregate="run", aggregate_id=run_id,
                      args={"reason": args.get("reason") or classification})
    run_res = reject_command(conn, run_cmd)
    if not run_res.accepted:
        return run_res
    return CommandResult(
        accepted=True, aggregate_id=run_id,
        produced_event_ids=(rejected.event_id, *run_res.produced_event_ids),
    )


# ── Task 8.4 — withdraw_intent: the requester's OWN abandonment reuses SP-0 `withdraw` (§11, §13) ────
def withdraw_intent(conn: DbConn, cmd: Command) -> CommandResult:
    """Requester-initiated abandonment (§11, §13, Decision D13). Reuses SP-0's data-scientist-owned
    `withdraw` (→ RUN_WITHDRAWN) behind SP-2's request-owner guard — NOT the validator-only `reject`.
    Human + data_scientist + request-owner only; appends NO feature_contract event (withdrawal is a
    run-level terminal). Adds no authz row: it delegates to SP-0's existing `withdraw` capability.
    X4: it folds FC state to decide, so it refolds immediately before the delegated run append and
    denies `stale` if the FC head advanced concurrently."""
    args = cmd.args
    run_id = args["run_id"]
    if cmd.actor.actor_kind != "human":
        return _deny_audited(conn, cmd, run_id, "withdraw requires a human requester")
    if "data_scientist" not in cmd.actor.role_claims:
        return _deny_audited(conn, cmd, run_id, "withdraw requires the data_scientist role")
    stream = load_feature_contract(conn, run_id)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="no feature contract for this run")
    state = fold_feature_contract_state(stream)
    head_version = stream[-1].stream_version    # X4 — the folded head, captured at gate time
    if state.is_terminal:                       # P2 FeatureContractState property (R3)
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason=f"contract already terminal (status={state.status.value})")
    if not actor_is_request_owner(state, cmd.actor):
        return _deny_audited(conn, cmd, run_id,
                             "actor is not the request owner; withdrawal is requester-initiated")
    # X4/C2 refold-before-append: `withdraw_intent` emits no feature_contract event, so there is no FC
    # append to CAS. Instead confirm the FC head has NOT advanced since the owner/terminal gate — a
    # concurrent transition (e.g. CONTRACT_CONFIRMED) must not let a stale fold withdraw the run.
    if load_feature_contract(conn, run_id)[-1].stream_version != head_version:
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="stale: contract advanced concurrently")
    run_cmd = replace(
        cmd, aggregate="run", aggregate_id=run_id,
        args={"reason": args.get("reason", "requester withdrew intent")},
    )
    try:
        return withdraw_command(conn, run_cmd)   # SP-0's own run-stream OCC; a lost race → stale
    except ConcurrencyError:
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="stale: run advanced concurrently")


# ── Phase-4-local classifier override (NOT a shared seam) ─────────────────────────────────────
# R10: the LLM / redactor / catalog collaborator seams are the canonical module-globals owned by
# P3 (`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported
# above, NEVER redefined here (Phase 9's `_wire` composition root wires all four in production —
# NOT register_sp2, which is conn-less schema/catalog registration only; tests wire stubs via the
# same `register_*` functions). Phase 4 keeps ONLY a local override of P2's pure
# `classify_intent` so a test can pin the banking outcome deterministically.
_CLASSIFIER = None  # None ⟹ production default `classify_intent`


def register_intake_classifier(fn) -> None:
    global _CLASSIFIER
    _CLASSIFIER = fn


def _current_classifier():
    return _CLASSIFIER if _CLASSIFIER is not None else classify_intent


def reset_intake_seams() -> None:
    global _CLASSIFIER
    _CLASSIFIER = None


# ── feature_contract append path (R1) ─────────────────────────────────────────────────────────
# `append_fc_event` is the R1 seam imported (aliased) from `intake.store`; it sets
# aggregate="feature_contract", aggregate_id=run_id, feature_contract_id=run_id. Phase 4 does NOT
# define its own append helper and threads only `run_id` (feature_contract_id == run_id).
# X4 (CAS on the folded head): `INTENT_SUBMITTED` opens the brand-new stream at expected_version=0;
# every later fc append passes expected_version=_fc_head(conn, run_id) (the folded head re-loaded
# right before the append, which includes `call_llm`'s interleaved `LLM_CALL_RECORDED`) and treats a
# raised ConcurrencyError as a `stale` denial — never expected_version=None (`aggregates/_append.py:76`
# treats None as "current head at append time", the lost-update hazard X4 removes).


# `_SP2_CATALOG` is a TUPLE of (action, handler) pairs (mirrors SP-0's `_CATALOG`); Phase 4 appends only
# `submit_intent` (Task 4.4). Later phases extend commands.py with their own handlers — P5/P6/P7 add
# answer_clarification / select_candidate_doc / confirm_contract / request_edit, and P8 adds the
# standalone `reject_intent` (X5 — NOT Phase 4). The tuple is ASSIGNED at the END of this module,
# after `submit_intent` is defined; `register_sp2_commands` reads the module-global at call time.


def register_sp2_commands() -> None:
    """Idempotent (mirrors SP-1's `register_overlay_commands`): `register_command` raises on a
    duplicate and the command registry persists across tests, so skip already-registered actions."""
    for action, handler in _SP2_CATALOG:
        try:
            get_command(action)
        except KeyError:
            register_command(action, handler)


# ── pure body assemblers (Task 4.2) ───────────────────────────────────────────────────────────
# PURE: no DB / no LLM call (that is Task 4.4's `submit_intent`). These build the DRAFT_CONTRACT and
# ASSUMPTION_LEDGER bodies from the normalized LLM output; the platform — NOT the model — owns the
# SP-0 envelope. Outputs conform to Task-2.1's schemas and pass Task-2.2's `validate_semantics`.
DRAFT_STATUS = "NEEDS_CLARIFICATION"
DRAFT_SCHEMA_VERSION = 1


def assemble_ledger_body(*, request_id: str, assumptions: list[dict]) -> dict:
    """Build the ASSUMPTION_LEDGER body (§4.3). The top-level array is `assumptions` (SP-0's required
    name, R9); each item keeps SP-0's required `field`/`value`/`rationale` and adds the SP-2 semantic
    extras + a stamped `auto_resolved_at`. `source` defaults to `llm` and `auto_resolved_at` is
    stamped now when the model omitted them."""
    stamped = datetime.now(UTC).isoformat()
    items = []
    for a in assumptions:
        item = {
            "field": a["field"],
            "value": a["value"],
            "rationale": a["rationale"],
            "source": a.get("source", "llm"),
            "auto_resolved_at": a.get("auto_resolved_at", stamped),
        }
        # ambiguity/confidence are optional numeric extras (§4.3): the content-schema types them as
        # `number`, so a null must be OMITTED — never stamped as None — to stay schema-valid when the
        # model supplied only SP-0's required field/value/rationale.
        for extra in ("ambiguity", "confidence"):
            if a.get(extra) is not None:
                item[extra] = a[extra]
        items.append(item)
    return {"request_id": request_id, "assumptions": items}


def assemble_draft_body(
    *,
    request_id: str,
    intake_mode: str,
    raw_input_ref: str,
    raw_input_classification: str,
    assumption_ledger_ref: str,
    llm_output: dict,
    llm_call_ref: str,
    risk_flags: list[str] | None = None,
    product: str | None = None,
    region: str | None = None,
) -> dict:
    """Build the DRAFT_CONTRACT body (§4.1) from the LLM's semantic subset + the authoritative SP-0
    envelope. The platform owns the envelope: `request_id`, `raw_input_ref`,
    `raw_input_classification`, `assumption_ledger_ref`, `risk_flags`, and `status` are set here, NEVER
    taken from the model — any echoed envelope field is discarded (the no-silent-boundary for the
    envelope). `risk_flags` (P1-d) is computed platform-side from the intake classification (see
    `_risk_flags_for`) → carried through refinement/edit → read by `_requires_independent_validation` at
    Gate #1. Only the semantic subset (`proposed_feature_name`, `feature_semantics`, `field_scores`,
    `open_fields`, `open_questions`) is read from `llm_output`."""
    return {
        "request_id": request_id,
        "intake_mode": intake_mode,
        "raw_input_ref": raw_input_ref,
        "raw_input_classification": raw_input_classification,
        "proposed_feature_name": llm_output["proposed_feature_name"],
        "feature_semantics": llm_output["feature_semantics"],
        "field_scores": llm_output.get("field_scores", {}),
        "open_fields": list(llm_output.get("open_fields", [])),
        "open_questions": list(llm_output.get("open_questions", [])),
        "assumption_ledger_ref": assumption_ledger_ref,
        "risk_flags": list(risk_flags or []),
        # P2-b/F6 — the scoped-use-case context, carried so the confirm-time §8.4 re-screen can pass the
        # intent's product/region (assemble_draft_body previously dropped them → re-screen ran None/None).
        "product": product,
        "region": region,
        "provenance": {"llm_call_refs": [llm_call_ref], "schema_version": DRAFT_SCHEMA_VERSION},
        "status": DRAFT_STATUS,
    }


# ── no-silent-assumption rule (Task 4.3, §5.3) ────────────────────────────────────────────────
class NoSilentAssumptionError(IntakeError):
    """Raised when a Draft carries an inferred field that is neither an open question nor a
    recorded Assumption Ledger entry (§5.3 — no field is silently settled)."""


def assert_no_silent_assumption(draft_body: dict, ledger_body: dict) -> None:
    """§5.3, enforced deterministically at Draft production. Every field the agent did not take
    verbatim must be surfaced — either as an open question (unresolved) or a ledger entry
    (auto-recorded). There is no third option."""
    open_fields = set(draft_body.get("open_fields", []))
    open_q_fields = {q["field"] for q in draft_body.get("open_questions", [])}
    ledger_fields = {a["field"] for a in ledger_body.get("assumptions", [])}

    # (1) every open field is backed by an open question
    for f in open_fields:
        if f not in open_q_fields:
            raise NoSilentAssumptionError(
                f"open field {f!r} has no matching open_question (§5.3)"
            )

    # (2) every inferred (non-verbatim) field is accounted — in the ledger or in open_fields
    for field, score in draft_body.get("field_scores", {}).items():
        if score.get("source") not in ("default", "catalog"):
            continue  # a verbatim/model-grounded reading (source == "llm") needs no accounting
        accounted = (
            field in ledger_fields
            or field in open_fields
            or any(of == field or of.startswith(field + ".") for of in open_fields)
        )
        if not accounted:
            raise NoSilentAssumptionError(
                f"inferred field {field!r} (source={score.get('source')!r}) is neither in the "
                f"Assumption Ledger nor an open field (§5.3)"
            )


# ── submit_intent — the first intake command handler (Task 4.4, §5.2) ──────────────────────────
# Definition-mode, in-scope/CLEAR happy path: classify → redact → structure_intent (call_llm) →
# assemble Draft + Assumption Ledger → enforce §5.3 → freeze both governance-retained documents →
# append INTENT_SUBMITTED then DRAFT_CONTRACT_PRODUCED on the feature_contract aggregate. The banking
# terminal-reject / onboarding-park branches return clearly-marked placeholders here (hardened in
# Tasks 4.5–4.7); the CLEAR / *_CLARIFY paths are complete.
PROMPT_STRUCTURE_INTENT_ID = "sp2.structure_intent"
PROMPT_STRUCTURE_INTENT_VERSION = 1
OUTPUT_SCHEMA_ID = "DRAFT_CONTRACT"
OUTPUT_SCHEMA_VERSION = 1
_GEN_SETTINGS = {"provider": "fake", "model": "fake", "thinking": "adaptive", "max_tokens": 4096}


def _fc_head(conn: DbConn, run_id: str) -> int:
    """The folded head `stream_version` of the `feature_contract` aggregate (0 for a brand-new/empty
    stream), captured IMMEDIATELY before a CAS append (X4 — the Global-Constraints lost-update guard).
    Re-loading here naturally accounts for `call_llm`'s interleaved `LLM_CALL_RECORDED`. Every fc
    append after the opening `INTENT_SUBMITTED` passes this as `expected_version`; a raised
    `ConcurrencyError` is denied as `stale` (never `expected_version=None`)."""
    stream = load_feature_contract(conn, run_id)
    return stream[-1].stream_version if stream else 0


def _scan_text_classification(text: str) -> str:
    """Single source of truth for classifying ANY un-vetted free-text ingress → `contains_pii` |
    `clean`. Scans with BOTH the SP-2 shared redaction pattern set (email/SSN/PAN/phone/IBAN/account/
    DOB/address — via `_first_pii`) AND SP-0's inline-secret detector (`assert_no_inline_pii`). Used to
    rescan a caller-supplied `clean` (never trusted, N2) and to re-classify each clarification answer
    at its own ingress. Text we scanned is, by definition, not `unscanned`."""
    if _first_pii(text) is not None:
        return "contains_pii"
    try:
        assert_no_inline_pii({"intent": text})
    except InlinePIIError:
        return "contains_pii"
    return "clean"


def _classify_raw_input(text: str, provided: str | None) -> str:
    """Determine the SP-0 envelope `raw_input_classification`. Ingest may supply it; otherwise scan
    (`_scan_text_classification`) → clean | contains_pii. `unscanned` is only ever caller-supplied
    (an intent no scanner touched).

    N2 (SP-2): a caller-supplied `clean` is NOT trusted — we rescan the raw text and, if a PII pattern
    hits, override to `contains_pii` so redaction actually runs (or fails closed) instead of a
    mislabelled `clean` bypassing the redactor. `contains_pii`/`unscanned` are honoured as-is (they
    only tighten, never loosen)."""
    if provided is not None:
        if provided not in RAW_INPUT_CLASSIFICATIONS:
            raise IntakeError(f"invalid raw_input_classification: {provided!r}")
        # Never trust a caller `clean`: a PII hit reclassifies so the redactor runs / fails closed.
        if provided == "clean" and _scan_text_classification(text) == "contains_pii":
            return "contains_pii"
        return provided
    return _scan_text_classification(text)


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _emit_document(
    conn: DbConn,
    *,
    stage: str,
    body: dict,
    run_id: str,
    request_id: str,
    actor: IdentityEnvelope,
    derived_from: tuple[str, ...] = (),
    supersedes: tuple[str, ...] = (),
) -> str:
    """Emit one frozen, content-hashed governance-retained document on the run's DAG (§3.4). The body
    itself rides the DRAFT_CONTRACT_PRODUCED event for replay AND is written to the write-once blob
    store keyed by `body_ref`, so the document's `body_ref` is durably resolvable via read_blob (F1) —
    the body is no longer a minted-but-unwritten ref. The document carries the content hash for
    lineage/integrity (the body is never inlined on the document row). A revised Draft `supersedes`
    the prior one (§3.4) so the full refinement history is retained on the DAG."""
    doc_id = mint_id("doc")
    body_ref = mint_id("blob")
    write_blob(conn, body_ref, body)  # durable body: read_blob(body_ref) round-trips the exact body
    append_document(
        conn,
        NewDocument(
            doc_id=doc_id,
            stage=stage,
            schema_version=DRAFT_SCHEMA_VERSION,
            branch_role="primary",
            content_hash=compute_content_hash(_canonical(body)),
            body_classification="governance-retained",
            provenance=provenance_for(stage),
            body_ref=body_ref,
            derived_from=derived_from,
            supersedes=supersedes,
        ),
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )
    return doc_id


# N6 — every human task carries an SLA so SP-0's timer ladder (reminder at sla/2, escalation at sla*1.5,
# auto-park at sla*2) ARMS. Without an sla, open_task schedules no timers and a task can stall forever.
_HUMAN_TASK_SLA = "3d"        # clarification / Gate #1 confirmation / fail-closed manual review
_ONBOARDING_TASK_SLA = "10d"  # use-case governance onboarding legitimately takes longer


def _open_clarification_task(conn: DbConn, *, run_id: str, actor: IdentityEnvelope) -> str:
    """Open a human CLARIFICATION gate task (Task 4.6 pattern) and return its real `task_id` — the
    CLARIFICATION_REQUESTED schema requires it. Shared by the fail-closed manual path and the
    non-terminal sensitive-proxy / ambiguous routing."""
    return open_task(
        conn,
        GateTaskSpec(
            gate="CLARIFICATION", required_inputs=(), eligible_assignees={"role": "intake_reviewer"},
            allowed_responses=("clarify",), run_id=run_id, delegation_allowed=True, sla=_HUMAN_TASK_SLA,
        ),
        actor,
    )


def _fail_into_clarification(
    conn: DbConn, *, run_id: str, request_id: str | None, actor: IdentityEnvelope, reason: str,
) -> EventEnvelope:
    """Fail-closed manual path (§9.4 redaction / §9.2 exhausted structure). No payload was dispatched
    and no Draft is frozen — a human handles it via the clarification path. Opens a CLARIFICATION gate
    task and threads its real `task_id` (the CLARIFICATION_REQUESTED schema requires it). R1: threads
    only run_id (feature_contract_id == run_id); R2: no id fields in the payload."""
    task_id = _open_clarification_task(conn, run_id=run_id, actor=actor)
    return append_fc_event(  # R1 seam — feature_contract_id == run_id, thread only run_id
        conn, run_id=run_id, type=CLARIFICATION_REQUESTED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
        payload={  # R2 — no aggregate-id fields; task_id is the schema-required gate-task ref
            "task_id": task_id,
            "field": "raw_intent",
            "question": f"the intent could not be safely {reason}; please restate without sensitive "
                        f"data or specify the feature directly",
            "kind": f"{reason}_failed", "routed_to": "human", "blocks_progress": True,
        },
        actor=actor, request_id=request_id,
    )


def _emit_banking_clarification(
    conn: DbConn, *, run_id: str, request_id: str | None, actor: IdentityEnvelope,
    classification: IntakeClassification,
) -> EventEnvelope:
    """Non-terminal sensitive-proxy / ambiguous routing (§5.4 outcomes 3–4). A doubt to be reviewed,
    never a block — the Draft is still produced; this records the compliance-review / disambiguation
    need on a CLARIFICATION gate task (the full clarification task machinery is Phase 5)."""
    task_id = _open_clarification_task(conn, run_id=run_id, actor=actor)
    return append_fc_event(  # R1 seam — feature_contract_id == run_id, thread only run_id
        conn, run_id=run_id, type=CLARIFICATION_REQUESTED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
        payload={  # R2 — no aggregate-id fields.
            "task_id": task_id, "field": "banking_scope",
            "question": classification.reason or "requires clarification / compliance review",
            "kind": f"banking_{classification.outcome.value.lower()}",
            "routed_to": "human", "blocks_progress": False,
        },
        actor=actor, request_id=request_id,
    )


def _produce_draft(
    conn: DbConn,
    *,
    cmd: Command,
    run_id: str,
    request_id: str,
    intent_text: str,
    intake_mode: str,
    raw_input_ref: str,
    raw_input_classification: str,
    classification: IntakeClassification,
    product: str | None,
    region: str | None,
    produced: list,
) -> CommandResult:
    """Redact → structure_intent → Draft + Assumption Ledger (§5.2), fail-closed at both the redaction
    egress and the structured-output boundary (§9.2/§9.4). The LLM sees only redacted, LLM-safe text +
    catalog metadata (never raw data / PII); every inferred field is accounted (§5.3) before the Draft
    is frozen. Non-terminal sensitive-proxy / ambiguous outcomes still produce the Draft and append a
    compliance-review clarification (§5.4 #3–#4)."""
    # 1. Redact — fail closed on `unscanned` / un-redactable (§9.4): no payload is ever dispatched and
    #    no Draft is frozen; a human handles it via the clarification path.
    redactor = current_intent_redactor()  # R10 seam (P3, redaction.py)
    redaction = redactor.redact(intent_text, raw_input_classification)
    if redaction.text is None or redaction.disposition != "ok":
        clar = _fail_into_clarification(
            conn, run_id=run_id, request_id=request_id, actor=cmd.actor, reason="redacted"
        )
        produced.append(clar.event_id)
        return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))

    # 2. Structure the intent through the event-sourced, egress-guarded LLM wrapper. build_llm_inputs
    #    keeps the §9.4 redaction_version / input_redaction egress-guard fields (never the raw intent).
    #    N3 — ground the FIRST normalization in the SP-1 merged-view catalog metadata (names/types/grain,
    #    LLM-safe — never values/PII), exactly as the refinement + candidate-generation paths do; the
    #    catalog NAMES ride catalog_metadata separately and are re-scanned by the egress guard.
    deps = current_intake_deps()
    catalog_metadata = dict(deps.catalog.metadata()) if deps and deps.catalog else {}
    inputs = build_llm_inputs(  # reserved-keyed, LLM-safe (§9.4) — guaranteed-safe past the check above
        redaction, catalog_metadata=catalog_metadata, raw_input_classification=raw_input_classification
    )
    request = LLMRequest(
        task="structure_intent",
        prompt_id=PROMPT_STRUCTURE_INTENT_ID,
        prompt_version=PROMPT_STRUCTURE_INTENT_VERSION,
        inputs=inputs,
        output_schema_id=OUTPUT_SCHEMA_ID,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        generation_settings=_GEN_SETTINGS,
    )
    result = call_llm(conn, current_llm_client(), request, run_id=run_id, actor=cmd.actor)  # R10 seam
    # 2b. LLM fail-closed (§9.2): exhausted repair / refusal / non-retryable → clarification, NO Draft.
    if result.status == STATUS_FAILED:
        clar = _fail_into_clarification(
            conn, run_id=run_id, request_id=request_id, actor=cmd.actor, reason="structured"
        )
        produced.append(clar.event_id)
        return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))
    out = result.output

    ledger_body = assemble_ledger_body(request_id=request_id, assumptions=out.get("assumptions", []))
    ledger_doc = _emit_document(
        conn, stage=Stage.ASSUMPTION_LEDGER.value, body=ledger_body,
        run_id=run_id, request_id=request_id, actor=cmd.actor,
    )
    # P1-d — compute risk_flags platform-side from the intake classification + the catalog's declared
    # high-risk set (never the LLM), so a high-risk use-case sets requires_independent_validation at
    # Gate #1. Fail-closed: an unset catalog yields no flag (RIV stays False), never a fabricated pass.
    try:
        _catalog = current_intake_catalog()
    except IntakeCatalogNotConfigured:
        _catalog = None
    draft_body = assemble_draft_body(
        request_id=request_id, intake_mode=intake_mode, raw_input_ref=raw_input_ref,
        raw_input_classification=raw_input_classification, assumption_ledger_ref=ledger_doc,
        llm_output=out, llm_call_ref=result.call_ref,
        risk_flags=_risk_flags_for(classification, _catalog),
        product=product, region=region,
    )
    assert_no_silent_assumption(draft_body, ledger_body)  # §5.3 — no field silently settled
    validate_draft(draft_body)                            # SP-0 envelope + required-field validation
    validate_semantics(draft_body, stage="DRAFT_CONTRACT")  # R6 — raises ContractSemanticError
    draft_doc = _emit_document(
        conn, stage=Stage.DRAFT_CONTRACT.value, body=draft_body,
        run_id=run_id, request_id=request_id, actor=cmd.actor, derived_from=(ledger_doc,),
    )
    produced_evt = append_fc_event(
        conn, run_id=run_id, type=DRAFT_CONTRACT_PRODUCED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head (incl. interleaved LLM_CALL_RECORDED)
        payload={
            # R12 standardized doc-ref keys; R2 — NO run_id/request_id id fields in the payload.
            "draft_doc_id": draft_doc,
            "assumption_ledger_ref": ledger_doc,
            "catalog_version": classification.catalog_version,
            "intake_mode": intake_mode,
            "open_fields": list(draft_body.get("open_fields", [])),
            "draft_body": draft_body,               # Phase-8 read model replays the frozen body
            "assumption_ledger_body": ledger_body,
            "status": DRAFT_STATUS,
        },
        actor=cmd.actor, request_id=request_id,
        provenance=provenance_for(Stage.DRAFT_CONTRACT.value),
    )
    produced.append(produced_evt.event_id)

    # 4. Non-terminal sensitive-proxy / ambiguous outcomes also raise a clarification (§5.4 #3–#4):
    #    the Draft stands; the clarification records the compliance-review / disambiguation need.
    if classification.outcome in (
        IntakeOutcome.SENSITIVE_PROXY_CLARIFY, IntakeOutcome.AMBIGUOUS_CLARIFY
    ):
        clar = _emit_banking_clarification(
            conn, run_id=run_id, request_id=request_id, actor=cmd.actor, classification=classification
        )
        produced.append(clar.event_id)
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))


def _do_reject_intent(
    conn: DbConn, *, run_id: str, request_id: str | None, actor: IdentityEnvelope,
    classification: str, catalog_version: str | None, reason: str | None, matched_class: str | None,
) -> list[str]:
    """The INTAKE-TIME, platform/service-issued deterministic rejection (§5.4, §13, X5). INTENT_REJECTED
    (fc) carries the classification + catalog version (+ matched class); RUN_REJECTED (SP-0 run) makes the
    run terminal (X8). X4: the fc append is CAS-guarded on the folded head. NOT SP-0's validator-only
    `reject`, and NOT the standalone P8 `reject_intent` command — submit_intent appends this ITSELF."""
    rej_fc = append_fc_event(
        conn, run_id=run_id, type=INTENT_REJECTED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
        payload={
            # R2 — no id fields; run_id/request_id ride the seam kwargs.
            "classification": classification, "catalog_version": catalog_version,
            "matched_class": matched_class, "reason": reason,
        },
        actor=actor, request_id=request_id,
    )
    rej_run = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_REJECTED",
        payload={"run_id": run_id, "reason": reason or classification}, actor=actor, run_id=run_id,
    )
    return [rej_fc.event_id, rej_run.event_id]


def _park_run(
    conn: DbConn, *, run_id: str, actor: IdentityEnvelope, owner: str,
    waiting_on_fact: str | None = None,
) -> EventEnvelope:
    """Park the SP-0 `run` aggregate (RUN_PARKED). R2/strict-SP-0-schema: the payload carries ONLY
    `{run_id, owner, waiting_on_fact}` — extra keys are rejected by the RUN_PARKED CHECK. X6: an
    onboarding/fail-closed hold NEVER overloads `waiting_on_fact` (SP-1's fact-confirmed-resume key,
    run_lifecycle.py:112) — callers pass `waiting_on_fact=None`; the hold rides the fc aggregate."""
    return append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_PARKED",
        payload={"run_id": run_id, "owner": owner, "waiting_on_fact": waiting_on_fact},
        actor=actor, run_id=run_id,
    )


def _do_onboarding_park(
    conn: DbConn, *, run_id: str, request_id: str | None, actor: IdentityEnvelope, catalog_version,
) -> list[str]:
    """In-scope banking, unknown use-case → NEEDS_USE_CASE_ONBOARDING (§5.4). The folded hold-state
    rides USE_CASE_ONBOARDING_REQUESTED on the fc aggregate (§4.6); SP-2 opens the governance
    onboarding gate task and parks — the onboarding workflow itself is out of scope (§14)."""
    onb = append_fc_event(
        conn, run_id=run_id, type=USE_CASE_ONBOARDING_REQUESTED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
        payload={"catalog_version": catalog_version},  # R2 — no id fields
        actor=actor, request_id=request_id,
    )
    open_task(
        conn,
        GateTaskSpec(
            gate=USE_CASE_ONBOARDING_GATE, required_inputs=(),
            eligible_assignees={"role": "governance"}, allowed_responses=("acknowledge",),
            run_id=run_id, delegation_allowed=True, sla=_ONBOARDING_TASK_SLA,
        ),
        actor,
    )
    parked = _park_run(conn, run_id=run_id, actor=actor, owner="governance", waiting_on_fact=None)
    return [onb.event_id, parked.event_id]


def _fail_closed_park(
    conn: DbConn, *, run_id: str, request_id: str | None, actor: IdentityEnvelope,
    field: str, question: str,
) -> list[str]:
    """§4.5(b): the banking catalog is unavailable/unversioned → fail closed. Never auto-pass an
    absent classification — open a human CLARIFICATION review task, record the clarification (its
    schema-required `task_id` points at that task) and park for manual review (X6 waiting_on_fact=None)."""
    task_id = open_task(
        conn,
        GateTaskSpec(
            gate="CLARIFICATION", required_inputs=(), eligible_assignees={"role": "intake_reviewer"},
            allowed_responses=("clarify",), run_id=run_id, delegation_allowed=True, sla=_HUMAN_TASK_SLA,
        ),
        actor,
    )
    clar = append_fc_event(
        conn, run_id=run_id, type=CLARIFICATION_REQUESTED,
        expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
        payload={  # R2 — no aggregate-id fields; task_id is the schema-required gate-task ref
            "task_id": task_id, "field": field, "question": question,
            "kind": "manual", "routed_to": "human", "blocks_progress": True,
        },
        actor=actor, request_id=request_id,
    )
    parked = _park_run(conn, run_id=run_id, actor=actor, owner="intake-manual", waiting_on_fact=None)
    return [clar.event_id, parked.event_id]


def submit_intent(conn: DbConn, cmd: Command) -> CommandResult:
    """Definition-mode intake happy path (§5.2): open the SP-0 request+run, classify at the banking
    boundary, then on CLEAR / *_CLARIFY normalize into a frozen Draft + Assumption Ledger. Opens the
    feature_contract stream with INTENT_SUBMITTED (expected_version=0); every later fc append is
    CAS-guarded on the folded head (X4). Raw intent is held by reference only — never inlined (§9.4)."""
    args = cmd.args
    intent_text = args["intent_text"]
    intake_mode = args.get("intake_mode", "definition")
    if intake_mode not in INTAKE_MODES:
        return CommandResult(
            accepted=False, aggregate_id="", denied_reason=f"invalid intake_mode: {intake_mode!r}"
        )
    product = args.get("product")
    region = args.get("region")

    # 1. Open the SP-0 request + run.
    concept = args.get("feature_concept") or intent_text
    req = create_request_command(
        conn,
        Command(
            "create_request", "request", None,
            {"feature_concept": concept, "intake_mode": intake_mode},
            cmd.actor, cmd.idempotency_key + ":req",
        ),
    )
    if not req.accepted:
        return req
    request_id = req.aggregate_id
    run = create_run_command(
        conn,
        Command(
            "create_run", "run", None, {"request_id": request_id},
            cmd.actor, cmd.idempotency_key + ":run",
        ),
    )
    if not run.accepted:
        return run
    run_id = run.aggregate_id

    # 2. Envelope classification + hold the raw intent by reference only (§9.4). The raw text is
    #    written to the write-once blob store keyed by `raw_input_ref` — this is the audit-of-record
    #    for raw-intent replay + the confirm-time raw re-screen (F1, P2-c). It is held BY REFERENCE
    #    ONLY: it is NEVER inlined into an event or document, and NEVER sent to the LLM (the redactor
    #    produces the LLM-safe text separately).
    raw_input_classification = _classify_raw_input(intent_text, args.get("raw_input_classification"))
    raw_input_ref = mint_id("blob")
    write_blob(conn, raw_input_ref, {"raw_input": intent_text})  # resolvable via read_blob(raw_input_ref)

    # 3. Deterministic banking-boundary classification (§5.4, over the read-only BankingDomainCatalog)
    #    runs BEFORE INTENT_SUBMITTED so the event can persist classification.as_mapping() (R9). An
    #    unavailable/unversioned catalog leaves `classification is None` → fail-closed park (Task 4.6).
    catalog = current_intake_catalog()  # R8/R10 canonical seam (P2, catalog.py); fail-closed if unset
    classification = (
        _current_classifier()(intent_text, product=product, region=region, catalog=catalog)
        if getattr(catalog, "version", None) is not None
        else None
    )

    # 4. INTENT_SUBMITTED opens the feature_contract aggregate (folded NEEDS_CLARIFICATION, §4.6).
    #    R2: NO id fields in the payload (run_id/request_id ride the seam kwargs / typed columns).
    #    R9: persist classification.as_mapping(). R4: mirror requester = this event's actor.subject.
    submitted = append_fc_event(
        conn, run_id=run_id, type=INTENT_SUBMITTED,
        payload={
            "intake_mode": intake_mode,
            "raw_input_ref": raw_input_ref,
            "raw_input_classification": raw_input_classification,
            "product": product,
            "region": region,
            "requester": cmd.actor.subject,
            "classification": classification.as_mapping() if classification is not None else None,
        },
        actor=cmd.actor, request_id=request_id, expected_version=0,
    )
    produced = [submitted.event_id]

    # 5–6. Route on the classification outcome. Every fc append after the opening INTENT_SUBMITTED is
    #      CAS-guarded on the folded head (X4, via `_fc_head`); a ConcurrencyError from any branch is a
    #      `stale` denial. (submit_intent OPENS the stream, so in practice this never fires here — but
    #      the guard is threaded uniformly per the Global-Constraints lost-update rule.)
    try:
        if classification is None:
            # §4.5(b): catalog unavailable/unversioned → fail-closed park (INTENT_SUBMITTED already opened the fc).
            produced.extend(
                _fail_closed_park(
                    conn, run_id=run_id, request_id=request_id, actor=cmd.actor,
                    field="banking_scope",
                    question="the banking-domain catalog is unavailable/unversioned; manual review required",
                )
            )
            return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))
        outcome = classification.outcome
        if outcome in (IntakeOutcome.OUT_OF_SCOPE, IntakeOutcome.PROHIBITED_DATA_CLASS):
            # X8 — both are terminal rejects. X5 — submit_intent appends INTENT_REJECTED ITSELF (never P8).
            produced.extend(
                _do_reject_intent(
                    conn, run_id=run_id, request_id=request_id, actor=cmd.actor,
                    classification=outcome.value, catalog_version=classification.catalog_version,
                    reason=classification.reason, matched_class=classification.matched_class,
                )
            )
            return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))
        if outcome is IntakeOutcome.NEEDS_USE_CASE_ONBOARDING:
            produced.extend(
                _do_onboarding_park(
                    conn, run_id=run_id, request_id=request_id, actor=cmd.actor,
                    catalog_version=classification.catalog_version,
                )
            )
            return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=tuple(produced))

        # CLEAR / SENSITIVE_PROXY_CLARIFY / AMBIGUOUS_CLARIFY → normalize into a Draft.
        return _produce_draft(
            conn, cmd=cmd, run_id=run_id, request_id=request_id, intent_text=intent_text,
            intake_mode=intake_mode, raw_input_ref=raw_input_ref,
            raw_input_classification=raw_input_classification,
            classification=classification, product=product, region=region, produced=produced,
        )
    except ConcurrencyError:  # X4 — a concurrent transition advanced the fc head between fold and append.
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="stale")


# Populated here (after the handler is defined) so `register_sp2_commands` picks it up at call time.
_SP2_CATALOG = (
    ("submit_intent", submit_intent),
)


# ═══ Task 5.5 — Human Clarification task + the bounded Contract Refinement Loop (§6.5, §6.6) ═════════
#
# Round budget for the Contract Refinement Loop (Decision 6, spec §6.6) — bounded by SP-0's durable
# hard-loop-limit posture, config-gated; on exhaustion the run auto-parks for human follow-up (never
# loops forever). Read as a module-global so a test can monkeypatch it.
MAX_REFINEMENT_ROUNDS = int(os.environ.get("FEATUREGEN_MAX_REFINEMENT_ROUNDS", "5"))
_REFINEMENT_PARK_OWNER = "governance:intake-refinement"
_RENORM_SETTINGS = {"provider": "fake", "model": "fake-structured", "max_tokens": 2048}

# The `renormalize` structured-output schema call_llm validates the re-normalization output against
# (§9.1). Intentionally LENIENT vs the full DRAFT_CONTRACT content-schema: the re-normalization returns
# only the revised SEMANTIC subset (feature_semantics + the still-open fields); the platform re-wraps
# the SP-0 envelope. additionalProperties stays open (additive-friendly, mirrors events.py/critique.py).
RENORMALIZE_SCHEMA_ID = "renormalize"
RENORMALIZE_SCHEMA_VERSION = 1
RENORMALIZE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["feature_semantics"],
    "properties": {
        "feature_semantics": {"type": "object"},
        "open_fields": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


def register_renormalize_schema(registry) -> None:
    """Register the `renormalize` structured-output schema in SP-0's document registry so call_llm can
    validate the re-normalization response (§9.1). Idempotent (register_schema upserts)."""
    registry.register_schema(
        RENORMALIZE_SCHEMA_ID, RENORMALIZE_SCHEMA_VERSION, RENORMALIZE_OUTPUT_SCHEMA,
        CONTRACT_SCHEMA_OWNER,
    )


@dataclass(frozen=True, slots=True)
class RefineResult:
    status: str                 # clarifying | validated | mcv_failed | parked | stale (X4 CAS lost-update)
    draft_doc_id: str | None
    open_fields: tuple[str, ...]
    mcv: object | None          # MCVResult when a checklist ran, else None


@dataclass(frozen=True, slots=True)
class IntakeDeps:
    client: object      # LLMClient (§9.1)
    redactor: object    # IntentRedactor (§9.4)
    catalog: object     # CatalogView (scoring seam, §6.1)


_INTAKE_DEPS: IntakeDeps | None = None


def register_intake_deps(*, client, redactor, catalog) -> None:
    """Single-source registration of the Layer-2 runtime deps (LLM client / redactor / merged-view
    catalog). P9 bootstrap wires FakeLLM + DefaultIntentRedactor + the SP-1 merged-view adapter; the
    auto-drive in the Refinement Loop uses these when a caller does not pass them explicitly."""
    global _INTAKE_DEPS
    _INTAKE_DEPS = IntakeDeps(client=client, redactor=redactor, catalog=catalog)


def current_intake_deps() -> IntakeDeps | None:
    return _INTAKE_DEPS


# ── the frozen-body DAG documents (P4 seam) ─────────────────────────────────────────────────────────
# Documents are opaque-by-reference (documents/store.py — a content_hash + body_ref, never the body);
# SP-0's encrypted blob store is the durable resolver of `body_ref`. `freeze_draft` freezes the DAG
# documents (real rows + lineage) for the revised Draft; the SEMANTIC body itself rides the
# DRAFT_CONTRACT_PRODUCED / CONTRACT_REFINED events inline (Phase-8 replay), so the READ path is the
# event stream (mcv._latest_body), NOT any in-process map — replay-/cross-process-safe.


def freeze_draft(
    conn: DbConn,
    *,
    run_id: str,
    request_id: str,
    body: dict,
    ledger_body: dict,
    actor: IdentityEnvelope,
    supersedes: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Freeze a Draft + its Assumption Ledger as governance-retained DAG documents (§3.4) and return
    `(draft_doc_id, ledger_doc_id)`. The ledger is frozen first so its real doc id can be threaded into
    the Draft's `assumption_ledger_ref` (R12); the Draft `derived_from` the ledger and `supersedes` the
    prior Draft (full refinement history retained). The bodies carry NO raw intent / PII, only the
    semantic contract; the authoritative READ is the inlined event stream (mcv._latest_body), never a
    by-doc-id lookup here."""
    ledger_doc_id = _emit_document(
        conn, stage=Stage.ASSUMPTION_LEDGER.value, body=ledger_body,
        run_id=run_id, request_id=request_id, actor=actor,
    )
    draft_body = {**body, "assumption_ledger_ref": ledger_doc_id}
    draft_doc_id = _emit_document(
        conn, stage=Stage.DRAFT_CONTRACT.value, body=draft_body,
        run_id=run_id, request_id=request_id, actor=actor,
        derived_from=(ledger_doc_id,), supersedes=tuple(supersedes),
    )
    return draft_doc_id, ledger_doc_id


# ── feature_contract stream readers ─────────────────────────────────────────────────────────────────
def _first(stream, event_type: str):
    return next((e for e in stream if e.type == event_type), None)


def _current_draft_doc_id(stream) -> str | None:
    """The newest frozen Draft doc-id on the stream (a CONTRACT_REFINED supersedes a
    DRAFT_CONTRACT_PRODUCED). Delegates to `_final_draft` (Task 7.1) — the ONE newest-Draft scan; DRY
    per the 7.1 review."""
    return _final_draft(stream)[0]


def _answered_fields(stream) -> dict[str, object]:
    """Pinned answers: {field: answer} from every CLARIFICATION_ANSWERED (last write wins). Pinning
    answered fields is what makes the Loop converge (§6.6)."""
    answers: dict[str, object] = {}
    for e in stream:
        if e.type == CLARIFICATION_ANSWERED:
            field = e.payload.get("field")
            if field is not None:
                answers[field] = e.payload.get("answer")
    return answers


def _requested_field(stream, task_id: str) -> str | None:
    """The `field` the CLARIFICATION_REQUESTED that opened this task asked about — threaded onto the
    answer's CLARIFICATION_ANSWERED so the P2 fold prunes it from open_fields and the Loop pins it
    (§6.6). None when the task has no matching request shadow on the stream."""
    for e in stream:
        if e.type == CLARIFICATION_REQUESTED and e.payload.get("task_id") == task_id:
            return e.payload.get("field")
    return None


# ── clarification task ──────────────────────────────────────────────────────────────────────────────
def open_clarification_task(
    conn: DbConn,
    *,
    run_id: str,
    request_id: str,
    draft_doc_id: str,
    field: str,
    question: str,
    owner_subject: str,
    actor: IdentityEnvelope,
    candidate_readings: tuple = (),
    expected_version: int | None = None,
) -> str:
    """Open an SP-0 CLARIFICATION human-gate task for one must-ask field (§6.5), then emit
    CLARIFICATION_REQUESTED on the feature_contract aggregate. The eligible assignee is the REQUEST
    OWNER (author-owned intent lock) and `delegation_allowed=False` — the subject guard alone is
    necessary but not sufficient, since GateTaskSpec.delegation_allowed defaults to True and a delegate
    could otherwise stand in (§8.2). `required_inputs=[draft_doc_id]` so a later re-normalization stales
    any pending answer (SP-0 task staleness). **X4**: a folded caller (the Refinement Loop) passes
    `expected_version` to CAS the emit on its running head; a standalone call leaves it None (current head)."""
    spec = GateTaskSpec(
        gate="CLARIFICATION",
        required_inputs=(draft_doc_id,),
        eligible_assignees={"role": "data_scientist", "subject": owner_subject},
        allowed_responses=("confirm", "edit", "reject"),
        run_id=run_id,
        delegation_allowed=False,
        sla=_HUMAN_TASK_SLA,
    )
    task_id = open_task(conn, spec, actor)
    append_fc_event(
        conn, run_id=run_id, type=CLARIFICATION_REQUESTED,
        payload={"task_id": task_id, "field": field, "question": question, "routed_to": "human",
                 "draft_doc_id": draft_doc_id, "candidate_readings": list(candidate_readings),
                 "blocks_progress": True},
        actor=actor, request_id=request_id, expected_version=expected_version,  # X4 CAS when folded
    )
    return task_id


# ── contract semantics helpers ────────────────────────────────────────────────────────────────────
def _base(path: str) -> str:
    return path.split(".", 1)[0]


def _concepts(semantics) -> dict[str, str]:
    """Concept-bearing fields for the catalog-cardinality check (§6.1). Only fields whose meaning binds
    to a catalog object / declared code get a cardinality lookup."""
    concepts: dict[str, str] = {}
    entity = semantics.get("entity")
    if entity and entity != UNKNOWN:
        concepts["entity"] = entity
    filters = semantics.get("filters") or []
    if isinstance(filters, list) and filters and isinstance(filters[0], dict):
        concept = filters[0].get("concept")
        if concept:
            concepts["filters"] = concept
    return concepts


def _policy_fields(classification, semantics) -> set[str]:
    """Policy-sensitive fields that may NEVER auto-resolve (§6.2): any sensitive-proxy field the
    classifier flagged, plus a present `target` (credit-decisioning use-cases pin the label at Gate #1)."""
    fields = set((classification or {}).get("sensitive_fields", []) or [])
    target = semantics.get("target")
    if target not in (None, UNKNOWN):
        fields.add("target")
    td = semantics.get("target_definition")
    if isinstance(td, str) and td and td != UNKNOWN and not td.startswith("N/A"):
        fields.add("target")
    return fields


def _ledger_entry(field, semantics, score) -> dict:
    return {
        "field": field,
        "value": semantics.get(field),
        "source": score["source"],
        "rationale": f"auto-resolved: {field} is low-ambiguity ({score['ambiguity']}) from {score['source']}",
        "ambiguity": score["ambiguity"],
        "confidence": score["confidence"],
        "auto_resolved_at": datetime.now(UTC).isoformat(),
    }


# F5 / P2-a — the synthetic open field a refine round raises when the challenger critique could not run
# (LLM failure/refusal). It keeps the draft in NEEDS_CLARIFICATION (manual review) instead of letting it
# converge as if the critique passed clean; it clears automatically once the critique runs successfully.
_CRITIQUE_REVIEW_FIELD = "critique_review"


def _open_questions(routing, question_by_field) -> list[dict]:
    return [
        {"field": f, "question": question_by_field.get(f, f"Please specify {f}."),
         "blocks_progress": True, "routed_to": "human"}
        for f, decision in routing.items() if decision == "human"
    ]


def _candidate_count(conn: DbConn, run_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM documents WHERE run_id=%s AND stage=%s AND branch_role='candidate'",
        (run_id, Stage.DRAFT_CONTRACT.value),
    ).fetchone()
    return int(row[0]) if row else 0


def _run_is_parked(conn: DbConn, run_id: str) -> bool:
    """True iff the SP-0 run is CURRENTLY parked (a RUN_PARKED not since cleared by RUN_UNPARKED). The
    bounded-exhaustion auto-park is a direct park outside execute_command, so this idempotency check is
    what stops an auto-park re-drive from appending a duplicate RUN_PARKED on a still-parked run."""
    parked = False
    for e in load_stream(conn, "run", run_id):
        if e.type == "RUN_PARKED":
            parked = True
        elif e.type == "RUN_UNPARKED":
            parked = False
    return parked


def _redact_answers(redactor, answers) -> dict:
    """Belt-and-suspenders: a clarification answer is human free text — redact it before it enters an
    LLM renormalize request (§9.4). call_llm additionally egress-guards the whole request.

    N2 (SP-2): each answer is RE-classified at its OWN ingress (`_scan_text_classification`) — it does
    NOT inherit the intent's original label. A PII-bearing answer is redacted even if the origin intent
    was `clean` (the stale-clean bypass); a clean answer on a `contains_pii`-origin run is no longer
    force-redacted to "[REDACTED]"."""
    out: dict[str, str] = {}
    for field, answer in answers.items():
        text = str(answer)
        red = redactor.redact(text, _scan_text_classification(text))
        out[field] = red.text if red.text is not None else "[REDACTED]"
    return out


# ── the bounded Contract Refinement Loop (§6.6) ─────────────────────────────────────────────────────
def refine_contract(
    conn: DbConn,
    run_id: str,
    *,
    client=None,
    redactor=None,
    catalog=None,
    actor: IdentityEnvelope,
    thresholds=None,
    max_rounds: int | None = None,
) -> RefineResult:
    """One bounded refinement round (spec §6.6): re-normalize (only when an answer targets a still-open
    field) → re-score (5.1) → re-critique (5.3) → re-route (5.2) → auto-resolve safe fields (ledger +
    FIELD_AUTO_RESOLVED) → freeze the revised Draft (CONTRACT_REFINED) → open must-ask tasks; converge to
    MCV when no open field remains (MINIMUM_CONTRACT_VALIDATED); auto-park when the round budget is
    exhausted (never loops forever). Deps default to the P5 accessors. **X4** — every domain append is
    CAS-pinned to a running head re-anchored past this round's own LLM audit appends; a concurrent
    transition (or a status advance since the fold) returns status="stale" and commits nothing."""
    deps = current_intake_deps()
    client = client or (deps.client if deps else None)
    redactor = redactor or (deps.redactor if deps else None)
    catalog = catalog or (deps.catalog if deps else None)
    thresholds = thresholds or default_thresholds()
    budget = MAX_REFINEMENT_ROUNDS if max_rounds is None else max_rounds

    stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(stream)   # R3 — the P2 fold; `state.requester` is the owner (R4)
    intent = _first(stream, INTENT_SUBMITTED)
    # R2: request_id rides the typed event column (submit_intent puts NO id fields in the payload); a
    # seed that inlines it in the payload still resolves via the fallback.
    request_id = intent.request_id or intent.payload.get("request_id")
    mode = intent.payload["intake_mode"]
    classification = intent.payload.get("classification")   # the recorded R9 mapping (`.catalog_version`)
    # N2: clarification answers are RE-classified at their own ingress inside `_redact_answers` (they no
    # longer inherit the intent's `raw_input_classification`), so the intent label is not read here.
    draft_doc_id = _current_draft_doc_id(stream)
    # Read the CURRENT draft/ledger body from the INLINED event stream (the same scan mcv._latest_body
    # uses) — the real producer (_produce_draft/submit_intent) inlines draft_body/assumption_ledger_body
    # on DRAFT_CONTRACT_PRODUCED and refine re-inlines them on CONTRACT_REFINED, so the read is
    # replay-/cross-process-safe (never the in-process body map).
    latest_draft = _latest_body(stream, "draft_body")
    if latest_draft is None:
        raise IntakeError(f"no draft_body on the feature_contract stream for run {run_id!r}")
    draft_body = dict(latest_draft)
    latest_ledger = _latest_body(stream, "assumption_ledger_body")
    ledger_body = dict(latest_ledger) if latest_ledger is not None else {
        "request_id": request_id, "assumptions": []}
    answers = _answered_fields(stream)
    rounds = sum(1 for e in stream if e.type == CONTRACT_REFINED)

    # 1) Re-normalize only when an answer targets a field still open on the current Draft. The
    #    re-normalization output is AUTHORITATIVE on what remains open (an answer that fails to resolve
    #    keeps its field open → the Loop cannot converge and eventually parks, §6.6).
    unfolded = [f for f in answers if any(_base(of) == f for of in draft_body.get("open_fields", []))]
    if unfolded:
        register_renormalize_schema(DocumentSchemaRegistry(conn))  # §9.1 output-schema (idempotent)
        request = LLMRequest(
            task="renormalize", prompt_id="renormalize", prompt_version=1,
            inputs={
                "prior_semantics": draft_body["feature_semantics"],
                "answers": _redact_answers(redactor, {f: answers[f] for f in unfolded}),
                INPUT_KEY_CATALOG: dict(catalog.metadata()),
                # The renormalize payload is composed ENTIRELY from ALREADY-REDACTED structured draft
                # fields (prior_semantics from the frozen Draft, answers pre-redacted just above) — NOT
                # the raw intent — so it is `clean` by construction and carries a redaction_version,
                # exactly as the sibling already-redacted-payload callers do (critique.py / candidates.py).
                # Forwarding the raw intent's original `contains_pii` label (with no redaction_version)
                # would make assert_llm_safe (§9.4) HARD-RAISE on every clarification round of a
                # PII-origin run — a false-positive egress block, never a leak. The `_first_pii` pre-scan
                # below still fails closed on any GENUINE residual PII in the composed content.
                INPUT_KEY_CLASSIFICATION: "clean",       # egress guard (§9.4) — LLM-safe, no raw intent
                INPUT_KEY_REDACTION_VERSION: REDACTION_VERSION,
            },
            output_schema_id=RENORMALIZE_SCHEMA_ID, output_schema_version=RENORMALIZE_SCHEMA_VERSION,
            generation_settings=dict(_RENORM_SETTINGS),
        )
        # Egress hard-backstop (§9.4), mirroring critique.py: call_llm's assert_llm_safe only scans the
        # reserved intent/catalog keys, but the renormalize request carries model-facing content under
        # non-reserved keys — `prior_semantics` (sent RAW) and `answers` (pre-redacted). Scan both with
        # redaction's OWN detector and FAIL CLOSED before dispatch — residual PII is an upstream
        # invariant breach that must surface (EgressViolation), never be silently sent.
        hit = _first_pii(request.inputs["prior_semantics"], request.inputs["answers"])
        if hit:
            raise EgressViolation(f"un-redacted {hit} detected in renormalize model-facing content")
        result = call_llm(conn, client, request, run_id=run_id, actor=actor)
        semantics = result.output["feature_semantics"]
        llm_scores = result.self_reported_scores
        open_fields = list(result.output.get("open_fields", []))
        rounds += 1
        renormalized = True
    else:
        semantics = draft_body["feature_semantics"]
        llm_scores = {f: dict(s) for f, s in draft_body.get("field_scores", {}).items()}
        open_fields = list(draft_body.get("open_fields", []))
        renormalized = False

    # 2) Re-score (LLM self-report ⊕ catalog cardinality, cautious-max).
    field_scores = score_fields(llm_scores, _concepts(semantics), catalog.candidate_count)

    # 3) Re-run the challenger critique and 4) route, ORing blocking findings to must-ask.
    critique = contract_review(conn, client, semantics, run_id=run_id, actor=actor,
                               catalog_metadata=catalog.metadata())
    routing = apply_critique(
        route_draft(field_scores, open_fields, mode=mode,
                    policy_sensitive_fields=_policy_fields(classification, semantics), thresholds=thresholds),
        critique,
    )
    # F5 / P2-a — the challenger fails CLOSED. A non-usable critique (LLM failure/refusal) must NOT let the
    # draft converge as if it passed clean: raise a manual-review open field so this round opens a
    # clarification task instead of reaching MCV (§9.2). Lift it once the critique runs successfully.
    if not critique.usable:
        if _CRITIQUE_REVIEW_FIELD not in open_fields:
            open_fields.append(_CRITIQUE_REVIEW_FIELD)
        routing[_CRITIQUE_REVIEW_FIELD] = "human"
        field_scores[_CRITIQUE_REVIEW_FIELD] = {"ambiguity": 1.0, "confidence": 0.0, "source": "critique"}
    elif _CRITIQUE_REVIEW_FIELD in open_fields:  # challenger recovered → lift the manual-review block
        open_fields.remove(_CRITIQUE_REVIEW_FIELD)
        routing.pop(_CRITIQUE_REVIEW_FIELD, None)
        field_scores.pop(_CRITIQUE_REVIEW_FIELD, None)

    # X4 — re-anchor the running CAS head PAST this round's own LLM audit appends (renormalize / critique
    # each appended LLM_CALL_RECORDED / CONTRACT_CRITIQUED to this same stream), refusing the round if a
    # concurrent transition advanced the status since the fold, then thread `expected` (a running head)
    # through every domain append below. A lost-update race trips ConcurrencyError → the whole round is stale.
    domain_stream = load_feature_contract(conn, run_id)
    if fold_feature_contract_state(domain_stream).status != state.status:
        return RefineResult("stale", draft_doc_id, tuple(open_fields), None)
    expected = domain_stream[-1].stream_version if domain_stream else 0

    def _append_domain(**kw):
        """CAS every refine domain append on the running head (X4); advance it after each append so the
        next transition CASes on the correct version."""
        nonlocal expected
        env = append_fc_event(conn, expected_version=expected, **kw)
        expected = env.stream_version
        return env

    try:
        # 5) Auto-resolve safe fields → ledger + FIELD_AUTO_RESOLVED (never a field already in the ledger
        #    or already human-answered).
        ledger_fields = {a["field"] for a in ledger_body.get("assumptions", [])}
        additions = []
        for field, decision in routing.items():
            if decision == "auto" and field not in ledger_fields and field not in answers:
                entry = _ledger_entry(field, semantics, field_scores[field])
                additions.append(entry)
                _append_domain(run_id=run_id, type=FIELD_AUTO_RESOLVED,
                               payload={"field": field, "value": entry["value"], "source": entry["source"],
                                        "ambiguity": entry["ambiguity"], "confidence": entry["confidence"]},
                               actor=actor)

        # 6) Freeze the revised Draft + Ledger and emit CONTRACT_REFINED when anything changed. The
        #    payload INLINES draft_body + assumption_ledger_body + open_fields + field_scores +
        #    open_questions so the P2 fold tracks the open-field/question set + scores and
        #    mcv._latest_body reads the CURRENT body — omit them and a later MCV validates the STALE
        #    original Draft / the fold keeps the stale pre-refinement open_questions.
        question_by_field = {e.payload["field"]: e.payload.get("question", f"Please specify {e.payload['field']}.")
                             for e in stream if e.type == CLARIFICATION_REQUESTED and e.payload.get("field")}
        new_ledger = {"request_id": request_id,
                      "assumptions": list(ledger_body.get("assumptions", [])) + additions}
        new_draft = {**draft_body, "feature_semantics": semantics, "field_scores": field_scores,
                     "open_fields": open_fields, "open_questions": _open_questions(routing, question_by_field),
                     "status": DRAFT_STATUS}
        changed = renormalized or bool(additions) or open_fields != list(draft_body.get("open_fields", [])) \
            or field_scores != draft_body.get("field_scores", {})
        if changed:
            draft_doc_id, ledger_doc_id = freeze_draft(
                conn, run_id=run_id, request_id=request_id, body=new_draft, ledger_body=new_ledger,
                actor=actor, supersedes=(draft_doc_id,) if draft_doc_id else (),
            )
            new_draft["assumption_ledger_ref"] = ledger_doc_id
            _append_domain(run_id=run_id, type=CONTRACT_REFINED,
                           payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                                    "open_fields": open_fields, "field_scores": field_scores,
                                    "open_questions": new_draft["open_questions"],
                                    "draft_body": new_draft, "assumption_ledger_body": new_ledger,
                                    "iteration": rounds}, actor=actor)

        must_ask = [f for f, d in routing.items() if d == "human"
                    and any(_base(of) == f for of in open_fields)]

        # 7) Converge → MCV; or bounded-exhausted → auto-park; or open must-ask tasks and loop.
        if not open_fields and not must_ask:
            candidate_count = _candidate_count(conn, run_id) if mode == "hypothesis" else 0
            mcv = minimum_contract_validated(new_draft, new_ledger, classification, mode=mode,
                                             candidate_count=candidate_count, confirmed_fields=set(answers))
            if mcv.passed:
                _append_domain(run_id=run_id, type=MINIMUM_CONTRACT_VALIDATED,
                               payload={"draft_doc_id": draft_doc_id, "checks": {"failures": []}}, actor=actor)
                return RefineResult("validated", draft_doc_id, (), mcv)
            return RefineResult("mcv_failed", draft_doc_id, (), mcv)

        if rounds >= budget:
            # Bounded (§6.6): stop looping — auto-park the run for human follow-up (X6 waiting_on_fact=None).
            # This is a DIRECT park_command (outside execute_command → its idempotency_key is NOT honoured
            # and run_is_terminal is NOT checked), so guard it here: never park an already-terminal run,
            # and make an auto-park re-drive idempotent (no duplicate RUN_PARKED on a still-parked run).
            if not run_is_terminal(conn, run_id) and not _run_is_parked(conn, run_id):
                park_command(conn, Command(
                    action="park", aggregate="run", aggregate_id=run_id,
                    args={"owner": _REFINEMENT_PARK_OWNER, "waiting_on_fact": None},
                    actor=actor, idempotency_key=f"refine-park:{run_id}:{rounds}",
                ))
            return RefineResult("parked", draft_doc_id, tuple(open_fields), None)

        owner = state.requester   # R4 — the INTENT_SUBMITTED actor.subject; never payload.get("requested_by")
        open_task_fields = {e.payload["field"] for e in stream
                            if e.type == CLARIFICATION_REQUESTED and e.payload.get("field")} & set(must_ask)
        for field in must_ask:
            if field in open_task_fields:
                continue  # a task for this field already exists on the stream (refresh handled by staleness)
            open_clarification_task(conn, run_id=run_id, request_id=request_id, draft_doc_id=draft_doc_id,
                                    field=field, question=question_by_field.get(field, f"Please specify {field}."),
                                    owner_subject=owner, actor=actor, expected_version=expected)
            expected += 1   # X4 — the CLARIFICATION_REQUESTED landed at expected+1 → advance the head
        return RefineResult("clarifying", draft_doc_id, tuple(open_fields), None)
    except ConcurrencyError:
        # X4 — a concurrent feature_contract transition advanced the head since the fold. The whole round
        # is stale; nothing this round committed (single transaction) — the Loop re-drives on the fresh head.
        return RefineResult("stale", draft_doc_id, tuple(open_fields), None)


# ═══ Task 5.6 — the answer_clarification command (request-owner guard → drives the loop, §6.5) ════════
# The request-owner / SoD denial helper is the shared `_deny_audited` (Task 7.1, below) — a subject
# guard denial is a security event routed to the tamper-evident security-audit stream (R15, §6.2/§8.2),
# NEVER the domain stream. `_deny_owner_guard` was folded into it (DRY, per the 7.1 review).


def answer_clarification(conn: DbConn, cmd: Command) -> CommandResult:
    """Answer a Human Clarification task (spec §6.5). SP-2 adds the request-owner guard SP-0 does not
    provide: SP-0's `submit_human_signal` checks role/scope/quorum but NEVER that the acting subject is
    the task's requester (`gates/tasks.py`), so role-authz alone would let ANY data_scientist answer
    another author's clarification. R4: the ONE owner predicate is `actor_is_request_owner(state, actor)`
    AND a human actor-kind; a non-owner / non-human is DENIED + security-audited (R15), never counted and
    with NO state change. On a counted, quorum-met answer it emits the CLARIFICATION_ANSWERED domain
    shadow (X4 — CAS-pinned to the folded head; a concurrent transition denies `stale`) and drives the
    Contract Refinement Loop (`refine_contract`) when the Layer-2 deps are registered.

    Handler on an EXISTING stream: fold → decide → deny BEFORE any side-effecting append (execute_command
    does NOT roll back on accepted=False, so a benign non-count / owner denial must commit nothing)."""
    args = cmd.args
    task_id = args["task_id"]
    row = conn.execute("SELECT run_id FROM human_tasks WHERE task_id=%s", (task_id,)).fetchone()
    if row is None or row[0] is None:
        return CommandResult(accepted=False, aggregate_id="", denied_reason="unknown clarification task")
    run_id = row[0]
    stream = load_feature_contract(conn, run_id)
    # X4 (CAS on the folded head): pin the answer shadow to the folded head. `submit_human_signal`
    # advances only `human_tasks` (not the feature_contract stream), so the head captured here is still
    # the head at the CLARIFICATION_ANSWERED append; a concurrent feature_contract transition since the
    # fold trips ConcurrencyError → deny `stale` (the counted signal + shadow ride one transaction).
    head_version = stream[-1].stream_version if stream else 0
    state = fold_feature_contract_state(stream)   # R3 — the P2 fold; state.requester is the owner (R4)

    # ── SP-2 request-owner guard (subject-level; SP-0 authz is role-level only) ──────────────────
    # R4: the ONE owner predicate is actor_is_request_owner(state, actor) — never payload.get("requested_by").
    # DECIDE-BEFORE-APPEND: a mismatch (non-owner / non-human) denies + security-audits and commits nothing.
    if cmd.actor.actor_kind != "human" or not actor_is_request_owner(state, cmd.actor):
        return _deny_audited(
            conn, cmd, run_id, "answer_clarification denied: actor is not the request owner"
        )

    result = submit_human_signal(
        conn, task_id, response=args["response"], actor=cmd.actor,
        expected_task_version=args["expected_task_version"],
    )
    if not result.counted:
        # Benign non-count (stale task_version / already-closed) — NOT a security event.
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"clarification not counted (status={result.status})",
        )

    field = _requested_field(stream, task_id)
    try:
        append_fc_event(
            conn, run_id=run_id, type=CLARIFICATION_ANSWERED,
            payload={"task_id": task_id, "field": field, "answer": args.get("answer"),
                     "response": args["response"], "answered_by": cmd.actor.subject},
            actor=cmd.actor, expected_version=head_version,   # X4 — CAS on the folded head
        )
    except ConcurrencyError:
        # A concurrent feature_contract transition raced this fold → fail closed, not counted.
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="stale")

    # Drive the Refinement Loop once quorum is met (§6.6) — only when the runtime deps are wired
    # (P9 bootstrap / test registration). Absent deps, the loop is driven by the durable runtime.
    if result.quorum_met:
        deps = current_intake_deps()
        if deps is not None and deps.client is not None:
            refine_contract(conn, run_id, client=deps.client, redactor=deps.redactor,
                            catalog=deps.catalog, actor=cmd.actor)
    return CommandResult(accepted=True, aggregate_id=run_id)


# Task 5.6 — extend the P4 command catalog (this is the command P9 registers in the SP-2 catalog).
# Reassigned AFTER answer_clarification is defined; register_sp2_commands reads the module-global at
# call time (idempotent), so the appended entry is picked up.
_SP2_CATALOG = _SP2_CATALOG + (("answer_clarification", answer_clarification),)


# ═══ Task 6.5 — select_candidate_doc (document PRIMARY_SELECTED promotion, owner+human guarded) ═══════
# ── shared candidate guard + PRIMARY_SELECTED promotion (Task 6.5 owns these; confirm_contract's
#    hypothesis-mode calculation_method_chosen, Task 7.5, reuses the SAME two helpers) ────────────────
def _candidate_doc_guard(
    conn: DbConn, run_id: str, candidate_doc_id: str, stage: str = "DRAFT_CONTRACT"
) -> str | None:
    """The existence + branch_role guard: the chosen doc MUST exist under (run_id, stage) AND carry
    branch_role='candidate'. Returns a fail-closed denial REASON for a foreign / unknown / non-candidate
    id, else None. A PURE READ (no append) so callers can decide BEFORE any promotion — and, in
    confirm_contract, BEFORE the Gate #1 task OCC — so a bogus id promotes nothing and mutates no state."""
    row = conn.execute(
        "SELECT branch_role FROM documents WHERE doc_id=%s AND run_id=%s AND stage=%s",
        (candidate_doc_id, run_id, stage),
    ).fetchone()
    if row is None:
        return f"unknown candidate doc {candidate_doc_id} for (run={run_id}, stage={stage})"
    if row[0] != "candidate":
        return f"doc {candidate_doc_id} is branch_role={row[0]!r}, not a candidate"
    return None


def _promote_candidate(
    conn: DbConn, run_id: str, candidate_doc_id: str, actor, stage: str = "DRAFT_CONTRACT"
) -> tuple[str | None, object | None]:
    """calculation_method_chosen (hypothesis mode) — the document PRIMARY_SELECTED promotion of the
    chosen candidate on the RUN aggregate (§7.1). NOT the request-level select_candidate; records ONLY
    the chosen doc (the losers stay untouched candidate-role docs). Callers MUST pre-validate via
    `_candidate_doc_guard`. Returns (None, appended) on success, or ("stale", None) when a concurrent
    run-aggregate write advanced the run head between the guard read and this append (ConcurrencyError —
    execute_command does NOT catch it, so this helper does; mirrors submit_intent / refine_contract /
    answer_clarification).

    X4 / SP-0 carve-out: PRIMARY_SELECTED rides the RUN aggregate under the run stream's OWN OCC head
    (current_version(conn,"run",run_id)) — NOT the feature_contract folded head (no FC transition here)."""
    event = new_primary_selected(
        run_id=run_id,
        stage=stage,
        doc_id=candidate_doc_id,
        actor=actor,
        provenance=provenance_for(artifact_type=stage),
    )
    try:
        appended = append_event(
            conn,
            event,
            expected_version=current_version(conn, "run", run_id),
            table_version=table_version_for(conn, "run", run_id),
        )
    except ConcurrencyError:
        return ("stale", None)
    return (None, appended)


def select_candidate_doc(conn: DbConn, cmd: Command) -> CommandResult:
    """Hypothesis-mode candidate selection (§7.1): a document-level `PRIMARY_SELECTED` promotion of
    the chosen candidate doc on the RUN aggregate (`new_primary_selected`) — records ONLY the chosen
    doc; the losing candidate docs are write-once and LEFT UNTOUCHED (no per-doc reject event; their
    `doc_id`s live only in the Gate #1 confirmation record, §8.3). This is NOT the request-level
    `select_candidate` command (which promotes *run* candidates on a *request* stream — the wrong
    granularity; SP-2's candidates are documents under a single run). Owner + human guarded. Shares its
    existence+branch_role guard and PRIMARY_SELECTED promotion (`_candidate_doc_guard` /
    `_promote_candidate`) with `confirm_contract`'s hypothesis-mode calculation_method_chosen (Task 7.5);
    confirm calls those helpers directly — NOT this handler. OCC on the run stream serializes concurrent
    selects. X4: the `feature_contract` fold here is OWNER-GUARD-ONLY — this handler appends NO
    `feature_contract` transition, so there is no FC folded head to CAS on; the `PRIMARY_SELECTED`
    append rides the RUN aggregate under the run stream's own OCC (per SP-0). Do NOT pass the
    feature_contract folded head as this append's `expected_version` (wrong aggregate).

    Handler on an EXISTING stream: fold → decide → deny BEFORE any side-effecting append/promotion
    (execute_command does NOT roll back on accepted=False, so every denial must commit nothing)."""
    args = cmd.args
    run_id = args["run_id"]
    candidate_doc_id = args["candidate_doc_id"]
    stage = args.get("stage", Stage.DRAFT_CONTRACT.value)

    # Gate #1 is an author-owned intent lock: the confirmer MUST be the authenticated human requester
    # (never a service, never the LLM, never a different data scientist). SP-0 authz admits any
    # data_scientist human, so SP-2 enforces the fine owner-guard here (§8.2).
    # R15: a NON-HUMAN (service / LLM) attempting the human-only Gate #1 is the escalation signal the
    # security-audit stream exists to capture — deny via `record_denial` (mirrors `_deny_audited`,
    # which handles both the non-human and non-owner arms), never a plain unaudited denial.
    if cmd.actor.actor_kind != "human":
        reason = "select_candidate_doc requires the human requester (not a service)"
        record_denial(conn, replace(cmd, aggregate_id=run_id), reason)  # R15 — decision="denied"
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=reason)
    # R3/R4: fold the feature_contract stream and call the state-based owner predicate owned by P2
    # (intake/state.py) — never the (conn, run_id, actor) mcv form. `state.requester` is the
    # INTENT_SUBMITTED event actor.subject.
    fc_stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(fc_stream)
    if not actor_is_request_owner(state, cmd.actor):
        # R15 — writes decision="denied"; `replace(cmd, aggregate_id=run_id)` so the security record is
        # traceable to the run (cmd.aggregate_id is None here — run_id rides args), mirroring `_deny_audited`.
        record_denial(conn, replace(cmd, aggregate_id=run_id), "actor is not the request owner")
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason="actor is not the request owner (owner-guard, §8.2)",
        )

    # N5 — lifecycle guards: candidate selection is a HYPOTHESIS-mode Gate #1 action, on a NON-terminal,
    # MCV-validated run, WITH an open Gate #1 task. Without these a candidate could be promoted at the
    # WRONG point (pre-MCV, definition mode, a terminal run, or with no gate open). Decided BEFORE the
    # promotion append (execute_command does NOT roll back accepted=False).
    if state.status in TERMINAL_STATUSES:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"contract already {state.status.value}; cannot select a candidate (no-regression)",
        )
    if state.status is not FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED:
        status = state.status.value if state.status is not None else None
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"no open Gate #1 to select a candidate (status={status})",
        )
    if state.intake_mode != "hypothesis":
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"candidate selection is a hypothesis-mode Gate #1 action (mode={state.intake_mode})",
        )
    if not _gate1_task_open(conn, run_id, fc_stream):
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason="no open Gate #1 task to select a candidate against",
        )

    # Existence + branch_role guard (shared with confirm_contract's hypothesis promotion). A wrong
    # doc_id here is a benign owner client error (the owner+human guards above already passed), so it
    # stays a PLAIN unaudited denial — not the security-stream `record_denial` the non-owner/non-human
    # arms use. Decide BEFORE the promotion append (execute_command does NOT roll back accepted=False).
    guard_reason = _candidate_doc_guard(conn, run_id, candidate_doc_id, stage)
    if guard_reason is not None:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=guard_reason)

    # X4 / SP-0 carve-out: PRIMARY_SELECTED is a document promotion on the RUN aggregate — its OCC is
    # the run stream's own head, NOT the feature_contract folded head (this handler appends no FC
    # transition; the fold above is owner-guard-only). A concurrent run-aggregate write → deny `stale`.
    promote_reason, appended = _promote_candidate(conn, run_id, candidate_doc_id, cmd.actor, stage)
    if promote_reason is not None:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=promote_reason)
    return CommandResult(
        accepted=True, aggregate_id=run_id, produced_event_ids=(appended.event_id,)
    )


# Task 6.5 — extend the SP-2 command catalog with the document PRIMARY_SELECTED promotion.
_SP2_CATALOG = _SP2_CATALOG + (("select_candidate_doc", select_candidate_doc),)


# ═══ Phase 7 — Human Gate #1 (the AUDITED INTENT LOCK, §8.2/§8.6) ═════════════════════════════════
# Gate #1 = author-self-confirms: the eligible confirmer is the authenticated HUMAN requester (the
# request owner) — NEVER a service principal, the LLM, or a second signer (independent validation is
# deferred to Gate #2 / SP-5). The dedicated confirm task opens ONLY after Minimum Contract Validation
# passes (Task 5.4); it is the task `confirm_contract` (Task 7.2) completes.


# ── shared Gate-#1 stream helpers (pure reads of the feature_contract stream) ─────────────────────
def _request_id(stream) -> str | None:
    """The request_id the feature_contract carries — read off INTENT_SUBMITTED (typed column first,
    payload fallback; R2 keeps id fields off the payload)."""
    for e in stream:
        if e.type == INTENT_SUBMITTED:
            return getattr(e, "request_id", None) or e.payload.get("request_id")
    return None


def _final_draft(stream) -> tuple[str | None, dict | None]:
    """The (doc_id, body) of the latest Draft the contract carries — the initial
    DRAFT_CONTRACT_PRODUCED or the most recent CONTRACT_REFINED supersession (§8.6)."""
    doc_id: str | None = None
    body: dict | None = None
    for e in stream:
        if e.type in (DRAFT_CONTRACT_PRODUCED, CONTRACT_REFINED):
            doc_id = e.payload.get("draft_doc_id")
            body = e.payload.get("draft_body")
    return doc_id, body


def _deny_audited(conn: DbConn, cmd: Command, aggregate_id: str, reason: str) -> CommandResult:
    """A confirmer/authority denial happens INSIDE the handler (SP-0's coarse authorizer only audits
    role/kind/scope), so route it to the tamper-evident security-audit stream (§8.2) — a spoofed
    confirmer never leaves zero audit trace. Benign wrong-state / stale-OCC / prohibited-block denials
    stay unaudited (plain CommandResult)."""
    record_denial(conn, replace(cmd, aggregate_id=aggregate_id), reason)
    return CommandResult(accepted=False, aggregate_id=aggregate_id, denied_reason=reason)


def _freeze_contract_doc(
    conn: DbConn,
    *,
    run_id: str,
    request_id: str | None,
    stage: str,
    body: dict,
    branch_role: str,
    derived_from: tuple[str, ...],
    supersedes: tuple[str, ...],
    actor,
) -> str:
    """Freeze one write-once, content-hashed SP-2 contract document on the SP-0 DAG (§4.6). The body
    itself rides the emitting FC event payload (replayable, non-PII — mirrors SP-1's confirmed value);
    the document row is the DAG artifact (content-hash + lineage). governance-retained (§8.3)."""
    doc_id = mint_id("doc")
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    append_document(
        conn,
        NewDocument(
            doc_id=doc_id,
            stage=stage,
            schema_version=1,
            branch_role=branch_role,
            content_hash=compute_content_hash(body_bytes),
            body_classification="governance-retained",
            provenance=provenance_for(stage),
            body_ref=None,
            derived_from=tuple(derived_from),
            supersedes=tuple(supersedes),
        ),
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )
    return doc_id


# ── Gate #1 vs. clarification task discrimination (shared by _open_gate1_task + advance_intake) ──────
# Both ride gate="CLARIFICATION" and carry the confirm/edit/reject response set, so they are
# indistinguishable at the human_tasks-row level ALONE. The one structural tell: every per-field /
# manual clarification task emits a CLARIFICATION_REQUESTED shadow carrying its `task_id`, while
# `_open_gate1_task` appends NO feature_contract event — so a Gate #1 task's id NEVER appears in a
# CLARIFICATION_REQUESTED. That shadow set is the discriminator both helpers below key on.
_GATE1_RESPONSES = frozenset({"confirm", "edit", "reject"})


def _clarification_task_ids(stream) -> set[str]:
    """The gate-task ids the per-field / manual CLARIFICATION_REQUESTED shadows point at (a Gate #1 task
    is never among them — `_open_gate1_task` appends no fc event)."""
    ids = {e.payload.get("task_id") for e in stream if e.type == CLARIFICATION_REQUESTED}
    ids.discard(None)
    return ids


def _open_clarification_task_ids(conn: DbConn, run_id: str, stream) -> list[str]:
    """Open human_tasks for the run that ARE per-field / manual clarifications (a CLARIFICATION_REQUESTED
    shadow points at them). Excludes an open Gate #1 confirmation task (no shadow) — so advance_intake
    can tell "waiting on a human answer" from "the gate is already open"."""
    shadowed = _clarification_task_ids(stream)
    rows = conn.execute(
        "SELECT task_id FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,)
    ).fetchall()
    return [tid for (tid,) in rows if tid in shadowed]


def _gate1_task_open(conn: DbConn, run_id: str, stream) -> bool:
    """True iff an open Gate #1 confirmation task already exists for the run: an OPEN CLARIFICATION-gate
    task whose allowed_responses are exactly the Gate #1 set AND which carries NO CLARIFICATION_REQUESTED
    shadow (a per-field clarification always carries one; a leftover non-gate task uses a different
    response set). Lets a retried open_gate1_task / advance_intake NO-OP instead of cancelling +
    recreating a live gate (which would strand the requester's in-flight confirm on a superseded
    task_version)."""
    shadowed = _clarification_task_ids(stream)
    rows = conn.execute(
        "SELECT task_id, allowed_responses FROM human_tasks "
        "WHERE run_id=%s AND status='open' AND gate='CLARIFICATION'",
        (run_id,),
    ).fetchall()
    return any(tid not in shadowed and set(resp or ()) == _GATE1_RESPONSES for tid, resp in rows)


# ── open_gate1_task (Task 7.1) ────────────────────────────────────────────────────────────────────
def _open_gate1_task(conn: DbConn, run_id: str, *, actor) -> CommandResult:
    """Open the SEPARATE, dedicated Gate-#1 confirmation task — ONLY after MCV passes (§8.6). Reused by
    both the `open_gate1_task` command and `request_edit`'s gate re-open (Task 7.6). X4: this handler
    folds the FC status for its guards but appends NO feature_contract event (it writes only human_tasks
    rows via cancel_tasks_on_run_advance + open_task), so there is no FC-stream append to CAS on — the
    shared X4 rule has nothing to attach to here and is a no-op."""
    stream = load_stream(conn, "feature_contract", run_id)
    if not stream:
        return CommandResult(
            accepted=False, aggregate_id=run_id or "", denied_reason="unknown feature_contract"
        )
    state = fold_feature_contract_state(stream)
    if state.status is not FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED:
        status = state.status.value if state.status is not None else None
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason=f"Gate #1 requires MINIMUM_CONTRACT_VALIDATED (status={status})",
        )
    if state.open_fields:  # open_fields_empty (§6.7) — never open Gate #1 on an under-specified contract
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason=f"open_fields not empty: {tuple(state.open_fields)}",
        )
    owner = state.requester
    if owner is None:
        return CommandResult(
            accepted=False, aggregate_id=run_id, denied_reason="no request owner recorded"
        )
    # Idempotency (Task 9.2a): a retried open (a re-driven advance_intake, a re-dispatched
    # open_gate1_task) must NOT cancel + recreate an already-open Gate #1 task — churning its
    # task_version would strand the requester's in-flight confirm on a now-superseded version. If the
    # gate is already open, this is a no-op accept — CHECKED FIRST, before the defensive cancel below.
    if _gate1_task_open(conn, run_id, stream):
        return CommandResult(accepted=True, aggregate_id=run_id)
    draft_doc_id, _ = _final_draft(stream)
    # Defensive close: cancel any still-pending per-field clarification tasks so none can be answered
    # behind the open gate (§8.6). After a passing MCV there should be none.
    cancel_tasks_on_run_advance(
        conn, run_id, reason="Gate #1 opened — pending clarification tasks cancelled"
    )
    open_task(
        conn,
        GateTaskSpec(
            gate="CLARIFICATION",  # rides the existing CLARIFICATION gate — no new gate value (§8.6)
            required_inputs=(draft_doc_id,) if draft_doc_id else (),
            eligible_assignees={"role": "data_scientist", "subject": owner},
            allowed_responses=("confirm", "edit", "reject"),
            run_id=run_id,
            delegation_allowed=False,  # the author-owned intent lock (§8.2)
            sla=_HUMAN_TASK_SLA,
        ),
        actor,
    )
    return CommandResult(accepted=True, aggregate_id=run_id)


def open_gate1_task(conn: DbConn, cmd: Command) -> CommandResult:
    """Open the dedicated post-MCV Human Gate #1 confirmation task (the audited intent lock, §8.6)."""
    run_id = cmd.args.get("run_id") or cmd.aggregate_id
    return _open_gate1_task(conn, run_id, actor=cmd.actor)


# Task 7.1 — extend the SP-2 command catalog with the dedicated Gate-#1 opener.
_SP2_CATALOG = _SP2_CATALOG + (("open_gate1_task", open_gate1_task),)


# ═══ Task 9.2a — advance_intake: the thin production driver (Draft → clarification | MCV → Gate #1) ═══
# The pipeline-initiation driver a durable runtime dispatches after submit_intent / answer_clarification
# to carry an opened contract forward WITHOUT a fresh human answer — closing the gap the E2E surfaced
# (submit_intent leaves a Draft; nothing wired it to MCV / Gate #1). It is a THIN driver over existing
# pieces and duplicates NO router / MCV logic: `refine_contract` (Task 5.5) stays the routing engine
# (score → critique → doubt-router → open must-ask tasks, or MINIMUM_CONTRACT_VALIDATED when clean) and
# `_open_gate1_task` (Task 7.1) stays the gate opener. It decides off the P2 fold and NEVER leaves the
# run stuck. X4: refine_contract / _open_gate1_task each CAS-pin their OWN head; advance_intake itself
# appends only the Gate #1 human_task (no feature_contract transition of its own).
def _generate_hypothesis_candidates(
    conn: DbConn, run_id: str, state: FeatureContractState, *, actor: IdentityEnvelope
) -> CommandResult | None:
    """Task 9.5a — freeze the hypothesis-mode scored candidate set BEFORE the first-pass route (§7.2),
    so MCV #2 (`calculation_method_available`, §6.7 #2) sees a NON-EMPTY candidate set (closing gap B —
    without this a hypothesis run parks at 0 candidates and never reaches Gate #1). Runs the registered
    CandidateGenerator through the per-run event-sourcing RecordingLLMClient (one auditable
    `LLM_CALL_RECORDED` for `generate_candidates`), freezes 1–3 candidate-role Draft docs, then records
    a `CANDIDATES_GENERATED` shadow whose `candidate_doc_ids` the P2 fold reads into
    `state.candidate_doc_ids` (so `run_minimum_contract_validation`'s MCV #2 and `refine_contract`'s
    DB-count MCV #2 AGREE — closing gap D). Idempotent: the caller only invokes this when no candidate
    exists yet. Returns a `CommandResult` to SHORT-CIRCUIT advance_intake — a fail-closed park on empty
    generation (never a silent zero-candidate MCV pass), or `stale` on a raced CAS append — or `None`
    to continue the normal route. X4: the shadow CASes on the folded head."""
    stream = load_feature_contract(conn, run_id)
    draft_doc_id, draft_body = _final_draft(stream)
    if draft_body is None:
        raise IntakeError(f"no draft_body on the feature_contract stream for run {run_id!r}")
    request_id = state.request_id or _request_id(stream)
    deps = current_intake_deps()
    catalog_metadata = dict(deps.catalog.metadata()) if deps and deps.catalog else {}
    candidate_doc_ids = generate_candidates_for_run(
        conn,
        draft=draft_body,
        catalog_metadata=catalog_metadata,
        domain_context=None,   # per-use-case DomainCatalogEntry slice deferred (SP-2 prep, §4.5)
        draft_doc_id=draft_doc_id,
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )
    if not candidate_doc_ids:
        # Fail closed (§7.2): generation produced NO candidate → the run must not strand or silently
        # pass MCV. Park for human/retry follow-up via the same manual review path §4.5(b) / mcv_failed
        # use (opens a CLARIFICATION task + parks; a re-driven advance then no-ops on that open task).
        _fail_closed_park(
            conn, run_id=run_id, request_id=request_id, actor=actor,
            field="calculation_method",
            question="hypothesis-mode candidate generation produced no candidates; manual review/retry required",
        )
        return CommandResult(accepted=True, aggregate_id=run_id)
    # Record the candidate_doc_ids so the P2 fold surfaces state.candidate_doc_ids (gap D). X4 — CAS on
    # the folded head, re-loaded PAST the generation pass's interleaved LLM_CALL_RECORDED.
    try:
        append_fc_event(
            conn, run_id=run_id, type=CANDIDATES_GENERATED,
            expected_version=_fc_head(conn, run_id),  # X4 — CAS on the folded head
            payload={  # R2 — no aggregate-id fields; doc_ids are DAG refs, not aggregate ids.
                "draft_doc_id": draft_doc_id,
                "candidate_doc_ids": list(candidate_doc_ids),
            },
            actor=actor, request_id=request_id,
        )
    except ConcurrencyError:
        return CommandResult(
            accepted=False, aggregate_id=run_id, denied_reason="stale: contract advanced concurrently"
        )
    return None


def advance_intake(conn: DbConn, cmd: Command) -> CommandResult:
    """Advance an opened feature_contract one production step (§6.6/§8.6). Folds the FC status, then:

      * CONFIRMED / OUT_OF_SCOPE / PROHIBITED_DATA_CLASS (terminal) → no-op accept (nothing to drive).
      * MINIMUM_CONTRACT_VALIDATED → open Gate #1 (idempotent — an already-open gate is a no-op).
      * NEEDS_CLARIFICATION with an OPEN clarification task → no-op accept (waiting on the human).
      * NEEDS_CLARIFICATION with NO open clarification task → (Task 9.5a) in HYPOTHESIS mode with no
        candidate yet, first generate the scored candidate set (event-sourced, `CANDIDATES_GENERATED`
        shadow so MCV #2 sees a non-empty set, §6.7 #2 — a fail-closed park on empty generation), then
        run the FIRST-PASS route through `refine_contract` (no prior answer needed: it re-scores →
        critiques → routes the CURRENT Draft):
          - `validated`      → open Gate #1;
          - `clarifying`     → leave the must-ask tasks it opened (no-op further);
          - `parked`         → the Loop auto-parked for human follow-up (no-op further);
          - `mcv_failed`     → open a manual review CLARIFICATION task + park (never a stranding denial);
          - `stale`          → deny (X4 lost-update — a concurrent transition raced the fold).

    Handler on an EXISTING stream: fold → decide; every commit is owned by refine_contract /
    _open_gate1_task, each CAS-pinned to its own head (X4, fail-closed)."""
    run_id = cmd.args.get("run_id") or cmd.aggregate_id
    stream = load_feature_contract(conn, run_id)
    if not stream:
        return CommandResult(
            accepted=False, aggregate_id=run_id or "", denied_reason="unknown feature_contract"
        )
    state = fold_feature_contract_state(stream)
    status = state.status

    # A no-regression-locked terminal (CONFIRMED / the banking-boundary rejects) — nothing to advance.
    if state.is_terminal:
        return CommandResult(accepted=True, aggregate_id=run_id)
    # Already validated → open the audited Gate #1 (idempotent — _open_gate1_task no-ops a live gate).
    if status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED:
        return _open_gate1_task(conn, run_id, actor=cmd.actor)
    # NEEDS_CLARIFICATION is the only drivable state; an onboarding hold (or an un-opened stream) waits.
    if status is not FeatureContractStatus.NEEDS_CLARIFICATION:
        return CommandResult(accepted=True, aggregate_id=run_id)
    # Waiting on a human: an open per-field / manual clarification task exists → no-op (do not re-route).
    if _open_clarification_task_ids(conn, run_id, stream):
        return CommandResult(accepted=True, aggregate_id=run_id)

    # Hypothesis mode (Task 9.5a, closes gap B): freeze the scored candidate set BEFORE the first-pass
    # route so MCV #2 sees a non-empty candidate set (§6.7 #2). Idempotent — skip when candidates
    # already exist. A short-circuit result (fail-closed park on empty generation, or a raced `stale`)
    # is returned as-is; None means "candidates ready — continue the normal route".
    if state.intake_mode == "hypothesis" and _candidate_count(conn, run_id) == 0:
        short_circuit = _generate_hypothesis_candidates(conn, run_id, state, actor=cmd.actor)
        if short_circuit is not None:
            return short_circuit

    # No open clarification → drive the first-pass route through the routing engine (NOT raw MCV): it
    # opens must-ask tasks OR converges to MINIMUM_CONTRACT_VALIDATED. Deps default to the registered
    # Layer-2 collaborators (client / redactor / catalog).
    result = refine_contract(conn, run_id, actor=cmd.actor)
    if result.status == "validated":
        return _open_gate1_task(conn, run_id, actor=cmd.actor)
    if result.status == "stale":
        return CommandResult(
            accepted=False, aggregate_id=run_id, denied_reason="stale: contract advanced concurrently"
        )
    if result.status == "mcv_failed":
        # Never strand the run on a denial (a resolved-but-MCV-failing Draft has no open field for the
        # Loop to re-ask): route it to a human via a manual review CLARIFICATION task + a park — the same
        # fail-closed manual path §4.5(b) uses. A re-driven advance then no-ops on that open task.
        failures = getattr(result.mcv, "failures", ()) or ()
        _fail_closed_park(
            conn, run_id=run_id, request_id=state.request_id, actor=cmd.actor,
            field="minimum_contract",
            question="minimum contract validation failed (" + ",".join(failures)
                     + "); manual review required",
        )
        return CommandResult(accepted=True, aggregate_id=run_id)
    # "clarifying" (must-ask tasks opened) / "parked" (bounded-exhaustion follow-up) — leave them as is.
    return CommandResult(accepted=True, aggregate_id=run_id)


# Task 9.2a — register advance_intake in the SP-2 command catalog (picked up by register_sp2_commands).
_SP2_CATALOG = _SP2_CATALOG + (("advance_intake", advance_intake),)


# ═══ Task 7.2 — confirm_contract (definition-mode happy path → CONFIRMED, §8.2/§8.5/§8.6) ═════════════
def _risk_flags_for(classification, catalog) -> list[str]:
    """Platform-side risk tagging (Decision 2 / P1-d): a CATALOG-DECLARED high-risk matched use-case →
    a `high_risk_use_case:<name>` flag that sets requires_independent_validation at Gate #1. Derived from
    the ALREADY-computed intake classification + the catalog's declared high-risk set — NO new LLM call,
    NO PII. Fail-closed: a missing signal simply yields no flag (RIV stays False); it never fabricates
    approval. The second SIGNER is deferred to Gate #2 (SP-5); SP-2 only sets the flag."""
    use_case = getattr(classification, "matched_use_case", None)
    if not use_case or catalog is None:
        return []
    if use_case in getattr(catalog, "high_risk_use_cases", frozenset()):
        return [f"high_risk_use_case:{use_case}"]
    return []


def _requires_independent_validation(draft_body: dict) -> bool:
    """§8.4 #1 risk-flag → requires_independent_validation. A FLAG ONLY: SP-2 does not require a
    second signer and does not block; the independent validation is Gate #2 (SP-5)."""
    return bool(draft_body.get("risk_flags"))


def _screen_text(draft_body: dict) -> str:
    """The confirmation-time screen text: the target/feature concept + filter concepts the §8.4
    deterministic classifier screens against the catalog's blocked/sensitive classes (non-PII)."""
    fs = draft_body.get("feature_semantics", {})
    parts = [str(draft_body.get("proposed_feature_name", "")), str(fs.get("target_definition", ""))]
    for f in fs.get("filters", []) or []:
        parts.append(str(f.get("concept", "")))
    return " ".join(p for p in parts if p)


def _prohibited_intent_screen(conn: DbConn, draft_body: dict) -> str | None:
    """§8.4 #2 — the fail-closed compliance backstop, AUTHORITATIVE for the block. Re-runs the §5.4
    deterministic classification over the CURRENT BankingDomainCatalog (the R8 module-global
    current_intake_catalog() seam) at the moment of confirmation (so a version drift that would flip
    the classification is caught, §8.4(d)). Returns a denial reason, or None when CLEAR. NEVER
    pretends to approve compliance. (conn is unused — the catalog rides the module-global seam.)"""
    catalog = current_intake_catalog()
    if catalog is None or not getattr(catalog, "version", None):
        return "banking catalog unavailable/unversioned at confirmation — fail-closed park for review (§8.4)"
    product = draft_body.get("product")
    region = draft_body.get("region")
    # P2-b/F6 — screen the ORIGINAL RAW intent (resolved by reference from the F1 write-once blob store),
    # NOT only the lossy structured Draft text: a prohibited phrase dropped/softened during structuring
    # would otherwise escape this backstop. Both texts are re-classified over the CURRENT catalog with the
    # intent's product/region; most-restrictive-wins — EITHER being non-CLEAR blocks the confirm.
    texts = [_screen_text(draft_body)]
    raw_ref = draft_body.get("raw_input_ref")
    raw_blob = read_blob(conn, raw_ref) if raw_ref else None
    if raw_blob and raw_blob.get("raw_input"):
        texts.append(str(raw_blob["raw_input"]))
    for text in texts:
        cls = classify_intent(text, product=product, region=region, catalog=catalog)
        if cls.outcome is IntakeOutcome.PROHIBITED_DATA_CLASS:
            return (
                f"prohibited data class: {cls.matched_class} (catalog {cls.catalog_version}); cannot confirm — "
                f"edit the intent or withdraw (§8.4)"
            )
        if cls.outcome is not IntakeOutcome.CLEAR:
            return (
                f"confirmation-time classification requires clarification/review "
                f"(outcome={cls.outcome.value}, catalog {cls.catalog_version}) (§8.4)"
            )
    return None  # CLEAR under the current catalog — the allow is auditable at cls.catalog_version (§4.5(c))


def _sibling_candidates(conn: DbConn, run_id: str, selected_doc_id: str) -> list[str]:
    """The write-once losing candidate docs (documents are never rejected — §7.1, §8.3). They stay
    untouched candidate-role docs; their `doc_id`s live ONLY in the Gate #1 confirmation record."""
    rows = conn.execute(
        "SELECT doc_id FROM documents "
        "WHERE run_id=%s AND stage='DRAFT_CONTRACT' AND branch_role='candidate' AND doc_id <> %s "
        "ORDER BY global_seq",
        (run_id, selected_doc_id),
    ).fetchall()
    return [r[0] for r in rows]


class _GateRollback(Exception):
    """Signals a fail-closed denial INSIDE a Gate #1 write savepoint (stale task / stale promotion) so
    `conn.transaction()` rolls the WHOLE side-effecting block back — no stranded run (F3 / P1-c)."""


def confirm_contract(conn: DbConn, cmd: Command) -> CommandResult:
    """Human Gate #1 — the author-self-confirm happy path (§8.2/§8.5/§8.6). The request owner (the
    authenticated HUMAN requester) confirms an MCV-passed Draft: fold → no-regression + MCV guards →
    consume the Gate #1 task (task-version OCC) → assemble the CONFIRMED_CONTRACT body via R7
    `assemble_confirmed` (which stamps status="CONFIRMED", the confirmation record, and
    requires_independent_validation — the handler does NOT re-derive them) → R6 semantic backstop →
    freeze the CONFIRMED_CONTRACT document `derived_from` the final Draft → append CONTRACT_CONFIRMED
    (folds status → CONFIRMED). Persists the FULL confirmation record incl. the CONFIRMER IDENTITY
    (Decision 2). X4: the FC append CASes on the folded head; a ConcurrencyError (a concurrent
    transition since the fold) is a `stale` denial.

    Handler on an EXISTING stream: fold → decide → deny BEFORE any side-effecting append (execute_command
    does NOT roll back on accepted=False, so every non-count / wrong-state denial must commit nothing).
    Guardrails / the §8.4 prohibited-intent screen / hypothesis-mode candidate promotion are Tasks
    7.3–7.5 (the marked INSERT slots)."""
    run_id = cmd.args.get("run_id") or cmd.aggregate_id
    task_id = cmd.args["task_id"]
    expected_task_version = cmd.args["expected_task_version"]
    stream = load_stream(conn, "feature_contract", run_id)
    if not stream:
        return CommandResult(
            accepted=False, aggregate_id=run_id or "", denied_reason="unknown feature_contract"
        )
    state = fold_feature_contract_state(stream)
    head_version = stream[-1].stream_version  # X4 — pin the folded head for CAS on the FC append (§12)
    # No-regression guard (§4.6, §11): CONFIRMED / the terminal rejects refuse a conflicting re-advance.
    if state.status in TERMINAL_STATUSES:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"contract already {state.status.value}; no re-advance (no-regression)",
        )
    if state.status is not FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED:
        status = state.status.value if state.status is not None else None
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"not ready for Gate #1 (status={status})",
        )
    # §8.2 — the confirmer MUST be the authenticated human requester (never a service, the LLM, or a
    # DIFFERENT data scientist). A mismatch is denied + security-audited, before the gate is consumed.
    if not confirmer_is_requester_human(state, cmd.actor):
        return _deny_audited(
            conn, cmd, run_id,
            "Gate #1 confirm requires the authenticated human requester (confirmer_is_requester_human)",
        )
    draft_doc_id, draft_body = _final_draft(stream)
    if draft_body is None:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="no draft to confirm")
    intake_mode = draft_body.get("intake_mode")
    candidate_doc_id = cmd.args.get("candidate_doc_id")
    # [Task 7.4 INSERT: §8.4 prohibited-intent screen + version-drift re-check]
    blocked = _prohibited_intent_screen(conn, draft_body)
    if blocked is not None:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=blocked)
    # [Task 7.5 INSERT: hypothesis calculation_method_chosen guard]
    # Fail-closed (§7.1): a hypothesis contract cannot be confirmed with no chosen calculation method —
    # confirmation binds the human-SELECTED candidate (promoted to PRIMARY). No selection → DENY.
    if intake_mode == "hypothesis":
        if not candidate_doc_id:
            return CommandResult(
                accepted=False, aggregate_id=run_id,
                denied_reason="hypothesis mode requires a selected candidate_doc_id (calculation_method_chosen)",
            )
        # Integrity guard (§7.1): the human-selected candidate MUST be a real candidate doc of THIS run —
        # it must exist under (run_id, DRAFT_CONTRACT) AND carry branch_role='candidate' (the SAME guard
        # select_candidate_doc enforces). A foreign / unknown / non-candidate id (owner typo or client
        # bug) is a fail-closed AUDITED deny, decided BEFORE the task OCC + promotion below — so a bogus
        # id never promotes to PRIMARY nor lands an internally-inconsistent confirmation record.
        guard_reason = _candidate_doc_guard(conn, run_id, candidate_doc_id)
        if guard_reason is not None:
            return _deny_audited(conn, cmd, run_id, guard_reason)

    # ── reads: resolve the FULL Confirmed body BEFORE any write, so the write block below is a tight,
    # atomic savepoint (F3 / P1-c). selected/rejected/chosen_method are pure reads; the PRIMARY_SELECTED
    # promotion (a write) moves into the savepoint. An unresolvable candidate or an invalid body denies
    # here with NOTHING committed (the Gate #1 task is not yet consumed).
    selected_candidate: str | None = None
    rejected_candidates: list[str] = []
    # Definition mode: chosen_method stays None so R7 reshapes the Draft's `rolling_*` label from the
    # semantics (§4.2). It is a tagged method_variant Mapping ONLY in hypothesis mode — NEVER the raw
    # calculation_method string (reshape_calculation_method dict()s a non-None chosen_method).
    chosen_method: dict | None = None
    if intake_mode == "hypothesis":
        selected_candidate = candidate_doc_id
        rejected_candidates = _sibling_candidates(conn, run_id, candidate_doc_id)
        # P1-a — load the CHOSEN candidate's durable body (F1) and bind its tagged calculation_method so
        # the confirmed contract reflects the human's selection, not the Draft. Fail closed if unresolvable.
        cand_doc = get_document(conn, candidate_doc_id)
        cand_body = read_blob(conn, cand_doc["body_ref"]) if cand_doc and cand_doc.get("body_ref") else None
        if cand_body is None:
            return CommandResult(
                accepted=False, aggregate_id=run_id,
                denied_reason="cannot resolve the selected candidate body (fail-closed)",
            )
        # candidate.calculation_method is the tagged {method_version, chosen, considered}; reshape wants the
        # inner `chosen` variant verbatim (it re-wraps). Fail closed if the candidate has no chosen method.
        chosen_variant = (cand_body.get("calculation_method") or {}).get("chosen")
        if not isinstance(chosen_variant, dict) or not chosen_variant:
            return CommandResult(
                accepted=False, aggregate_id=run_id,
                denied_reason="selected candidate has no chosen calculation_method (fail-closed)",
            )
        chosen_method = dict(chosen_variant)

    feature_name = cmd.args.get("feature_name") or draft_body.get("proposed_feature_name")
    riv = _requires_independent_validation(draft_body)
    # P1-a — the Confirmed contract derives from the Draft AND (hypothesis) the chosen candidate doc.
    confirmed_derived_from = tuple(d for d in (draft_doc_id, selected_candidate) if d)
    # Decision 2 — the FULL confirmation record: selected/rejected candidates, human edits, ambiguity
    # notes, AND the CONFIRMER IDENTITY (subject / role claims / source of authority).
    confirmation = {
        "confirmed_by": cmd.actor.subject,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "confirmer_role_claims": list(cmd.actor.role_claims),
        "source_of_authority": cmd.actor.source_of_authority,
        "selected_candidate": selected_candidate,
        "rejected_candidates": rejected_candidates,
        "human_edits": list(cmd.args.get("human_edits") or []),
        "ambiguity_notes": cmd.args.get("ambiguity_notes", ""),
    }
    # R7 — pass ALL pinned args to P2's assemble_confirmed; it stamps status="CONFIRMED", the
    # confirmation record, and requires_independent_validation. The handler does NOT re-derive them.
    confirmed_body = assemble_confirmed(
        draft_body,
        confirmation=confirmation,
        derived_from=confirmed_derived_from,
        requires_independent_validation=riv,
        chosen_method=chosen_method,
        feature_name=feature_name,
    )
    # R6 — semantic backstop on the assembled Confirmed body (raises ContractSemanticError).
    validate_semantics(confirmed_body, stage="CONFIRMED_CONTRACT")
    request_id = _request_id(stream)

    # ── writes (F3 / P1-c): ONE savepoint. psycopg3 `conn.transaction()` COMMITS on normal exit and
    # ROLLS BACK on exception, so the Gate #1 task-consume + hypothesis PRIMARY_SELECTED promotion +
    # frozen Confirmed doc + the X4 CAS append are ATOMIC. A stale task, a stale promotion, or a stale
    # CAS raises → the savepoint unwinds → NOTHING commits (no stranded run). execute_command does not
    # roll back on accepted=False, which is exactly why this section must roll itself back.
    try:
        with conn.transaction():
            sig = submit_human_signal(
                conn, task_id, response="confirm", actor=cmd.actor,
                expected_task_version=expected_task_version,
            )
            if not sig.counted:
                raise _GateRollback(
                    f"stale/closed Gate #1 task (status={sig.status}); re-fetch task_version (OCC)"
                )
            if intake_mode == "hypothesis":
                # candidate_doc_id was existence+branch_role validated above (pre-OCC); promote it to
                # PRIMARY on the RUN aggregate. A concurrent run-aggregate write since that guard read →
                # stale (rolls back with this block, not a strand).
                promote_reason, _ = _promote_candidate(conn, run_id, candidate_doc_id, cmd.actor)
                if promote_reason is not None:
                    raise _GateRollback(promote_reason)
            confirmed_doc_id = _freeze_contract_doc(
                conn, run_id=run_id, request_id=request_id, stage=Stage.CONFIRMED_CONTRACT.value,
                body=confirmed_body, branch_role="primary",
                derived_from=confirmed_derived_from, supersedes=(), actor=cmd.actor,
            )
            evt = append_fc_event(
                conn, run_id=run_id, type=CONTRACT_CONFIRMED,
                payload={  # R2 — no aggregate-id fields; confirmed_doc_id is the schema-required doc ref.
                    "confirmed_doc_id": confirmed_doc_id,
                    "confirmed_by": cmd.actor.subject,
                    "feature_name": feature_name,
                    "confirmed_body": confirmed_body,
                    "confirmation": confirmation,
                    "requires_independent_validation": riv,
                    "selected_candidate": selected_candidate,
                    "rejected_candidates": rejected_candidates,
                },
                actor=cmd.actor, request_id=request_id,
                expected_version=head_version,  # X4 — CAS on the folded head (§12)
            )
    except ConcurrencyError:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason="stale: feature_contract advanced concurrently since fold (OCC)",
        )
    except _GateRollback as exc:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=str(exc))
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


# Task 7.2 — extend the SP-2 command catalog with the Gate-#1 confirm handler.
_SP2_CATALOG = _SP2_CATALOG + (("confirm_contract", confirm_contract),)


# ═══ Task 7.6 — request_edit (owner edits at Gate #1 → REVISED Draft + re-run MCV + re-open, §8.6) ════
def _apply_edit(body: dict, field: str, value) -> None:
    """Set a dotted-path field on a Draft body (e.g. 'feature_semantics.calculation_method')."""
    parts = field.split(".")
    node = body
    for p in parts[:-1]:
        node = node[p]
    node[parts[-1]] = value


def request_edit(conn: DbConn, cmd: Command) -> CommandResult:
    """Human Gate #1 EDIT loop (§8.6): the request owner amends the MCV-passed Draft at the open gate.
    GENUINELY re-validate the edited body against the machine-checkable MCV floor (§6.7) BEFORE deciding
    whether the gate may re-open — an edit can never bypass the floor. Supersede the Draft with a REVISED
    DRAFT_CONTRACT carrying the edit, append CONTRACT_REFINED, and:
      * re-open a fresh Gate #1 task ONLY when the REVISED body still PASSES the pure MCV checklist
        (`minimum_contract_validated`, R5) — the existing happy path (e.g. a proposed_feature_name rename);
      * otherwise re-open the edited field into `open_fields` (CONTRACT_REFINED folds → NEEDS_CLARIFICATION)
        so the run drops back into the Refinement Loop with NO confirmable gate. The edit is "invalidating"
        when the new value is UNKNOWN/blank — aligned with `mcv._is_unknown` (`""`, `None`, `[]`, not only
        the exact sentinel), so blanking a required field consistently re-clarifies — OR when the revised
        body fails the pure checklist for any other reason. An edit never confirms (or re-opens a
        confirmable gate on) an invalid contract.

    Owner+human guarded (§8.2 `confirmer_is_requester_human`): a non-owner / non-human is DENIED +
    security-audited (R15), before the gate is consumed. task-version OCC via `submit_human_signal
    (response="edit")` — a stale/superseded Gate #1 task is not counted. Handler on an EXISTING stream:
    fold → decide → deny BEFORE any side-effecting append (execute_command does NOT roll back on
    accepted=False). X4: the CONTRACT_REFINED append CASes on the folded head; a ConcurrencyError (a
    concurrent transition since the fold) is a `stale` denial. F3/P1-c: the task-consume, the REVISED
    Draft freeze, and the CAS append run inside ONE `conn.transaction()` savepoint, so a stale task or a
    stale CAS rolls ALL of them back — the run is never stranded with a consumed task / frozen doc and no
    folded transition."""
    run_id = cmd.args.get("run_id") or cmd.aggregate_id
    task_id = cmd.args["task_id"]
    expected_task_version = cmd.args["expected_task_version"]
    field_edit = cmd.args["field_edit"]
    stream = load_stream(conn, "feature_contract", run_id)
    if not stream:
        return CommandResult(accepted=False, aggregate_id=run_id or "", denied_reason="unknown feature_contract")
    state = fold_feature_contract_state(stream)
    head_version = stream[-1].stream_version  # X4 — pin the folded head for CAS on the FC append (§12)
    # No-regression guard (§4.6, §11): CONFIRMED / the terminal rejects refuse a conflicting re-advance.
    if state.status in TERMINAL_STATUSES:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"contract already {state.status.value}; no re-advance (no-regression)",
        )
    if state.status is not FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED:
        status = state.status.value if state.status is not None else None
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"no open Gate #1 to edit (status={status})",
        )
    # §8.2 — the editor MUST be the authenticated human requester (never a service, the LLM, or a
    # DIFFERENT data scientist). A mismatch is denied + security-audited, before the gate is consumed.
    if not confirmer_is_requester_human(state, cmd.actor):
        return _deny_audited(
            conn, cmd, run_id,
            "Gate #1 edit requires the authenticated human requester (confirmer_is_requester_human)",
        )
    draft_doc_id, draft_body = _final_draft(stream)
    request_id = _request_id(stream)
    revised = copy.deepcopy(draft_body)
    field = field_edit["field"]
    value = field_edit["to"]
    _apply_edit(revised, field, value)

    # Close the MCV-floor-bypass-via-edit hole (§6.7): GENUINELY re-validate the REVISED body against the
    # pure MCV checklist (R5) BEFORE deciding whether the gate may re-open. The DB-backed R5 wrapper
    # short-circuits on the stale MINIMUM_CONTRACT_VALIDATED status and would accept WITHOUT re-checking
    # the edited body, so re-run the PURE `minimum_contract_validated` directly on `revised` — the same
    # in-handler pattern refine_contract uses to converge the Loop, reading its inputs off the inlined
    # stream. An edit is INVALIDATING when either (1) the new value is UNKNOWN/blank — aligned with
    # mcv._is_unknown ("", None, [], not only the exact sentinel), so blanking a required field always
    # re-clarifies — OR (2) the revised body FAILS the checklist for any other reason. Only a
    # still-passing revised body re-opens a confirmable Gate #1; every other edit re-opens the edited
    # field into open_fields → CONTRACT_REFINED folds → NEEDS_CLARIFICATION (the Refinement Loop).
    reopened = _is_unknown(value)
    if not reopened:
        intent = _first(stream, INTENT_SUBMITTED)
        classification = intent.payload.get("classification") if intent is not None else None
        ledger_body = _latest_body(stream, "assumption_ledger_body") or {
            "request_id": request_id, "assumptions": []}
        mode = state.intake_mode or "definition"
        candidate_count = _candidate_count(conn, run_id) if mode == "hypothesis" else 0
        revalidation = minimum_contract_validated(
            revised, ledger_body, classification, mode=mode,
            candidate_count=candidate_count, confirmed_fields=set(_answered_fields(stream)),
        )
        reopened = not revalidation.passed
    if reopened and field not in revised.setdefault("open_fields", []):
        revised["open_fields"].append(field)  # a re-opened / invalidated field MUST be in open_fields (§3.5)
    human_edit = {"field": field, "from": field_edit.get("from"), "to": value}

    # ── writes (F3 / P1-c): ONE savepoint over the task-consume + REVISED-doc freeze + X4 CAS append, so
    # a stale Gate #1 task or a stale CAS rolls the WHOLE edit back — never a consumed-task / frozen-doc
    # strand with no folded transition. psycopg3 conn.transaction() COMMITS on normal exit, ROLLS BACK on
    # exception. The CONTRACT_REFINED payload INLINES draft_body (mcv._latest_body reads the newest event
    # carrying it — else a later MCV re-runs on the STALE draft) AND the TOP-LEVEL open_fields /
    # open_questions / field_scores the P2 fold reads (state.py — omit open_fields and the fold silently
    # CLEARS the open-field set, so a re-opened field would not drop status to NEEDS_CLARIFICATION).
    try:
        with conn.transaction():
            # task-version OCC — an edit against a stale/superseded Gate #1 task is NOT counted (§8.6, §12).
            sig = submit_human_signal(
                conn, task_id, response="edit", actor=cmd.actor,
                expected_task_version=expected_task_version,
            )
            if not sig.counted:
                raise _GateRollback(
                    f"stale/closed Gate #1 task (status={sig.status}); re-fetch task_version (OCC)"
                )
            revised_doc_id = _freeze_contract_doc(
                conn, run_id=run_id, request_id=request_id, stage="DRAFT_CONTRACT",
                body=revised, branch_role="primary", derived_from=(),
                supersedes=(draft_doc_id,) if draft_doc_id else (), actor=cmd.actor,
            )
            append_fc_event(
                conn, run_id=run_id, type=CONTRACT_REFINED,
                payload={
                    "draft_doc_id": revised_doc_id,
                    "assumption_ledger_ref": revised.get("assumption_ledger_ref"),
                    "draft_body": revised,
                    "open_fields": list(revised.get("open_fields", [])),
                    "open_questions": list(revised.get("open_questions", [])),
                    "field_scores": revised.get("field_scores", {}),
                    "human_edits": [human_edit],
                    "reopened": reopened,
                },
                actor=cmd.actor,
                expected_version=head_version,  # X4 — CAS on the folded head (§12)
            )
    except ConcurrencyError:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason="stale: feature_contract advanced concurrently since fold (OCC)",
        )
    except _GateRollback as exc:
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=str(exc))
    if reopened:
        # An UNKNOWN/blank or otherwise MCV-FAILING edit re-opened the field → back into the Refinement
        # Loop (§6.6); NO confirmable Gate #1 re-opens on a contract that would fail the MCV floor (§6.7).
        return CommandResult(accepted=True, aggregate_id=run_id)
    # The revised body genuinely re-passed the pure MCV floor above (and, with no re-opened field, the
    # fold keeps status MINIMUM_CONTRACT_VALIDATED) → re-open a fresh Gate #1 task on the REVISED Draft (§8.6).
    _open_gate1_task(conn, run_id, actor=cmd.actor)
    return CommandResult(accepted=True, aggregate_id=run_id)


# Task 7.6 — extend the SP-2 command catalog with the Gate-#1 edit handler (Phase-7 last command).
_SP2_CATALOG = _SP2_CATALOG + (("request_edit", request_edit),)

# Task 8.7 — register the standalone, post-intake service terminal reject (X5). `reject_intent` is
# defined above (Task 8.3, R16) under the additive ("reject_intent","","intake-agent","service",None)
# authz row (P1/Task 1.6).
_SP2_CATALOG = _SP2_CATALOG + (("reject_intent", reject_intent),)

# Task 9.1 — register the requester's own abandonment (Task 8.4) so it is dispatchable via
# execute_command. `withdraw_intent` reuses SP-0's RUN_WITHDRAWN behind SP-2's request-owner guard;
# execute_command routes authz by cmd.action, so it needs its OWN action row —
# ("withdraw_intent","","data_scientist","human",None), seeded from bootstrap._SP2_POLICY_ROWS —
# distinct from SP-0's `withdraw` row. Without this wiring the Task-8.4 requester-abandonment guards
# are unreachable in production (the Task-8.7 review orphan). SP-0's validator-only `reject` is untouched.
_SP2_CATALOG = _SP2_CATALOG + (("withdraw_intent", withdraw_intent),)
