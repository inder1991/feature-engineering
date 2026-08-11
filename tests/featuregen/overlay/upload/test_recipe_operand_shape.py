"""SE-5 (shape half) — structural contradictions refuse before tie logic, and only contradictions.

The two closed rules: an identifier concept never serves a measure operand (regardless of its
physical type), and a declared type outside the operand class's family refuses (varchar cannot
be summed; a code column cannot anchor an event window). Absence of a type fact is NOT a
refusal — missing evidence and contradictory evidence stay different conditions. Authority
floors are deliberately absent here (staged behind the SE-4b funnel).
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_operand_policy import (
    IDENTIFIER_NOT_A_MEASURE,
    TYPE_INCOMPATIBLE,
    bind_v2_operands,
    shape_refusal,
)
from featuregen.overlay.upload.recipe_registry_v2 import PROBE_RECIPE

SOURCE = "shapebank"


def _build(db, rows) -> None:
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _base_rows(*, amount_type: str = "numeric", ts_type: str = "timestamp"):
    return [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", amount_type, additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", ts_type,
                      definition="when the transaction was booked"), "event_timestamp"),
    ]


def _verdict(verdicts, role):
    return next(v for v in verdicts if v.role == role)


def _measure_role() -> str:
    return next(op.role for op in PROBE_RECIPE.operands if op.operand_class == "measure")


def _time_role() -> str:
    return next(op.role for op in PROBE_RECIPE.operands
                if op.operand_class == "event_timestamp")


def test_a_varchar_amount_is_blocked_not_bound_and_not_unresolved(db):
    _build(db, _base_rows(amount_type="varchar(50)"))
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE)
    verdict = _verdict(verdicts, _measure_role())
    assert verdict.status == "blocked"
    assert TYPE_INCOMPATIBLE in verdict.reason_codes
    assert "public.transactions.amount" not in (verdict.selected_ref or "")
    assert "right shape" in verdict.resolution


def test_a_text_event_timestamp_is_blocked(db):
    _build(db, _base_rows(ts_type="varchar(30)"))
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE)
    verdict = _verdict(verdicts, _time_role())
    assert verdict.status == "blocked"
    assert TYPE_INCOMPATIBLE in verdict.reason_codes


def test_an_identifier_concept_never_serves_a_measure_even_when_numeric(db):
    # A NUMERIC column carrying an identifier concept, offered to a measure operand: the
    # type is fine, the MEANING is the contradiction (plan invariant 9).
    rows = _base_rows()
    rows[1] = (CanonicalRow(SOURCE, "transactions", "amount", "numeric",
                            definition="the customer number, denormalized"), "customer_id")
    _build(db, rows)
    measure_op = next(op for op in PROBE_RECIPE.operands if op.operand_class == "measure")
    probe = replace(PROBE_RECIPE, operands=tuple(
        replace(op, concept="customer_id") if op.operand_class == "measure" else op
        for op in PROBE_RECIPE.operands))
    verdicts = bind_v2_operands(db, probe, catalog_source=SOURCE)
    verdict = _verdict(verdicts, measure_op.role)
    assert verdict.status == "blocked"
    assert IDENTIFIER_NOT_A_MEASURE in verdict.reason_codes
    assert "never a measure" in verdict.resolution


def test_a_shape_bad_twin_cannot_manufacture_a_tie(db):
    # Two columns carry the measure concept: one numeric, one varchar. Before SE-5 the pair
    # tied and blocked the recipe on adjudication; now the impossible twin filters and the
    # legitimate column binds cleanly.
    rows = _base_rows()
    rows.append((CanonicalRow(SOURCE, "transactions", "amount_txt", "varchar(64)",
                              definition="amount as text, legacy export"), "monetary_flow"))
    _build(db, rows)
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE)
    verdict = _verdict(verdicts, _measure_role())
    assert verdict.status == "bound"
    assert verdict.selected_ref and verdict.selected_ref.endswith(".amount")


def test_an_unknown_type_is_not_a_contradiction():
    # Missing/unrecognized declared types never refuse — absence is not contradiction.
    class _Col:
        object_ref = "public.t.c"
        data_type = ""
        concept = "monetary_flow"

    measure_op = next(op for op in PROBE_RECIPE.operands if op.operand_class == "measure")
    assert shape_refusal(measure_op, _Col()) is None


def test_a_clean_catalog_still_binds_exactly_as_before(db):
    _build(db, _base_rows())
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE)
    assert {v.role: v.status for v in verdicts} \
        == {op.role: "bound" for op in PROBE_RECIPE.operands}
