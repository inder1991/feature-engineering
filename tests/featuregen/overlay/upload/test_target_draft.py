"""The proposed draft: what the tool fills in, what it leaves blank, and why."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_draft import DraftError, TargetDraftV1


def _draft(**over) -> TargetDraftV1:
    base = dict(
        shape="state_change",
        fields={"name": "tgt_npe_90d", "entity": "customer", "anchor_catalog": "cib",
                "window_days": 90, "column_ref": "public.bo_cib_customer.cust_perf_nonperf_flg"},
        needs_input=("from_values", "to_values"),
        notes={"from_values": "no_value_profile", "to_values": "no_value_profile"})
    return TargetDraftV1(**{**base, **over})


def test_a_draft_may_be_INCOMPLETE_where_a_rule_may_not():
    """The whole reason this type exists: the tool cannot know the flag's values, so it must be
    able to hand back a form with those fields blank rather than guessing them."""
    d = _draft()
    assert "from_values" in d.needs_input
    assert "from_values" not in d.fields


def test_a_field_cannot_be_both_FILLED_and_NEEDED():
    """That contradiction is how a guessed value gets rendered as if a person supplied it."""
    with pytest.raises(DraftError, match="both"):
        _draft(fields={"window_days": 90}, needs_input=("window_days",),
               notes={"window_days": "not_stated"})


def test_every_needed_field_must_say_WHY_it_is_needed():
    """A blank with no reason is indistinguishable from a bug, and gets filled in carelessly."""
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",), notes={})


def test_the_reason_must_be_one_the_form_can_render():
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",),
                      notes={"from_values": "because I said so"})


def test_the_shape_is_closed():
    with pytest.raises(DraftError, match="shape"):
        _draft(shape="whatever")


def test_a_complete_draft_needs_nothing():
    assert _draft(needs_input=(), notes={}).needs_input == ()


def test_a_frozen_draft_cannot_be_mutated_through_its_dicts():
    """`frozen=True` protects the attributes, not the dicts they point at. Copying on construction
    makes the guarantee real rather than advertised."""
    supplied = {"name": "tgt_npe_90d"}
    draft = TargetDraftV1(shape="state_change", fields=supplied)
    supplied["name"] = "tampered"
    assert draft.fields["name"] == "tgt_npe_90d"
