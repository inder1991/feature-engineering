"""The derived-label contract: a label is a RULE, refused at construction when malformed."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    TargetContractError,
    TargetHeaderV1,
)


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
                grain_ref="public.bo_cib_customer.cust_num",
                as_of_ref="public.bo_cib_customer.business_dt",
                window_days=90, label_type="binary", operator=">=", threshold=1.0)
    return TargetHeaderV1(**{**base, **over})


def test_a_well_formed_binary_header_is_accepted():
    h = _header()
    assert (h.name, h.window_days, h.direction) == ("tgt_npe_90d", 90, "forward")


def test_the_name_must_carry_the_tgt_prefix():
    """The prefix is the owner's decision and it is what makes a label recognisable in a
    registry it shares with nothing else."""
    with pytest.raises(TargetContractError, match="name"):
        _header(name="npe_90d")


def test_direction_is_always_forward_and_a_backward_rule_is_REFUSED():
    """A rule that reads backward from the as-of date is a FEATURE. Correcting it silently
    would hide the confusion; the refusal is the point."""
    with pytest.raises(TargetContractError, match="forward"):
        _header(direction="backward")


def test_the_anchor_catalog_is_mandatory():
    """`graph_node.object_ref` is only `public.{table}.{column}` — a bare ref does not identify
    a column (M3), which is why `_column_meta` scopes every lookup to a pair."""
    with pytest.raises(TargetContractError, match="anchor_catalog"):
        _header(anchor_catalog="")


def test_a_binary_label_REQUIRES_operator_and_threshold():
    with pytest.raises(TargetContractError, match="binary"):
        _header(operator=None, threshold=None)


def test_an_unrecognised_operator_is_reported_as_such():
    """"Requires an operator" misdirects when one WAS supplied and is simply not a comparison."""
    with pytest.raises(TargetContractError, match="operator"):
        _header(operator="~=")


def test_a_count_label_FORBIDS_operator_and_threshold():
    """`count` measures; it does not threshold. Carrying both is the field pair most likely to
    be filled in inconsistently, so it is checked rather than trusted."""
    with pytest.raises(TargetContractError, match="count"):
        _header(label_type="count", operator=">=", threshold=1.0)


def test_a_count_label_without_a_threshold_is_accepted():
    assert _header(label_type="count", operator=None, threshold=None).label_type == "count"


def test_the_window_must_be_positive():
    with pytest.raises(TargetContractError, match="window_days"):
        _header(window_days=0)
