"""BR-18 — the Formula-v2 expectation registry: reviewed v2-shaped expectations, by pinned hash.

The v1 registry (``recipe_formula_expectations``) holds unary count-distinct BLUEPRINTS; a
Formula-v2 expectation is richer — window offsets, authority refs, composite bodies — and BR-6
already built the reviewed carrier for exactly that: the ``gold_v2`` corpus, where each fixture
pins a proposal's canonical bytes and sha256 under the no-self-refresh discipline. So a v2
expectation IS a reviewed gold proposal: this registry pins, per expectation ref, the fixture
name and the proposal's canonical sha256. The corpus lives with the tests, so the
fixture-side proof (the named fixture exists, parses under ``parse_versioned``, declares v2,
and hashes to EXACTLY the pin) runs in the test suite — editing either side without the other
fails CI, which is the same freeze discipline the v1 manifest carries.

Membership here is what BR-7's fold reads as ``reviewed_expectation`` for v2-shaped recipes —
the same meaning the v1 registry carries for its two anchors, one gate for both generations
(:func:`has_reviewed_expectation`).

**The key is an EXPECTATION REF, never a recipe id.** ``FormulaReferenceV2.expectation_ref`` is
the only thing that may be looked up here: 295 of the 317 registry recipes declare a ref that is
not their own name (``retail:balance_slope`` for ``balance_slope``), and the three that agree do
so by coincidence. A caller holding a recipe id resolves its definition first — see
``semantic_option_decision.has_reviewed_formula_expectation``.

**GROWING THIS REGISTRY IS AN OPERATOR ACT, NOT AN ENGINEERING ONE (D-2, task A5).** Membership
here IS review: an added entry flips ``has_reviewed_formula_expectation``, which clears the
``FORMULA_NOT_REVIEWED`` activation blocker on the materialization ladder. A2 derives a
blueprint for 90 of the 317 recipes, but *which* of those 90 a human has actually reviewed is a
governance answer no derivation can supply, and no ``recipe_review_event`` row exists yet. So
``posted_debit_amount`` remains the only entry. Adding one requires, per entry, all four:

1. an ``approved`` ``recipe_review_event`` from every role
   ``recipe_review_validity.required_reviewer_roles`` names, at the recipe's CURRENT
   ``canonical_recipe_v2_hash`` — the event carries the blueprint hash the decision covers
   (``recipe_formula_shadow.capture_blueprint_hash``);
2. a reviewed ``tests/featuregen/formula/gold_v2/`` fixture for that expectation;
3. its canonical proposal sha256, pinned here beside the fixture name;
4. green ``validate_v2_expectation_registry`` (below) and the fixture-side pin test in
   ``tests/featuregen/overlay/upload/recipes/test_transaction_foundation.py`` — which parses the
   named fixture under ``parse_versioned``, requires ``formula_schema_version == 2``, and
   compares its ``expected_proposal_hash`` to the pin, so editing either side alone fails CI.
"""
from __future__ import annotations

#: expectation ref -> (gold_v2 fixture name, pinned canonical proposal sha256).
RECIPE_FORMULA_V2_EXPECTATIONS: dict[str, tuple[str, str]] = {
    # The BR-18 canonical exemplar: sum of eligible posted debit economic amount per account,
    # carrying every governed policy its contract names (BR-6 increment 8's reviewed fixture).
    "posted_debit_amount": (
        "30_posted_debit_amount_exemplar.json",
        "093de7a0e954122f2e0e5706eea9af65ec18b42ddb7b29f4dbd9860911930bbc"),
}


def has_reviewed_expectation(expectation_ref: str) -> bool:
    """One membership gate for BOTH expectation generations (v1 blueprints and v2 fixtures)."""
    from featuregen.overlay.upload.recipe_formula_expectations import (
        RECIPE_FORMULA_EXPECTATIONS,
    )

    return (expectation_ref in RECIPE_FORMULA_EXPECTATIONS
            or expectation_ref in RECIPE_FORMULA_V2_EXPECTATIONS)


def validate_v2_expectation_registry() -> None:
    for ref, (fixture_name, pinned_hash) in RECIPE_FORMULA_V2_EXPECTATIONS.items():
        if not fixture_name.strip() or len(pinned_hash) != 64:
            raise ValueError(f"v2 expectation {ref!r} needs a fixture name and a sha256 pin")


validate_v2_expectation_registry()
