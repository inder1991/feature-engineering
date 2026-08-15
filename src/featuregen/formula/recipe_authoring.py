"""Recipe-specific authoring controls: closed tools and exact expectation preservation.

**Both generations live here, side by side, and the v1 half is FROZEN.** ``formula_facts``,
``recipe_tool_runner`` and ``recipe_expectation_validator`` are byte-frozen: their bytes decide
live work items whose ``provider_input_hash`` was sealed before v2 existed. The v2 siblings
(``formula_facts_v2``, ``recipe_tool_runner_v2``, ``recipe_expectation_validator_v2``) are
separate callables, selected by the work item's OWN declared expectation schema — never a widened
v1.

The three things a v2 run needs that the v1 seams cannot give it:

* **Facts keyed by ``logical_ref``.** ``resolve_output_v2`` looks operands up BY REF; v1's bundle
  is keyed by internal body PATH because v1's resolver is. Keying either one the other's way
  resolves every operand to empty facts and assembles a policy out of nothing (A3's plan defect 4).
* **Tools that speak v2.** ``list_supported_operations`` answers out of the v1 ``AggregateFunction``
  enum and ``validate_draft_formula`` runs ``parse_proposal_v1`` — under a v2 run the first names a
  grammar the model is not authoring in and the second calls a perfectly valid v2 draft *invalid*.
* **A validator over the twelve v2 expression keys.** v1's preserves seven and is hard-wired to a
  single unary-identity expression.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

# The C1 slot→field mapping, the empty-fact projection and the fail-closed attribution come from
# the LIVE v2 reader rather than being restated: ``authoring_v2`` is the one place that mapping
# lives, and a frozen reader that disagreed with the live one about what a fact IS would be a second
# authority. (``authoring_v2`` imports ``authoring``'s privates for exactly the same reason.)
from featuregen.formula.authoring_v2 import (
    _GRAIN_FIELD as _GRAIN_FIELD_V2,
)
from featuregen.formula.authoring_v2 import (
    _OPERAND_FACT_FIELDS as _OPERAND_FACT_FIELDS_V2,
)
from featuregen.formula.authoring_v2 import (
    _fact_text as _fact_text_v2,
)
from featuregen.formula.authoring_v2 import (
    _hard_failure as _hard_failure_v2,
)
from featuregen.formula.operations_v2 import operation_rule
from featuregen.formula.output_authority import ExprFacts
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.result import AuthorityFailure
from featuregen.formula.schema import (
    DiffBody,
    RatioBody,
    TypedFormulaProposalV1,
    UnaryBody,
)
from featuregen.formula.schema_v2 import (
    OPERATION_GRAMMAR_VERSION_V2,
    AggregateFunctionV2,
    CompositeBodyV2,
    FinalOperationV2,
    TypedFormulaProposalV2,
    body_expressions_v2,
)
from featuregen.formula.tools import run_tool
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
    EXPRESSION_PATHS_BY_FINAL_OPERATION,
    composite_expression_path,
)

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

    def formula_facts_v2(
        self, proposal: TypedFormulaProposalV2
    ) -> tuple[dict[str, OperandFactsV2], tuple[AuthorityFailure, ...]]:
        """``resolve_output_v2``'s fact bundle, out of the FROZEN snapshot — keyed by ``logical_ref``.

        The v2 sibling of :meth:`formula_facts`, and the keying is the whole point: v1's bundle is
        keyed by internal body PATH because v1's resolver is, while ``resolve_output_v2`` looks its
        operands up by ref. A bundle keyed v1's way resolves every operand to empty facts and
        assembles a policy out of nothing (A3's plan defect 4).

        Returns the bundle AND which governed read failed CLOSED, exactly as
        ``authoring_v2._read_c1_facts_v2`` does live — the slot→field mapping and the
        empty-fact / hard-fail projections are IMPORTED from there rather than restated, so the
        frozen reader and the live reader can never disagree about what a fact is. The second
        operand is read too (v1 has no such thing), and so is every grain key: a grain key whose
        governance failed closed is the same missing authority as an operand's.
        """
        facts_by_ref: dict[str, OperandFactsV2] = {}
        failures: list[AuthorityFailure] = []
        for expr in body_expressions_v2(proposal.body):
            for ref in (expr.operand, expr.second_operand):
                if ref is None or ref in facts_by_ref:
                    continue
                fields = self._items.get(ref, {})
                reads = {slot: fields.get(field) for slot, field in _OPERAND_FACT_FIELDS_V2}
                facts_by_ref[ref] = OperandFactsV2(**{
                    slot: _fact_text_v2(value) for slot, value in reads.items()})
                for slot, field in _OPERAND_FACT_FIELDS_V2:
                    failure = _hard_failure_v2(reads[slot], ref, field)
                    if failure is not None:
                        failures.append(failure)
        for key in proposal.grain.keys:
            failure = _hard_failure_v2(
                self._items.get(key, {}).get(_GRAIN_FIELD_V2), key, _GRAIN_FIELD_V2)
            if failure is not None:
                failures.append(failure)
        return facts_by_ref, tuple(failures)


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


# ── the v2 siblings ──────────────────────────────────────────────────────────────────────────────

#: The window keys a v2 expectation POLICY carries beside its event-time ref and its length — the
#: nine v1 ones plus ``offset_periods``. Read off ``WindowPolicyExpectationV2``'s own field names by
#: the comparison below, never re-listed as literals here.
_WINDOW_POLICY_KEYS_V2: tuple[str, ...] = (
    "basis", "unit", "start_inclusive", "end_inclusive", "timezone",
    "empty_window", "null_input", "offset_periods",
)

_AUTHORITY_REF_KEYS_V2: tuple[str, ...] = (
    "status_policy_ref", "direction_policy_ref", "reversal_policy_ref", "currency_conversion_ref",
)


def recipe_tool_runner_v2(
    allowed_refs: frozenset[str],
    *,
    frozen_context: FrozenRecipeReadContext | None = None,
):
    """The v2 runner: the same closed tool set and the same frozen-ref gate, answering in the v2
    grammar.

    The ref gate and the frozen-context read are v1's, unchanged — they carry no grammar. What
    could not be shared is the two tools that DO: ``list_supported_operations`` answers out of the
    v1 ``AggregateFunction`` enum, and ``validate_draft_formula`` runs ``parse_proposal_v1``. Under
    a v2 run the first names a grammar the model is not authoring in, and the second calls a
    perfectly valid v2 draft *invalid* — a tool result that would teach the model to abandon a
    correct proposal. Both are answered here from the v2 vocabulary instead, and the result KEYS are
    v1's so nothing downstream needs new words.
    """
    from featuregen.formula.capability_v2 import classify_formula_capability_v2
    from featuregen.formula.parse_v2 import parse_proposal_v2
    from featuregen.formula.schema import SchemaError

    def _list_supported_operations_v2() -> dict:
        return {
            "aggregate_functions": [
                {
                    "name": fn.value,
                    "supported": True,
                    "operand_required": operation_rule(fn).operand_required,
                    "second_operand": operation_rule(fn).second_operand,
                    "argument": operation_rule(fn).argument,
                }
                for fn in AggregateFunctionV2
            ],
            "final_operations": [op.value for op in FinalOperationV2],
            "operation_grammar_version": OPERATION_GRAMMAR_VERSION_V2,
        }

    def _validate_draft_formula_v2(arguments: Mapping[str, Any]) -> dict:
        draft = arguments.get("proposal")
        if not isinstance(draft, Mapping):
            return {"error": "validate_draft_formula requires an object 'proposal'"}
        try:
            parsed = parse_proposal_v2(draft)
        except SchemaError as exc:
            return {"verdict": "invalid", "detail": str(exc)[:500],
                    "operation_grammar_version": OPERATION_GRAMMAR_VERSION_V2}
        # ``engine=None`` asks the GRAMMAR question only; the engine arm is a separate governed
        # registry and a draft-time tool has no engine to speak for. The verdict is stamped with
        # the GRAMMAR version rather than v1's ``capability_policy_version``: ``capability_v2``
        # declares no policy-version constant, and a tool result is not where to invent one.
        return {"verdict": classify_formula_capability_v2(parsed, engine=None), "detail": None,
                "operation_grammar_version": OPERATION_GRAMMAR_VERSION_V2}

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
        if tool_name == "list_supported_operations":
            return _list_supported_operations_v2()
        if tool_name == "validate_draft_formula":
            return _validate_draft_formula_v2(arguments)
        return run_tool(conn, tool_name, arguments, roles=tuple(roles))

    return _run


def _expected_paths_v2(final_operation: str, count: int) -> tuple[str, ...] | None:
    """The canonical expression paths for one final operation, from the ONE vocabulary
    ``recipe_formula_contracts_v2`` declares — never a second list here."""
    try:
        combiner = FinalOperationV2(final_operation)
    except ValueError:
        return None
    if combiner is FinalOperationV2.SIGNED_SUM:
        return tuple(composite_expression_path(index) for index in range(count))
    return EXPRESSION_PATHS_BY_FINAL_OPERATION[combiner]


def _expectation_is_coherent_v2(expected: Mapping[str, Any]) -> bool:
    """Is this ONE expected expression coherent with the operation rule table?

    The validator receives the work item's stored payload, not the bound dataclass, so it re-asks
    the question ``bind_formula_expectation_v2`` answered at capture: does the aggregation exist,
    and do its operand / second-operand / argument slots agree with its RULE? A degraded expectation
    can otherwise be "preserved" by a proposal that is equally degraded.
    """
    try:
        rule = operation_rule(AggregateFunctionV2(str(expected.get("aggregation"))))
    except (ValueError, KeyError):
        return False
    if rule.operand_required != (expected.get("operand_ref") is not None):
        return False
    second = expected.get("second_operand_ref")
    if (rule.second_operand == "required" and second is None) or (
            rule.second_operand == "forbidden" and second is not None):
        return False
    argument = expected.get("aggregation_argument")
    if rule.argument == "percentile":
        return (isinstance(argument, (int, float)) and not isinstance(argument, bool)
                and 0 < argument < 100)
    return argument is None


def recipe_expectation_validator_v2(expectation: Mapping[str, Any]):
    """Build an exact validator for the **v2** recipe-authoring vocabulary: all twelve expression
    keys, every body shape, and the composite's term coherence.

    Same contract as v1's — a tuple of violation codes, empty when the proposal preserved the
    reviewed expectation — and the same stance: this asks *did the author change what a human
    reviewed*, never *is the formula good*. It runs AFTER ``parse_versioned``, so the proposal is
    already shape- and semantically-valid; every code below is a PRESERVATION failure.
    """
    expected_expressions = expectation.get("expressions")
    expected_final_operation = expectation.get("final_operation")

    def _validate(proposal: TypedFormulaProposalV2) -> tuple[str, ...]:
        if (not isinstance(expected_expressions, Sequence)
                or isinstance(expected_expressions, (str, bytes))
                or not expected_expressions
                or not all(isinstance(item, Mapping) for item in expected_expressions)
                or not all(_expectation_is_coherent_v2(item) for item in expected_expressions)):
            return ("EXPECTATION_SHAPE_INVALID",)
        paths = _expected_paths_v2(
            str(expected_final_operation), len(expected_expressions))
        if paths is None or len(paths) != len(expected_expressions):
            return ("EXPECTATION_SHAPE_INVALID",)
        if tuple(str(item.get("expression_path")) for item in expected_expressions) != paths:
            return ("EXPECTATION_SHAPE_INVALID",)

        violations: list[str] = []
        # The COMBINER decides the body shape, so a disagreement here makes every positional
        # comparison below meaningless — refuse whole, exactly as v1 does.
        if proposal.body.final_operation.value != expected_final_operation:
            return ("FINAL_OPERATION_NOT_PRESERVED",)
        authored = body_expressions_v2(proposal.body)
        if len(authored) != len(expected_expressions):
            return ("EXPRESSION_COUNT_NOT_PRESERVED",)

        for expression, expected in zip(authored, expected_expressions, strict=True):
            checks = {
                "AGGREGATION_NOT_PRESERVED": (
                    expression.aggregation.value, expected.get("aggregation")),
                "OPERAND_NOT_PRESERVED": (
                    expression.operand, expected.get("operand_ref")),
                "SECOND_OPERAND_NOT_PRESERVED": (
                    expression.second_operand, expected.get("second_operand_ref")),
                "AGGREGATION_ARGUMENT_NOT_PRESERVED": (
                    expression.aggregation_argument, expected.get("aggregation_argument")),
                "SOURCE_RELATION_NOT_PRESERVED": (
                    expression.source_relation.table_ref, expected.get("source_relation_ref")),
                "EVENT_TIME_NOT_PRESERVED": (
                    expression.window.event_time_ref, expected.get("event_time_ref")),
                "WINDOW_LENGTH_NOT_PRESERVED": (
                    expression.window.length, expected.get("window_length")),
                "AUTHORITY_REFS_NOT_PRESERVED": (
                    _authority_refs_plain(expression.authority_refs),
                    _authority_refs_plain(expected.get("authority_refs"))),
            }
            for code, (actual, want) in checks.items():
                if actual != want and code not in violations:
                    violations.append(code)

            expected_window = expected.get("window")
            if not isinstance(expected_window, Mapping):
                if "WINDOW_POLICY_EXPECTATION_INVALID" not in violations:
                    violations.append("WINDOW_POLICY_EXPECTATION_INVALID")
            else:
                actual_window = {
                    key: _plain_token(getattr(expression.window, key))
                    for key in _WINDOW_POLICY_KEYS_V2
                }
                if actual_window != {key: expected_window.get(key)
                                     for key in _WINDOW_POLICY_KEYS_V2}:
                    if "WINDOW_POLICY_NOT_PRESERVED" not in violations:
                        violations.append("WINDOW_POLICY_NOT_PRESERVED")
            if expression.filter is not None and "UNAUTHORED_FILTER" not in violations:
                violations.append("UNAUTHORED_FILTER")

        # A signed sum's terms carry an authored NAME and a ±1 SIGN, and reordering or re-signing
        # them changes the arithmetic while preserving every other key. Outside a signed sum the
        # bound expectation pins ``""``/``0``, and there is nothing on the proposal to disagree.
        if isinstance(proposal.body, CompositeBodyV2):
            for term, expected in zip(proposal.body.terms, expected_expressions, strict=True):
                if term.name != expected.get("term_name") and (
                        "TERM_NAME_NOT_PRESERVED" not in violations):
                    violations.append("TERM_NAME_NOT_PRESERVED")
                if term.sign != expected.get("term_sign") and (
                        "TERM_SIGN_NOT_PRESERVED" not in violations):
                    violations.append("TERM_SIGN_NOT_PRESERVED")

        if proposal.grain.entity != expectation.get("grain_entity"):
            violations.append("GRAIN_ENTITY_NOT_PRESERVED")
        if list(proposal.grain.keys) != list(expectation.get("grain_key_refs") or ()):
            violations.append("GRAIN_KEYS_NOT_PRESERVED")
        if {
            "precision": proposal.decimal.precision,
            "scale": proposal.decimal.scale,
            "rounding": proposal.decimal.rounding.value,
            "overflow": proposal.decimal.overflow.value,
        } != expectation.get("decimal"):
            violations.append("DECIMAL_POLICY_NOT_PRESERVED")
        if proposal.parameters:
            violations.append("UNAUTHORED_PARAMETERS")
        if proposal.allocation_policy_ref != (expectation.get("allocation_policy_ref") or ""):
            violations.append("ALLOCATION_POLICY_NOT_PRESERVED")
        return tuple(violations)

    return _validate


def _plain_token(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _authority_refs_plain(value: Any) -> dict[str, str] | None:
    """Both sides of the authority comparison in ONE shape: the four named policy refs, or
    ``None``. A block of four blanks is not the same statement as no block at all, and the
    comparison must not quietly make them equal."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {key: str(value.get(key) or "") for key in _AUTHORITY_REF_KEYS_V2}
    return {key: str(getattr(value, key, "") or "") for key in _AUTHORITY_REF_KEYS_V2}
