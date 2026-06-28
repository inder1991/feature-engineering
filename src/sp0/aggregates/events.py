from __future__ import annotations

from sp0.contracts import SchemaRegistry

OWNER = "sp0-aggregates"


def _obj(required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {key: {} for key in required},
        "required": required,
        "additionalProperties": True,
    }


EVENT_SCHEMAS: dict[str, dict] = {
    # request stream
    "REQUEST_CREATED": _obj(["request_id", "concept_key"]),
    "CANDIDATE_ADDED": _obj(["request_id", "run_id"]),
    "DUPLICATE_OF": _obj(["request_id"]),
    "CANDIDATE_SELECTED": _obj(["request_id", "selected_run_id", "feature_id"]),
    "CANDIDATE_REJECTED": _obj(["request_id", "run_id"]),
    # feature stream
    "FEATURE_CREATED": _obj(["feature_id", "request_id"]),
    "VERSION_MINTED": _obj(["feature_id", "feature_version_id", "produced_by_run"]),
    "VERSION_ACTIVATED": _obj(["feature_id", "feature_version_id", "use_case", "activation_state"]),
    "ACTIVATION_CONFLICT": _obj(["feature_id", "feature_version_id", "use_case"]),
    # §3.8 governance guard rejected the activation/supersession (use_case_not_blocked or a
    # policy-parameterized guard). Audited with the failed guard name + resolved inputs/result.
    "ACTIVATION_BLOCKED": _obj(["feature_id", "feature_version_id", "use_case", "guard"]),
    "VERSION_SUPERSEDED": _obj(["feature_id", "feature_version_id", "use_case"]),
    "VERSION_QUIESCED": _obj(["feature_id", "feature_version_id", "use_case", "impacted_consumers"]),
    "VERSION_DEPRECATED": _obj(["feature_id", "feature_version_id"]),
    "VERSION_RETIERED": _obj(["feature_id", "feature_version_id", "new_risk_tier"]),
    "VERSION_EXPIRED": _obj(["feature_id", "feature_version_id", "use_case"]),
    "CONSUMER_REGISTERED": _obj(["feature_id", "consumer_id", "consumer_kind", "consumer_ref"]),
    "CONSUMER_DEREGISTERED": _obj(["feature_id", "consumer_id"]),
    "MONITORING_ALERT_RAISED": _obj(["feature_id"]),
    "REVALIDATION_REQUIRED": _obj(["feature_id"]),
    "REVALIDATION_OUTCOME_RECORDED": _obj(["feature_id", "outcome"]),
    # run stream
    "RUN_CREATED": _obj(["run_id"]),
    "RUN_CANCELLED": _obj(["run_id"]),
    "RUN_WITHDRAWN": _obj(["run_id"]),
    "RUN_REJECTED": _obj(["run_id"]),
    "RUN_PARKED": _obj(["run_id"]),
    "RUN_UNPARKED": _obj(["run_id"]),
    "FACT_CONFIRMED_RESUME": _obj(["run_id", "fact_key"]),
    "SOURCE_CHANGED_REVALIDATE": _obj(["run_id", "source_ref"]),
    # saga step 1: emitted on the RUN stream in the run's own tx (§5.8); drives the
    # feature-side activate_version handler. Carries every arg apply_activation needs,
    # because the Phase-04 worker passes only HandlerContext (run_id + this triggering
    # event), never the queue payload, to the handler.
    "ACTIVATION_REQUESTED": _obj(["run_id", "feature_id", "feature_version_id",
                                  "use_case", "approval_type"]),
}


def register_phase06_event_types(registry: SchemaRegistry) -> None:
    for type_name, schema in EVENT_SCHEMAS.items():
        registry.register_schema(type_name, 1, schema, OWNER)
