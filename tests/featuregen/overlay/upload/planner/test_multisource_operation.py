"""Phase 3C.2b-i-A · Task 3 — the closed operation→slot→path-strategy matrix + shape validation.

Pins spec §4: the matrix is total + closed (an ``OperationSpec`` per ``FinalOperation`` with the exact
allowed per-slot ``PathAggregation`` sets, window/time requirements, ordered-role sequence), and
``validate_operation_shape`` performs EXACT role→slot validation (multiset of roles; each
``ordered_slot_id``/``time_slot_id`` references a real, correctly-roled, distinct operand; no
duplicate operand slot_id; window/time present iff required; ``stddev`` →
``unsupported_path_aggregation``; ``take_latest`` ⇒ ``ordering_anchor_concept`` else
``ordering_anchor_missing``). Pure + deterministic — no DB/I/O."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.planner import multisource_contracts as m
from featuregen.overlay.upload.planner import multisource_operation as mo
from featuregen.overlay.upload.planner.contracts import (
    OPERATION_POLICY_VERSION,
    AdditivityClass,
)

R = m.SemanticRole
A = m.PathAggregation
Op = m.FinalOperation
Reason = m.MultiSourceReason


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _strategy(agg: m.PathAggregation, anchor: str | None = None) -> m.PathStrategyV1:
    return m.PathStrategyV1(
        aggregation=agg, output_type="numeric",
        output_additivity=AdditivityClass.additive, external_type_required=False,
        ordering_anchor_concept=anchor)


def _binding() -> m.GovernedSourceBindingV1:
    return m.GovernedSourceBindingV1(
        source_grain_entity="account", source_grain_key_refs=("core.t.account_id",),
        grain_fact_key="gf")


def _slot(slot_id: str, role: m.SemanticRole, agg: m.PathAggregation,
          anchor: str | None = None) -> m.OperandSlotV1:
    return m.OperandSlotV1(
        slot_id=slot_id, semantic_role=role, catalog_source="core",
        object_ref=f"core.t.{slot_id}", authoritative_concept="c",
        path_strategy=_strategy(agg, anchor), source_binding=_binding())


def _intent(operands: tuple[m.OperandSlotV1, ...], operation: m.FinalOperation,
            ordered: tuple[str, ...], time_slot: str | None = None,
            window: str | None = None) -> m.MultiSourcePlannerIntentV1:
    fe = m.FinalExpressionV1(
        operation=operation, ordered_slot_ids=ordered, time_slot_id=time_slot,
        window=window, output_additivity=AdditivityClass.non_additive)
    return m.MultiSourcePlannerIntentV1(
        target_entity="account", operands=operands, final_expression=fe,
        operation_policy_version=OPERATION_POLICY_VERSION)


# canonical valid intents per operation ------------------------------------


def _identity() -> m.MultiSourcePlannerIntentV1:
    return _intent((_slot("mea", R.measure, A.avg),), Op.identity, ("mea",))


def _count() -> m.MultiSourcePlannerIntentV1:
    return _intent((_slot("cnt", R.counted, A.count),), Op.count, ("cnt",))


def _count_distinct() -> m.MultiSourcePlannerIntentV1:
    return _intent((_slot("cnt", R.counted, A.count_distinct),), Op.count_distinct, ("cnt",))


def _recency() -> m.MultiSourcePlannerIntentV1:
    return _intent((_slot("tim", R.time, A.take_latest, anchor="as_of"),),
                   Op.recency, (), time_slot="tim")


def _trend() -> m.MultiSourcePlannerIntentV1:
    return _intent(
        (_slot("mea", R.measure, A.avg), _slot("tim", R.time, A.take_latest, anchor="as_of")),
        Op.trend, ("mea",), time_slot="tim", window="P3M")


def _ratio(num_agg: m.PathAggregation = A.avg, den_agg: m.PathAggregation = A.take_latest,
           den_anchor: str | None = "as_of") -> m.MultiSourcePlannerIntentV1:
    return _intent(
        (_slot("num", R.numerator, num_agg),
         _slot("den", R.denominator, den_agg, anchor=den_anchor)),
        Op.ratio, ("num", "den"))


def _difference() -> m.MultiSourcePlannerIntentV1:
    return _intent(
        (_slot("mnd", R.minuend, A.sum),
         _slot("sbt", R.subtrahend, A.take_latest, anchor="as_of")),
        Op.difference, ("mnd", "sbt"))


# ---------------------------------------------------------------------------
# matrix: total + closed + spec §4 allowed sets
# ---------------------------------------------------------------------------


def test_matrix_is_total_over_every_operation():
    assert set(mo.OPERATION_MATRIX) == set(Op)


def test_matrix_allowed_aggregations_match_spec_section_4():
    full = {A.avg, A.sum, A.min, A.max}
    operand_full = full | {A.take_latest}
    spec = mo.OPERATION_MATRIX
    assert set(spec[Op.identity].allowed_for(R.measure)) == full
    assert set(spec[Op.count].allowed_for(R.counted)) == {A.count}
    assert set(spec[Op.count_distinct].allowed_for(R.counted)) == {A.count_distinct}
    assert set(spec[Op.recency].allowed_for(R.time)) == {A.take_latest}
    assert set(spec[Op.trend].allowed_for(R.measure)) == {A.avg, A.sum}
    assert set(spec[Op.trend].allowed_for(R.time)) == {A.take_latest}
    assert set(spec[Op.ratio].allowed_for(R.numerator)) == operand_full
    assert set(spec[Op.ratio].allowed_for(R.denominator)) == operand_full
    assert set(spec[Op.difference].allowed_for(R.minuend)) == operand_full
    assert set(spec[Op.difference].allowed_for(R.subtrahend)) == operand_full
    # stddev is in NO allowed set anywhere in the matrix
    for entry in spec.values():
        for role in entry.required_roles:
            assert A.stddev not in entry.allowed_for(role)


def test_matrix_window_and_time_requirements():
    spec = mo.OPERATION_MATRIX
    assert spec[Op.trend].requires_window is True
    assert spec[Op.trend].time_role is R.time
    for op in (Op.identity, Op.count, Op.count_distinct, Op.ratio, Op.difference):
        assert spec[op].requires_window is False
        assert spec[op].time_role is None
    assert spec[Op.recency].requires_window is False
    assert spec[Op.recency].time_role is R.time
    # order-sensitive ops carry two distinct ordered roles
    assert spec[Op.ratio].ordered_roles == (R.numerator, R.denominator)
    assert spec[Op.difference].ordered_roles == (R.minuend, R.subtrahend)


def test_operation_spec_is_frozen_slotted():
    entry = mo.OPERATION_MATRIX[Op.identity]
    assert not hasattr(entry, "__dict__")
    with pytest.raises(Exception):
        entry.requires_window = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# valid intents → None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent_factory", [
    _identity, _count, _count_distinct, _recency, _trend, _ratio, _difference])
def test_valid_intents_return_none(intent_factory):
    assert mo.validate_operation_shape(intent_factory()) is None


def test_valid_ratio_with_take_latest_denominator_and_anchor():
    # the canonical AVG(txn)/latest(balance) — operands MAY be take_latest (spec §4)
    assert mo.validate_operation_shape(_ratio(num_agg=A.avg, den_agg=A.take_latest)) is None


def test_valid_ratio_min_over_max_operands():
    # min/max are allowed for ratio operands (neither needs an anchor)
    assert mo.validate_operation_shape(_ratio(num_agg=A.min, den_agg=A.max, den_anchor=None)) is None


def test_valid_call_is_deterministic():
    intent = _ratio()
    assert mo.validate_operation_shape(intent) == mo.validate_operation_shape(intent)


# ---------------------------------------------------------------------------
# operand_shape_invalid (spec §4/§9)
# ---------------------------------------------------------------------------


def test_identity_over_counted_is_shape_invalid():
    intent = _intent((_slot("cnt", R.counted, A.count),), Op.identity, ("cnt",))
    assert mo.validate_operation_shape(intent) is Reason.operand_shape_invalid


def test_trend_without_window_is_shape_invalid():
    good = _trend()
    bad = _intent(good.operands, Op.trend, ("mea",), time_slot="tim", window=None)
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_duplicate_operand_slot_id_is_shape_invalid():
    intent = _intent(
        (_slot("dup", R.numerator, A.avg), _slot("dup", R.denominator, A.sum)),
        Op.ratio, ("dup", "dup"))
    assert mo.validate_operation_shape(intent) is Reason.operand_shape_invalid


def test_time_slot_id_pointing_at_measure_is_shape_invalid():
    # a well-formed TREND, but time_slot_id references the MEASURE operand, not the TIME one
    ops = (_slot("mea", R.measure, A.avg), _slot("tim", R.time, A.take_latest, anchor="as_of"))
    bad = _intent(ops, Op.trend, ("mea",), time_slot="mea", window="P3M")
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_ratio_numerator_equal_denominator_is_shape_invalid():
    # ordered slots must be distinct (numerator≠denominator)
    bad = _intent(
        (_slot("num", R.numerator, A.avg), _slot("den", R.denominator, A.sum)),
        Op.ratio, ("num", "num"))
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_ordered_slot_id_referencing_unknown_operand_is_shape_invalid():
    bad = _intent((_slot("mea", R.measure, A.avg),), Op.identity, ("ghost",))
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_extra_operand_wrong_multiset_is_shape_invalid():
    bad = _intent(
        (_slot("m1", R.measure, A.avg), _slot("m2", R.measure, A.sum)),
        Op.identity, ("m1",))
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_window_present_when_not_required_is_shape_invalid():
    bad = _intent((_slot("mea", R.measure, A.avg),), Op.identity, ("mea",), window="P3M")
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_time_slot_present_when_not_required_is_shape_invalid():
    bad = _intent(
        (_slot("num", R.numerator, A.avg), _slot("den", R.denominator, A.sum)),
        Op.ratio, ("num", "den"), time_slot="num")
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_recency_missing_time_slot_id_is_shape_invalid():
    bad = _intent((_slot("tim", R.time, A.take_latest, anchor="as_of"),), Op.recency, ())
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_take_latest_on_identity_measure_is_shape_invalid():
    # take_latest is NOT allowed for an IDENTITY measure (anchor present, so not anchor-missing)
    bad = _intent((_slot("mea", R.measure, A.take_latest, anchor="as_of"),), Op.identity, ("mea",))
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


def test_trend_measure_min_not_allowed_is_shape_invalid():
    ops = (_slot("mea", R.measure, A.min), _slot("tim", R.time, A.take_latest, anchor="as_of"))
    bad = _intent(ops, Op.trend, ("mea",), time_slot="tim", window="P3M")
    assert mo.validate_operation_shape(bad) is Reason.operand_shape_invalid


# ---------------------------------------------------------------------------
# unsupported_path_aggregation — stddev (spec §4, deferred/fail-closed)
# ---------------------------------------------------------------------------


def test_stddev_measure_is_unsupported_path_aggregation():
    intent = _intent((_slot("mea", R.measure, A.stddev),), Op.identity, ("mea",))
    assert mo.validate_operation_shape(intent) is Reason.unsupported_path_aggregation


def test_stddev_takes_precedence_over_wrong_role():
    # even where the shape is otherwise off, a stddev operand fails closed as unsupported
    intent = _intent((_slot("num", R.numerator, A.stddev),), Op.identity, ("num",))
    assert mo.validate_operation_shape(intent) is Reason.unsupported_path_aggregation


# ---------------------------------------------------------------------------
# ordering_anchor_missing — take_latest without ordering_anchor_concept
# ---------------------------------------------------------------------------


def test_take_latest_without_anchor_is_ordering_anchor_missing():
    intent = _ratio(num_agg=A.avg, den_agg=A.take_latest, den_anchor=None)
    assert mo.validate_operation_shape(intent) is Reason.ordering_anchor_missing


def test_recency_take_latest_without_anchor_is_ordering_anchor_missing():
    intent = _intent((_slot("tim", R.time, A.take_latest, anchor=None),),
                     Op.recency, (), time_slot="tim")
    assert mo.validate_operation_shape(intent) is Reason.ordering_anchor_missing


def test_empty_string_anchor_is_ordering_anchor_missing():
    intent = _ratio(num_agg=A.avg, den_agg=A.take_latest, den_anchor="")
    assert mo.validate_operation_shape(intent) is Reason.ordering_anchor_missing
