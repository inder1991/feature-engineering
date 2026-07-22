"""Spec §E canonicalization + content hash for ``TypedFormulaV1`` — THE identity contract.

``canonical_json(f)`` converts the §A dataclass tree to a plain JSON document,
applies the §E pinned ordering rules, and serializes it with the vendored
RFC 8785 JCS (``_jcs.dumps``). ``formula_content_hash(f)`` is the sha256 hex
digest of those canonical UTF-8 bytes.

§E rules implemented here (each with the discriminating test in
``tests/featuregen/formula/test_canonical.py``):

- NFC Unicode normalization of every serialized string;
- every ``LogicalRef`` normalized via ``overlay.upload.object_ref``
  (``parse_ref`` + ``normalize_ref``: per-component strip + lower-case)
  BEFORE hashing; a malformed or empty-component ref fails closed;
- ordered slots preserved: ``body.expr``, numerator/denominator,
  minuend/subtrahend — never sorted;
- grain ``keys`` order preserved (ORDER IS SEMANTIC, §D);
- associative ``AND``/``OR``: nested same-op children are FLATTENED first,
  THEN the flattened children are sorted by the sha256 of their own canonical
  JCS bytes (not a mere immediate-children sort); ``NOT`` is never flattened,
  collapsed, or sorted;
- ``IN``/``NOT_IN`` ``right_set`` sorted + deduplicated by each member's
  canonical JCS bytes; ``ParameterDecl.allowed_set`` sorted + deduplicated on
  UTF-16 code units (the JCS key collation);
- ``parameters`` sorted by ``name``; duplicate names rejected (``SchemaError``);
- enums serialize as their ``.value``; typed-literal values stay the canonical
  strings they already are (never floats);
- the hash covers the ``TypedFormulaV1`` identity object ONLY — no capability
  version, no provenance, and nothing else is added. Internal expression paths
  appear in error messages only, never in the serialized document.
"""
from __future__ import annotations

import hashlib
import unicodedata
from enum import Enum
from typing import TypeVar

