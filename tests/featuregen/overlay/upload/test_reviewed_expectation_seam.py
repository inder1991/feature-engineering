"""Task A5 — the reviewed-expectation seam, end to end on a real decision row.

Two halves, and only one of them is engineering:

* the MECHANISM (here): a review event records the content hash of the blueprint the capture
  path would bind, so "reviewed at this revision" and "reviewed this blueprint" are ONE fact;
  and a registered expectation demonstrably clears ``FORMULA_NOT_REVIEWED`` on the
  materialization ladder — the first of §0.3's four codes to fall — while the other three stay
  named.
* the REGISTRY GROWTH (not here): membership in ``RECIPE_FORMULA_V2_EXPECTATIONS`` IS review
  under D-2, so choosing which of A2's 90 derivable blueprints count as reviewed is an
  operator's governance act. ``posted_debit_amount`` stays the only entry, and
  ``test_the_v2_registry_pins_reviewed_fixtures`` pins that it does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.activation_policy import activation_decision
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_planning_contracts import (
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
)
from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
    RECIPE_FORMULA_V2_EXPECTATIONS,
    has_reviewed_expectation,
    validate_v2_expectation_registry,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    capture_blueprint_for,
    capture_blueprint_hash,
)
from featuregen.overlay.upload.recipe_planning_lens import (
    DatasetStoryV1,
    V2RecipeCandidateV1,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import record_review_event, review_events
from featuregen.overlay.upload.semantic_option_decision import (
    assemble_current_activation_state,
    decision_facts_for_candidate,
    load_frozen_option_facts,
    persist_option_decisions,
)

#: The one reviewed Formula-v2 expectation. Registry growth is the operator's act (D-2).
EXEMPLAR = "posted_debit_amount"
#: A registry recipe with a derivable blueprint and NO reviewed expectation — the discriminator.
UNREVIEWED = "balance_slope"

_GOLD_V2 = Path(__file__).resolve().parents[2] / "formula" / "gold_v2"

#: Every rule that lives ONLY on the materialization rung — the closed set A5 is measured
#: against, so "one code fell" is provable rather than asserted.
MATERIALIZATION_ONLY = {R.READINESS_NOT_MATERIALIZATION_READY, R.FORMULA_NOT_REVIEWED,
                        R.FORMULA_SCHEMA_UNSUPPORTED, R.EXTERNAL_VALIDATION_OUTSTANDING,
                        R.EXECUTION_AUTHORITY_UNEVALUATED, R.EXECUTION_AUTHORITY_UNMET}
#: What still blocks the reviewed exemplar after A5 **and C2**. ``EXTERNAL_VALIDATION_OUTSTANDING``
#: is absent from both rows because this fixture's idea is ``DESIGN_CHECKED`` (its rule short-
#: circuits on that); ``EXECUTION_AUTHORITY_UNMET`` is the *elif* arm of the code that does fire.
#: ``FORMULA_SCHEMA_UNSUPPORTED`` fell at C2: the engine registry advertises the exemplar's one
#: demand (``sum``, no offset, no horizon), so the second of §0.3's four codes is gone — for the
#: REVIEWED row only.
STILL_BLOCKING = {R.READINESS_NOT_MATERIALIZATION_READY, R.EXECUTION_AUTHORITY_UNEVALUATED}
#: The unreviewed discriminator carries TWO more codes, coupled by design: an unreviewed recipe
#: has no reviewed demands for C2 to compare, so ``FORMULA_SCHEMA_UNSUPPORTED`` stays exactly as
#: long as ``FORMULA_NOT_REVIEWED`` does. Review is the gate that opens the capability question.
UNREVIEWED_EXTRA = {R.FORMULA_NOT_REVIEWED, R.FORMULA_SCHEMA_UNSUPPORTED}


def _candidate(recipe_id: str) -> V2RecipeCandidateV1:
    definition = v2_recipe_by_id(recipe_id)
    assert definition is not None, f"{recipe_id!r} is not a registry recipe"
    request = planning_request_from_recipe(definition)
    return V2RecipeCandidateV1(
        recipe_id=recipe_id, relationship="primary", planning_request=request,
        planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=(), binding_state="missing",
        readiness=definition.readiness, temporal_pit_text="pit", temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility={},
        dataset_story=DatasetStoryV1(
            population_ref="accounts", population_basis="declared_grain",
            dataset_tables=("accounts",), cross_dataset=False, codes=()))


def _blocker_codes(conn, recipe_id: str) -> tuple[set[str], bool]:
    """Freeze ONE real ``semantic_option_decision`` row for this recipe, then ask the activation
    policy for ``execute_materialization`` over the frozen + current layers. Returns the blocker
    codes and the frozen reviewed-expectation fact."""
    candidate = _candidate(recipe_id)
    idea = FeatureIdea(name=recipe_id, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=recipe_id)
    facts = decision_facts_for_candidate(candidate, idea, None, "ctx")
    written = persist_option_decisions(
        conn, considered_revision_id=f"rev_{recipe_id}", generation_run_id="run_a5",
        metadata_snapshot_id=None, facts_by_option_id={f"opt_{recipe_id}": facts})
    assert written == 1
    frozen = load_frozen_option_facts(
        conn, considered_revision_id=f"rev_{recipe_id}", option_id=f"opt_{recipe_id}")
    assert frozen is not None
    current = assemble_current_activation_state(conn, frozen=frozen, snapshot_id=None)
    decision = activation_decision(frozen, current, "execute_materialization")
    assert decision.allowed is False
    return {b.code for b in decision.blockers}, frozen.has_reviewed_formula_expectation


def test_a_registered_expectation_flips_has_reviewed_formula_expectation(conn):
    """The seam, on a real row: the exemplar's registered expectation makes the FROZEN fact
    true, and ``FORMULA_NOT_REVIEWED`` is gone from the materialization decision — the first of
    §0.3's four codes to fall. The other three are asserted STILL PRESENT: A5 clears one gate,
    and a test that only checked the absence could not tell that from a policy that stopped
    blocking altogether."""
    codes, reviewed = _blocker_codes(conn, EXEMPLAR)
    assert reviewed is True
    assert codes & MATERIALIZATION_ONLY == STILL_BLOCKING


def test_a_recipe_with_no_registered_expectation_still_carries_FORMULA_NOT_REVIEWED(conn):
    """The discriminator. ``balance_slope`` derives a blueprint (A2) and is otherwise identical
    to the exemplar in this fixture — only the registry entry differs. Without it the code is
    still there, so the test above is measuring the registry and not the fixture."""
    codes, reviewed = _blocker_codes(conn, UNREVIEWED)
    assert reviewed is False
    assert codes & MATERIALIZATION_ONLY == STILL_BLOCKING | UNREVIEWED_EXTRA


def test_the_v2_registry_pins_reviewed_fixtures():
    """The pin law, plus the membership A5 deliberately did NOT change.

    Growing the registry flips an activation blocker, so it is a governance act with an
    operator's name on it (D-2): no ``recipe_review_event`` exists for any of A2's 90 derivable
    blueprints, and picking which of them count as reviewed is not an engineer's call. The
    exact-set assertion is what makes a silent addition fail CI.
    """
    from featuregen.formula.parse_v2 import parse_versioned

    assert set(RECIPE_FORMULA_V2_EXPECTATIONS) == {EXEMPLAR}
    validate_v2_expectation_registry()
    for ref, (fixture_name, pinned_hash) in RECIPE_FORMULA_V2_EXPECTATIONS.items():
        doc = json.loads((_GOLD_V2 / fixture_name).read_text())
        assert parse_versioned(doc["proposal"]).formula_schema_version == 2, ref
        assert doc["expected_proposal_hash"] == pinned_hash, ref
        assert has_reviewed_expectation(ref) is True


def test_the_merchant_v1_entry_is_untouched():
    """D-7 restated at A5: the reviewed v1 expectation for ``merchant_mcc_diversity`` still
    declares MERCHANT grain while its definition computes per customer. No task here re-keyed
    it, substituted the derived customer-grain blueprint for it, or made the disagreement
    invisible — that decision belongs to a human."""
    blueprint = RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"]
    assert (blueprint.grain.entity, blueprint.grain.key_roles) == ("merchant", ("merchant",))
    assert v2_recipe_by_id("merchant_mcc_diversity").output_grain == "customer"
    # And the capture path still resolves the REVIEWED v1 object itself, by identity.
    resolved = capture_blueprint_for("merchant_mcc_diversity")
    assert resolved is not None and resolved.blueprint is blueprint


def test_a_review_event_records_the_blueprint_it_covers(conn):
    """The A5 mechanism: the recorded hash IS the capture path's blueprint hash for that recipe
    — one resolution, not a second derivation — and it rides the same event as the revision hash
    that pins the definition it came from."""
    definition = v2_recipe_by_id(EXEMPLAR)
    expected = capture_blueprint_hash(EXEMPLAR)
    assert expected is not None and len(expected) == 64
    record_review_event(
        conn, recipe_id=EXEMPLAR, recipe_revision_hash="rev_exemplar", decision="approved",
        reviewer="user:sme", reviewer_role="banking_sme",
        output_id=definition.output.output_id,
        formula_expectation_hash=expected)
    (event,) = review_events(conn, EXEMPLAR)
    assert event.formula_expectation_hash == expected
    assert event.recipe_revision_hash == "rev_exemplar"


def test_the_v1_and_v2_generations_hash_their_own_blueprint():
    """``capture_blueprint_hash`` resolves exactly as ``CaptureBlueprintV1.bind`` does, so a
    ``formula-v1`` recipe records its REVIEWED v1 blueprint (D-7: merchant grain), never the
    customer-grain blueprint its definition derives. (Measured: the v1 and v2 content hashers
    agree byte for byte on a v1 blueprint today — both are ``_plain(asdict(...))`` over sorted
    JSON — so it is the BLUEPRINT choice, not the hasher choice, that this pins.)"""
    from featuregen.overlay.upload.recipe_formula_blueprint_derivation import (
        derive_blueprint_v2,
    )
    from featuregen.overlay.upload.recipe_formula_contracts import expectation_content_hash
    from featuregen.overlay.upload.recipe_formula_contracts_v2 import (
        expectation_content_hash_v2,
    )

    v1 = capture_blueprint_hash("merchant_mcc_diversity")
    assert v1 == expectation_content_hash(RECIPE_FORMULA_EXPECTATIONS["merchant_mcc_diversity"])
    derived = derive_blueprint_v2(v2_recipe_by_id("merchant_mcc_diversity"))
    assert v1 != expectation_content_hash_v2(derived)     # the two coexist; neither replaced

    v2 = capture_blueprint_hash(EXEMPLAR)
    assert v2 == expectation_content_hash_v2(derive_blueprint_v2(v2_recipe_by_id(EXEMPLAR)))


@pytest.mark.parametrize("recipe_id", ["salary_confidence", "llm:not_a_registry_recipe"])
def test_a_recipe_with_no_bindable_blueprint_records_no_hash(recipe_id):
    """A conceptual pattern and an id the registry never minted both determine no blueprint.
    The review is still recordable — it simply covers no executable shape, honestly ``None``."""
    assert capture_blueprint_hash(recipe_id) is None
