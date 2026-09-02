"""Spec §7.4's four worked examples, constructed. A contract that cannot build the labels its own
design advertises is the failure this file exists to catch — every other test in this program uses
one hand-rolled fixture, which only proves the contract accepts itself."""
from __future__ import annotations

from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetHeaderV1,
    refs_read,
)
from featuregen.overlay.upload.target_store import register_target, targets_for_entity

CIB_GRAIN = "public.bo_cib_customer.cust_num"
CIB_ASOF = "public.bo_cib_customer.business_dt"
FTR_TABLE = "public.comp_financial_tran_repos_dly"


def _header(name: str, window: int, label_type: str = "binary",
            operator: str | None = ">=", threshold: float | None = 1.0) -> TargetHeaderV1:
    thresholded = label_type == "binary"
    return TargetHeaderV1(
        name=name, entity="customer", anchor_catalog="cib",
        grain_ref=CIB_GRAIN, as_of_ref=CIB_ASOF, window_days=window,
        label_type=label_type,
        operator=operator if thresholded else None,
        threshold=threshold if thresholded else None)


def _tgt_npe_90d() -> StateChangeRuleV1:
    # Values are ILLUSTRATIVE: nothing profiles this varchar(20), which is why the authoring
    # conversation asks rather than guesses (spec §11).
    return StateChangeRuleV1(
        header=_header("tgt_npe_90d", 90),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def _tgt_restricted_90d() -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=_header("tgt_restricted_90d", 90),
        column_ref="public.bo_cib_customer.cust_susp_flg",
        from_values=("N",), to_values=("Y",))


def _tgt_churned_90d() -> EventWindowRuleV1:
    """Zero rows in the window — "no transaction for 90 days", the churn definition the spec opens
    with and the one nothing in the platform could express."""
    return EventWindowRuleV1(
        header=_header("tgt_churned_90d", 90, operator="==", threshold=0.0),
        event_catalog="ftr", event_table=FTR_TABLE,
        event_date_ref=f"{FTR_TABLE}.pstd_date", join_left=CIB_GRAIN,
        join_right=f"{FTR_TABLE}.cif_id", aggregate="count")


def _tgt_fx_active_90d() -> EventWindowRuleV1:
    return EventWindowRuleV1(
        header=_header("tgt_fx_active_90d", 90),
        event_catalog="ftr", event_table=FTR_TABLE,
        event_date_ref=f"{FTR_TABLE}.pstd_date", join_left=CIB_GRAIN,
        join_right=f"{FTR_TABLE}.cif_id",
        event_filters=(EventFilterV1(column_ref=f"{FTR_TABLE}.tran_crncy",
                                     op="!=", value="AED"),),
        aggregate="count")


def _all_four():
    return (_tgt_npe_90d(), _tgt_restricted_90d(), _tgt_churned_90d(), _tgt_fx_active_90d())


def test_all_four_spec_examples_construct():
    for rule in _all_four():
        assert rule.header.direction == "forward"


def test_the_churn_label_needs_a_zero_threshold_to_mean_NO_activity():
    """"Churned" is the ABSENCE of events. `count == 0` — the one example where the threshold is
    not `>= 1`, and the one most likely to be written the wrong way round."""
    rule = _tgt_churned_90d()
    assert (rule.header.operator, rule.header.threshold) == ("==", 0.0)


def test_the_fx_example_puts_its_FILTER_column_in_lineage():
    """`tgt_fx_active_90d` is defined BY `tran_crncy`. Under the free-text filter that column never
    reached `target_derives_from`, so "which labels break if tran_crncy is retired?" answered
    "none" — about the one label that would."""
    assert ("ftr", f"{FTR_TABLE}.tran_crncy") in refs_read(_tgt_fx_active_90d())


def test_a_cross_catalog_example_reads_BOTH_catalogs():
    """The event shape spans catalogs by construction — anchored in `cib`, counting in `ftr`."""
    catalogs = {catalog for catalog, _ in refs_read(_tgt_churned_90d())}
    assert catalogs == {"cib", "ftr"}


def test_the_state_change_examples_read_only_the_ANCHOR_catalog():
    for rule in (_tgt_npe_90d(), _tgt_restricted_90d()):
        assert {catalog for catalog, _ in refs_read(rule)} == {"cib"}


def test_the_four_examples_register_and_are_all_findable_for_the_entity(db):
    for rule in _all_four():
        register_target(db, rule, description="spec example", registered_by="user:tester")
    assert {t["name"] for t in targets_for_entity(db, "customer")} == {
        "tgt_npe_90d", "tgt_restricted_90d", "tgt_churned_90d", "tgt_fx_active_90d"}
