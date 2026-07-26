"""Recipe-specific authoring controls: closed tools and exact expectation preservation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from featuregen.formula.output_authority import ExprFacts
from featuregen.formula.schema import (
    DiffBody,
    RatioBody,
    TypedFormulaProposalV1,
    UnaryBody,
)
from featuregen.formula.tools import run_tool
from featuregen.overlay.upload.object_ref import parse_ref

RECIPE_TOOL_POLICY_VERSION = 1
_ALLOWED_TOOLS = frozenset({
    "get_column_metadata",
    "list_supported_operations",
    "validate_draft_formula",
})


@dataclass(frozen=True, slots=True)
class FrozenOperationalValue:
    """The immutable subset of OperationalValue consumed by formula output policy."""

    value: object | None
    status: str
    conflict_status: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenRecipeReadContext:
    """Snapshot-backed catalog reads for one recipe formula run."""

    snapshot_id: str
    _items: Mapping[str, Mapping[str, FrozenOperationalValue]]
    _metadata: Mapping[str, Mapping[str, Any]]

    @classmethod
    def load(
        cls,
        conn,
        snapshot_id: str,
        allowed_refs: frozenset[str],
    ) -> FrozenRecipeReadContext:
        rows = conn.execute(
            "SELECT logical_ref,field_or_fact_type,value_json,authority_json "
            "FROM catalog_metadata_snapshot_item "
            "WHERE snapshot_id=%s AND logical_ref = ANY(%s) "
            "ORDER BY logical_ref,field_or_fact_type",
            (snapshot_id, list(sorted(allowed_refs))),
        ).fetchall()
        items: dict[str, dict[str, FrozenOperationalValue]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for logical_ref, field, value_json, authority_json in rows:
            if logical_ref not in allowed_refs:
                raise ValueError("snapshot returned a ref outside the frozen recipe")
            value = (value_json or {}).get("value")
            authority = authority_json or {}
            fields = items.setdefault(logical_ref, {})
            if field in fields:
                raise ValueError(
                    f"snapshot has duplicate field {field!r} for {logical_ref!r}")
            fields[field] = FrozenOperationalValue(
                value=value,
                status=str(authority.get("status") or "unverifiable"),
                conflict_status=authority.get("conflict_status"),
            )
            metadata.setdefault(logical_ref, {})[field] = {
                "value": value,
                "authority": str(authority.get("authority") or "hint"),
                "provenance": authority.get("provenance"),
            }
        missing = sorted(allowed_refs - frozenset(items))
        if missing:
            raise ValueError(f"snapshot does not contain recipe refs: {missing!r}")
        return cls(snapshot_id, items, metadata)

    def get_column_metadata(self, logical_ref: str) -> dict:
        fields = self._items.get(logical_ref)
        metadata = self._metadata.get(logical_ref)
        if fields is None or metadata is None:
            return {"found": False}
        _source, _schema, table, column = parse_ref(logical_ref)
        if column is None:
            return {"found": False}
        logical_type = fields.get("logical_representation")
        declared_type = fields.get("declared_type")
        data_type = (
            logical_type.value
            if logical_type is not None and logical_type.value is not None
            else declared_type.value if declared_type is not None else None
        )
        return {
            "found": True,
            "logical_ref": logical_ref,
            "table": table,
            "column": column,
            "data_type": data_type,
            "facts": dict(metadata),
            "snapshot_id": self.snapshot_id,
        }

    def formula_facts(self, proposal: TypedFormulaProposalV1):
        body = proposal.body
        expressions = (
            (("body.expr", body.expr),)
            if isinstance(body, UnaryBody)
            else (
                ("body.numerator", body.numerator),
                ("body.denominator", body.denominator),
            )
            if isinstance(body, RatioBody)
            else (
                ("body.minuend", body.minuend),
                ("body.subtrahend", body.subtrahend),
            )
            if isinstance(body, DiffBody)
            else ()
        )
        per_expr: dict[str, ExprFacts] = {}
        for path, expression in expressions:
            if expression.operand is None:
                per_expr[path] = ExprFacts()
                continue
            fields = self._items.get(expression.operand, {})
            per_expr[path] = ExprFacts(
                output_type=fields.get("logical_representation"),
                additivity=fields.get("additivity"),
                unit=fields.get("unit"),
                currency=fields.get("currency"),
            )
        grain = {
            ref: self._items.get(ref, {}).get("is_grain")
            for ref in proposal.grain.keys
        }
        return per_expr, grain


def recipe_tool_runner(
    allowed_refs: frozenset[str],
    *,
    frozen_context: FrozenRecipeReadContext | None = None,
):
    """Return a runner that cannot search for or substitute operands outside the frozen recipe."""

    def _run(conn, tool_name: str, arguments: Mapping[str, Any], *, roles=()):
        if tool_name not in _ALLOWED_TOOLS:
            return {"error": "tool is unavailable for frozen recipe authoring"}
        if (
            tool_name == "get_column_metadata"
            and arguments.get("logical_ref") not in allowed_refs
        ):
            return {"error": "logical_ref is outside the frozen recipe bindings"}
        if tool_name == "get_column_metadata" and frozen_context is not None:
            return frozen_context.get_column_metadata(str(arguments["logical_ref"]))
        return run_tool(conn, tool_name, arguments, roles=tuple(roles))

    return _run


def recipe_expectation_validator(expectation: Mapping[str, Any]):
    """Build an exact validator for the current unary recipe-authoring vocabulary."""
    expressions = expectation.get("expressions")
    expected_expression = (
        expressions[0]
        if isinstance(expressions, list) and len(expressions) == 1
        else None
    )

    def _validate(proposal: TypedFormulaProposalV1) -> tuple[str, ...]:
        violations: list[str] = []
        if expectation.get("final_operation") != "identity" or not isinstance(
            proposal.body, UnaryBody
        ):
            return ("FINAL_OPERATION_NOT_PRESERVED",)
        if not isinstance(expected_expression, Mapping):
            return ("EXPECTATION_SHAPE_INVALID",)
        expression = proposal.body.expr
        checks = {
            "AGGREGATION_NOT_PRESERVED": (
                expression.aggregation.value,
                expected_expression.get("aggregation"),
            ),
            "OPERAND_NOT_PRESERVED": (
                expression.operand,
                expected_expression.get("operand_ref"),
            ),
            "SOURCE_RELATION_NOT_PRESERVED": (
                expression.source_relation.table_ref,
                expected_expression.get("source_relation_ref"),
            ),
            "EVENT_TIME_NOT_PRESERVED": (
                expression.window.event_time_ref,
                expected_expression.get("event_time_ref"),
            ),
            "WINDOW_LENGTH_NOT_PRESERVED": (
                expression.window.length,
                expected_expression.get("window_length"),
            ),
            "GRAIN_ENTITY_NOT_PRESERVED": (
                proposal.grain.entity,
                expectation.get("grain_entity"),
            ),
            "GRAIN_KEYS_NOT_PRESERVED": (
                list(proposal.grain.keys),
                expectation.get("grain_key_refs"),
            ),
            "DECIMAL_POLICY_NOT_PRESERVED": (
                {
                    "precision": proposal.decimal.precision,
                    "scale": proposal.decimal.scale,
                    "rounding": proposal.decimal.rounding.value,
                    "overflow": proposal.decimal.overflow.value,
                },
                expectation.get("decimal"),
            ),
        }
        for code, (actual, expected) in checks.items():
            if actual != expected:
                violations.append(code)

        expected_window = expected_expression.get("window")
        if not isinstance(expected_window, Mapping):
            violations.append("WINDOW_POLICY_EXPECTATION_INVALID")
        else:
            actual_window = asdict(expression.window)
            actual_window.pop("event_time_ref")
            actual_window.pop("length")
            actual_window = {
                key: value.value if hasattr(value, "value") else value
                for key, value in actual_window.items()
            }
            expected_policy = {
                key: expected_window[key]
                for key in (
                    "basis",
                    "unit",
                    "start_inclusive",
                    "end_inclusive",
                    "timezone",
                    "empty_window",
                    "null_input",
                )
            }
            if actual_window != expected_policy:
                violations.append("WINDOW_POLICY_NOT_PRESERVED")
        if expression.filter is not None:
            violations.append("UNAUTHORED_FILTER")
        if proposal.parameters:
            violations.append("UNAUTHORED_PARAMETERS")
        return tuple(violations)

    return _validate
