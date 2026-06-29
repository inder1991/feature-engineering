from __future__ import annotations

from typing import Any, Mapping, Sequence

from featuregen.contracts import SchemaRegistry

OWNER = "featuregen-aggregates"

# Reusable property fragments. Identifier-bearing fields are required to be non-empty
# strings (id-shape where known); fields that are legitimately optional/clearable in the
# emitting handlers are typed as nullable. Enum fields carry their full closed value set so a
# bogus enum/state value fails registry validation (rather than silently passing as before).
_ID = {"type": "string", "minLength": 1}
_NID = {"type": ["string", "null"]}
_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_INT = {"type": "integer"}

# Enums mirror the DDL CHECK constraints / handler-computed values they back.
_ACTIVATION_STATE = {"type": "string", "enum": ["ACTIVE_EXPERIMENTAL", "PRODUCTION", "DEPRECATED"]}
_APPROVAL_TYPE = {"type": "string", "enum": ["EXPERIMENTAL", "PRODUCTION"]}
_CONSUMER_KIND = {"type": "string", "enum": ["model", "feature"]}


def _obj(
    properties: Mapping[str, Any],
    required: Sequence[str],
    *,
    additional: bool = False,
) -> dict:
    """Build a closed-by-default object schema with real per-field types.

    Required fields get concrete types (string/enum/id-shape); known-optional fields are
    declared too so `additionalProperties: false` can reject misspelled/unexpected keys
    without rejecting the handlers' legitimate payloads."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": additional,
    }


EVENT_SCHEMAS: dict[str, dict] = {
    # ---- request stream ----
    "REQUEST_CREATED": _obj(
        {"request_id": _ID, "concept_key": _STR, "intake_mode": _STR},
        ["request_id", "concept_key"],
    ),
    "CANDIDATE_ADDED": _obj(
        {"request_id": _ID, "run_id": _ID},
        ["request_id", "run_id"],
    ),
    "DUPLICATE_OF": _obj(
        {"request_id": _ID, "duplicate_of_request_id": _NID,
         "duplicate_of_feature_id": _NID, "concept_key": _NSTR},
        ["request_id"],
    ),
    "CANDIDATE_SELECTED": _obj(
        {"request_id": _ID, "selected_run_id": _ID, "feature_id": _ID,
         "candidates_explored_count": _INT},
        ["request_id", "selected_run_id", "feature_id"],
    ),
    "CANDIDATE_REJECTED": _obj(
        {"request_id": _ID, "run_id": _ID, "reason": _NSTR},
        ["request_id", "run_id"],
    ),
    # ---- feature stream ----
    "FEATURE_CREATED": _obj(
        {"feature_id": _ID, "request_id": _ID, "concept_key": _NSTR, "origin_run_id": _NID},
        ["feature_id", "request_id"],
    ),
    "VERSION_MINTED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "produced_by_run": _ID,
         "base_feature_version_id": _NID},
        ["feature_id", "feature_version_id", "produced_by_run"],
    ),
    "VERSION_ACTIVATED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR,
         "activation_state": _ACTIVATION_STATE, "base_feature_version_id": _NID},
        ["feature_id", "feature_version_id", "use_case", "activation_state"],
    ),
    "ACTIVATION_CONFLICT": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR,
         "base_feature_version_id": _NID, "current_active_version_id": _NID, "reason": _NSTR},
        ["feature_id", "feature_version_id", "use_case"],
    ),
    # §3.8 governance guard rejected the activation/supersession (use_case_not_blocked or a
    # policy-parameterized guard). Audited with the failed guard name + resolved inputs/result.
    "ACTIVATION_BLOCKED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR, "guard": _STR,
         "base_feature_version_id": _NID, "approval_type": _APPROVAL_TYPE,
         "guard_inputs": {"type": "object"}, "guard_result": {}},
        ["feature_id", "feature_version_id", "use_case", "guard"],
    ),
    "VERSION_SUPERSEDED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR,
         "superseded_version_id": _NID},
        ["feature_id", "feature_version_id", "use_case"],
    ),
    "VERSION_QUIESCED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR,
         "impacted_consumers": {"type": "array"}, "grace_seconds": _INT, "reason": _NSTR},
        ["feature_id", "feature_version_id", "use_case", "impacted_consumers"],
    ),
    "VERSION_DEPRECATED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _NSTR,
         "reason": _NSTR, "via": _NSTR},
        ["feature_id", "feature_version_id"],
    ),
    "VERSION_RETIERED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "new_risk_tier": _STR,
         "old_risk_tier": _NSTR, "requested_by": _NSTR},
        ["feature_id", "feature_version_id", "new_risk_tier"],
    ),
    "VERSION_EXPIRED": _obj(
        {"feature_id": _ID, "feature_version_id": _ID, "use_case": _STR},
        ["feature_id", "feature_version_id", "use_case"],
    ),
    "CONSUMER_REGISTERED": _obj(
        {"feature_id": _ID, "consumer_id": _ID, "consumer_kind": _CONSUMER_KIND,
         "consumer_ref": _STR},
        ["feature_id", "consumer_id", "consumer_kind", "consumer_ref"],
    ),
    "CONSUMER_DEREGISTERED": _obj(
        {"feature_id": _ID, "consumer_id": _ID, "consumer_kind": _CONSUMER_KIND,
         "consumer_ref": _STR},
        ["feature_id", "consumer_id"],
    ),
    "MONITORING_ALERT_RAISED": _obj(
        {"feature_id": _ID, "feature_version_id": _NID, "alert_ref": _NSTR},
        ["feature_id"],
    ),
    "REVALIDATION_REQUIRED": _obj(
        {"feature_id": _ID, "feature_version_id": _NID, "reason": _NSTR},
        ["feature_id"],
    ),
    "REVALIDATION_OUTCOME_RECORDED": _obj(
        {"feature_id": _ID, "outcome": _STR, "feature_version_id": _NID, "new_run_id": _NID},
        ["feature_id", "outcome"],
    ),
    # ---- run stream ----
    "RUN_CREATED": _obj(
        {"run_id": _ID, "request_id": _NID, "reopened_from": _NID,
         "feature_id": _NID, "origin": _NSTR},
        ["run_id"],
    ),
    "RUN_CANCELLED": _obj({"run_id": _ID, "reason": _NSTR}, ["run_id"]),
    "RUN_WITHDRAWN": _obj({"run_id": _ID, "reason": _NSTR}, ["run_id"]),
    "RUN_REJECTED": _obj({"run_id": _ID, "reason": _NSTR}, ["run_id"]),
    "RUN_PARKED": _obj(
        {"run_id": _ID, "owner": _NSTR, "waiting_on_fact": _NSTR}, ["run_id"]
    ),
    "RUN_UNPARKED": _obj({"run_id": _ID}, ["run_id"]),
    "FACT_CONFIRMED_RESUME": _obj({"run_id": _ID, "fact_key": _STR}, ["run_id", "fact_key"]),
    "SOURCE_CHANGED_REVALIDATE": _obj(
        {"run_id": _ID, "source_ref": _STR, "new_snapshot": _NSTR},
        ["run_id", "source_ref"],
    ),
    # saga step 1: emitted on the RUN stream in the run's own tx (§5.8); drives the
    # feature-side activate_version handler. Carries every arg apply_activation needs,
    # because the Phase-04 worker passes only HandlerContext (run_id + this triggering
    # event), never the queue payload, to the handler.
    "ACTIVATION_REQUESTED": _obj(
        {"run_id": _ID, "feature_id": _ID, "feature_version_id": _ID, "use_case": _STR,
         "approval_type": _APPROVAL_TYPE, "base_feature_version_id": _NID, "expires_at": _NSTR},
        ["run_id", "feature_id", "feature_version_id", "use_case", "approval_type"],
    ),
}


def register_phase06_event_types(registry: SchemaRegistry) -> None:
    for type_name, schema in EVENT_SCHEMAS.items():
        registry.register_schema(type_name, 1, schema, OWNER)
