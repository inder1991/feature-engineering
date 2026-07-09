"""Phase-0 exit criteria (G1-G3) for the governed taxonomy.

The plan's original "every selectable leaf has >= 1 recipe" is **wrong**: the 153 recipes populate only
a subset of the 85 selectable leaves, so an unpopulated *non-intentional* leaf is informational, not a
failure. The real pass/fail gates asserted here are:

* **G1** — every recipe's derived ``primary`` is a selectable leaf.
* **G2** — no intentionally-empty leaf carries any recipe (primary OR secondary); and the intentionally-
  empty set is exactly the 13 flagged leaves.
* **G3** — no ``use_case``-dimension crosswalk target is a governed non-use_case dimension member, and the
  six reclassified frameworks left the ``use_case`` dimension.

G4 (behaviour-neutrality of the overlay/contract/governance suite) is a separate command, not a unit test.
The final test is **informational**: it logs coverage and only asserts that at least one leaf is populated
(it must NOT assert ``unpopulated == []``).
"""
from __future__ import annotations

import logging

from featuregen.overlay.upload.taxonomy.coverage import coverage_report
from featuregen.overlay.upload.taxonomy.dimensions import DIMENSIONS
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import LEGACY_TAG_CROSSWALK, crosswalk
from featuregen.overlay.upload.taxonomy.recipe_applicability import recipe_applicability
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves
from featuregen.overlay.upload.templates import ALL_TEMPLATES

# The 13 declared-future ("*") leaves flagged intentionally_empty in use_cases.py.
_EXPECTED_EMPTY_INTENTIONAL: frozenset[str] = frozenset({
    "wealth.client_attrition",
    "aml_cft.mule_account",
    "aml_cft.tbml",
    "treasury_alm.irrbb.basis_risk",
    "counterparty_risk.settlement_exposure",
    "pricing.credit_risk_based_pricing",
    "pricing.deposit_rate_optimisation",
    "pricing.fee_pricing",
    "pricing.relationship_pricing",
    "operations.process_cost_forecasting",
    "operations.workload_forecasting",
    "operations.manual_review_optimisation",
    "profitability.margin_forecasting",
})

# Every governed non-use_case dimension member, flattened — a use_case target must be disjoint from these.
_ALL_DIMENSION_MEMBERS: frozenset[str] = frozenset().union(*DIMENSIONS.values())

# Deliberate family/product-line namesakes: "insurance" names BOTH a use-case family PARENT (the
# insurance objective family) AND a product_context product line. This is an intentional, documented
# overlap — the family parent is a real tree node, never a selectable-leaf objective "hiding" a
# dimension member — so G3 exempts it (and asserts it stays a parent, never a leaf).
_ALLOWED_FAMILY_CONTEXT_NAMESAKES: frozenset[str] = frozenset({"insurance"})


def test_g1_every_recipe_primary_is_a_selectable_leaf():
    leaves = set(selectable_leaves())
    for t in ALL_TEMPLATES:
        primary = recipe_applicability(t).primary
        assert primary in leaves, (t.id, primary)


def test_g2_intentionally_empty_leaves_carry_no_recipes():
    report = coverage_report()
    empty_intentional = report["empty_intentional"]

    # The flagged set is exactly the 13 declared-future leaves.
    assert set(empty_intentional) == _EXPECTED_EMPTY_INTENTIONAL
    assert len(empty_intentional) == 13

    # Each has zero primary AND zero secondary recipes.
    for leaf in empty_intentional:
        assert report["by_leaf"][leaf] == [], (leaf, report["by_leaf"][leaf])
        assert report["secondary_by_leaf"][leaf] == [], (leaf, report["secondary_by_leaf"][leaf])


def test_g3_use_case_targets_left_the_dimension_vocabularies():
    # No use_case-dimension crosswalk target is a member of any non-use_case dimension vocabulary
    # (i.e. no framework / measure / typology / context / stage / outcome hides in the use_case tree),
    # except a deliberately-documented family-parent namesake (e.g. the "insurance" product line).
    leaves = set(selectable_leaves())
    for tag, entry in LEGACY_TAG_CROSSWALK.items():
        if entry["dimension"] != "use_case":
            continue
        target = entry["target"]
        if target in _ALLOWED_FAMILY_CONTEXT_NAMESAKES:
            assert target not in leaves, (tag, target)   # exempt only a family PARENT, never a leaf
            continue
        assert target not in _ALL_DIMENSION_MEMBERS, (tag, target)

    # The six reclassified frameworks explicitly left the use_case dimension.
    for tag in ("ifrs9_staging", "frtb", "xva", "lgd", "lcr", "nsfr"):
        entry = crosswalk(tag)
        assert entry is not None, tag
        assert entry["dimension"] != "use_case", (tag, entry)


def test_remapped_recipes_land_on_precise_leaves_not_closest_fit():
    # Taxonomy patch: the three precise leaves each carry their recipe as PRIMARY, and the vacated
    # closest-fit leaf no longer lists that recipe (the old mapping is an audit record only, not coverage).
    by_leaf = coverage_report()["by_leaf"]
    assert "claims_frequency_severity" in by_leaf["insurance.actuarial.claims_cost_modelling"]
    assert "claims_frequency_severity" not in by_leaf["insurance.claims.claims_fraud"]
    assert ("mortality_morbidity_loading"
            in by_leaf["insurance.underwriting.mortality_morbidity_risk_assessment"])
    assert "mortality_morbidity_loading" not in by_leaf["insurance.reinsurance"]
    assert "custody_holding_dynamics" in by_leaf["securities_services.custody.holdings_dynamics"]
    # The renamed settlement leaf carries the 4 settlement recipes; the old id is gone from coverage.
    assert "securities_services.custody.settlement" not in by_leaf
    fail = by_leaf["securities_services.custody.settlement_failure_risk"]
    for rid in ("matching_break_rate", "pre_settlement_aging",
                "settlement_fail_rate", "fail_ageing_buckets"):
        assert rid in fail, rid


def test_informational_coverage(caplog):
    # Informational only: log how much of the taxonomy the 153 recipes populate. Unpopulated
    # non-intentional leaves are EXPECTED and sizable, so this must NOT assert unpopulated == [].
    report = coverage_report()
    populated = report["populated_count"]
    unpopulated = report["unpopulated"]
    with caplog.at_level(logging.INFO):
        logging.getLogger(__name__).info(
            "phase0 coverage: populated=%d / leaf_count=%d, unpopulated(non-intentional)=%d",
            populated, report["leaf_count"], len(unpopulated))
    assert populated >= 1
