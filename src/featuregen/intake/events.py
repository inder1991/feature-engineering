"""SP-2 event-type constants + JSON schemas for the `feature_contract` aggregate (design §2.1 #2,
§9.3, §11). These are the contract-lifecycle events SP-2 emits on its own `feature_contract`
aggregate (admitted by the 0508 aggregate-CHECK widening); the terminal RUN outcomes
(RUN_REJECTED/RUN_WITHDRAWN/RUN_PARKED) stay on SP-0's `run` aggregate via its existing lifecycle
commands. Every schema is STRUCTURAL-ONLY and carries NO inline PII / no data values — bodies are
referenced (raw_input_ref / *_doc_id / *_ref), never inlined (append's assert_no_inline_pii, §9.4).
Schemas are additive-friendly (additionalProperties: true) so P4–P8 handlers may enrich payloads
without a schema bump; the registry's backward-compat rule treats added optional fields as
compatible."""

from __future__ import annotations

from collections.abc import Mapping

# ---- the twelve feature_contract-aggregate event types (§2.1 #2) ----
INTENT_SUBMITTED = "INTENT_SUBMITTED"
DRAFT_CONTRACT_PRODUCED = "DRAFT_CONTRACT_PRODUCED"
CONTRACT_CRITIQUED = "CONTRACT_CRITIQUED"
FIELD_AUTO_RESOLVED = "FIELD_AUTO_RESOLVED"
CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
CLARIFICATION_ANSWERED = "CLARIFICATION_ANSWERED"
CONTRACT_REFINED = "CONTRACT_REFINED"
MINIMUM_CONTRACT_VALIDATED = "MINIMUM_CONTRACT_VALIDATED"
CONTRACT_CONFIRMED = "CONTRACT_CONFIRMED"
USE_CASE_ONBOARDING_REQUESTED = "USE_CASE_ONBOARDING_REQUESTED"
INTENT_REJECTED = "INTENT_REJECTED"
LLM_CALL_RECORDED = "LLM_CALL_RECORDED"

# ---- additive gate value + the FC onboarding hold-state SP-2 registers (§2.1 #6, §5.4, §11) ----
# The gate value is admitted by 0509's human_tasks_gate CHECK widening. NEEDS_USE_CASE_ONBOARDING is
# NOT a DB value and is NOT stored in RUN_PARKED.waiting_on_fact (X6) — that field is SP-1's
# fact-confirmed-resume key (run_lifecycle.py:112), so overloading it would let a later
# fact_confirmed_resume WRONGLY unpark an onboarding hold. The hold is instead the `feature_contract`
# folded status NEEDS_USE_CASE_ONBOARDING (carried by the USE_CASE_ONBOARDING_REQUESTED event, P4)
# plus the USE_CASE_ONBOARDING gate task; a run parked for onboarding sets waiting_on_fact=None. These
# are the canonical constants handlers/folds pass.
USE_CASE_ONBOARDING_GATE = "USE_CASE_ONBOARDING"
NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"

SP2_EVENT_SCHEMA_VERSION = 1
SP2_OWNER = "featuregen-intake"

# ---- reusable structural fragments (closed enums mirror the content-schema vocabularies, §4.0) ----
_ID = {"type": "string", "minLength": 1}
_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_ARR = {"type": "array"}
_INTAKE_MODE = {"enum": ["definition", "hypothesis"]}
_RAW_CLASS = {"enum": ["clean", "contains_pii", "unscanned"]}
_REJECT_CLASS = {"enum": ["OUT_OF_SCOPE", "PROHIBITED_DATA_CLASS"]}
_ROUTED_TO = {"enum": ["human", "auto"]}


