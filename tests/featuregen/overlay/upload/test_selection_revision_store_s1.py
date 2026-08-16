"""S1 — the target-reading and selection revisions, persisted (migration 1072).

Three gates: *"a second reading creates a NEW revision; a `human_confirmed` reading is never
silently overwritten"*, *"legacy `exploring` rows map with no loss"*, and *"the link is
append-only"*.

The first is what the shipped code cannot do — `record_target_reading` UPDATEs in place, so the
reading a leakage gate ran against is gone the moment anyone re-reads.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.selection_revision_store import (
    current_target_reading,
    migrate_legacy_reading,
    read_target_reading_revision,
    record_feature_selection,
    record_target_reading_revision,
    selections_for_reading,
)
from featuregen.overlay.upload.selection_revisions import (
    ExplorationTargetV1,
    FeatureSelectionRevisionV1,
    PredictionTargetV1,
    TargetModeV1,
    TargetProvenanceV1,
)

CHURN = "hdfc::public.txns.churned"
ATTRITED = "hdfc::public.txns.attrited"


def _prediction(ref: str = CHURN) -> PredictionTargetV1:
    return PredictionTargetV1(target_logical_ref=ref, target_type="boolean", horizon_days=90)


def _record(db, revision_id: str, *, intent_id: str = "int-1", reading=None, **kw):
    return record_target_reading_revision(
        db, revision_id=revision_id, intent_id=intent_id,
        reading=reading if reading is not None else _prediction(),
        provenance=kw.pop("provenance", TargetProvenanceV1.HUMAN_CONFIRMED),
        confirmed_by=kw.pop("confirmed_by", "alice@bank.example"), **kw)


def _intent(db, intent_id: str, **columns) -> None:
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, actor) "
        "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (intent_id) DO NOTHING",
        (intent_id, "accounts posting more debit value attrite", "hypothesis",
         '{"subject": "alice@bank.example"}'))
    if columns:
        sets = ", ".join(f"{k} = %s" for k in columns)
        db.execute(f"UPDATE contract_intent SET {sets} WHERE intent_id = %s",
                   (*columns.values(), intent_id))


# ══ GATE 1 — a second reading APPENDS ════════════════════════════════════════════════════════════
def test_A_SECOND_READING_CREATES_A_NEW_REVISION(db):
    """The whole difference from an UPDATE: the reading a leakage gate ran against is still there."""
    _intent(db, "int-1")
    first = _record(db, "trr-1")
    second = _record(db, "trr-2", reading=_prediction(ATTRITED))

    assert second.supersedes_revision_id == "trr-1"
    assert read_target_reading_revision(db, "trr-1") is not None, "the first is STILL readable"
    assert read_target_reading_revision(db, "trr-1").reading.target_logical_ref == CHURN
    assert current_target_reading(db, "int-1").revision_id == "trr-2"
    assert first.content_hash != second.content_hash


def test_the_current_reading_is_the_one_NOTHING_SUPERSEDES(db):
    """Found by absence of a successor, not by newest timestamp: two readings recorded in one
    transaction share a clock, and 'newest by time' would then be a coin flip."""
    _intent(db, "int-1")
    _record(db, "trr-1")
    _record(db, "trr-2", reading=_prediction(ATTRITED))
    _record(db, "trr-3", reading=_prediction("hdfc::public.txns.dormant"))
    assert current_target_reading(db, "int-1").revision_id == "trr-3"


def test_A_CONFIRMED_TARGET_IS_NEVER_SILENTLY_ERASED(db):
    """The guard the shipped UPDATE path does not have — and cannot have, because it overwrites."""
    _intent(db, "int-1")
    _record(db, "trr-1")

    with pytest.raises(ValueError, match="Name who acknowledged that"):
        _record(db, "trr-2", reading=ExplorationTargetV1())

    assert current_target_reading(db, "int-1").revision_id == "trr-1", "nothing was written"


def test_the_acknowledged_loss_is_RECORDED(db):
    _intent(db, "int-1")
    _record(db, "trr-1")
    _record(db, "trr-2", reading=ExplorationTargetV1(),
            acknowledged_human_loss_by="alice@bank.example")

    current = current_target_reading(db, "int-1")
    assert current.mode is TargetModeV1.EXPLORATION
    assert current.acknowledged_human_loss_by == "alice@bank.example"


def test_changing_WHICH_target_needs_no_acknowledgement(db):
    """Changing your mind is ordinary; erasing the subject is not."""
    _intent(db, "int-1")
    _record(db, "trr-1")
    _record(db, "trr-2", reading=_prediction(ATTRITED))


def test_a_reading_is_APPEND_ONLY_in_the_database(db):
    import psycopg

    _intent(db, "int-1")
    _record(db, "trr-1")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE target_reading_revision SET target_type = %s WHERE revision_id = %s",
                   ("numeric", "trr-1"))


def test_a_revision_may_be_superseded_at_MOST_ONCE(db):
    """Two successors would fork the chain, and 'which reading is current' would depend on which
    fork a reader walked."""
    import psycopg

    _intent(db, "int-1")
    _record(db, "trr-1")
    _record(db, "trr-2", reading=_prediction(ATTRITED))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO target_reading_revision (revision_id, intent_id, mode, provenance, "
            "target_logical_ref, target_type, horizon_days, supersedes_revision_id, content_hash) "
            "VALUES (%s, %s, 'prediction', 'human_confirmed', %s, 'boolean', 90, %s, %s)",
            ("trr-fork", "int-1", CHURN, "trr-1", "sha256:x"))


# ══ the discriminated union survives the round trip ══════════════════════════════════════════════
def test_a_prediction_round_trips_with_its_CATALOG(db):
    """Two catalogs that both contain `public.txns.churned` stay distinguishable — the thing the
    shipped schema cannot do, because it drops `catalog_source`."""
    _intent(db, "int-1")
    _intent(db, "int-2")
    _record(db, "trr-1", intent_id="int-1", reading=_prediction("hdfc::public.txns.churned"))
    _record(db, "trr-2", intent_id="int-2", reading=_prediction("adcb::public.txns.churned"))

    assert read_target_reading_revision(db, "trr-1").catalog_source == "hdfc"
    assert read_target_reading_revision(db, "trr-2").catalog_source == "adcb"


def test_an_exploration_row_stores_NO_prediction_fields(db):
    """Not nulled-out fields on a prediction row — the database CHECK makes it agree with the type
    rather than merely permit it."""
    _intent(db, "int-1")
    _record(db, "trr-1", reading=ExplorationTargetV1())
    row = db.execute(
        "SELECT target_logical_ref, target_type, horizon_days FROM target_reading_revision "
        "WHERE revision_id = %s", ("trr-1",)).fetchone()
    assert row == (None, None, None)
    assert isinstance(read_target_reading_revision(db, "trr-1").reading, ExplorationTargetV1)


def test_the_database_REFUSES_a_half_populated_prediction(db):
    import psycopg

    _intent(db, "int-1")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO target_reading_revision (revision_id, intent_id, mode, provenance, "
            "target_logical_ref, content_hash) "
            "VALUES (%s, %s, 'prediction', 'human_confirmed', %s, %s)",
            ("trr-bad", "int-1", CHURN, "sha256:x"))


# ══ GATE 2 — legacy rows map with NO LOSS ════════════════════════════════════════════════════════
def test_A_LEGACY_EXPLORING_ROW_MAPS_WITH_NO_LOSS(db):
    """`exploring` becomes (EXPLORATION, None). The None is TRUTHFUL: writing `exploring` into the
    provenance column is what destroyed the declarer in the first place."""
    _intent(db, "int-legacy", target_provenance="exploring")
    migrated = migrate_legacy_reading(db, intent_id="int-legacy", revision_id="trr-legacy")

    assert migrated.mode is TargetModeV1.EXPLORATION
    assert migrated.provenance is None
    assert isinstance(migrated.reading, ExplorationTargetV1)


def test_a_legacy_human_confirmed_row_KEEPS_its_declarer(db):
    _intent(db, "int-legacy2", target_provenance="human_confirmed", target_ref=CHURN,
            target_type="boolean", target_window_days=90,
            target_confirmed_by="bob@bank.example")
    migrated = migrate_legacy_reading(db, intent_id="int-legacy2", revision_id="trr-legacy2")

    assert migrated.mode is TargetModeV1.PREDICTION
    assert migrated.provenance is TargetProvenanceV1.HUMAN_CONFIRMED
    assert migrated.confirmed_by == "bob@bank.example"
    assert migrated.reading.target_logical_ref == CHURN


def test_an_intent_with_no_recorded_provenance_migrates_to_NOTHING(db):
    """Inventing a reading for it would assert a decision nobody made."""
    _intent(db, "int-blank")
    assert migrate_legacy_reading(db, intent_id="int-blank", revision_id="trr-blank") is None


def test_a_legacy_PREDICTION_with_no_ref_refuses_to_migrate(db):
    """A shape the old schema permitted and the type does not. There is no target to migrate, and
    inventing one would put a column nobody chose into a governed reading."""
    _intent(db, "int-refless", target_provenance="human_confirmed")
    assert migrate_legacy_reading(db, intent_id="int-refless", revision_id="trr-refless") is None


def test_a_migrated_legacy_reading_becomes_the_CURRENT_one(db):
    _intent(db, "int-legacy3", target_provenance="exploring")
    migrate_legacy_reading(db, intent_id="int-legacy3", revision_id="trr-legacy3")
    assert current_target_reading(db, "int-legacy3").revision_id == "trr-legacy3"


# ══ GATE 3 — the selection link is APPEND-ONLY ═══════════════════════════════════════════════════
def _selection(revision_id: str, reading_id: str = "trr-1", option: str = "opt-1"):
    return FeatureSelectionRevisionV1(
        revision_id=revision_id, target_reading_revision_id=reading_id,
        considered_revision_id="cons-1", option_id=option, decision_id="dec-1",
        planning_request_hash="sha256:plan", binding_plan_hash="sha256:binding")


def test_a_selection_is_recorded_against_its_EXACT_reading(db):
    _intent(db, "int-1")
    _record(db, "trr-1")
    record_feature_selection(db, _selection("fsr-1"))
    record_feature_selection(db, _selection("fsr-2", option="opt-2"))

    stored = selections_for_reading(db, "trr-1")
    assert [s.revision_id for s in stored] == ["fsr-1", "fsr-2"]
    assert stored[0].option_id == "opt-1"


def test_THE_LINK_IS_APPEND_ONLY(db):
    """A decision that can be rewritten after the generation it authorized has run."""
    import psycopg

    _intent(db, "int-1")
    _record(db, "trr-1")
    record_feature_selection(db, _selection("fsr-1"))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE feature_selection_revision SET option_id = %s WHERE revision_id = %s",
                   ("opt-other", "fsr-1"))


def test_selecting_ONE_option_TWICE_under_one_reading_refuses(db):
    """One decision recorded twice is not two decisions."""
    import psycopg

    _intent(db, "int-1")
    _record(db, "trr-1")
    record_feature_selection(db, _selection("fsr-1"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        record_feature_selection(db, _selection("fsr-dup"))


def test_a_selection_CANNOT_reference_a_reading_that_does_not_exist(db):
    """The link is a foreign key, so a selection cannot float free of the target it was made under."""
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_feature_selection(db, _selection("fsr-orphan", reading_id="trr-nope"))


def test_the_SAME_option_may_be_selected_under_a_DIFFERENT_reading(db):
    """A person who changes the target and re-selects the same feature made a new decision."""
    _intent(db, "int-1")
    _record(db, "trr-1")
    _record(db, "trr-2", reading=_prediction(ATTRITED))
    record_feature_selection(db, _selection("fsr-1", reading_id="trr-1"))
    record_feature_selection(db, _selection("fsr-2", reading_id="trr-2"))

    assert len(selections_for_reading(db, "trr-1")) == 1
    assert len(selections_for_reading(db, "trr-2")) == 1
