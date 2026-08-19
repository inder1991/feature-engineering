"""Closed, bounded provider projection for recipe-backed formula authoring.

**This module is the fail-close boundary to a provider.** Nothing reaches a model that this
whitelist has not exact-keyed and bounded, and a shape it does not recognise is refused rather
than forwarded.

Two expectation generations cross it, and they are dispatched on the payload's OWN declared
schema version (task A4, increment 1):

* **v1 — the UNDECLARED shape.** It carries no version key, because it never did: its bytes are
  frozen inside live ``recipe_formula_shadow_work_item`` rows, whose ``provider_input_hash`` and
  ``payload_hash`` were sealed before this arm existed, and the worker re-validates that stored
  payload before every dispatch (``recipe_formula_worker.py:301``). Adding a key to the v1 shape
  would refuse every work item already on the queue. So absence IS the v1 declaration, the v1 arm
  is byte-frozen, and a test pins its source.
* **v2 — the DECLARED shape**, ``formula_schema_version == "formula-v2"`` (the
  ``recipe_contract_v2.FormulaReferenceV2`` literal). Twelve expression keys, ten window keys,
  and every one of them bounded.

Anything else — a declaration this module does not know, a null declaration, a stray key on
either shape — is a :class:`RecipeEgressViolation`. The lengths and counts below are this
module's OWN bounds, stated here rather than imported, so that widening the grammar can never
silently widen the boundary; the closed token vocabularies (aggregate names, window bases, units)
ARE read from the grammar, because they are our own authored words and a name the grammar does
not know is not a name a reviewer can act on. A test pins the offset bound against the grammar's.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from featuregen.formula.operations_v2 import operation_rule
from featuregen.formula.schema_leaves import (
    EmptyWindowResult,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    WindowUnit,
)
from featuregen.formula.schema_v2 import (
    AggregateFunctionV2,
    FinalOperationV2,
    WindowBasisV2,
)
from featuregen.intake.redaction import REDACTION_VERSION
from featuregen.overlay.upload.recipe_formula_contracts import (
    BoundRecipeFormulaExpectationV1,
)
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    BoundRecipeFormulaExpectationV2,
)
from featuregen.overlay.upload.sample_parser import parse_sample_profile
from featuregen.overlay.upload.sanitize import SANITIZER_VERSION, sanitize_definition

RECIPE_EGRESS_POLICY_VERSION = 2
FORMULA_PROSE_POLICY_VERSION = (
    f"recipe-formula-prose-v1+{SANITIZER_VERSION}+{REDACTION_VERSION}"
)
#: The one declared expectation generation this boundary knows. The v1 shape declares nothing.
FORMULA_EXPECTATION_SCHEMA_V2 = "formula-v2"
MAX_PROSE = 4_000
MAX_REF = 512
MAX_ROLE = 128
MAX_EXPRESSIONS = 4
MAX_TOOL_ITEMS = 25
MAX_WINDOW_LENGTH = 100_000
#: This boundary's own offset bound, pinned against ``schema_v2.MAX_WINDOW_OFFSET_PERIODS`` by a
#: test rather than imported from it — a grammar that widens must widen egress deliberately.
MAX_OFFSET_PERIODS = 12
#: A governed policy identifier (``eligible_status:foundation-posted-events``). Bounded and
#: character-classed: an authority ref is a key, never prose, and prose is what leaks.
_POLICY_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]*\Z")

_EXPECTATION_KEYS_V1 = frozenset({
    "final_operation",
    "expressions",
    "grain_entity",
    "grain_key_refs",
    "decimal",
    "policy_version",
})
_EXPRESSION_KEYS_V1 = frozenset({
    "expression_path",
    "aggregation",
    "operand_ref",
    "source_relation_ref",
    "event_time_ref",
    "window_length",
    "window",
})
_WINDOW_KEYS_V1 = frozenset({
    "event_time_role",
    "basis",
    "length_parameter",
    "unit",
    "start_inclusive",
    "end_inclusive",
    "timezone",
    "empty_window",
    "null_input",
})
_EXPECTATION_KEYS_V2 = _EXPECTATION_KEYS_V1 | {"formula_schema_version"}
#: The ``BoundExpressionExpectationV2`` keys. ``row_selections`` (C-A3b) is a STRUCTURAL statement
#: the model may see — it is what tells the author "this expression wants DEBIT rows" without the
#: author inferring it from the recipe name — so it crosses the boundary, bounded and closed like
#: every other token here.
_EXPRESSION_KEYS_V2 = _EXPRESSION_KEYS_V1 | {
    "second_operand_ref",
    "aggregation_argument",
    "authority_refs",
    "row_selections",
    "term_name",
    "term_sign",
}
#: The three ``SemanticRowSelectionV1`` keys, and its closed vocabularies. A selection carries no
#: free text: the kind and the value are members of OUR vocabulary, and the role is bounded.
_SELECTION_KEYS = frozenset({"kind", "role", "semantic_value"})
_WINDOW_KEYS_V2 = _WINDOW_KEYS_V1 | {"offset_periods"}
_AUTHORITY_REF_KEYS = frozenset({
    "status_policy_ref",
    "direction_policy_ref",
    "reversal_policy_ref",
    "currency_conversion_ref",
})

_AGGREGATIONS_V2 = frozenset(member.value for member in AggregateFunctionV2)
_FINAL_OPERATIONS_V2 = frozenset(member.value for member in FinalOperationV2)
_WINDOW_ENUM_VOCABULARY: dict[str, frozenset[str]] = {
    "basis": frozenset(member.value for member in WindowBasisV2),
    "unit": frozenset(member.value for member in WindowUnit),
    "start_inclusive": frozenset(member.value for member in Inclusivity),
    "end_inclusive": frozenset(member.value for member in Inclusivity),
    "empty_window": frozenset(member.value for member in EmptyWindowResult),
    "null_input": frozenset(member.value for member in NullInput),
}
_ROUNDING_MODES = frozenset(member.value for member in RoundingMode)
_OVERFLOW_BEHAVIORS = frozenset(member.value for member in OverflowBehavior)

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


def _exact_keys(value: Mapping[str, Any], expected: AbstractSet[str], path: str) -> None:
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
    """Dispatch on the payload's OWN declared expectation generation, and fail close.

    Absence of ``formula_schema_version`` is the v1 declaration (see the module docstring: the
    stored payloads that predate v2 cannot grow a key). Any other declared value — including
    ``null`` — is refused, never guessed at.
    """
    if not isinstance(value, Mapping):
        raise RecipeEgressViolation("formula_expectation must be an object")
    if "formula_schema_version" not in value:
        _validate_formula_expectation_v1(value)
    elif value["formula_schema_version"] == FORMULA_EXPECTATION_SCHEMA_V2:
        _validate_formula_expectation_v2(value)
    else:
        raise RecipeEgressViolation(
            "unsupported declared formula expectation schema version: "
            f"{value['formula_schema_version']!r}")
    return _plain(dict(value))


def _validate_formula_expectation_v1(value: Mapping[str, Any]) -> None:
    """THE FROZEN v1 ARM. Its behaviour is pinned byte-for-byte against the pre-A4
    implementation (a golden payload digest and every refusal message, in
    ``test_recipe_egress.py``) and its source is hash-pinned so it cannot drift unnoticed.
    Live work items were sealed against exactly these bounds."""
    _exact_keys(value, _EXPECTATION_KEYS_V1, "formula_expectation")
    _bounded_text(value["final_operation"], path="final_operation", limit=MAX_ROLE)
    _bounded_text(value["grain_entity"], path="grain_entity", limit=MAX_ROLE)
    expressions = value["expressions"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= MAX_EXPRESSIONS:
        raise RecipeEgressViolation("expressions must be a bounded non-empty list")
    for index, expression in enumerate(expressions):
        if not isinstance(expression, Mapping):
            raise RecipeEgressViolation(f"expressions[{index}] must be an object")
        _exact_keys(expression, _EXPRESSION_KEYS_V1, f"expressions[{index}]")
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
        _validate_window_length(expression["window_length"])
        window = expression["window"]
        if not isinstance(window, Mapping):
            raise RecipeEgressViolation("window must be an object")
        _exact_keys(window, _WINDOW_KEYS_V1, f"expressions[{index}].window")
        for key, item in window.items():
            _bounded_text(
                item, path=f"expressions[{index}].window.{key}", limit=MAX_ROLE)
    _validate_grain_key_refs(value["grain_key_refs"])
    decimal = value["decimal"]
    if not isinstance(decimal, Mapping):
        raise RecipeEgressViolation("decimal must be an object")
    _exact_keys(decimal, {"precision", "scale", "rounding", "overflow"}, "decimal")
    _validate_decimal_precision(decimal)
    _bounded_text(decimal["rounding"], path="decimal.rounding", limit=MAX_ROLE)
    _bounded_text(decimal["overflow"], path="decimal.overflow", limit=MAX_ROLE)
    if not isinstance(value["policy_version"], int):
        raise RecipeEgressViolation("policy_version must be an integer")


def _validate_window_length(length: Any) -> None:
    if (not isinstance(length, int) or isinstance(length, bool)
            or not 1 <= length <= MAX_WINDOW_LENGTH):
        raise RecipeEgressViolation("window_length is outside the reviewed bound")


def _validate_grain_key_refs(grain_refs: Any) -> None:
    if not isinstance(grain_refs, list) or not 1 <= len(grain_refs) <= 16:
        raise RecipeEgressViolation("grain_key_refs must be a bounded non-empty list")
    for index, ref in enumerate(grain_refs):
        _validate_ref(ref, f"grain_key_refs[{index}]")


def _validate_decimal_precision(decimal: Mapping[str, Any]) -> None:
    if (
        not isinstance(decimal["precision"], int)
        or not isinstance(decimal["scale"], int)
        or not 1 <= decimal["precision"] <= 38
        or not 0 <= decimal["scale"] <= decimal["precision"]
    ):
        raise RecipeEgressViolation("decimal precision/scale is invalid")


def _closed_token(value: Any, vocabulary: AbstractSet[str], path: str) -> str:
    """A token from one of OUR closed vocabularies. Bounded first, then a member — a name the
    grammar does not know is not a name a reviewer can act on."""
    token = _bounded_text(value, path=path, limit=MAX_ROLE)
    if token not in vocabulary:
        raise RecipeEgressViolation(f"{path} is not in the closed vocabulary: {token!r}")
    return token


def _validate_policy_ref(value: Any, path: str) -> None:
    ref = _bounded_text(value, path=path, limit=MAX_ROLE, allow_empty=True)
    if ref and not _POLICY_REF_PATTERN.fullmatch(ref):
        raise RecipeEgressViolation(f"{path} is not a governed policy identifier")


def _validate_authority_refs(value: Any, path: str) -> None:
    """``None`` is the honest answer for an expression under no governed row policy; a present
    block is exactly four bounded policy identifiers, at least one of them non-blank (the same
    non-vacuity law ``AuthorityRefsV2`` states)."""
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise RecipeEgressViolation(f"{path} must be an object or null")
    _exact_keys(value, _AUTHORITY_REF_KEYS, path)
    for key in sorted(_AUTHORITY_REF_KEYS):
        _validate_policy_ref(value[key], f"{path}.{key}")
    if not any(str(value[key]).strip() for key in _AUTHORITY_REF_KEYS):
        raise RecipeEgressViolation(f"{path} declares no policy at all")


def _validate_window_v2(window: Any, path: str) -> None:
    if not isinstance(window, Mapping):
        raise RecipeEgressViolation("window must be an object")
    _exact_keys(window, _WINDOW_KEYS_V2, path)
    for key in ("event_time_role", "length_parameter", "timezone"):
        _bounded_text(window[key], path=f"{path}.{key}", limit=MAX_ROLE)
    for key, vocabulary in _WINDOW_ENUM_VOCABULARY.items():
        _closed_token(window[key], vocabulary, f"{path}.{key}")
    offset = window["offset_periods"]
    if (not isinstance(offset, int) or isinstance(offset, bool)
            or not 0 <= offset <= MAX_OFFSET_PERIODS):
        raise RecipeEgressViolation(f"{path}.offset_periods is outside the reviewed bound")


def _validate_selections(selections: Any, path: str) -> None:
    """C-A3b — the row selections, egressed under the same closed discipline as every other token.

    Membership is checked against :mod:`schema_v3`'s vocabulary rather than a copy: the whole point
    of a closed token set is that ONE definition says what a legal selection is, and a second copy
    here would be a boundary that could quietly widen while the schema stayed narrow.
    """
    from featuregen.formula.schema_v3 import SELECTION_TOKENS, SelectionKind

    if not isinstance(selections, tuple | list):
        raise RecipeEgressViolation(f"{path} must be a sequence")
    seen: set[tuple[str, str]] = set()
    for index, selection in enumerate(selections):
        where = f"{path}[{index}]"
        if not isinstance(selection, Mapping):
            raise RecipeEgressViolation(f"{where} must be an object")
        _exact_keys(selection, _SELECTION_KEYS, where)
        kind = _closed_token(
            selection["kind"], {k.value for k in SelectionKind}, f"{where}.kind")
        _bounded_text(selection["role"], path=f"{where}.role", limit=MAX_ROLE)
        _closed_token(selection["semantic_value"],
                      SELECTION_TOKENS[SelectionKind(kind)], f"{where}.semantic_value")
        key = (kind, selection["role"])
        if key in seen:
            raise RecipeEgressViolation(f"{where} duplicates (kind={kind}, role={key[1]!r})")
        seen.add(key)


def _validate_expression_v2(expression: Any, path: str, *, signed_sum: bool) -> None:
    if not isinstance(expression, Mapping):
        raise RecipeEgressViolation(f"{path} must be an object")
    _exact_keys(expression, _EXPRESSION_KEYS_V2, path)
    _bounded_text(expression["expression_path"], path=f"{path}.expression_path", limit=MAX_ROLE)
    aggregation = _closed_token(
        expression["aggregation"], _AGGREGATIONS_V2, f"{path}.aggregation")
    rule = operation_rule(AggregateFunctionV2(aggregation))

    operand = expression["operand_ref"]
    if operand is not None:
        _validate_ref(operand, f"{path}.operand_ref")
    if rule.operand_required != (operand is not None):
        raise RecipeEgressViolation(f"{path}.operand_ref disagrees with {aggregation}")
    second_operand = expression["second_operand_ref"]
    if second_operand is not None:
        _validate_ref(second_operand, f"{path}.second_operand_ref")
    if ((rule.second_operand == "required" and second_operand is None)
            or (rule.second_operand == "forbidden" and second_operand is not None)):
        raise RecipeEgressViolation(f"{path}.second_operand_ref disagrees with {aggregation}")

    _validate_selections(expression["row_selections"], f"{path}.row_selections")

    _validate_ref(expression["source_relation_ref"], f"{path}.source_relation_ref")
    _validate_ref(expression["event_time_ref"], f"{path}.event_time_ref")
    _validate_window_length(expression["window_length"])

    argument = expression["aggregation_argument"]
    if rule.argument == "percentile":
        if (not isinstance(argument, (int, float)) or isinstance(argument, bool)
                or not 0 < argument < 100):
            raise RecipeEgressViolation(
                f"{path}.aggregation_argument must be a percentile strictly inside (0, 100)")
    elif argument is not None:
        raise RecipeEgressViolation(f"{path}.aggregation_argument: {aggregation} takes none")

    _validate_authority_refs(expression["authority_refs"], f"{path}.authority_refs")

    term_name = expression["term_name"]
    term_sign = expression["term_sign"]
    if not isinstance(term_sign, int) or isinstance(term_sign, bool):
        raise RecipeEgressViolation(f"{path}.term_sign must be an integer")
    _bounded_text(term_name, path=f"{path}.term_name", limit=MAX_ROLE, allow_empty=True)
    if signed_sum:
        if not term_name.strip() or term_sign not in (1, -1):
            raise RecipeEgressViolation(
                f"{path} is a signed-sum term: it carries a name and a sign of +1 or -1")
    elif term_name or term_sign != 0:
        raise RecipeEgressViolation(f"{path} names or signs a term outside a signed sum")

    _validate_window_v2(expression["window"], f"{path}.window")


def _validate_formula_expectation_v2(value: Mapping[str, Any]) -> None:
    """The DECLARED v2 arm. Twelve expression keys, ten window keys, every one bounded."""
    _exact_keys(value, _EXPECTATION_KEYS_V2, "formula_expectation")
    final_operation = _closed_token(
        value["final_operation"], _FINAL_OPERATIONS_V2, "final_operation")
    _bounded_text(value["grain_entity"], path="grain_entity", limit=MAX_ROLE)
    expressions = value["expressions"]
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= MAX_EXPRESSIONS:
        raise RecipeEgressViolation("expressions must be a bounded non-empty list")
    signed_sum = final_operation == FinalOperationV2.SIGNED_SUM.value
    if signed_sum and len(expressions) < 2:
        raise RecipeEgressViolation("a signed sum carries at least two terms")
    for index, expression in enumerate(expressions):
        _validate_expression_v2(expression, f"expressions[{index}]", signed_sum=signed_sum)
    _validate_grain_key_refs(value["grain_key_refs"])
    decimal = value["decimal"]
    if not isinstance(decimal, Mapping):
        raise RecipeEgressViolation("decimal must be an object")
    _exact_keys(decimal, {"precision", "scale", "rounding", "overflow"}, "decimal")
    _validate_decimal_precision(decimal)
    _closed_token(decimal["rounding"], _ROUNDING_MODES, "decimal.rounding")
    _closed_token(decimal["overflow"], _OVERFLOW_BEHAVIORS, "decimal.overflow")
    if not isinstance(value["policy_version"], int) or isinstance(value["policy_version"], bool):
        raise RecipeEgressViolation("policy_version must be an integer")


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
    expectation: BoundRecipeFormulaExpectationV1 | BoundRecipeFormulaExpectationV2,
) -> RecipeAuthoringEgressV1:
    """Project a bound expectation of EITHER generation onto the provider vocabulary.

    The projection is deliberately narrow and identical in both generations, plus v2's own
    declaration: the governance keys a bound expectation also carries (``expectation_ref``,
    ``recipe_candidate_key``, ``blueprint_content_hash``, ``semantic_parameter_binding_hash``,
    ``allocation_policy_ref``) stay server-private — a provider authors a formula, it does not
    audit our registry.
    """
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
    if isinstance(expectation, BoundRecipeFormulaExpectationV2):
        formula_expectation["formula_schema_version"] = FORMULA_EXPECTATION_SCHEMA_V2
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
