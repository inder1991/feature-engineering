"""The reviewed corpus the Formula-v2/v3 evaluator measures against — pinned, never derived.

▲ **THE V1 CORPUS IS NOT REUSABLE, and this is not a preference.** It covers two legacy V1 recipes
(`merchant_mcc_diversity`, `obligor_facility_count`) whose expectations are unary count-distinct
blueprints. It cannot certify a lane whose formulas carry window offsets, authority refs and
composite bodies, and running it against V2 would produce a green result that means nothing.

**WHY THE PINS LIVE HERE AND THE BYTES LIVE IN `tests/`.** Production must never read from the test
tree, so this module holds each fixture's NAME and sha256 while the fixture itself stays beside the
tests that already review it. A test compares the two, so editing either side alone fails CI —
exactly the freeze discipline `RECIPE_FORMULA_V2_EXPECTATIONS` already carries for the clean half.

**THE TWO HALVES HAVE DIFFERENT OWNERS, and that is the whole shape of this file.**

* The CLEAN cases come from the v2 expectation registry, and membership there IS review: adding one
  flips `has_reviewed_formula_expectation` and clears a live activation blocker. It needs approved
  `recipe_review_event` rows from every required role. None exist. So there is exactly ONE clean
  case today, and growing that number is a governance act (§0.10 step 5B), not an engineering one.
* The ADVERSARIAL cases are language-level facts — this proposal is malformed, this one names a
  capability no engine advertises — reviewed as part of the gold_v2 corpus and carrying no claim
  about any recipe. They need no review event, so all eleven are here.

That asymmetry is why `corpus_adequacy` exists rather than a validator that simply raises: the
corpus is *complete for what engineering can supply* and *short of what certification requires*, and
those are different sentences that must not be collapsed into one boolean.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
    RECIPE_FORMULA_V2_EXPECTATIONS,
    validate_v2_expectation_registry,
)
from featuregen.overlay.upload.recipe_formula_shadow import content_hash

V2_CORPUS_VERSION = "recipe-formula-gold-v2"

#: How many CLEAN cases a corpus needs before a run under it may claim to certify authoring quality.
#: Ten, matching what the V1 corpus demands per recipe — a provider that produces one correct
#: formula once has not demonstrated it does so reliably, and reliability is the only thing an
#: authoring evaluator measures.
#:
#: ▲ Declared as a NAMED FLOOR rather than enforced by a raise, because the corpus falling short is
#: a governance fact and not a bug. `corpus_adequacy` reports it; the evaluator refuses to certify
#: on it. Neither pretends it is met.
CLEAN_CASE_FLOOR = 10

#: The refusal classes the adversarial half must cover. A corpus missing one of these cannot tell
#: "the platform blocks malformed proposals" from "the platform was never shown one".
REQUIRED_REFUSAL_CLASSES = ("schema_error", "unsupported_capability")

#: (fixture name, sha256 of the fixture FILE, the reviewed refusal class).
#:
#: The FILE hash, not the proposal hash: an invalid proposal has no canonical form, so
#: `expected_proposal_hash` is empty in every one of these. The reviewed material is the fixture.
REVIEWED_ADVERSARIAL_FIXTURES: tuple[tuple[str, str, str], ...] = (
    ("05_avg_without_operand_invalid.json",
     "d45e1a54835c32fed416b325acf5e34802b4da6b1568bf413c0389b754c7ea2f",
     "schema_error"),
    ("06_cross_source_ratio_unsupported.json",
     "5a99e8d95e341713629aab832b41a78d5b62779cc9e3bd184da76b1603d85e5d",
     "unsupported_capability"),
    ("11_percentile_without_argument_invalid.json",
     "fe4bb5d782dc825d2426e544b67493895805f06c4841340a115d195189201533",
     "schema_error"),
    ("12_argument_on_sum_invalid.json",
     "f555d0c2764c120b3c5dcf95f8ce4f9b45ad82b4c2734792545ed3973263c402",
     "schema_error"),
    ("19_negative_offset_invalid.json",
     "7ab345419f321a464119f14b0fecf9c2ed9dfb384cdb6f5d4497e1b7f62a548f",
     "schema_error"),
    ("20_second_operand_on_sum_invalid.json",
     "d931f4dcd535e2973f0db466f065e9e27bd592c431cf9a11a9a89340d764464e",
     "schema_error"),
    ("24_slope_without_operand_invalid.json",
     "82822004da55d9d408390a48764b68e0c93bb8467723d8a4baabf0629d2e52db",
     "schema_error"),
    ("29_last_known_over_future_invalid.json",
     "bb1bdfe86515877bc0f6287a644d1c62d1b2f1b6898eddc80338ec8b3e17a86f",
     "schema_error"),
    ("32_vacuous_authority_block_invalid.json",
     "438d6ba07f7f43f238a2bc44450aba6d6febd2388565a9ab59d40f1d8b3a158b",
     "schema_error"),
    ("35_single_term_signed_sum_invalid.json",
     "ebc690ff1104046c013ed86cf82de5e9fcfd45ced3fa269a82e93c8f1a6be30a",
     "schema_error"),
    ("36_effective_without_valid_to_invalid.json",
     "f77b51518e8de0d0146b6534866bdda0b58f4d563fcdfe76641c0ec40bb56acc",
     "schema_error"),
)


@dataclass(frozen=True, slots=True)
class FormulaGoldCaseV2:
    """One reviewed case. What was asked, which reviewed bytes answer it, and what must happen."""

    case_id: str
    case_kind: str
    fixture_name: str
    fixture_pin: str
    expected: dict[str, Any]
    #: Present on CLEAN cases only — the registry key whose reviewed formula this case expects.
    #: Absent on adversarial cases because they make no claim about any recipe.
    expectation_ref: str | None = None


def _clean_cases() -> tuple[FormulaGoldCaseV2, ...]:
    """One case per REVIEWED expectation. Never per recipe: 295 of the 317 registry recipes declare
    an expectation ref that is not their own name, and three agree only by coincidence.

    ▲ The expected disposition is `READY_FOR_OUTPUT_BINDING`, NOT `RESOLVED`. A correct V3 run
    captures the author's intent and stops — the compiler resolves output authority (C-A7). An
    evaluator that expected `RESOLVED` here would mark every correct V3 run as a failure and every
    run that wrongly resolved its own output as a success, which is the precise inversion this lane
    exists to prevent.
    """
    validate_v2_expectation_registry()
    return tuple(
        FormulaGoldCaseV2(
            case_id=f"{ref}-clean",
            case_kind="clean",
            expectation_ref=ref,
            fixture_name=fixture_name,
            fixture_pin=pinned_hash,
            expected={
                "disposition": "READY_FOR_OUTPUT_BINDING",
                "output_status": "deferred_to_compiler",
                "proposal_hash": pinned_hash,
            })
        for ref, (fixture_name, pinned_hash) in sorted(RECIPE_FORMULA_V2_EXPECTATIONS.items()))


def _adversarial_cases() -> tuple[FormulaGoldCaseV2, ...]:
    """One case per reviewed malformed or unsupportable fixture.

    The expected outcome is a REFUSAL, named by class. `schema_error` must be caught structurally —
    before any capability question is asked — and `unsupported_capability` must be caught by the
    engine gate, because a proposal that parses cleanly and names an operation nothing can render is
    a different failure with a different owner.
    """
    return tuple(
        FormulaGoldCaseV2(
            case_id=f"{fixture_name.removesuffix('.json')}-adversarial",
            case_kind="adversarial",
            fixture_name=fixture_name,
            fixture_pin=pin,
            expected={"refusal_class": refusal_class})
        for fixture_name, pin, refusal_class in REVIEWED_ADVERSARIAL_FIXTURES)


def formula_gold_v2_cases() -> tuple[FormulaGoldCaseV2, ...]:
    """The whole corpus, clean first. Rebuilt on call so a registry change is never stale."""
    return _clean_cases() + _adversarial_cases()


def v2_corpus_content_hash() -> str:
    """The corpus's identity, over the CASES rather than the fixture list.

    Over the cases because that is what a run is actually measured against: a change to what a case
    expects moves this hash even when the fixture set is untouched, and a run recorded under the old
    hash is then visibly not evidence for the new expectation.
    """
    return content_hash([asdict(case) for case in formula_gold_v2_cases()])


def corpus_adequacy() -> tuple[str, ...]:
    """What this corpus CANNOT yet certify, named. Empty means nothing is missing.

    ▲ Returns shortfalls rather than raising, deliberately. Every shortfall below is a governance
    fact — reviewed material that does not exist yet — and raising would make the evaluator
    unbuildable rather than honest. The caller that must not proceed on a short corpus is
    `create_evaluation_run_v2`, which refuses by NAME, so the reason reaches whoever asked.
    """
    cases = formula_gold_v2_cases()
    shortfalls: list[str] = []

    clean = sum(case.case_kind == "clean" for case in cases)
    if clean < CLEAN_CASE_FLOOR:
        shortfalls.append(
            f"CLEAN_CASES_BELOW_FLOOR: {clean} of {CLEAN_CASE_FLOOR}. Each one needs an approved "
            f"recipe_review_event from every required role at the recipe's current "
            f"canonical_recipe_v2_hash, plus a reviewed gold_v2 fixture — an operator act")

    covered = {case.expected.get("refusal_class") for case in cases
               if case.case_kind == "adversarial"}
    missing = tuple(cls for cls in REQUIRED_REFUSAL_CLASSES if cls not in covered)
    if missing:
        shortfalls.append(
            f"REFUSAL_CLASSES_UNCOVERED: {', '.join(missing)}. A corpus missing one cannot tell "
            f"'the platform refuses these' from 'the platform was never shown one'")

    return tuple(shortfalls)


def validate_formula_gold_v2_corpus() -> None:
    """Structural checks only — the things that are engineering errors rather than missing review.

    Deliberately NOT the adequacy question. A duplicate case id or an unpinned fixture is a bug in
    this file; a corpus of one clean case is a governance state. Conflating them would either make
    the module refuse to import over a governance gap, or let a genuine bug through as "expected".
    """
    cases = formula_gold_v2_cases()
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("v2 gold case ids must be unique")
    for case in cases:
        if len(case.fixture_pin) != 64:
            raise ValueError(f"v2 gold case {case.case_id!r} needs a sha256 fixture pin")
        if case.case_kind not in {"clean", "adversarial"}:
            raise ValueError(f"v2 gold case {case.case_id!r} has an unknown kind")
        if (case.expectation_ref is None) != (case.case_kind == "adversarial"):
            raise ValueError(
                f"v2 gold case {case.case_id!r}: clean cases name an expectation ref and "
                f"adversarial cases do not")
    pins = [pin for _name, pin, _cls in REVIEWED_ADVERSARIAL_FIXTURES]
    if len(pins) != len(set(pins)):
        raise ValueError("two adversarial fixtures share a pin; one of them is not what it says")


validate_formula_gold_v2_corpus()
