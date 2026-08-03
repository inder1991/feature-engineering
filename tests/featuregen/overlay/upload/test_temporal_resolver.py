"""Release-B Task 8 — the temporal resolver's truth table.

One test per row of §6.5: which selection kind answers a CURRENT question and which answers a
HISTORICAL one, what each compiles into, and which of the eight closed refusal codes fires when it
cannot. Plus the two rules the plan states in words and that a resolver is most likely to break
quietly:

  * a historical request NEVER falls back to today's row (§5.5);
  * effective-time and availability-time predicates stay SEPARATE (§8 Task 8) — "when was this
    true" and "when could we have known it" are different questions, and one merged "as of"
    answers neither.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.data_agent.dimensions import AttributionBasis
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    SelectionError,
    human_declaration_provenance,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)
from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy
from featuregen.overlay.upload.temporal_resolver import (
    RequestTemporality,
    attribution_policy_for,
    resolve_row_selection,
    selection_code_for_engine_refusal,
    snapshot_policy_for,
)

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
SEG = normalize_ref(_SRC, None, "customer_segment")
SEG_FROM = normalize_ref(_SRC, None, "customer_segment", "effective_from")
SEG_TO = normalize_ref(_SRC, None, "customer_segment", "effective_to")
SEG_FLAG = normalize_ref(_SRC, None, "customer_segment", "is_current")
SEG_SNAP = normalize_ref(_SRC, None, "customer_segment", "snapshot_date")
SEG_SEQ = normalize_ref(_SRC, None, "customer_segment", "load_seq")
SEG_AVAIL = normalize_ref(_SRC, None, "customer_segment", "available_at")

CUTOFF_REF = "report_cutoff_param"
PROFILE_HASH = "deadbeef"


@pytest.fixture
def seeded(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customer_segment", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_segment", "effective_from", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "effective_to", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "is_current", "boolean"),
        CanonicalRow(_SRC, "customer_segment", "snapshot_date", "date"),
        CanonicalRow(_SRC, "customer_segment", "load_seq", "int"),
        CanonicalRow(_SRC, "customer_segment", "available_at", "timestamp"),
    ])
    return db


def _revision(**over) -> DatasetTemporalPolicyRevisionV1:
    kw = dict(dataset_logical_ref=SEG, temporal_storage_model=TemporalStorageModel.SCD2,
              current_selection=TemporalSelectionKind.CURRENT_RECORD,
              historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
              effective_from_ref=SEG_FROM, effective_to_ref=SEG_TO, current_flag_ref=SEG_FLAG,
              provenance=human_declaration_provenance(producer_ref="user:priya"))
    kw.update(over)
    return DatasetTemporalPolicyRevisionV1(**kw)


def _publish(db, **over) -> DatasetTemporalPolicyRevisionV1:
    revision = _revision(**over)
    publish_temporal_policy(db, revision, expected_pointer_version=0, actor="user:priya")
    return revision


def _resolve(db, temporality=RequestTemporality.HISTORICAL, cutoff_value_ref=CUTOFF_REF):
    return resolve_row_selection(
        db, dataset_logical_ref=SEG, dataset_profile_hash=PROFILE_HASH,
        temporality=temporality, cutoff_value_ref=cutoff_value_ref)


def _kinds(outcome) -> list[str]:
    return [str(p["kind"]) for p in outcome.row_selection.predicate_payloads]


def _confirm_temporal_model(db, value):
    for action, actor, idem in (("propose_override", ADMIN_A, f"tm-p-{value}"),
                                ("confirm_override", ADMIN_B, f"tm-c-{value}")):
        cas = read_field_cas(db, source=_SRC, object_ref="public.customer_segment",
                             field="temporal_storage_model")
        result = apply_field_correction(
            db, source=_SRC, object_ref="public.customer_segment",
            field="temporal_storage_model", action=action, actor=actor, idempotency_key=idem,
            replacement_value=value,
            expected_latest_decision_id=cas["latest_decision_id"],
            expected_evidence_set_hash=cas["evidence_set_hash"],
            expected_policy_version=cas["policy_version"])
        assert result["accepted"] is True, result


# ── the policy is the declaration, and its absence is a refusal ─────────────────────────────────


def test_a_dataset_with_no_temporal_policy_refuses(seeded):
    outcome = _resolve(seeded)
    assert not outcome.resolved
    assert outcome.refusal.code == TEMPORAL_MODEL_UNKNOWN
    assert outcome.refusal.subject_refs == (SEG,)


def test_a_policy_contradicting_the_governed_classification_refuses(seeded):
    """A policy may not overrule the classification the execution gates read (the agreement rule).

    Published FIRST and contradicted AFTER, because the store refuses to publish a contradiction —
    this is the state a later four-eyes correction creates under an already-current policy."""
    _publish(seeded)
    _confirm_temporal_model(seeded, "snapshot")
    outcome = _resolve(seeded)
    assert not outcome.resolved
    assert outcome.refusal.code == TEMPORAL_MODEL_UNKNOWN
    assert "snapshot" in outcome.refusal.detail


# ── VALID_AT_REPORT_CUTOFF (REUSE) ──────────────────────────────────────────────────────────────


def test_a_historical_request_compiles_the_half_open_interval(seeded):
    policy = _publish(seeded)
    outcome = _resolve(seeded)
    assert outcome.resolved
    selection = outcome.row_selection
    assert selection.selection_kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF
    assert selection.temporal_policy_revision_id == policy.revision_id
    assert selection.cutoff_value_ref == CUTOFF_REF
    # HALF-OPEN [from, to): `<=` on the start INCLUDES a row beginning exactly on the cutoff, `>`
    # on the end EXCLUDES one ending exactly on it. Complementary, so adjacent rows select one.
    assert [(p["column_ref"], p["operator"]) for p in selection.predicate_payloads] == [
        (SEG_FROM, "<="), (SEG_TO, ">")]


def test_effective_time_and_availability_time_stay_separate_predicates(seeded):
    _publish(seeded, availability_ref=SEG_AVAIL)
    outcome = _resolve(seeded)
    assert _kinds(outcome) == ["effective_time", "effective_time", "availability_time"]
    availability = outcome.row_selection.predicate_payloads[-1]
    assert availability["column_ref"] == SEG_AVAIL


def test_a_kind_that_needs_a_cutoff_RETURNS_a_refusal_without_the_ref(seeded):
    """CHANGE OF INTENT (Task-8 review, F1). This used to RAISE — the one place in the new code that
    did — and grounding's blanket `except SelectionError: continue` erased it: the plan came back
    with no row rule, no refusal and `resolved` True. A refusal is a decision outcome, so it rides
    the payload like every other one in this module.

    The code is TEMPORAL_MODEL_UNKNOWN rather than TEMPORAL_HISTORICAL_CURRENT_ONLY: this dataset
    KEEPS history, and a refusal may not assert something false about the data to get a
    better-worded question. See `resolve_row_selection` for the full adjudication."""
    _publish(seeded)
    outcome = _resolve(seeded, cutoff_value_ref=None)
    assert not outcome.resolved
    assert outcome.refusal.code == TEMPORAL_MODEL_UNKNOWN
    assert "report cutoff" in outcome.refusal.detail
    assert outcome.refusal.subject_refs == (SEG,)


def test_a_kind_that_needs_NO_cutoff_resolves_without_one(seeded):
    """`current_record` reads the row the source FLAGS as current, which needs no instant — so the
    refusal above is about the two as-of kinds, not about a missing argument in general."""
    _publish(seeded)
    outcome = _resolve(seeded, temporality=RequestTemporality.CURRENT, cutoff_value_ref=None)
    assert outcome.resolved


# ── CURRENT_RECORD (ENGINE A) ───────────────────────────────────────────────────────────────────


def test_a_current_request_compiles_the_declared_current_flag(seeded):
    _publish(seeded)
    outcome = _resolve(seeded, temporality=RequestTemporality.CURRENT)
    assert outcome.resolved
    assert outcome.row_selection.selection_kind is TemporalSelectionKind.CURRENT_RECORD
    assert [(p["kind"], p["column_ref"], p["operator"])
            for p in outcome.row_selection.predicate_payloads] == [
        ("current_flag", SEG_FLAG, "is_true")]


def test_on_a_current_only_dataset_every_row_is_current_so_there_is_no_predicate(seeded):
    """The honest empty predicate set. A current-only table has one row per key BY CONSTRUCTION,
    and inventing a filter for it would imply a distinction the data does not carry."""
    _publish(seeded, temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
             historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
             effective_from_ref=None, effective_to_ref=None, current_flag_ref=None)
    outcome = _resolve(seeded, temporality=RequestTemporality.CURRENT, cutoff_value_ref=None)
    assert outcome.resolved
    assert outcome.row_selection.predicate_payloads == ()


# ── a historical question a dataset cannot answer ───────────────────────────────────────────────


def test_a_historical_request_against_a_current_only_dataset_never_answers_from_today(seeded):
    _publish(seeded, temporal_storage_model=TemporalStorageModel.CURRENT_ONLY,
             historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
             effective_from_ref=None, effective_to_ref=None, current_flag_ref=None)
    outcome = _resolve(seeded, temporality=RequestTemporality.HISTORICAL)
    assert not outcome.resolved
    assert outcome.refusal.code == TEMPORAL_HISTORICAL_CURRENT_ONLY


def test_an_scd1_dataset_without_history_is_the_same_refusal(seeded):
    """SCD1 overwrites in place, so it keeps no more history than a current-only table does.

    It still needs `current_flag_ref` — unlike a current-only table it is not one row per key by
    construction, so the contract refuses to let "the current row" be whichever is read first."""
    _publish(seeded, temporal_storage_model=TemporalStorageModel.SCD1,
             historical_selection=TemporalSelectionKind.EXPLICIT_ONLY,
             effective_from_ref=None, effective_to_ref=None)
    outcome = _resolve(seeded, temporality=RequestTemporality.HISTORICAL)
    assert outcome.refusal.code == TEMPORAL_HISTORICAL_CURRENT_ONLY


def test_a_current_request_with_no_current_rule_is_an_unsettled_model(seeded):
    _publish(seeded, temporal_storage_model=TemporalStorageModel.SNAPSHOT,
             current_selection=TemporalSelectionKind.EXPLICIT_ONLY,
             historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
             effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
             snapshot_ref=SEG_SNAP)
    outcome = _resolve(seeded, temporality=RequestTemporality.CURRENT)
    assert not outcome.resolved
    assert outcome.refusal.code == TEMPORAL_MODEL_UNKNOWN


# ── LATEST_SNAPSHOT_AS_OF (ENGINE B) ────────────────────────────────────────────────────────────


def test_a_snapshot_dataset_compiles_a_snapshot_time_predicate(seeded):
    _publish(seeded, temporal_storage_model=TemporalStorageModel.SNAPSHOT,
             current_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
             historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
             effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
             snapshot_ref=SEG_SNAP, tie_break_refs=(SEG_SEQ,))
    outcome = _resolve(seeded)
    assert outcome.resolved
    assert [(p["kind"], p["column_ref"], p["operator"])
            for p in outcome.row_selection.predicate_payloads] == [
        ("snapshot_time", SEG_SNAP, "<=")]


# ── identity: the runtime value is a parameter, never part of the decision ──────────────────────


def test_the_row_selection_carries_the_cutoff_REF_and_never_a_value(seeded):
    _publish(seeded)
    first = _resolve(seeded).row_selection
    second = _resolve(seeded).row_selection
    assert first.content_hash == second.content_hash
    assert "2026" not in first.content_hash
    with pytest.raises(SelectionError, match="literal date"):
        resolve_row_selection(seeded, dataset_logical_ref=SEG, dataset_profile_hash=PROFILE_HASH,
                              temporality=RequestTemporality.HISTORICAL,
                              cutoff_value_ref="2026-06-30")


# ── engine adapters ─────────────────────────────────────────────────────────────────────────────


def test_the_report_cutoff_adapter_emits_the_EXISTING_scd2_engine(seeded):
    policy = _revision()
    attribution = attribution_policy_for(
        policy, kind=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF, cutoff_value="2026-06-30")
    assert attribution.attribution_basis is AttributionBasis.REPORT_CUTOFF
    assert attribution.effective_from_column == "effective_from"
    assert attribution.report_cutoff == "2026-06-30"


def test_the_current_record_adapter_emits_the_new_current_value_basis(seeded):
    attribution = attribution_policy_for(
        _revision(), kind=TemporalSelectionKind.CURRENT_RECORD)
    assert attribution.attribution_basis is AttributionBasis.CURRENT_VALUE
    assert attribution.current_flag_column == "is_current"
    assert attribution.report_cutoff == ""


def test_a_snapshot_kind_is_not_handed_to_the_attribution_engine(seeded):
    with pytest.raises(SelectionError, match="not an interval selection"):
        attribution_policy_for(_revision(), kind=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF)


def test_the_snapshot_adapter_carries_the_policys_governed_tie_breakers(seeded):
    policy = _revision(temporal_storage_model=TemporalStorageModel.SNAPSHOT,
                       current_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
                       historical_selection=TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
                       effective_from_ref=None, effective_to_ref=None, current_flag_ref=None,
                       snapshot_ref=SEG_SNAP, tie_break_refs=(SEG_SEQ,))
    snapshot = snapshot_policy_for(policy, cutoff_value="2026-06-30", key_column="cif_id")
    assert snapshot.snapshot_column == "snapshot_date"
    assert snapshot.tie_break_columns == ("load_seq",)
    assert snapshot.key_column == "cif_id"


# ── the engine's own refusals, in the closed selection vocabulary ───────────────────────────────


def test_the_existing_overlap_refusal_is_translated_not_reimplemented():
    """SCD overlap detection stays `assert_no_dimension_overlap`. This is only its SELECTION
    spelling, so the clarification Task 7 wrote renders — and it deliberately remains outside
    `REFUSAL_TO_GAP`, because no decision closes a data defect."""
    assert selection_code_for_engine_refusal(
        "ATTRIBUTION_OVERLAPPING_RECORDS") == TEMPORAL_SCD_OVERLAP


def test_a_capability_limit_translates_to_nothing():
    assert selection_code_for_engine_refusal("ANALYSIS_UNSUPPORTED_MEASURE") is None
