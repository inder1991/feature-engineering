"""Catalog resolution: the checks that cannot be made without the catalog, kept OUT of the
contract so the contract stays a pure unit."""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.target_catalog_check import (
    check_target_against_catalog,
    selectable_entities,
)
from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1

_GRAIN = "public.customers.cust_num"
_ASOF = "public.customers.business_dt"
_FLAG = "public.customers.perf_flg"


def _catalog(db):
    rows = [
        (CanonicalRow(SOURCE, "customers", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(SOURCE, "customers", "business_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow(SOURCE, "customers", "perf_flg", "text"), "npe_flag"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog=SOURCE,
                grain_ref=_GRAIN, as_of_ref=_ASOF, window_days=90,
                as_of_frequency="monthly",
                label_type="binary", operator=">=", threshold=1.0)
    return TargetHeaderV1(**{**base, **over})


def _rule(**over) -> StateChangeRuleV1:
    base = dict(header=_header(), column_ref=_FLAG,
                from_values=("P",), to_values=("N",))
    return StateChangeRuleV1(**{**base, **over})


def test_a_rule_whose_refs_all_resolve_is_accepted(db):
    _catalog(db)
    assert check_target_against_catalog(db, _rule(), roles=("data_owner",)) == ()


def test_an_UNRESOLVABLE_ref_is_refused_and_named(db):
    """An invented ref is rejected, never repaired — a rule pointing at a column that is not
    there computes nothing, silently."""
    _catalog(db)
    reasons = check_target_against_catalog(
        db, _rule(column_ref="public.customers.does_not_exist"), roles=("data_owner",))
    assert any("does_not_exist" in r for r in reasons)


def test_a_ref_the_caller_CANNOT_READ_is_refused(db):
    """Read-scope holds here exactly as it does everywhere else: a label must not be definable
    over a column its author cannot see."""
    _catalog(db)
    db.execute("UPDATE graph_node SET sensitivity = 'pii' WHERE object_ref = %s", (_FLAG,))
    reasons = check_target_against_catalog(db, _rule(), roles=())
    assert any("perf_flg" in r for r in reasons)


def test_a_ref_that_exists_in_ANOTHER_catalog_does_not_resolve_here(db):
    """M3, guarded. A rule declaring `anchor_catalog` must not be satisfied by a same-named column
    sitting in a different catalog — that is the defect `_column_meta` is pair-scoped to avoid."""
    _catalog(db)
    elsewhere = _rule(header=_header(anchor_catalog="a_catalog_that_does_not_hold_these"))
    reasons = check_target_against_catalog(db, elsewhere, roles=("data_owner",))
    unresolved = [r for r in reasons if "does not resolve" in r]
    assert len(unresolved) == 3, "all three refs are absent from that catalog"
    # And the grain check fires too — nothing keys anything in a catalog that holds no columns.
    assert any("grain" in r for r in reasons)


def test_the_as_of_ref_must_actually_be_an_as_of_column(db):
    """`as_of_ref` carries the append-only assumption the whole state_change shape rests on.
    Pointing it at an ordinary column makes the rule quietly wrong rather than refused."""
    _catalog(db)
    # `cust_num` is not an as-of column, so pointing the ANCHOR at it must be refused. (The
    # contract already refuses as_of_ref == column_ref, so the two checks do not overlap.)
    bad_anchor = _rule(header=_header(as_of_ref=_GRAIN))
    reasons = check_target_against_catalog(db, bad_anchor, roles=("data_owner",))
    assert any("as_of" in r for r in reasons)


# ══ option C: the person picks from what the catalog can actually serve ══════════════════════════

def test_selectable_entities_are_those_with_a_KEYED_SPINE_TABLE(db):
    """Not the 38-name vocabulary and not the recogniser's guess — only entities this catalog can
    genuinely anchor a label on, which is what makes the choice unwrong-able rather than merely
    validated afterwards."""
    _catalog(db)
    assert [(e["entity"], e["spine_ref"]) for e in
            selectable_entities(db, SOURCE, roles=("data_owner",))] == [("customer", _GRAIN)]


def test_a_catalog_with_no_tagged_grain_offers_NOTHING_rather_than_guessing(db):
    """It degrades quietly otherwise: an empty dropdown reads as a bug, so the caller must be able
    to say "this catalog cannot anchor a label" instead of offering a blank list."""
    _catalog(db)
    db.execute("UPDATE graph_node SET entity = NULL WHERE object_ref = %s", (_GRAIN,))
    assert selectable_entities(db, SOURCE, roles=("data_owner",)) == []


def test_a_grain_ref_that_is_not_the_grain_for_the_DECLARED_entity_is_refused(db):
    """The contradiction check. Choosing `customer` while anchoring on a column that is not the
    customer key makes every row of the label the wrong shape — and nothing else would catch it."""
    _catalog(db)
    wrong = _rule(header=_header(grain_ref=_FLAG))
    reasons = check_target_against_catalog(db, wrong, roles=("data_owner",))
    assert any("grain" in r for r in reasons)


def test_declaring_an_entity_the_grain_column_does_not_carry_is_refused(db):
    _catalog(db)
    wrong = _rule(header=_header(entity="account"))
    reasons = check_target_against_catalog(db, wrong, roles=("data_owner",))
    assert any("account" in r for r in reasons)
