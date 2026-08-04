"""Release C Task 10 — the crosswalk definition store (migration 1050).

Immutable revisions, a CAS current pointer, read-back verification, and whole-payload read scope.
The truth rules this suite holds:

* a confirmation moves review status and NOTHING else — not visibility, not safety, not identity;
* a crosswalk naming a hidden mapping dataset is withheld WHOLE, never trimmed;
* corruption raises instead of being served.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB,
    CIB_TABLE,
    FTR,
    FTR_TABLE,
    MAP,
    MAP_CIB,
    MAP_FTR,
    MAP_TABLE,
    build_catalog,
    definition,
    endpoint,
    wide_endpoint,
)

from featuregen.overlay.upload.bridge_assessment import (
    EvidenceKind,
    EvidenceRefV1,
    LinkReviewStatus,
)
from featuregen.overlay.upload.crosswalk import (
    CROSSWALK_WRITE_INVALID,
    CrosswalkContractError,
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.crosswalk_store import (
    MAX_PAIRS_PER_LEG,
    CrosswalkStoreConflict,
    crosswalks_for_column,
    crosswalks_for_column_listing,
    current_crosswalk_definition,
    load_crosswalk_definition_revision,
    publish_crosswalk_definition,
    record_crosswalk_definition_revision,
    set_crosswalk_review_status,
    set_current_crosswalk_definition,
    visible_crosswalk_definitions,
    visible_crosswalk_listing,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.source_selection import SelectionError

ACTOR = "user:priya"


def _published(db, revision=None, **over):
    revision = revision if revision is not None else definition()
    return publish_crosswalk_definition(
        db, revision, expected_pointer_version=0, actor=ACTOR, **over)


# ── immutable revisions ─────────────────────────────────────────────────────────────────────────

def test_a_revision_round_trips_through_the_store_byte_identical(db) -> None:
    build_catalog(db)
    revision = definition()
    record_crosswalk_definition_revision(db, revision, authored_by=ACTOR)
    loaded = load_crosswalk_definition_revision(db, revision.revision_id)
    assert loaded is not None
    assert loaded.revision_id == revision.revision_id
    assert loaded.definition_id == revision.definition_id
    assert loaded.content_payload() == revision.content_payload()


def test_recording_the_same_revision_twice_is_idempotent(db) -> None:
    build_catalog(db)
    revision = definition()
    first = record_crosswalk_definition_revision(db, revision, authored_by=ACTOR)
    second = record_crosswalk_definition_revision(db, revision, authored_by="user:sam")
    assert first == second
    assert db.execute(
        "SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 1
    # The FIRST author keeps the row: a revision is immutable, and so is who wrote it.
    assert db.execute(
        "SELECT authored_by FROM crosswalk_definition_revision").fetchone()[0] == ACTOR


def test_evidence_refs_survive_the_round_trip(db) -> None:
    build_catalog(db)
    revision = definition(evidence_refs=(
        EvidenceRefV1(evidence_id="ev-role", kind=EvidenceKind.STRUCTURAL_METADATA,
                      producer="taxonomy", content_hash="a" * 64),
        EvidenceRefV1(evidence_id="ev-llm", kind=EvidenceKind.LLM_RECOMMENDATION, producer="llm"),
    ))
    record_crosswalk_definition_revision(db, revision, authored_by=ACTOR)
    loaded = load_crosswalk_definition_revision(db, revision.revision_id)
    assert {ref.evidence_id for ref in loaded.evidence_refs} == {"ev-role", "ev-llm"}
    assert loaded.revision_id == revision.revision_id


def test_a_corrupted_revision_raises_instead_of_being_served(db) -> None:
    """A supposedly stored crosswalk that cannot reproduce its own identity is a store-integrity
    failure. Serving it would turn corruption into a quietly wrong relationship."""
    build_catalog(db)
    revision = definition()
    record_crosswalk_definition_revision(db, revision, authored_by=ACTOR)
    db.execute(
        "UPDATE crosswalk_definition_revision "
        "SET definition_json = jsonb_set(definition_json, '{mapping_dataset_ref}', "
        "    '\"cib::public.somewhere_else\"')")
    with pytest.raises(CrosswalkStoreConflict):
        load_crosswalk_definition_revision(db, revision.revision_id)


def _tamper(db, revision_id: str) -> None:
    """Corrupt ONE stored row so it can no longer reproduce its own identity."""
    db.execute(
        "UPDATE crosswalk_definition_revision SET definition_json = jsonb_set("
        "    definition_json, '{mapping_dataset_ref}', '\"cib::public.somewhere_else\"') "
        "WHERE revision_id = %s", (revision_id,))


def _second_crosswalk():
    """A DIFFERENT crosswalk through the same mapping table: CIB customer -> FTR legal entity.

    Every name here comes from this module's own imports: `test_crosswalk_contracts` RELOADS
    `crosswalk` to prove the bridge fact key survives the import, which leaves two distinct
    `LogicalMappingPairV1` classes in a combined run — mixing a fresh one with a stale one is a
    contract refusal that looks like a fixture bug."""
    return CrosswalkDefinitionRevisionV1(
        source_endpoint=endpoint("cib", CIB_TABLE, "cust_num", concept="customer_id",
                                 entity="customer"),
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(LogicalMappingPairV1(
            normalize_ref("cib", "public", CIB_TABLE, "cust_num"), MAP_CIB),),
        mapping_to_target_pairs=(LogicalMappingPairV1(
            normalize_ref("ftr", "public", FTR_TABLE, "party_lei"), MAP_FTR),),
        target_endpoint=endpoint("ftr", FTR_TABLE, "party_lei", concept="lei",
                                 entity="legal_entity"))


def test_a_corrupt_row_is_omitted_from_the_listing_and_COUNTED(db) -> None:
    """A LISTING must omit rather than raise (the module docstring already promises it). One
    tampered row taking every other crosswalk down with it turns a single corrupted record into a
    catalog-wide outage — and the corruption still has to be VISIBLE, so it is counted, not
    swallowed."""
    build_catalog(db)
    corrupt, healthy = definition(), _second_crosswalk()
    _published(db, corrupt)
    _published(db, healthy)
    _tamper(db, corrupt.revision_id)

    listing = visible_crosswalk_listing(db)
    assert [c.current.definition_id for c in listing.crosswalks] == [healthy.definition_id]
    assert listing.corrupt_definition_ids == (corrupt.definition_id,)
    # The plain reader keeps its shape and simply omits the unreadable one.
    assert [c.current.definition_id for c in visible_crosswalk_definitions(db)] == [
        healthy.definition_id]


def test_a_corrupt_row_does_not_end_a_column_anchored_read_either(db) -> None:
    build_catalog(db)
    corrupt = definition()
    _published(db, corrupt)
    _tamper(db, corrupt.revision_id)
    listing = crosswalks_for_column_listing(
        db, column_logical_ref=normalize_ref("cib", "public", CIB_TABLE, "acct_no"))
    assert listing.crosswalks == ()
    assert listing.corrupt_definition_ids == (corrupt.definition_id,)
    assert crosswalks_for_column(
        db, column_logical_ref=normalize_ref("cib", "public", CIB_TABLE, "acct_no")) == ()


def test_the_POINT_read_of_a_corrupt_revision_still_refuses(db) -> None:
    """The listing omits; the point read refuses. Asking for one specific crosswalk BY ID and
    getting `None` would report "no such crosswalk" for a crosswalk that exists and is broken."""
    build_catalog(db)
    revision = definition()
    _published(db, revision)
    _tamper(db, revision.revision_id)
    with pytest.raises(CrosswalkStoreConflict):
        load_crosswalk_definition_revision(db, revision.revision_id)
    with pytest.raises(CrosswalkStoreConflict):
        current_crosswalk_definition(db, revision.definition_id)


def test_a_pointer_at_a_missing_revision_raises(db) -> None:
    build_catalog(db)
    revision = definition()
    _published(db, revision)
    db.execute("ALTER TABLE crosswalk_definition_current DROP CONSTRAINT "
               "crosswalk_definition_current_revision_id_fkey")
    db.execute("DELETE FROM crosswalk_definition_revision")
    with pytest.raises(CrosswalkStoreConflict):
        current_crosswalk_definition(db, revision.definition_id)


# ── the CAS pointer ─────────────────────────────────────────────────────────────────────────────

def test_publishing_creates_the_pointer_at_version_one_unreviewed(db) -> None:
    build_catalog(db)
    revision = definition()
    revision_id, version = _published(db, revision)
    assert (revision_id, version) == (revision.revision_id, 1)
    current = current_crosswalk_definition(db, revision.definition_id)
    assert current.current.pointer_version == 1
    assert current.review_status is LinkReviewStatus.UNREVIEWED
    assert current.revision.revision_id == revision.revision_id


def test_a_stale_expected_version_is_a_conflict_not_a_silent_overwrite(db) -> None:
    build_catalog(db)
    revision = definition()
    _published(db, revision)
    with pytest.raises(CrosswalkStoreConflict):
        set_current_crosswalk_definition(
            db, definition_id=revision.definition_id, revision_id=revision.revision_id,
            expected_pointer_version=0, declared_by=ACTOR)


def test_a_new_revision_of_the_same_crosswalk_advances_the_same_pointer(db) -> None:
    """Widening a leg is a new REVISION of the same crosswalk, not a second crosswalk."""
    build_catalog(db)
    first = definition()
    _published(db, first)
    second = definition(mapping_temporal_policy_revision_id="dtp_" + "c" * 64)
    assert second.definition_id == first.definition_id
    revision_id, version = publish_crosswalk_definition(
        db, second, expected_pointer_version=1, actor=ACTOR)
    assert version == 2
    current = current_crosswalk_definition(db, first.definition_id)
    assert current.current.revision_id == revision_id != first.revision_id
    assert db.execute(
        "SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 2


# ── review is accountability, not permission ────────────────────────────────────────────────────

def test_human_confirmation_moves_review_status_and_nothing_else(db) -> None:
    """TRUTH RULE (§5.8 / Task 10 checklist). The confirmed crosswalk is the SAME crosswalk: same
    identity, same revision, still visible — and still with nothing anywhere that says it may run."""
    build_catalog(db)
    revision = definition()
    _published(db, revision)
    before = current_crosswalk_definition(db, revision.definition_id)
    version = set_crosswalk_review_status(
        db, definition_id=revision.definition_id, review_status=LinkReviewStatus.HUMAN_VERIFIED,
        expected_pointer_version=1, declared_by="user:sam")
    after = current_crosswalk_definition(db, revision.definition_id)
    assert version == 2
    assert after.review_status is LinkReviewStatus.HUMAN_VERIFIED
    assert after.revision.revision_id == before.revision.revision_id
    assert after.revision.content_payload() == before.revision.content_payload()
    # Availability is not a stored axis at all — before or after a review.
    assert len(visible_crosswalk_definitions(db)) == len(visible_crosswalk_definitions(db)) == 1


def test_reviewing_a_crosswalk_that_has_no_pointer_is_a_conflict(db) -> None:
    build_catalog(db)
    with pytest.raises(CrosswalkStoreConflict):
        set_crosswalk_review_status(
            db, definition_id="cwd_" + "e" * 64, review_status=LinkReviewStatus.HUMAN_VERIFIED,
            expected_pointer_version=1, declared_by=ACTOR)


# ── validation ──────────────────────────────────────────────────────────────────────────────────

def test_a_blank_author_is_refused_before_anything_is_written(db) -> None:
    build_catalog(db)
    with pytest.raises(CrosswalkContractError) as exc:
        record_crosswalk_definition_revision(db, definition(), authored_by="   ")
    assert exc.value.code == CROSSWALK_WRITE_INVALID
    assert db.execute("SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 0


def test_an_over_wide_leg_is_refused_as_a_discovery_bug(db) -> None:
    """A 17-member composite tuple key is a legal CONTRACT, so the bound has to live in the store —
    and it has to fire before the CHECK, so the message names the discovery bug rather than a
    constraint violation."""
    build_catalog(db)
    columns = tuple(f"k{i}" for i in range(MAX_PAIRS_PER_LEG + 1))
    wide = definition(
        source_endpoint=wide_endpoint("cib", CIB_TABLE, columns),
        source_to_mapping_pairs=tuple(
            LogicalMappingPairV1(
                normalize_ref("cib", "public", CIB_TABLE, column),
                normalize_ref("cib", "public", MAP_TABLE, column))
            for column in columns))
    with pytest.raises(CrosswalkContractError) as exc:
        record_crosswalk_definition_revision(db, wide, authored_by=ACTOR)
    assert exc.value.code == CROSSWALK_WRITE_INVALID
    assert db.execute("SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 0


def test_a_negative_expected_pointer_version_is_refused(db) -> None:
    build_catalog(db)
    with pytest.raises(CrosswalkContractError):
        set_current_crosswalk_definition(
            db, definition_id="cwd_" + "a" * 64, revision_id="cwd_" + "b" * 64,
            expected_pointer_version=-1, declared_by=ACTOR)


# ── read scope: whole payload ───────────────────────────────────────────────────────────────────

def test_an_author_who_cannot_see_the_mapping_dataset_cannot_publish(db) -> None:
    build_catalog(db, map_columns=(("acct_no", "account_id", "restricted"),
                                   ("ext_acct_ref", "external_account_ref", "restricted")))
    with pytest.raises(SelectionError):
        publish_crosswalk_definition(
            db, definition(), expected_pointer_version=0, actor=ACTOR, roles=())


def test_a_crosswalk_naming_a_hidden_mapping_dataset_is_withheld_WHOLE(db) -> None:
    """Not trimmed, not redacted, not listed-without-its-mapping: a redacted crosswalk still
    discloses that two identifier schemes are connected and by what shape, which is the fact being
    protected."""
    build_catalog(db, map_columns=(("acct_no", "account_id", "restricted"),
                                   ("ext_acct_ref", "external_account_ref", "restricted")))
    revision = definition()
    publish_crosswalk_definition(
        db, revision, expected_pointer_version=0, actor=ACTOR, roles=("restricted_reader",))
    assert visible_crosswalk_definitions(db, roles=("restricted_reader",))
    assert visible_crosswalk_definitions(db, roles=()) == ()


def test_a_hidden_endpoint_column_also_withholds_the_whole_crosswalk(db) -> None:
    build_catalog(db, ftr_columns=(("counter_party_acct_no", "external_account_ref", "restricted"),
                                   ("party_lei", "lei", "")))
    publish_crosswalk_definition(
        db, definition(), expected_pointer_version=0, actor=ACTOR, roles=("restricted_reader",))
    assert visible_crosswalk_definitions(db, roles=("restricted_reader",))
    assert visible_crosswalk_definitions(db, roles=()) == ()


def test_a_crosswalk_whose_refs_have_no_graph_node_fails_closed(db) -> None:
    build_catalog(db)
    revision = definition()
    record_crosswalk_definition_revision(db, revision, authored_by=ACTOR)
    set_current_crosswalk_definition(
        db, definition_id=revision.definition_id, revision_id=revision.revision_id,
        expected_pointer_version=0, declared_by=ACTOR)
    db.execute("DELETE FROM graph_node WHERE catalog_source = 'ftr'")
    assert visible_crosswalk_definitions(db, roles=("restricted_reader",)) == ()


# ── reads by dataset and by column ──────────────────────────────────────────────────────────────

def test_a_crosswalk_is_found_from_all_three_datasets_it_names(db) -> None:
    build_catalog(db)
    _published(db)
    for dataset in (CIB, FTR, MAP):
        assert len(visible_crosswalk_definitions(db, dataset_logical_ref=dataset)) == 1
    assert visible_crosswalk_definitions(
        db, dataset_logical_ref=normalize_ref("cib", "public", "unrelated")) == ()


def test_a_mapping_column_is_not_an_endpoint_anchor(db) -> None:
    """A mapping column takes part in the crosswalk's MECHANISM, not in the relationship it
    expresses, so a column-anchored read from the mapping table returns nothing."""
    build_catalog(db)
    _published(db)
    assert len(crosswalks_for_column(
        db, column_logical_ref=normalize_ref("cib", "public", CIB_TABLE, "acct_no"))) == 1
    assert len(crosswalks_for_column(
        db, column_logical_ref=normalize_ref(
            "ftr", "public", FTR_TABLE, "counter_party_acct_no"))) == 1
    assert crosswalks_for_column(
        db, column_logical_ref=normalize_ref("cib", "public", MAP_TABLE, "acct_no")) == ()
