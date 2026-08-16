"""C-B1/C-B2/C-B5b — the selection chain.

Three gates: *"the type round-trips; two catalogs with the same `public.t.c` are distinguishable"*,
*"a selection is constructible with no definition"*, and *"a two-grain build set refuses"*. The
first is the one the current code fails, because `record_target_reading` stores `target_ref` and
drops `catalog_source` entirely.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.selection_revisions import (
    NOT_APPLICABLE_EXPLORATION,
    BuildDeclarationV1,
    BuildSetRevisionV1,
    ExplorationTargetV1,
    FeatureSelectionRevisionV1,
    PredictionTargetV1,
    TargetProvenanceV1,
    TargetReadingRevisionV1,
    refuse_multi_grain,
    supersede_target_reading,
)

CHURN = "hdfc::public.txns.churned"


def _prediction(ref: str = CHURN) -> PredictionTargetV1:
    return PredictionTargetV1(target_logical_ref=ref, target_type="boolean", horizon_days=90)


def _reading(**overrides) -> TargetReadingRevisionV1:
    kwargs = dict(revision_id="trr-1", intent_id="int-1", reading=_prediction(),
                  provenance=TargetProvenanceV1.HUMAN_CONFIRMED, confirmed_by="alice@bank.example")
    kwargs.update(overrides)
    return TargetReadingRevisionV1(**kwargs)


# ══ THE GATE — two catalogs with the same object ref are distinguishable ═════════════════════════
def test_TWO_CATALOGS_WITH_THE_SAME_TABLE_AND_COLUMN_ARE_DISTINGUISHABLE():
    """What `record_target_reading` cannot do today: it stores `target_ref` and drops
    `catalog_source`, so these two readings are the same row."""
    hdfc = _reading(reading=_prediction("hdfc::public.txns.churned"))
    adcb = _reading(reading=_prediction("adcb::public.txns.churned"))

    assert hdfc.catalog_source == "hdfc"
    assert adcb.catalog_source == "adcb"
    assert hdfc.content_hash != adcb.content_hash


def test_the_catalog_source_is_DERIVED_not_stored_beside_the_ref():
    """One fact, not two — a check can be skipped and a derivation cannot."""
    import dataclasses

    assert "catalog_source" not in {f.name for f in dataclasses.fields(PredictionTargetV1)}
    assert _prediction().catalog_source == "hdfc"


@pytest.mark.parametrize("bad", ["public.txns.churned", "::public.txns.churned", "hdfc::", "hdfc"])
def test_a_non_canonical_ref_is_refused(bad):
    """A bare `public.txns.churned` names a target in neither catalog."""
    with pytest.raises(ValueError, match="canonical"):
        PredictionTargetV1(target_logical_ref=bad, target_type="boolean", horizon_days=90)


def test_the_reading_ROUND_TRIPS_through_its_identity_payload():
    reading = _reading()
    payload = reading.identity_payload()
    assert payload["reading"] == {"kind": "prediction", "target_logical_ref": CHURN,
                                  "target_type": "boolean", "horizon_days": 90}
    assert payload["provenance"] == "human_confirmed"
    assert reading.content_hash == _reading().content_hash


# ══ the union is DISCRIMINATED ═══════════════════════════════════════════════════════════════════
def test_an_exploration_reading_has_NO_FIELDS_TO_SET():
    """Stronger than nulling: a nulled column still admits a value, so "exploring, but somebody
    wrote a horizon" would stay representable."""
    import dataclasses

    assert dataclasses.fields(ExplorationTargetV1) == ()
    exploration = ExplorationTargetV1()
    assert exploration.leakage_applicable is False
    assert exploration.leakage_result == NOT_APPLICABLE_EXPLORATION


def test_a_prediction_target_requires_all_three_fields():
    with pytest.raises(ValueError, match="no type"):
        PredictionTargetV1(target_logical_ref=CHURN, target_type="  ", horizon_days=90)
    with pytest.raises(ValueError, match="no future to predict into"):
        PredictionTargetV1(target_logical_ref=CHURN, target_type="boolean", horizon_days=0)


