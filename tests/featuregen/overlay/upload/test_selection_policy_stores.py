"""Release-B Task 7 — the serving/temporal policy stores (migrations 1048/1049).

Proves the store half of the plan's Task-7 items: immutable content-addressed revisions, the
strictest CAS discipline in the repo (fail-closed on ANY pointer drift), read-back content
verification, authoring-time validation that every dataset AND column ref exists and is readable by
its author, the `entity_id + need_role + serving_purpose` policy key, the temporal AGREEMENT rule
including the D12.7 escape hatch for upload-only catalogs, and the absence of free SQL / a live
cutoff / an upload time / a watermark from stored identity.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.serving_policy_store import (
    current_serving_policy,
    load_serving_policy_revision,
    publish_serving_policy,
    record_serving_policy_revision,
    set_current_serving_policy,
)
from featuregen.overlay.upload.source_selection import (
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    PolicyStoreConflict,
    SelectionError,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)
from featuregen.overlay.upload.temporal_policy_store import (
    current_temporal_policy,
    load_temporal_policy_revision,
    publish_temporal_policy,
)

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
_CUST = normalize_ref(_SRC, None, "customer_master")
_ODS = normalize_ref(_SRC, None, "customer_ods")
_SEG = normalize_ref(_SRC, None, "customer_segment")
_SEG_FROM = normalize_ref(_SRC, None, "customer_segment", "effective_from")
_SEG_TO = normalize_ref(_SRC, None, "customer_segment", "effective_to")
_SEG_FLAG = normalize_ref(_SRC, None, "customer_segment", "is_current")


@pytest.fixture
def seeded(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customer_master", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_ods", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_segment", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_segment", "effective_from", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "effective_to", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "is_current", "boolean"),
    ])
    return db


def _policy(**over) -> DatasetServingPolicyRevisionV1:
    kw = dict(entity_id="customer", need_role=DatasetNeedRole.POPULATION,
              serving_purpose=ServingPurpose.ANALYTICAL,
              eligible_dataset_refs=(_CUST, _ODS), preferred_dataset_refs=(_CUST,),
              provenance=human_declaration_provenance(producer_ref="user:priya"))
    kw.update(over)
    return DatasetServingPolicyRevisionV1(**kw)


def _temporal(**over) -> DatasetTemporalPolicyRevisionV1:
    kw = dict(dataset_logical_ref=_SEG, temporal_storage_model=TemporalStorageModel.SCD2,
              current_selection=TemporalSelectionKind.CURRENT_RECORD,
              historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
              effective_from_ref=_SEG_FROM, effective_to_ref=_SEG_TO,
              current_flag_ref=_SEG_FLAG,
              provenance=human_declaration_provenance(producer_ref="user:priya"))
    kw.update(over)
    return DatasetTemporalPolicyRevisionV1(**kw)


def _confirm_temporal_model(db, value):
    """Drive the EXISTING four-eyes propose/confirm flow to a LOAD-BEARING profile value."""
    for action, actor, idem in (("propose_override", ADMIN_A, f"tsm-p-{value}"),
                                ("confirm_override", ADMIN_B, f"tsm-c-{value}")):
        cas = read_field_cas(db, source=_SRC, object_ref="public.customer_segment",
                             field="temporal_storage_model")
        res = apply_field_correction(
            db, source=_SRC, object_ref="public.customer_segment",
            field="temporal_storage_model", action=action, actor=actor, idempotency_key=idem,
            replacement_value=value,
            expected_latest_decision_id=cas["latest_decision_id"],
            expected_evidence_set_hash=cas["evidence_set_hash"],
            expected_policy_version=cas["policy_version"])
        assert res["accepted"] is True, res


# ── serving policy: revisions, CAS, key ─────────────────────────────────────────────────────────

def test_publish_and_read_back_the_current_policy(seeded):
    revision_id, version = publish_serving_policy(
        seeded, _policy(), expected_pointer_version=0, actor="user:priya")
    assert version == 1
    got = current_serving_policy(seeded, entity_id="customer", need_role="population",
                                 serving_purpose="analytical")
    assert got is not None
    revision, pointer = got
    assert revision.revision_id == revision_id
    assert revision.preferred_dataset_refs == (_CUST,)
    assert pointer.pointer_version == 1
    assert pointer.declared_by == "user:priya"


def test_the_policy_key_is_entity_need_role_and_serving_purpose(seeded):
    """Three keys, three independent policies — the SAME entity served differently for feature
    serving and for analysis is the normal case, not a collision."""
    publish_serving_policy(seeded, _policy(), expected_pointer_version=0, actor="user:priya")
    publish_serving_policy(seeded, _policy(serving_purpose=ServingPurpose.FEATURE_SERVING),
                           expected_pointer_version=0, actor="user:priya")
    publish_serving_policy(seeded, _policy(need_role=DatasetNeedRole.DIMENSION_SOURCE),
                           expected_pointer_version=0, actor="user:priya")
    publish_serving_policy(seeded, _policy(entity_id="account"),
                           expected_pointer_version=0, actor="user:priya")
    assert seeded.execute(
        "SELECT count(*) FROM dataset_serving_policy_current").fetchone()[0] == 4
    assert current_serving_policy(seeded, entity_id="customer", need_role="mapping",
                                  serving_purpose="analytical") is None


def test_the_cas_pointer_fails_closed_on_any_drift(seeded):
    publish_serving_policy(seeded, _policy(), expected_pointer_version=0, actor="user:priya")
    second = _policy(preferred_dataset_refs=(_ODS,))
    record_serving_policy_revision(seeded, second, authored_by="user:sam")
    for stale in (0, 2, 7):
        with pytest.raises(PolicyStoreConflict, match="advanced past version"):
            set_current_serving_policy(
                seeded, entity_id="customer", need_role="population",
                serving_purpose="analytical", revision_id=second.revision_id,
                expected_pointer_version=stale, declared_by="user:sam")
    assert set_current_serving_policy(
        seeded, entity_id="customer", need_role="population", serving_purpose="analytical",
        revision_id=second.revision_id, expected_pointer_version=1, declared_by="user:sam") == 2


def test_identical_re_declaration_reuses_the_revision_but_records_the_new_declarer(seeded):
    """Identity is CONTENT (§6.2 split). Re-declaring the same policy must not fork a revision that
    other decisions reference — but WHO made it current is still recorded, on the pointer."""
    first, _ = publish_serving_policy(seeded, _policy(), expected_pointer_version=0,
                                      actor="user:priya")
    second, version = publish_serving_policy(seeded, _policy(), expected_pointer_version=1,
                                             actor="user:sam")
    assert first == second and version == 2
    assert seeded.execute(
        "SELECT count(*) FROM dataset_serving_policy_revision").fetchone()[0] == 1
    _revision, pointer = current_serving_policy(
        seeded, entity_id="customer", need_role="population", serving_purpose="analytical")
    assert pointer.declared_by == "user:sam"


def test_a_corrupted_revision_raises_instead_of_being_served(seeded):
    revision_id, _ = publish_serving_policy(seeded, _policy(), expected_pointer_version=0,
                                            actor="user:priya")
    seeded.execute(
        "UPDATE dataset_serving_policy_revision SET preferred_dataset_refs = %s "
        "WHERE revision_id = %s", ('["bank::public.customer_ods"]', revision_id))
    with pytest.raises(PolicyStoreConflict, match="content verification"):
        load_serving_policy_revision(seeded, revision_id)


def test_migration_1048_check_pins_the_revision_id_shape(seeded):
    with pytest.raises(Exception):  # noqa: B017 — the CHECK constraint must refuse
        with seeded.transaction():
            seeded.execute(
                "INSERT INTO dataset_serving_policy_revision (revision_id, entity_id, need_role, "
                "  serving_purpose, eligible_dataset_refs, content_hash, authored_by) "
                "VALUES ('not-a-dsp-id','customer','population','analytical','[\"x\"]',%s,'u')",
                ("0" * 64,))


# ── authoring-time reference validation ─────────────────────────────────────────────────────────

def test_a_policy_may_not_name_a_dataset_that_does_not_exist(seeded):
    with pytest.raises(SelectionError, match="not a readable dataset"):
        publish_serving_policy(
            seeded, _policy(eligible_dataset_refs=(_CUST, normalize_ref(_SRC, None, "ghost")),
                            preferred_dataset_refs=(_CUST,)),
            expected_pointer_version=0, actor="user:priya")
    assert seeded.execute(
        "SELECT count(*) FROM dataset_serving_policy_revision").fetchone()[0] == 0


def test_hidden_and_missing_are_the_same_answer_to_an_author(db):
    """No existence oracle: an author who cannot see a dataset is told it is not readable, in the
    exact words used for one that was never uploaded."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customer_master", "cif_id", "text"),
        CanonicalRow(_SRC, "secret_master", "national_id", "text", sensitivity="pii"),
    ])
    hidden = normalize_ref(_SRC, None, "secret_master")
    with pytest.raises(SelectionError, match="not a readable dataset"):
        publish_serving_policy(db, _policy(eligible_dataset_refs=(hidden,),
                                           preferred_dataset_refs=()),
                               expected_pointer_version=0, actor="user:priya", roles=())
    # The same author holding `pii_reader` may declare it — the ref was always real.
    revision_id, _ = publish_serving_policy(
        db, _policy(eligible_dataset_refs=(hidden,), preferred_dataset_refs=()),
        expected_pointer_version=0, actor="user:priya", roles=("pii_reader",))
    assert revision_id.startswith("dsp_")


