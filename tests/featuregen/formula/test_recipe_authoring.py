from __future__ import annotations

from copy import deepcopy

from tests.featuregen.formula.test_parse import (
    AMT,
    CUSTOMER_KEY,
    EVENT_TS,
    TXN_TABLE,
    raw_unary_proposal,
)

from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.recipe_authoring import (
    FrozenRecipeReadContext,
    recipe_expectation_validator,
    recipe_tool_runner,
)


def _expectation() -> dict:
    raw = raw_unary_proposal()
    expression = raw["body"]["expr"]
    window = expression["window"]
    return {
        "final_operation": "identity",
        "grain_entity": "customer",
        "grain_key_refs": [CUSTOMER_KEY],
        "expressions": [{
            "aggregation": "sum",
            "operand_ref": AMT,
            "source_relation_ref": TXN_TABLE,
            "event_time_ref": EVENT_TS,
            "window_length": 90,
            "window": {
                key: window[key]
                for key in (
                    "basis",
                    "unit",
                    "start_inclusive",
                    "end_inclusive",
                    "timezone",
                    "empty_window",
                    "null_input",
                )
            },
        }],
        "decimal": dict(raw["decimal"]),
    }


def test_recipe_expectation_accepts_exact_formula() -> None:
    proposal = parse_proposal_v1(raw_unary_proposal())
    assert recipe_expectation_validator(_expectation())(proposal) == ()


def test_recipe_expectation_rejects_operand_and_window_substitution() -> None:
    raw = raw_unary_proposal()
    raw["body"]["expr"]["operand"] = CUSTOMER_KEY
    raw["body"]["expr"]["window"]["length"] = 30
    violations = recipe_expectation_validator(_expectation())(
        parse_proposal_v1(raw))
    assert "OPERAND_NOT_PRESERVED" in violations
    assert "WINDOW_LENGTH_NOT_PRESERVED" in violations


def test_recipe_expectation_rejects_unauthored_filter() -> None:
    raw = deepcopy(raw_unary_proposal())
    raw["body"]["expr"]["filter"] = {
        "kind": "predicate",
        "op": "is_not_null",
        "left": AMT,
    }
    violations = recipe_expectation_validator(_expectation())(
        parse_proposal_v1(raw))
    assert "UNAUTHORED_FILTER" in violations


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SnapshotConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def execute(self, query, parameters):
        assert "catalog_metadata_snapshot_item" in query
        assert parameters[0] == "snapshot-1"
        self.queries += 1
        return _Rows(self.rows)


def test_frozen_recipe_context_serves_tools_and_output_facts_without_live_reads() -> None:
    refs = frozenset({AMT, EVENT_TS, CUSTOMER_KEY})
    rows = []
    for ref in refs:
        rows.extend([
            (ref, "logical_representation", {"value": "decimal"},
             {"status": "resolved", "authority": "governed"}),
            (ref, "additivity", {"value": "additive"},
             {"status": "resolved", "authority": "governed"}),
            (ref, "unit", {"value": "AED"},
             {"status": "not_operational", "authority": "hint"}),
            (ref, "currency", {"value": "AED"},
             {"status": "not_operational", "authority": "hint"}),
            (ref, "is_grain", {"value": "true" if ref == CUSTOMER_KEY else "false"},
             {"status": "resolved", "authority": "governed"}),
        ])
    conn = _SnapshotConnection(rows)
    context = FrozenRecipeReadContext.load(conn, "snapshot-1", refs)
    runner = recipe_tool_runner(refs, frozen_context=context)

    metadata = runner(
        object(), "get_column_metadata", {"logical_ref": AMT}, roles=("analyst",))
    assert metadata["found"] is True
    assert metadata["facts"]["unit"]["value"] == "AED"
    assert conn.queries == 1

    per_expr, grain = context.formula_facts(
        parse_proposal_v1(raw_unary_proposal()))
    assert per_expr["body.expr"].output_type.status == "resolved"
    assert per_expr["body.expr"].unit.value == "AED"
    assert grain[CUSTOMER_KEY].status == "resolved"