def test_the_PROVENANCE_and_the_READING_cannot_disagree():
    with pytest.raises(ValueError, match="a reader cannot tell which is true"):
        _reading(provenance=TargetProvenanceV1.EXPLORING)
    with pytest.raises(ValueError, match="this reading has none"):
        _reading(reading=ExplorationTargetV1())


def test_exploration_names_its_own_leakage_result():
    """"No leakage found" and "there was nothing to look for" are different answers."""
    exploring = _reading(reading=ExplorationTargetV1(),
                         provenance=TargetProvenanceV1.EXPLORING, confirmed_by=None)
    assert exploring.reading.leakage_result == NOT_APPLICABLE_EXPLORATION
    assert exploring.catalog_source is None


# ══ append-only, and human confirmations are not silently erased ═════════════════════════════════
def test_A_SECOND_READING_CREATES_A_NEW_REVISION():
    """The whole difference from an UPDATE: the reading a leakage gate ran against is still there."""
    first = _reading()
    second = supersede_target_reading(
        first, revision_id="trr-2", reading=_prediction("hdfc::public.txns.attrited"),
        provenance=TargetProvenanceV1.USER_TYPED, confirmed_by="bob@bank.example")

    assert second.revision_id == "trr-2"
    assert second.supersedes_revision_id == "trr-1"
    assert second.intent_id == first.intent_id
    assert second.content_hash != first.content_hash


def test_A_HUMAN_CONFIRMED_READING_IS_NEVER_SILENTLY_ERASED():
    """A person said "predict churn"; an exploration declaration that quietly removes that target is
    how a governed run loses its subject between one screen and the next."""
    confirmed = _reading()
    with pytest.raises(ValueError, match="Name who acknowledged that"):
        supersede_target_reading(
            confirmed, revision_id="trr-2", reading=ExplorationTargetV1(),
            provenance=TargetProvenanceV1.EXPLORING)


def test_the_loss_may_be_ACKNOWLEDGED_and_is_then_recorded():
    acknowledged = supersede_target_reading(
        _reading(), revision_id="trr-2", reading=ExplorationTargetV1(),
        provenance=TargetProvenanceV1.EXPLORING, acknowledged_human_loss_by="alice@bank.example")
    assert acknowledged.acknowledged_human_loss_by == "alice@bank.example"
    assert acknowledged.identity_payload()["acknowledged_human_loss_by"] == "alice@bank.example"


def test_user_typed_is_ALSO_human_origin():
    """The person literally named the column — human-origin by construction, per the existing
    vocabulary's own docstring."""
    assert TargetProvenanceV1.USER_TYPED.is_human_origin
    assert TargetProvenanceV1.HUMAN_CONFIRMED.is_human_origin
    assert not TargetProvenanceV1.EXPLORING.is_human_origin

    typed = _reading(provenance=TargetProvenanceV1.USER_TYPED)
    with pytest.raises(ValueError, match="Name who acknowledged that"):
        supersede_target_reading(typed, revision_id="trr-2", reading=ExplorationTargetV1(),
                                 provenance=TargetProvenanceV1.EXPLORING)


def test_replacing_one_prediction_with_another_needs_no_acknowledgement():
    """Changing your mind about WHICH target is ordinary; removing the target entirely is not."""
    supersede_target_reading(
        _reading(), revision_id="trr-2", reading=_prediction("hdfc::public.txns.attrited"),
        provenance=TargetProvenanceV1.HUMAN_CONFIRMED, confirmed_by="alice@bank.example")


# ══ C-B2 — the selection pins what served it ═════════════════════════════════════════════════════
def _selection(**overrides) -> FeatureSelectionRevisionV1:
    kwargs = dict(revision_id="fsr-1", target_reading_revision_id="trr-1",
                  considered_revision_id="cons-1", option_id="opt-7", decision_id="dec-3",
                  planning_request_hash="sha256:plan", binding_plan_hash="sha256:binding")
    kwargs.update(overrides)
    return FeatureSelectionRevisionV1(**kwargs)