def test_a_temporal_policy_may_not_name_a_column_that_does_not_exist(seeded):
    with pytest.raises(SelectionError, match="not a readable column"):
        publish_temporal_policy(
            seeded, _temporal(availability_ref=normalize_ref(_SRC, None, "customer_segment",
                                                             "ghost_col")),
            expected_pointer_version=0, actor="user:priya")
    assert seeded.execute(
        "SELECT count(*) FROM dataset_temporal_policy_revision").fetchone()[0] == 0


# ── temporal policy: agreement rule + D12.7 escape hatch ────────────────────────────────────────

def test_an_upload_only_catalog_may_declare_its_temporal_model_through_the_policy(seeded):
    """D12.7. Nothing attested this dataset's temporal model — the POLICY is the operational
    declaration, and its provenance says a person made it."""
    revision_id, version = publish_temporal_policy(
        seeded, _temporal(), expected_pointer_version=0, actor="user:priya")
    assert version == 1
    revision, pointer = current_temporal_policy(seeded, _SEG)
    assert revision.revision_id == revision_id
    assert revision.historical_selection is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF
    assert pointer.declared_by == "user:priya"
    (evidence,) = revision.provenance.evidence
    assert (evidence.producer, evidence.strength) == ("human", "confirmed")


def test_a_policy_agreeing_with_a_load_bearing_value_is_accepted(seeded):
    _confirm_temporal_model(seeded, "scd2")
    revision_id, _ = publish_temporal_policy(seeded, _temporal(), expected_pointer_version=0,
                                             actor="user:priya")
    assert revision_id.startswith("dtp_")