def _evt(properties: Mapping[str, dict], required: list[str]) -> dict:
    """Structural object schema, additive-friendly (additionalProperties: true) — see module note."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": True,
    }


# R2 — id fields (feature_contract_id / run_id / request_id) ride typed event columns and appear in
# NO required[]; each schema requires only its SEMANTIC fields (LLM_CALL_RECORDED -> ["llm_call_ref"]).
# Emitters put NO id fields in the payload (mirrors SP-1 overlay events not requiring overlay_fact_id).
SP2_EVENT_SCHEMAS: dict[str, dict] = {
    INTENT_SUBMITTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "request_id": _ID,
            "intake_mode": _INTAKE_MODE,
            "raw_input_ref": _STR,  # SP-0 encrypted blob_/doc_ ref — raw text is NEVER inline
            "raw_input_classification": _RAW_CLASS,
            "catalog_version": _NSTR,
        },
        ["intake_mode", "raw_input_ref", "raw_input_classification"],
    ),
    DRAFT_CONTRACT_PRODUCED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _ID,
            "assumption_ledger_ref": _NSTR,
            "candidate_doc_ids": _ARR,      # hypothesis mode: 1–3 candidate docs (§7)
            "open_fields": _ARR,
            "catalog_version": _NSTR,
        },
        ["draft_doc_id"],
    ),
    CONTRACT_CRITIQUED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "critique_call_ref": _NSTR,     # llm_call_ref of the CONTRACT_REVIEW critique (§6.4)
            "findings": _ARR,
        },
        [],
    ),
    FIELD_AUTO_RESOLVED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "field": _STR,                  # a semantic field path, e.g. "entity_grain"
            "value": {},                    # the chosen SEMANTIC default (never a data value, §9.4)
            "source": {"enum": ["llm", "default", "catalog"]},
            "ambiguity": {"type": "number"},
            "confidence": {"type": "number"},
        },
        ["field"],
    ),
    CLARIFICATION_REQUESTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _ID,
            "field": _STR,
            "blocks_progress": _BOOL,
            "routed_to": _ROUTED_TO,
        },
        ["task_id", "field"],
    ),
    CLARIFICATION_ANSWERED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _ID,
            "field": _NSTR,
            "renormalize": _BOOL,           # thin domain shadow: the re-normalization trigger (§2.1 #2)
        },
        ["task_id"],
    ),
    CONTRACT_REFINED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _ID,
            "supersedes": _ARR,
            "iteration": _INT,
        },
        ["draft_doc_id"],
    ),
    MINIMUM_CONTRACT_VALIDATED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _NSTR,          # the final Draft the MCV checklist passed against
            "checks": {},                   # the deterministic MCV checklist result (§6.7)
        },
        [],
    ),
    CONTRACT_CONFIRMED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "confirmed_doc_id": _ID,        # the frozen CONFIRMED_CONTRACT document
            "confirmed_by": _NSTR,          # the authenticated requester subject (principal id)
            "requires_independent_validation": _BOOL,
            "selected_candidate": _NSTR,    # hypothesis mode: chosen candidate doc_id
        },
        ["confirmed_doc_id"],
    ),
    USE_CASE_ONBOARDING_REQUESTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _NSTR,               # the governance onboarding gate task (§5.4)
            "catalog_version": _STR,
            "proposed_use_case": _NSTR,
        },
        ["catalog_version"],
    ),
    INTENT_REJECTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "classification": _REJECT_CLASS,  # OUT_OF_SCOPE | PROHIBITED_DATA_CLASS (§5.4, §8.4)
            "reason": _NSTR,
            "catalog_version": _STR,          # stamped on every outcome (§4.5 completeness (c))
            "matched_class": _NSTR,           # the blocked_data_classes member, when prohibited
        },
        ["classification", "catalog_version"],
    ),
    LLM_CALL_RECORDED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "llm_call_ref": _ID,            # → the sensitive llm_call record store (§9.3)
            "task": _STR,                   # structure_intent | contract_review | generate_candidates | renormalize
            "status": _NSTR,                # ok | repaired | retried | failed_into_clarification (§9.2)
        },
        ["llm_call_ref"],
    ),
}


def register_sp2_event_types(registry) -> None:
    """Register the twelve SP-2 feature_contract event schemas (schema_version=1) so append_event
    validation passes (Global Constraint: every new event type MUST be registered before any append).
    Idempotent — register_schema is an upsert; safe to call repeatedly (P4–P8, P9 register_sp2)."""
    for type_name, schema in SP2_EVENT_SCHEMAS.items():
        registry.register_schema(
            type_name, SP2_EVENT_SCHEMA_VERSION, schema, owner=SP2_OWNER, status="active"
        )