def test_A_SELECTION_IS_CONSTRUCTIBLE_WITH_NO_DEFINITION():
    """C-B2's gate. `FeatureDefinitionV1` is created or resolved at AUTHORING, and requiring one
    here would mean a person cannot choose a feature before the system names it."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(FeatureSelectionRevisionV1)}
    assert not any("definition" in name for name in names)
    assert _selection().content_hash


@pytest.mark.parametrize("blank", ["considered_revision_id", "option_id", "decision_id",
                                   "planning_request_hash", "binding_plan_hash"])
def test_every_pin_is_required(blank):
    """An option id without the considered revision it came from could name a different option in
    a later run."""
    with pytest.raises(ValueError, match="does not pin what was selected"):
        _selection(**{blank: "  "})


def test_the_selection_pins_1063s_identity_rather_than_inventing_one():
    """Migration 1063 records every option SERVED, not which was chosen — so `considered_revision_id`
    and `option_id` together are what make this record refer to a real served option."""
    payload = _selection().identity_payload()
    assert payload["considered_revision_id"] == "cons-1"
    assert payload["option_id"] == "opt-7"


# ══ C-B5b — the missing root ═════════════════════════════════════════════════════════════════════
def _declaration(entity="account", keys=("acct_id",)) -> BuildDeclarationV1:
    return BuildDeclarationV1(entity=entity, grain_keys=keys, purpose="attrition model")


def _build_set(**overrides) -> BuildSetRevisionV1:
    kwargs = dict(revision_id="bsr-1", target_reading_revision_id="trr-1",
                  selection_revision_ids=("fsr-1", "fsr-2"), declaration=_declaration())
    kwargs.update(overrides)
    return BuildSetRevisionV1(**kwargs)


def test_the_build_set_graph_is_expressible_with_NO_FORWARD_REFERENCE():
    """`BuildSet -> selections -> definitions` and `DerivedGroup -> BuildSet + ordered executable
    revisions`: nothing here needs a derived group, which does not exist until S6."""
    build_set = _build_set()
    assert build_set.selection_revision_ids == ("fsr-1", "fsr-2")
    assert build_set.target_reading_revision_id == "trr-1"
    assert build_set.content_hash


def test_SELECTION_ORDER_IS_PART_OF_THE_BUILD_SET():
    """The order a person chose features in is a fact about the build; a set would discard it."""
    assert _build_set(selection_revision_ids=("fsr-1", "fsr-2")).content_hash != _build_set(
        selection_revision_ids=("fsr-2", "fsr-1")).content_hash


def test_a_duplicate_selection_is_refused():
    with pytest.raises(ValueError, match="which position is this feature in"):
        _build_set(selection_revision_ids=("fsr-1", "fsr-1"))


def test_a_build_set_with_no_selections_or_no_target_refuses():
    with pytest.raises(ValueError, match="builds nothing"):
        _build_set(selection_revision_ids=())
    with pytest.raises(ValueError, match="predicting nothing in particular"):
        _build_set(target_reading_revision_id="  ")


def test_A_TWO_GRAIN_BUILD_SET_REFUSES():
    """C-B5b's S11 gate. Two grains are two populations, and the spine, contract and group
    identities downstream are each keyed on exactly one."""
    refuse_multi_grain([_declaration(), _declaration()])          # one grain, fine
    with pytest.raises(ValueError, match="two populations"):
        refuse_multi_grain([_declaration(), _declaration(entity="customer", keys=("cif_id",))])
    with pytest.raises(ValueError, match="two populations"):
        refuse_multi_grain([_declaration(), _declaration(keys=("acct_id", "branch_id"))])


def test_a_declaration_needs_an_entity_and_grain_keys():
    with pytest.raises(ValueError, match="must name the entity"):
        BuildDeclarationV1(entity=" ", grain_keys=("acct_id",), purpose="p")
    with pytest.raises(ValueError, match="describes no population"):
        BuildDeclarationV1(entity="account", grain_keys=(), purpose="p")


def test_ONE_DECLARATION_LIVES_ON_THE_BUILD_SET_not_a_derived_group():
    """A derived group does not exist until S6, so a declaration that could only attach to one would
    have nowhere to live during selection."""
    import dataclasses

    assert "declaration" in {f.name for f in dataclasses.fields(BuildSetRevisionV1)}