def test_a_policy_contradicting_a_load_bearing_value_is_refused(seeded):
    """A policy may not overrule a governed classification: that classification is what the
    execution gates read, so the two must not be able to disagree."""
    _confirm_temporal_model(seeded, "snapshot")
    with pytest.raises(SelectionError, match="would overrule"):
        publish_temporal_policy(seeded, _temporal(), expected_pointer_version=0,
                                actor="user:priya")
    assert seeded.execute(
        "SELECT count(*) FROM dataset_temporal_policy_revision").fetchone()[0] == 0


def test_an_unknown_storage_model_may_not_promise_history_but_may_still_be_honest(seeded):
    """"We do not know how this table stores history" is a legitimate declaration — it just cannot
    promise a historical answer. The contract refuses the promise before the store is reached, and
    the honest `explicit_only` shape publishes normally."""
    with pytest.raises(SelectionError, match="keeps no history"):
        publish_temporal_policy(
            seeded,
            _temporal(temporal_storage_model=TemporalStorageModel.UNKNOWN,
                      historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF),
            expected_pointer_version=0, actor="user:priya")
    assert seeded.execute(
        "SELECT count(*) FROM dataset_temporal_policy_revision").fetchone()[0] == 0

    revision_id, _ = publish_temporal_policy(
        seeded,
        _temporal(temporal_storage_model=TemporalStorageModel.UNKNOWN,
                  historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
                  effective_from_ref=None, effective_to_ref=None),
        expected_pointer_version=0, actor="user:priya")
    assert current_temporal_policy(seeded, _SEG)[0].revision_id == revision_id


