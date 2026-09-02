"""The target registry — content-addressed, reusable across models, mirroring the feature registry."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    StateChangeRuleV1,
    TargetHeaderV1,
)
from featuregen.overlay.upload.target_store import (
    TargetNameTaken,
    register_target,
    target_by_name,
    targets_for_entity,
)


def _rule(name="tgt_npe_90d", window=90) -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=TargetHeaderV1(name=name, entity="customer", anchor_catalog="cib",
                              grain_ref="public.bo_cib_customer.cust_num",
                              as_of_ref="public.bo_cib_customer.business_dt",
                              window_days=window, as_of_frequency="monthly", label_type="binary",
                              operator=">=", threshold=1.0),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def test_registering_a_rule_stores_it_and_its_lineage(db):
    register_target(db, _rule(), description="credit deterioration", registered_by="user:tester")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert row["entity"] == "customer"
    assert row["verification"] == "DESIGN-CHECKED"
    assert ("cib", "public.bo_cib_customer.cust_perf_nonperf_flg") in row["derives_from"]


def test_the_ladder_never_starts_above_DESIGN_CHECKED(db):
    """The platform does not execute the rule, so it cannot know the class balance or whether the
    rule matched anything at all. DATA-CHECKED is unreachable here BY CONSTRUCTION. Spec §10."""
    register_target(db, _rule(), description="d", registered_by="user:tester")
    assert target_by_name(db, "customer", "tgt_npe_90d")["verification"] == "DESIGN-CHECKED"


def test_registering_the_SAME_rule_twice_is_one_definition(db):
    """Content-addressing: an identical rule authored twice is one row, not two."""
    first = register_target(db, _rule(), description="d", registered_by="a")
    second = register_target(db, _rule(), description="d", registered_by="b")
    assert first == second


def test_reusing_a_NAME_for_a_different_rule_is_a_typed_refusal(db):
    """Someone iterating on a definition will hit this, so it must not surface as a raw
    IntegrityError from the (entity, name) index. The refusal names the definition in the way."""
    register_target(db, _rule(), description="d", registered_by="a")
    changed = StateChangeRuleV1(
        header=_rule().header,
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Watchlist",))
    with pytest.raises(TargetNameTaken, match="tgt_npe_90d"):
        register_target(db, changed, description="d", registered_by="a")


def test_a_different_window_is_a_DIFFERENT_label(db):
    """`tgt_npe_60d` and `tgt_npe_90d` are two labels, both governed — a different window is not
    a variant of one rule."""
    register_target(db, _rule(), description="d", registered_by="a")
    register_target(db, _rule(name="tgt_npe_60d", window=60), description="d", registered_by="a")
    assert {t["name"] for t in targets_for_entity(db, "customer")} == {
        "tgt_npe_90d", "tgt_npe_60d"}


def test_search_is_scoped_to_the_entity(db):
    register_target(db, _rule(), description="d", registered_by="a")
    assert targets_for_entity(db, "account") == []


def test_the_proposal_and_the_comment_are_stored_with_the_definition(db):
    """The DIFF between proposed and submitted is the provenance — "proposed 90 days, human changed
    it to 180" — and the comment carries the why. For a label feeding regulated models that
    reasoning is worth as much as the rule."""
    register_target(db, _rule(), description="d", registered_by="a",
                    proposed_draft={"fields": {"window_days": 90}},
                    author_comment="180 because the FX desk reviews quarterly")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert row["proposed_draft"]["fields"]["window_days"] == 90
    assert "FX desk" in row["author_comment"]


def test_a_label_registered_in_code_has_no_proposal_and_an_empty_comment(db):
    """Registration is not form-only — a rule authored in code is first-class, and its honest
    provenance is absent rather than invented."""
    register_target(db, _rule(), description="d", registered_by="a")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert (row["proposed_draft"], row["author_comment"]) == (None, "")


def test_ADAPTING_a_label_records_its_ancestor(db):
    """Spec §7.5 Step 4. Without this, "we moved churn from 90 days to 60" is two
    unrelated-looking rows and nobody can see that one replaced the other."""
    ancestor = register_target(db, _rule(), description="d", registered_by="a")
    register_target(db, _rule(name="tgt_npe_60d", window=60), description="d",
                    registered_by="a", adapted_from=ancestor)
    assert target_by_name(db, "customer", "tgt_npe_60d")["adapted_from"] == ancestor
