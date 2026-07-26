"""Closed, bounded provider projection for recipe-backed formula authoring."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from featuregen.intake.redaction import REDACTION_VERSION
from featuregen.overlay.upload.recipe_formula_contracts import (
    BoundRecipeFormulaExpectationV1,
)
from featuregen.overlay.upload.sample_parser import parse_sample_profile
from featuregen.overlay.upload.sanitize import SANITIZER_VERSION, sanitize_definition

RECIPE_EGRESS_POLICY_VERSION = 2
FORMULA_PROSE_POLICY_VERSION = (
    f"recipe-formula-prose-v1+{SANITIZER_VERSION}+{REDACTION_VERSION}"
)
MAX_PROSE = 4_000
MAX_REF = 512
MAX_ROLE = 128
MAX_EXPRESSIONS = 4
MAX_TOOL_ITEMS = 25

_FORBIDDEN_KEYS = frozenset({
    "actor",
    "request_identity",
    "role_claims",
    "groups",
    "tenant",
    "evidence_id",
    "evidence_ids",
    "fact_id",
    "fact_key",
    "provenance",
    "considered_revision_id",
    "metadata_snapshot_id",
    "template_content_hash",
    "semantic_parameter_binding_hash",
    "blueprint_content_hash",
    "recipe_candidate_key",
    "raw_definition",
    "samples",
    "values",
})

_CONTEXTUAL_PERSON_PATTERNS = (
    re.compile(
        r"\b(?i:customer|client|account\s+holder|cardholder)\s+"
        r"(?i:named|called|name\s+is)\s+"
        r"(?P<person>[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3})\b"
    ),
    re.compile(
        r"\b(?i:Mr|Mrs|Ms|Dr)\.?\s+"
        r"(?P<person>[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3})\b"
    ),
)
_CONTEXTUAL_ACCOUNT_PATTERN = re.compile(
    r"\b(?i:account|acct|a/c)(?:\s+(?i:number|no))?\s*[:#-]?\s*"
    r"(?P<identifier>\d[\d -]{7,20}\d)\b"
)


class RecipeEgressViolation(ValueError):
    """A recipe provider payload contains an unknown, forbidden, or unbounded field."""


@dataclass(frozen=True, slots=True)
class RecipeAuthoringEgressV1:
    hypothesis: str
    prediction_goal: str
    target_entity: str
    formula_expectation: dict[str, Any]
    egress_policy_version: int
    redaction_policy_version: str
    input_redaction: dict[str, Any]
    content_hash: str

    def provider_payload(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "prediction_goal": self.prediction_goal,
            "target_entity": self.target_entity,
            "formula_expectation": self.formula_expectation,
            "egress_policy_version": self.egress_policy_version,
            "redaction_policy_version": self.redaction_policy_version,
            "input_redaction": self.input_redaction,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_keys(nested)


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RecipeEgressViolation(
            f"{path} keys differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}")


def _bounded_text(value: Any, *, path: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > limit:
        raise RecipeEgressViolation(f"{path} is not a bounded string")
    return value


def _contextual_name_spans(text: str) -> list[dict[str, int | str]]:
    spans: list[dict[str, int | str]] = []
    for pattern in _CONTEXTUAL_PERSON_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span("person")
            spans.append({"type": "PERSON_NAME", "start": start, "end": end})
    return sorted(spans, key=lambda span: (int(span["start"]), int(span["end"])))


def _contextual_account_spans(text: str) -> list[dict[str, int | str]]:
    return [
        {"type": "ACCOUNT", "start": match.start("identifier"), "end": match.end("identifier")}
        for match in _CONTEXTUAL_ACCOUNT_PATTERN.finditer(text)
    ]


def _replace_spans(text: str, spans: list[dict[str, int | str]]) -> str:
    redacted = text
    for span in reversed(spans):
        start, end = int(span["start"]), int(span["end"])
        redacted = redacted[:start] + f"[REDACTED:{span['type']}]" + redacted[end:]
    return redacted


def _sample_value_spans(text: str) -> list[dict[str, int | str]]:
    spans: list[dict[str, int | str]] = []
    cursor = 0
    for value in parse_sample_profile(text).sample_values:
        start = text.find(value, cursor)
        if start < 0:
            start = text.find(value)
        if start >= 0:
            end = start + len(value)
            spans.append({"type": "SAMPLE_VALUE", "start": start, "end": end})
            cursor = end
    return spans


def _sanitize_formula_prose(value: Any, *, field: str) -> tuple[str, list[dict[str, Any]]]:
    text = _bounded_text(value, path=field, limit=MAX_PROSE)
    contextual_spans = sorted(
        [*_contextual_name_spans(text), *_contextual_account_spans(text)],
        key=lambda span: (int(span["start"]), int(span["end"])),
    )
    sample_spans = _sample_value_spans(text)
    sanitized = sanitize_definition(_replace_spans(text, contextual_spans))
    if sanitized.reason or not sanitized.clean.strip():
        raise RecipeEgressViolation(f"{field} prose redaction failed closed")
    if sanitized.redaction_version != REDACTION_VERSION:
        raise RecipeEgressViolation(f"{field} used an unsupported redaction policy")
    pii_spans = [
        {"type": str(span["type"]), "start": int(span["start"]), "end": int(span["end"])}
        for span in sanitized.redacted_spans
    ]
    raw_spans: list[Mapping[str, Any]] = [
        *contextual_spans,
        *sample_spans,
        *pii_spans,
    ]
    audit: list[dict[str, Any]] = [
        {"type": str(span["type"]), "start": int(span["start"]), "end": int(span["end"])}
        for span in raw_spans
    ]
    audit.sort(
        key=lambda span: (int(span["start"]), int(span["end"]), str(span["type"]))
    )
    return sanitized.clean, audit


def _validate_redaction_audit(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise RecipeEgressViolation("input_redaction must be an object")
    _exact_keys(value, {"hypothesis", "prediction_goal"}, "input_redaction")
    for field in ("hypothesis", "prediction_goal"):
        field_audit = value[field]
        if not isinstance(field_audit, Mapping):
            raise RecipeEgressViolation(f"input_redaction.{field} must be an object")
        _exact_keys(field_audit, {"redacted_spans"}, f"input_redaction.{field}")
        spans = field_audit["redacted_spans"]
        if not isinstance(spans, list) or len(spans) > 128:
            raise RecipeEgressViolation(f"input_redaction.{field}.redacted_spans is invalid")
        for span in spans:
            if not isinstance(span, Mapping):
                raise RecipeEgressViolation("redaction span must be an object")
            _exact_keys(span, {"type", "start", "end"}, "redaction span")
            _bounded_text(span["type"], path="redaction span type", limit=MAX_ROLE)
            if (
                not isinstance(span["start"], int)
                or isinstance(span["start"], bool)
                or not isinstance(span["end"], int)
                or isinstance(span["end"], bool)
                or not 0 <= span["start"] < span["end"] <= MAX_PROSE
            ):
                raise RecipeEgressViolation("redaction span positions are invalid")


def _validate_ref(value: Any, path: str) -> str:
    ref = _bounded_text(value, path=path, limit=MAX_REF)
    if "::" not in ref:
        raise RecipeEgressViolation(f"{path} is not source-qualified")
    return ref


def _validate_formula_expectation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecipeEgressViolation("formula_expectation must be an object")
    expected = {
        "final_operation",
        "expressions",
        "grain_entity",
        "grain_key_refs",
        "decimal",
        "policy_version",
    }
    _exact_keys(value, expected, "formula_expectation")
    _bounded_text(value["final_operation"], path="final_operation", limit=MAX_ROLE)
    _bounded_text(value["grain_entity"], path="grain_entity", limit=MAX_ROLE)
    expressions = value["expressions"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= MAX_EXPRESSIONS:
        raise RecipeEgressViolation("expressions must be a bounded non-empty list")
    expression_keys = {
        "expression_path",
        "aggregation",
        "operand_ref",
        "source_relation_ref",
        "event_time_ref",
        "window_length",
        "window",
    }
    window_keys = {
        "event_time_role",
        "basis",
        "length_parameter",
        "unit",
        "start_inclusive",
        "end_inclusive",
        "timezone",
        "empty_window",
        "null_input",
    }
    for index, expression in enumerate(expressions):
        if not isinstance(expression, Mapping):
            raise RecipeEgressViolation(f"expressions[{index}] must be an object")
        _exact_keys(expression, expression_keys, f"expressions[{index}]")
        for key in ("expression_path", "aggregation"):
            _bounded_text(
                expression[key], path=f"expressions[{index}].{key}", limit=MAX_ROLE)
        operand = expression["operand_ref"]
        if operand is not None:
            _validate_ref(operand, f"expressions[{index}].operand_ref")
        _validate_ref(
            expression["source_relation_ref"],
            f"expressions[{index}].source_relation_ref",
        )
        _validate_ref(
            expression["event_time_ref"],
            f"expressions[{index}].event_time_ref",
        )
        length = expression["window_length"]
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 100_000:
            raise RecipeEgressViolation("window_length is outside the reviewed bound")
        window = expression["window"]
        if not isinstance(window, Mapping):
            raise RecipeEgressViolation("window must be an object")
        _exact_keys(window, window_keys, f"expressions[{index}].window")
        for key, item in window.items():
            _bounded_text(
                item, path=f"expressions[{index}].window.{key}", limit=MAX_ROLE)
    grain_refs = value["grain_key_refs"]
    if not isinstance(grain_refs, list) or not 1 <= len(grain_refs) <= 16:
        raise RecipeEgressViolation("grain_key_refs must be a bounded non-empty list")
    for index, ref in enumerate(grain_refs):
        _validate_ref(ref, f"grain_key_refs[{index}]")
    decimal = value["decimal"]
    if not isinstance(decimal, Mapping):
        raise RecipeEgressViolation("decimal must be an object")
    _exact_keys(decimal, {"precision", "scale", "rounding", "overflow"}, "decimal")
    if (
        not isinstance(decimal["precision"], int)
        or not isinstance(decimal["scale"], int)
        or not 1 <= decimal["precision"] <= 38
        or not 0 <= decimal["scale"] <= decimal["precision"]
    ):
        raise RecipeEgressViolation("decimal precision/scale is invalid")
    _bounded_text(decimal["rounding"], path="decimal.rounding", limit=MAX_ROLE)
    _bounded_text(decimal["overflow"], path="decimal.overflow", limit=MAX_ROLE)
    if not isinstance(value["policy_version"], int):
        raise RecipeEgressViolation("policy_version must be an integer")
    return _plain(dict(value))


def validate_recipe_provider_payload(payload: Mapping[str, Any]) -> None:
    _exact_keys(
        payload,
        {
            "hypothesis",
            "prediction_goal",
            "target_entity",
            "formula_expectation",
            "egress_policy_version",
            "redaction_policy_version",
            "input_redaction",
        },
        "recipe_egress",
    )
    forbidden = _FORBIDDEN_KEYS & set(_walk_keys(payload))
    if forbidden:
        raise RecipeEgressViolation(f"forbidden recipe egress keys: {sorted(forbidden)}")
    for field in ("hypothesis", "prediction_goal"):
        text = _bounded_text(payload[field], path=field, limit=MAX_PROSE)
        rescanned, residual_audit = _sanitize_formula_prose(text, field=field)
        if rescanned != text or residual_audit:
            raise RecipeEgressViolation(f"{field} contains residual unsafe prose")
    _bounded_text(payload["target_entity"], path="target_entity", limit=MAX_ROLE)
    _validate_formula_expectation(payload["formula_expectation"])
    if payload["egress_policy_version"] != RECIPE_EGRESS_POLICY_VERSION:
        raise RecipeEgressViolation("unsupported recipe egress policy version")
    if payload["redaction_policy_version"] != FORMULA_PROSE_POLICY_VERSION:
        raise RecipeEgressViolation("unsupported formula prose redaction policy version")
    _validate_redaction_audit(payload["input_redaction"])


def build_recipe_authoring_egress(
    *,
    hypothesis: str,
    prediction_goal: str,
    expectation: BoundRecipeFormulaExpectationV1,
) -> RecipeAuthoringEgressV1:
    safe_hypothesis, hypothesis_audit = _sanitize_formula_prose(
        hypothesis, field="hypothesis"
    )
    safe_goal, goal_audit = _sanitize_formula_prose(
        prediction_goal, field="prediction_goal"
    )
    raw = _plain(asdict(expectation))
    formula_expectation = {
        key: raw[key]
        for key in (
            "final_operation",
            "expressions",
            "grain_entity",
            "grain_key_refs",
            "decimal",
            "policy_version",
        )
    }
    input_redaction: dict[str, Any] = {
        "hypothesis": {"redacted_spans": hypothesis_audit},
        "prediction_goal": {"redacted_spans": goal_audit},
    }
    payload = {
        "hypothesis": safe_hypothesis,
        "prediction_goal": safe_goal,
        "target_entity": expectation.grain_entity,
        "formula_expectation": formula_expectation,
        "egress_policy_version": RECIPE_EGRESS_POLICY_VERSION,
        "redaction_policy_version": FORMULA_PROSE_POLICY_VERSION,
        "input_redaction": input_redaction,
    }
    validate_recipe_provider_payload(payload)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return RecipeAuthoringEgressV1(
        hypothesis=safe_hypothesis,
        prediction_goal=safe_goal,
        target_entity=expectation.grain_entity,
        formula_expectation=formula_expectation,
        egress_policy_version=RECIPE_EGRESS_POLICY_VERSION,
        redaction_policy_version=FORMULA_PROSE_POLICY_VERSION,
        input_redaction=input_redaction,
        content_hash=hashlib.sha256(encoded).hexdigest(),
    )


def project_recipe_tool_result(tool_name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Project an internal frozen tool result into the small recipe-provider vocabulary."""
    if tool_name == "get_verified_lineage":
        raise RecipeEgressViolation("recipe authoring does not expose graph lineage")
    if tool_name == "get_column_metadata":
        if result == {"found": False}:
            return {"found": False}
        _exact_keys(
            result,
            {"found", "logical_ref", "table", "column", "data_type", "facts"},
            "get_column_metadata",
        )
        if result["found"] is not True:
            return {"found": False}
        facts = result["facts"]
        if not isinstance(facts, Mapping) or len(facts) > 16:
            raise RecipeEgressViolation("column facts are invalid or over bound")
        projected_facts: dict[str, dict] = {}
        for name, fact in facts.items():
            if not isinstance(fact, Mapping):
                raise RecipeEgressViolation("column fact must be an object")
            _exact_keys(fact, {"value", "authority", "provenance"}, f"facts.{name}")
            _bounded_text(name, path="fact name", limit=MAX_ROLE)
            authority = _bounded_text(
                fact["authority"], path=f"facts.{name}.authority", limit=MAX_ROLE)
            value = fact["value"]
            if value is not None:
                _bounded_text(
                    value, path=f"facts.{name}.value", limit=MAX_REF, allow_empty=True)
            projected_facts[name] = {"value": value, "authority": authority}
        projected = {
            "found": True,
            "logical_ref": _validate_ref(result["logical_ref"], "logical_ref"),
            "table": _bounded_text(result["table"], path="table", limit=MAX_REF),
            "column": _bounded_text(result["column"], path="column", limit=MAX_ROLE),
            "data_type": result["data_type"],
            "facts": projected_facts,
        }
    elif tool_name in {"get_governed_grain", "get_time_anchor"}:
        list_key = "grain_columns" if tool_name == "get_governed_grain" else "time_anchor_columns"
        _exact_keys(result, {"table_ref", list_key, "governed"}, tool_name)
        items = result[list_key]
        if not isinstance(items, list) or len(items) > 16:
            raise RecipeEgressViolation(f"{list_key} is invalid or over bound")
        projected_items = []
        for item in items:
            if not isinstance(item, Mapping):
                raise RecipeEgressViolation(f"{list_key} item must be an object")
            _exact_keys(
                item, {"logical_ref", "column", "authority", "provenance"}, list_key)
            projected_items.append({
                "logical_ref": _validate_ref(item["logical_ref"], "logical_ref"),
                "column": _bounded_text(item["column"], path="column", limit=MAX_ROLE),
                "authority": _bounded_text(
                    item["authority"], path="authority", limit=MAX_ROLE),
            })
        projected = {
            "table_ref": _validate_ref(result["table_ref"], "table_ref"),
            list_key: projected_items,
            "governed": result["governed"] is True,
        }
    else:
        raise RecipeEgressViolation(f"recipe tool {tool_name!r} is not provider-exposable")
    forbidden = _FORBIDDEN_KEYS & set(_walk_keys(projected))
    if forbidden:
        raise RecipeEgressViolation(f"forbidden projected tool keys: {sorted(forbidden)}")
    return projected