def test_the_temporal_cas_pointer_fails_closed(seeded):
    publish_temporal_policy(seeded, _temporal(), expected_pointer_version=0, actor="user:priya")
    with pytest.raises(PolicyStoreConflict, match="advanced past version"):
        publish_temporal_policy(seeded, _temporal(availability_ref=_SEG_FROM),
                                expected_pointer_version=0, actor="user:sam")
    revision_id, version = publish_temporal_policy(
        seeded, _temporal(availability_ref=_SEG_FROM), expected_pointer_version=1,
        actor="user:sam")
    assert version == 2
    assert current_temporal_policy(seeded, _SEG)[0].revision_id == revision_id


def test_a_corrupted_temporal_revision_raises_instead_of_being_served(seeded):
    revision_id, _ = publish_temporal_policy(seeded, _temporal(), expected_pointer_version=0,
                                             actor="user:priya")
    seeded.execute(
        "UPDATE dataset_temporal_policy_revision SET current_selection = 'explicit_only' "
        "WHERE revision_id = %s", (revision_id,))
    with pytest.raises(PolicyStoreConflict, match="content verification"):
        load_temporal_policy_revision(seeded, revision_id)


def test_migration_1049_check_pins_the_revision_id_and_the_closed_vocabularies(seeded):
    for bad in (
        ("not-a-dtp-id", "scd2", "current_record", "valid_at_report_cutoff"),
        ("dtp_" + "0" * 64, "warehouse", "current_record", "valid_at_report_cutoff"),
        ("dtp_" + "0" * 64, "scd2", "whenever", "valid_at_report_cutoff"),
        ("dtp_" + "0" * 64, "scd2", "current_record", "whenever"),
    ):
        with pytest.raises(Exception):  # noqa: B017 — the CHECK constraint must refuse
            with seeded.transaction():
                seeded.execute(
                    "INSERT INTO dataset_temporal_policy_revision (revision_id, "
                    "  dataset_logical_ref, temporal_storage_model, current_selection, "
                    "  historical_selection, content_hash, authored_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'u')", (*bad, "0" * 64))


# ── identity carries no free SQL, no live cutoff, no upload time, no watermark ──────────────────

def test_no_stored_policy_column_can_carry_sql_a_value_or_a_watermark(seeded):
    """The store has no predicate-text column at all — "no free SQL" is structural, not a
    validation someone could forget to call. And no column names a time the data arrived."""
    for table in ("dataset_serving_policy_revision", "dataset_temporal_policy_revision"):
        columns = {r[0] for r in seeded.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
        assert not (columns & {"predicate_sql", "sql", "expression", "where_clause",
                               "cutoff_value", "report_cutoff", "watermark", "uploaded_at",
                               "catalog_watermark", "freshness_seconds"}), (table, columns)


def test_content_hash_is_stable_across_a_re_read(seeded):
    """The stored hash is recomputed from the stored row on every read; a policy that survived a
    round trip with a different hash would be a policy whose identity depends on the writer."""
    serving_id, _ = publish_serving_policy(seeded, _policy(), expected_pointer_version=0,
                                           actor="user:priya")
    temporal_id, _ = publish_temporal_policy(seeded, _temporal(), expected_pointer_version=0,
                                             actor="user:priya")
    assert load_serving_policy_revision(seeded, serving_id).revision_id == serving_id
    assert load_temporal_policy_revision(seeded, temporal_id).revision_id == temporal_id