from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.schema import (
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    DiffBody,
    FilterBool,
    FilterBoolOp,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    FormulaBody,
    FormulaOutputPolicyV1,
    Grain,
    LiteralType,
    ParamClass,
    ParameterDecl,
    ParameterRef,
    RatioBody,
    SchemaError,
    SourceRelation,
    TypedFormulaV1,
    TypedLiteral,
    UnaryBody,
    WindowPolicy,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

__all__ = ["canonical_json", "formula_content_hash"]


def canonical_json(f: TypedFormulaV1) -> str:
    """The §E canonical JSON text of ``f`` (the decoded RFC 8785 UTF-8 bytes)."""
    return _canonical_bytes(f).decode("utf-8")


def formula_content_hash(f: TypedFormulaV1) -> str:
    """``sha256(canonical_json(f))`` hex digest — the formula's content identity."""
    return hashlib.sha256(_canonical_bytes(f)).hexdigest()


def _canonical_bytes(f: TypedFormulaV1) -> bytes:
    return _jcs_dumps(_formula_plain(f))


# ---- leaf helpers ----

_E = TypeVar("_E", bound=Enum)


def _nfc(value: str, path: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{path}: expected a string, got {type(value).__name__}")
    return unicodedata.normalize("NFC", value)


def _opt_nfc(value: str | None, path: str) -> str | None:
    return None if value is None else _nfc(value, path)


def _identity_int(value: int, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{path}: expected an int, got {value!r}")
    return value


def _identity_bool(value: bool, path: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{path}: expected a bool, got {value!r}")
    return value


def _enum_value(member: _E, enum_cls: type[_E], path: str) -> str:
    if not isinstance(member, enum_cls):
        raise SchemaError(f"{path}: expected {enum_cls.__name__}, got {member!r}")
    return member.value


def _ref(ref: str, path: str) -> str:
    """`object_ref`-normalize a LogicalRef (NFC first, then the `_norm` fold)."""
    try:
        source, schema, table, column = parse_ref(_nfc(ref, path))
    except ValueError as exc:
        raise SchemaError(f"{path}: {ref!r} is not a parseable logical_ref") from exc
    components = (source, schema, table) + (() if column is None else (column,))
    if any(not component.strip() for component in components):
        raise SchemaError(f"{path}: {ref!r} has an empty logical_ref component")
    return normalize_ref(source, schema, table, column)


def _utf16_key(value: str) -> bytes:
    """The JCS property-name collation (UTF-16 code units), reused for string sets."""
    return value.encode("utf-16-be")


def _child_hash(plain: dict) -> bytes:
    """§E associative-child sort key: the sha256 of the child's own JCS bytes."""
    return hashlib.sha256(_jcs_dumps(plain)).digest()


# ---- structure converters (dataclass tree -> plain JSON-able tree) ----


def _formula_plain(f: TypedFormulaV1) -> dict:
    if not isinstance(f, TypedFormulaV1):
        raise SchemaError(
            f"formula_content_hash covers TypedFormulaV1 only, got {type(f).__name__}"
        )
    return {
        "formula_schema_version": _identity_int(f.formula_schema_version, "formula_schema_version"),
        "operation_grammar_version": _identity_int(
            f.operation_grammar_version, "operation_grammar_version"
        ),
        "output_policy_version": _identity_int(f.output_policy_version, "output_policy_version"),
        "canonicalization_version": _identity_int(
            f.canonicalization_version, "canonicalization_version"
        ),
        "grain": _grain_plain(f.grain),
        "body": _body_plain(f.body),
        "parameters": _parameters_plain(f.parameters),
        "decimal": _decimal_plain(f.decimal),
        "output": _output_plain(f.output),
    }


def _grain_plain(grain: Grain) -> dict:
    if not isinstance(grain, Grain):
        raise SchemaError(f"grain: expected a Grain, got {type(grain).__name__}")
    return {
        "entity": _nfc(grain.entity, "grain.entity"),
        # ORDER IS SEMANTIC (§D): preserved, never sorted.
        "keys": [_ref(key, f"grain.keys[{i}]") for i, key in enumerate(grain.keys)],
    }


def _body_plain(body: FormulaBody) -> dict:
    if isinstance(body, UnaryBody):
        return {
            "final_operation": body.final_operation.value,
            "expr": _expression_plain(body.expr, "body.expr"),
        }
    if isinstance(body, RatioBody):
        return {
            "final_operation": body.final_operation.value,
            # Ordered slots: numerator/denominator are SEMANTIC, never sorted.
            "numerator": _expression_plain(body.numerator, "body.numerator"),
            "denominator": _expression_plain(body.denominator, "body.denominator"),
            "zero_denominator": body.zero_denominator.value,
        }
    if isinstance(body, DiffBody):
        return {
            "final_operation": body.final_operation.value,
            # Ordered slots: minuend/subtrahend are SEMANTIC, never sorted.
            "minuend": _expression_plain(body.minuend, "body.minuend"),
            "subtrahend": _expression_plain(body.subtrahend, "body.subtrahend"),
        }
    raise SchemaError(
        f"body must be UnaryBody | RatioBody | DiffBody, got {type(body).__name__}"
    )


def _expression_plain(expr: AggregateExpression, path: str) -> dict:
    if not isinstance(expr, AggregateExpression):
        raise SchemaError(f"{path}: expected an AggregateExpression, got {type(expr).__name__}")
    if not isinstance(expr.source_relation, SourceRelation):
        raise SchemaError(f"{path}.source_relation: expected a SourceRelation")
    return {
        "aggregation": _enum_value(expr.aggregation, AggregateFunction, f"{path}.aggregation"),
        "operand": None if expr.operand is None else _ref(expr.operand, f"{path}.operand"),
        "source_relation": {
            "table_ref": _ref(
                expr.source_relation.table_ref, f"{path}.source_relation.table_ref"
            )
        },
        "filter": None if expr.filter is None else _filter_plain(expr.filter, f"{path}.filter"),
        "window": _window_plain(expr.window, f"{path}.window"),
    }


def _window_plain(window: WindowPolicy, path: str) -> dict:
    if not isinstance(window, WindowPolicy):
        raise SchemaError(f"{path}: expected a WindowPolicy, got {type(window).__name__}")
    return {
        "event_time_ref": _ref(window.event_time_ref, f"{path}.event_time_ref"),
        "basis": window.basis.value,
        "length": _identity_int(window.length, f"{path}.length"),
        "unit": window.unit.value,
        "start_inclusive": window.start_inclusive.value,
        "end_inclusive": window.end_inclusive.value,
        "timezone": _nfc(window.timezone, f"{path}.timezone"),
        "empty_window": window.empty_window.value,
        "null_input": window.null_input.value,
    }


def _filter_plain(node: FilterNode, path: str) -> dict:
    if isinstance(node, FilterPredicate):
        return _predicate_plain(node, path)
    if isinstance(node, FilterBool):
        if node.op is FilterBoolOp.NOT:
            # NOT is never flattened, collapsed, or sorted.
            children = [
                _filter_plain(child, f"{path}.children[{i}]")
                for i, child in enumerate(node.children)
            ]
        else:
            # Associative AND/OR: flatten nested same-op children FIRST, then
            # sort the flattened children by their own canonical JCS hash.
            flattened = _flatten_same_op(node.op, node.children)
            children = sorted(
                (_filter_plain(child, f"{path}.children[*]") for child in flattened),
                key=_child_hash,
            )
        return {"kind": node.kind.value, "op": node.op.value, "children": children}
    raise SchemaError(
        f"{path}: filter node must be FilterPredicate | FilterBool, got {type(node).__name__}"
    )


def _flatten_same_op(
    op: FilterBoolOp, children: tuple[FilterNode, ...]
) -> list[FilterNode]:
    flattened: list[FilterNode] = []
    for child in children:
        if isinstance(child, FilterBool) and child.op is op:
            flattened.extend(_flatten_same_op(op, child.children))
        else:
            flattened.append(child)
    return flattened


def _predicate_plain(node: FilterPredicate, path: str) -> dict:
    plain: dict = {
        "kind": node.kind.value,
        "op": _enum_value(node.op, FilterPredicateOp, f"{path}.op"),
        "left": _ref(node.left, f"{path}.left"),
        "right_literal": None
        if node.right_literal is None
        else _literal_plain(node.right_literal, f"{path}.right_literal"),
        "right_param": None,
        "right_set": None,
    }
    if node.right_param is not None:
        if not isinstance(node.right_param, ParameterRef):
            raise SchemaError(f"{path}.right_param: expected a ParameterRef")
        plain["right_param"] = {"name": _nfc(node.right_param.name, f"{path}.right_param.name")}
    if node.right_set is not None:
        members = [
            _literal_plain(member, f"{path}.right_set[{i}]")
            for i, member in enumerate(node.right_set)
        ]
        # §E: sorted + deduplicated, keyed on each member's canonical JCS bytes
        # (dedup runs AFTER NFC + value normalization, so equivalent forms merge).
        deduped = {_jcs_dumps(member): member for member in members}
        plain["right_set"] = [member for _, member in sorted(deduped.items())]
    return plain


def _literal_plain(literal: TypedLiteral, path: str) -> dict:
    if not isinstance(literal, TypedLiteral):
        raise SchemaError(f"{path}: expected a TypedLiteral, got {type(literal).__name__}")
    return {
        "type": _enum_value(literal.type, LiteralType, f"{path}.type"),
        # Values are ALREADY canonical strings (§A); NFC only, never re-typed.
        "value": _nfc(literal.value, f"{path}.value"),
    }


def _parameters_plain(parameters: tuple[ParameterDecl, ...]) -> list[dict]:
    plains: list[dict] = []
    seen: set[str] = set()
    for i, decl in enumerate(parameters):
        path = f"parameters[{i}]"
        if not isinstance(decl, ParameterDecl):
            raise SchemaError(f"{path}: expected a ParameterDecl, got {type(decl).__name__}")
        name = _nfc(decl.name, f"{path}.name")
        if name in seen:
            raise SchemaError(f"{path}: duplicate parameter name {decl.name!r} (§E rejects)")
        seen.add(name)
        plains.append(
            {
                "name": name,
                "type": _enum_value(decl.type, LiteralType, f"{path}.type"),
                "param_class": _enum_value(decl.param_class, ParamClass, f"{path}.param_class"),
                "classification": _nfc(decl.classification, f"{path}.classification"),
                "nullable": _identity_bool(decl.nullable, f"{path}.nullable"),
                "allowed_set": _allowed_set_plain(decl.allowed_set, path),
                "allowed_min": _opt_nfc(decl.allowed_min, f"{path}.allowed_min"),
                "allowed_max": _opt_nfc(decl.allowed_max, f"{path}.allowed_max"),
            }
        )
    plains.sort(key=lambda plain: _utf16_key(plain["name"]))
    return plains


def _allowed_set_plain(allowed_set: tuple[str, ...] | None, path: str) -> list[str] | None:
    if allowed_set is None:
        return None
    normalized = {
        _nfc(value, f"{path}.allowed_set[{i}]") for i, value in enumerate(allowed_set)
    }
    # §E: sorted + deduplicated (UTF-16 code-unit order, the JCS collation).
    return sorted(normalized, key=_utf16_key)


def _decimal_plain(decimal: DecimalPolicy) -> dict:
    if not isinstance(decimal, DecimalPolicy):
        raise SchemaError(f"decimal: expected a DecimalPolicy, got {type(decimal).__name__}")
    return {
        "precision": _identity_int(decimal.precision, "decimal.precision"),
        "scale": _identity_int(decimal.scale, "decimal.scale"),
        "rounding": decimal.rounding.value,
        "overflow": decimal.overflow.value,
    }


def _output_plain(output: FormulaOutputPolicyV1) -> dict:
    if not isinstance(output, FormulaOutputPolicyV1):
        raise SchemaError(
            f"output: expected a FormulaOutputPolicyV1, got {type(output).__name__}"
        )
    return {
        "output_type": _nfc(output.output_type, "output.output_type"),
        "unit": _opt_nfc(output.unit, "output.unit"),
        "currency": _opt_nfc(output.currency, "output.currency"),
        "output_additivity": output.output_additivity.value,
        "external_type_required": _identity_bool(
            output.external_type_required, "output.external_type_required"
        ),
    }
