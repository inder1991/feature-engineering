"""The V2 corpus pins, checked against the reviewed bytes they claim to freeze.

▲ **THIS IS THE HALF THAT CANNOT LIVE IN PRODUCTION.** `recipe_formula_gold_v2` holds each reviewed
fixture's NAME and sha256; the fixture itself lives here, beside the tests that review it, because
production must never read from the test tree. Neither half proves anything alone — a pin with no
bytes freezes nothing, and bytes with no pin are not frozen. This module is the join, so editing
either side without the other fails CI.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
    RECIPE_FORMULA_V2_EXPECTATIONS,
)
from featuregen.overlay.upload.recipe_formula_gold_v2 import (
    CLEAN_CASE_FLOOR,
    REQUIRED_REFUSAL_CLASSES,
    REVIEWED_ADVERSARIAL_FIXTURES,
    V2_CORPUS_VERSION,
    corpus_adequacy,
    formula_gold_v2_cases,
    v2_corpus_content_hash,
    validate_formula_gold_v2_corpus,
)

_GOLD = pathlib.Path(__file__).resolve().parents[2] / "formula" / "gold_v2"


# ══ THE FREEZE ═════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(("fixture_name", "pin", "refusal_class"), REVIEWED_ADVERSARIAL_FIXTURES)
def test_EVERY_ADVERSARIAL_PIN_MATCHES_ITS_REVIEWED_FIXTURE(fixture_name, pin, refusal_class):
    """The file hash, because an invalid proposal has no canonical form to hash instead — every one
    of these fixtures carries an empty `expected_proposal_hash`, which is correct and is exactly why
    the pin is over the bytes."""
    path = _GOLD / fixture_name
    assert path.exists(), f"{fixture_name} is pinned but does not exist"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == pin, (
        f"{fixture_name} changed without its pin moving, or the pin moved without the fixture")

    # The reviewed verdict has to agree too. A pin that froze the right bytes under the wrong
    # refusal class would still evaluate the wrong thing.
    assert json.loads(path.read_text())["expected"] == refusal_class


@pytest.mark.parametrize(("ref", "entry"), sorted(RECIPE_FORMULA_V2_EXPECTATIONS.items()))
def test_EVERY_CLEAN_PIN_MATCHES_ITS_REVIEWED_PROPOSAL(ref, entry):
    """The clean half pins the PROPOSAL's canonical sha256 rather than the file's: a clean fixture's
    reviewed content is the formula it asserts, not the JSON wrapper around it."""
    fixture_name, pinned_hash = entry
    fixture = json.loads((_GOLD / fixture_name).read_text())
    assert fixture["expected"] == "ok", f"{ref} is a clean expectation but its fixture is not"
    assert fixture["expected_proposal_hash"] == pinned_hash


# ══ WHAT THE CORPUS IS ═════════════════════════════════════════════════════════════════════════
def test_the_CORPUS_IS_STRUCTURALLY_VALID():
    validate_formula_gold_v2_corpus()
    cases = formula_gold_v2_cases()
    assert {case.case_kind for case in cases} == {"clean", "adversarial"}
    assert V2_CORPUS_VERSION == "recipe-formula-gold-v2"


def test_a_CLEAN_CASE_EXPECTS_READY_FOR_OUTPUT_BINDING_not_resolved():
    """▲ THE INVERSION THIS PINS AGAINST. A correct V3 run captures the author's intent and STOPS;
    the compiler resolves output authority (C-A7). An evaluator expecting `RESOLVED` would score
    every correct V3 run as a failure and every run that wrongly resolved its own output as a
    success — the precise opposite of what it is for."""
    clean = [case for case in formula_gold_v2_cases() if case.case_kind == "clean"]
    assert clean, "the clean half is empty; the corpus cannot measure authoring at all"
    for case in clean:
        assert case.expected["disposition"] == "READY_FOR_OUTPUT_BINDING"
        assert case.expected["output_status"] == "deferred_to_compiler"
        assert case.expectation_ref is not None


def test_the_ADVERSARIAL_HALF_COVERS_EVERY_REQUIRED_REFUSAL_CLASS():
    """Structural refusal and capability refusal are different failures with different owners: a
    proposal that parses cleanly and names an operation nothing renders is not malformed."""
    covered = {case.expected["refusal_class"] for case in formula_gold_v2_cases()
               if case.case_kind == "adversarial"}
    assert set(REQUIRED_REFUSAL_CLASSES) <= covered


# ══ THE HONEST GAP ═════════════════════════════════════════════════════════════════════════════
def test_the_CORPUS_REPORTS_ITS_OWN_GOVERNANCE_SHORTFALL_by_name():
    """▲ NOT A TEST THAT THE CORPUS IS COMPLETE — it is not, and asserting otherwise would be the
    dishonest version of this file.

    Exactly one reviewed expectation exists, because membership in that registry IS review and no
    `recipe_review_event` row exists to support a second. So the corpus is short of the floor, and
    what this pins is that the shortfall is REPORTED rather than silently passed: the day the tenth
    clean case is reviewed this assertion flips, and that is the correct moment for it to.
    """
    shortfalls = corpus_adequacy()
    clean = sum(case.case_kind == "clean" for case in formula_gold_v2_cases())

    if clean < CLEAN_CASE_FLOOR:
        assert any(s.startswith("CLEAN_CASES_BELOW_FLOOR") for s in shortfalls), (
            f"{clean} clean cases is below the floor of {CLEAN_CASE_FLOOR} and was not reported")
    else:
        assert not any(s.startswith("CLEAN_CASES_BELOW_FLOOR") for s in shortfalls)

    # The refusal half IS complete, and that must not be lumped in with the governance gap.
    assert not any(s.startswith("REFUSAL_CLASSES_UNCOVERED") for s in shortfalls)


def test_the_CORPUS_HASH_MOVES_WHEN_AN_EXPECTATION_DOES(monkeypatch):
    """Over the CASES, not the fixture list: a change to what a case expects must move the hash even
    when the fixture set is untouched, so a run recorded under the old hash is visibly not evidence
    for the new expectation."""
    import featuregen.overlay.upload.recipe_formula_gold_v2 as mod

    before = v2_corpus_content_hash()
    monkeypatch.setattr(mod, "REVIEWED_ADVERSARIAL_FIXTURES",
                        (*mod.REVIEWED_ADVERSARIAL_FIXTURES,
                         ("99_hypothetical.json", "a" * 64, "schema_error")))
    assert v2_corpus_content_hash() != before


def test_the_V1_CORPUS_AND_THE_V2_CORPUS_ARE_NOT_THE_SAME_MATERIAL():
    """§0.5: the V1 corpus covers two legacy V1 recipes and cannot certify this lane. Running it
    against V2 would go green and mean nothing, so the two must never share an identity."""
    from featuregen.overlay.upload.recipe_formula_gold import (
        CORPUS_CONTENT_HASH,
        CORPUS_VERSION,
    )

    assert V2_CORPUS_VERSION != CORPUS_VERSION
    assert v2_corpus_content_hash() != CORPUS_CONTENT_HASH
