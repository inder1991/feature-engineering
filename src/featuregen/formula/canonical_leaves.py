"""The canonicalizer's version-neutral half — the primitives and the FILTER form.

**Extracted for the reason `filter_plain`'s own docstring already gives.** It was made public so
that `materialize.expression_ir` — the expression compiler BOTH generations use — could ask the
question the canonical form already answers, rather than rendering a filter a second time and
disagreeing about what the filter IS. That argument has nothing to do with V1: a `FilterNode` is a
shared structural leaf, and the compiler carrying it is shared too. Leaving the one canonicalizer in
a module typed on `TypedFormulaV1` forced a shared consumer to import from V1.

The normalization primitives come with it because the filter form is built out of them, and because
they are equally neutral: NFC folding, enum-value extraction, ref checking and the child-hash
construction say nothing about which formula language is being canonicalized.

**Moved verbatim.** These bytes decide `formula_content_hash`, and every governed formula in the
system was sealed under them. A tidy during the move would re-identify artifacts already committed
to — the same reason `_intent_material` was carried across untouched.
"""
from __future__ import annotations

import hashlib
import unicodedata
from enum import Enum
from typing import TypeVar

from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.schema_leaves import (
    FilterBool,
    FilterBoolOp,
    FilterNode,
    FilterPredicate,
    FilterPredicateOp,
    LiteralType,
    ParameterRef,
    SchemaError,
    TypedLiteral,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

__all__ = ["filter_plain"]

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


def filter_plain(node: FilterNode, path: str) -> dict:
    """The §E canonical plain form of one filter subtree.

    Public since Spec-A Task 6 (it was the private ``_filter_plain``), for the same reason
    ``join_path.table_of_ref`` was made public in Task 3: an ADAPTER must ask the question the
    canonical form already answers. ``materialize.expression_ir`` carries a per-expression filter
    into its own identity, and a second rendering there could disagree with ``formula_content_hash``
    about what the filter IS — two hashes naming one filter. There is one canonicalizer.
    """
    if isinstance(node, FilterPredicate):
        return _predicate_plain(node, path)
    if isinstance(node, FilterBool):
        if node.op is FilterBoolOp.NOT:
            # NOT is never flattened, collapsed, or sorted.
            children = [
                filter_plain(child, f"{path}.children[{i}]")
                for i, child in enumerate(node.children)
            ]
        else:
            # Associative AND/OR: flatten nested same-op children FIRST, then
            # sort the flattened children by their own canonical JCS hash.
            flattened = _flatten_same_op(node.op, node.children)
            children = sorted(
                (filter_plain(child, f"{path}.children[*]") for child in flattened),
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
