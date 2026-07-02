"""SP-2 intake command hub (mirrors SP-1's `overlay/commands.py`): the collaborator-seam accessors
the handlers read, the R1 feature_contract append helper, and the idempotent command registrar.

R10: the LLM / redactor / catalog collaborator seams are the CANONICAL module-globals owned by P3
(`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported and
re-exported here, NEVER redefined. R1: `append_fc_event` is `intake.store.append_feature_contract_event`
imported verbatim (aliased), NOT a local redefinition. Phase 4 owns ONLY a Phase-4-local override of
P2's pure `classify_intent` (`register_intake_classifier`/`_current_classifier`/`reset_intake_seams`)
so a test can pin the banking outcome deterministically."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from featuregen.aggregates._append import append, provenance_for
from featuregen.aggregates.request_aggregate import (
    create_request_command,
    create_run_command,
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
    validate_draft,
)
from featuregen.documents.store import append_document, compute_content_hash
from featuregen.gates.tasks import open_task
from featuregen.idgen import mint_id
from featuregen.intake.banking_catalog import (
    IntakeClassification,
    IntakeOutcome,
    classify_intent,
)
from featuregen.intake.catalog import current_intake_catalog  # R8/R10 seam (P2, catalog.py)
from featuregen.intake.contract import validate_semantics  # R6 (P2, contract.py)
from featuregen.intake.events import (
    CLARIFICATION_REQUESTED,
    DRAFT_CONTRACT_PRODUCED,
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    USE_CASE_ONBOARDING_GATE,
    USE_CASE_ONBOARDING_REQUESTED,
)
from featuregen.intake.llm import (  # R10 seam (P3, llm.py)
    STATUS_FAILED,
    LLMRequest,
    call_llm,
    current_llm_client,
)
from featuregen.intake.redaction import (  # R10 seam (P3, redaction.py)
    build_llm_inputs,
    current_intent_redactor,
)
from featuregen.intake.store import (  # R1 seam (P1, store.py)
    append_feature_contract_event as append_fc_event,
)
from featuregen.intake.store import (
    load_feature_contract,
)
from featuregen.privacy.classification import InlinePIIError, assert_no_inline_pii

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
]


class IntakeError(Exception):
    """Raised on intake command misconfiguration."""


# ── Phase-4-local classifier override (NOT a shared seam) ─────────────────────────────────────
# R10: the LLM / redactor / catalog collaborator seams are the canonical module-globals owned by
# P3 (`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported
# above, NEVER redefined here (Phase 9's `register_sp2` wires all four in production; tests wire
# stubs via the same `register_*` functions). Phase 4 keeps ONLY a local override of P2's pure
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
) -> dict:
    """Build the DRAFT_CONTRACT body (§4.1) from the LLM's semantic subset + the authoritative SP-0
    envelope. The platform owns the envelope: `request_id`, `raw_input_ref`,
    `raw_input_classification`, `assumption_ledger_ref`, and `status` are set here, NEVER taken from
    the model — any echoed envelope field is discarded (the no-silent-boundary for the envelope).
    Only the semantic subset (`proposed_feature_name`, `feature_semantics`, `field_scores`,
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


def _classify_raw_input(text: str, provided: str | None) -> str:
    """Determine the SP-0 envelope `raw_input_classification`. Ingest may supply it; otherwise scan
    with SP-0's inline-secret detector (`assert_no_inline_pii`) → clean | contains_pii. `unscanned`
    is only ever caller-supplied (an intent no scanner touched)."""
    if provided is not None:
        if provided not in RAW_INPUT_CLASSIFICATIONS:
            raise IntakeError(f"invalid raw_input_classification: {provided!r}")
        return provided
    try:
        assert_no_inline_pii({"intent": text})
        return "clean"
    except InlinePIIError:
        return "contains_pii"


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
) -> str:
    """Emit one frozen, content-hashed governance-retained document on the run's DAG (§3.4). The body
    itself rides the DRAFT_CONTRACT_PRODUCED event for replay; the document carries the content hash
    for lineage/integrity (the body_ref points at the encrypted blob — never inlined here)."""
    doc_id = mint_id("doc")
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
            body_ref=mint_id("blob"),
            derived_from=derived_from,
        ),
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )
    return doc_id


def _open_clarification_task(conn: DbConn, *, run_id: str, actor: IdentityEnvelope) -> str:
    """Open a human CLARIFICATION gate task (Task 4.6 pattern) and return its real `task_id` — the
    CLARIFICATION_REQUESTED schema requires it. Shared by the fail-closed manual path and the
    non-terminal sensitive-proxy / ambiguous routing."""
    return open_task(
        conn,
        GateTaskSpec(
            gate="CLARIFICATION", required_inputs=(), eligible_assignees={"role": "intake_reviewer"},
            allowed_responses=("clarify",), run_id=run_id, delegation_allowed=True,
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
    inputs = build_llm_inputs(  # reserved-keyed, LLM-safe (§9.4) — guaranteed-safe past the check above
        redaction, catalog_metadata={}, raw_input_classification=raw_input_classification
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
    draft_body = assemble_draft_body(
        request_id=request_id, intake_mode=intake_mode, raw_input_ref=raw_input_ref,
        raw_input_classification=raw_input_classification, assumption_ledger_ref=ledger_doc,
        llm_output=out, llm_call_ref=result.call_ref,
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
            run_id=run_id, delegation_allowed=True,
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
            allowed_responses=("clarify",), run_id=run_id, delegation_allowed=True,
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

    # 2. Envelope classification + hold the raw intent by reference only (§9.4). The raw text stays
    #    in-memory for the redactor; it is NEVER inlined into an event or document.
    raw_input_classification = _classify_raw_input(intent_text, args.get("raw_input_classification"))
    raw_input_ref = mint_id("blob")

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
            classification=classification, produced=produced,
        )
    except ConcurrencyError:  # X4 — a concurrent transition advanced the fc head between fold and append.
        return CommandResult(accepted=False, aggregate_id=run_id, denied_reason="stale")


# Populated here (after the handler is defined) so `register_sp2_commands` picks it up at call time.
_SP2_CATALOG = (
    ("submit_intent", submit_intent),
)
