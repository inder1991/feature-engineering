"""SP-2 intake command hub (mirrors SP-1's `overlay/commands.py`): the collaborator-seam accessors
the handlers read, the R1 feature_contract append helper, and the idempotent command registrar.

R10: the LLM / redactor / catalog collaborator seams are the CANONICAL module-globals owned by P3
(`current_llm_client`, `current_intent_redactor`) and P2 (`current_intake_catalog`) — imported and
re-exported here, NEVER redefined. R1: `append_fc_event` is `intake.store.append_feature_contract_event`
imported verbatim (aliased), NOT a local redefinition. Phase 4 owns ONLY a Phase-4-local override of
P2's pure `classify_intent` (`register_intake_classifier`/`_current_classifier`/`reset_intake_seams`)
so a test can pin the banking outcome deterministically."""

from __future__ import annotations

from datetime import UTC, datetime

from featuregen.aggregates._append import append
from featuregen.commands.registry import get_command, register_command
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope
from featuregen.intake.banking_catalog import IntakeClassification, classify_intent
from featuregen.intake.catalog import current_intake_catalog  # R8/R10 seam (P2, catalog.py)
from featuregen.intake.llm import current_llm_client  # R10 seam (P3, llm.py)
from featuregen.intake.redaction import current_intent_redactor  # R10 seam (P3, redaction.py)
from featuregen.intake.store import (  # R1 seam (P1, store.py)
    append_feature_contract_event as append_fc_event,
)
from featuregen.intake.store import (
    load_feature_contract,
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
# standalone `reject_intent` (X5 — NOT Phase 4).
_SP2_CATALOG: tuple[tuple[str, object], ...] = ()


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
