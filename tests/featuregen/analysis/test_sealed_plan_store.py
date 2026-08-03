"""Sealing a plan, replaying it, and refusing to run a stale one (Release-B Task 9).

Three claims, against the pilot bank and nothing live:

  * a RESOLVED plan seals into the shared ``structured_result`` store (type
    ``analysis_execution_plan``, version 2) — no second replay table, no migration — and replays by
    its own identity;
  * a REFUSED plan seals nothing;
  * every one of the SIX PINS is re-read immediately before execution, and any drift is a TYPED
    refusal naming what moved — never a silent re-derivation. That is
    `authorize_execution_realizations`' doctrine, not runprep's.

And the pin property that the pin exists for: when a policy changes LATER, the sealed plan stays
exactly what it was. Nothing claims a historical output was corrected.
"""
from __future__ import annotations

import pytest
from tests.featuregen.analysis.release_b_bank import (
    ARCHIVE,
    CUST,
    CUTOFF_REF,
    QUESTION,
    SEG,
    SRC,
    TRAN,
    build_bank,
    pilot_plan,
    publish_temporal_policy_for,
)
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    PILOT_JOIN_EVIDENCE,
    PREVIOUS_MONTH,
    REPORT_CUTOFF,
)
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.execution import ExecutionInputs, plan_to_execution_ir
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.sealed_plan import (
    ANALYSIS_PLAN_CONTRACT,
    SEALED_PLAN_SOURCE_UNREADABLE,
    SEALED_PLAN_STALE_DATASET_PROFILE,
    SEALED_PLAN_STALE_PHYSICAL_BINDING,
    SEALED_PLAN_STALE_ROW_SELECTION,
    SEALED_PLAN_STALE_SERVING_POLICY,
    SEALED_PLAN_STALE_SOURCE_SELECTION,
    SEALED_PLAN_STALE_TEMPORAL_POLICY,
    SealedPlanError,
    build_execution_ir_v2,
    seal_refs_from_selections,
)
from featuregen.analysis.sealed_plan_store import (
    SEALED_PLAN_SUBJECT_KIND,
    analysis_question_ref,
    current_sealed_plan,
    replay_sealed_plan,
    revalidate_sealed_plan,
    seal_analysis_plan,
)
from featuregen.data_agent.binding_store import resolve_table
from featuregen.overlay.upload.catalog_profiles import build_catalog_profile_revision
from featuregen.overlay.upload.profile_store import (
    record_catalog_profile_revision,
    set_current_catalog_profile,
)
from featuregen.overlay.upload.serving_policy_store import publish_serving_policy
from featuregen.overlay.upload.source_selection import (
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind
from featuregen.overlay.upload.temporal_resolver import attribution_policy_for


@pytest.fixture
def bank(db, monkeypatch):
    return build_bank(db, monkeypatch)


def _grounded(bank, **over):
    return ground_analysis_plan(bank, pilot_plan(**over), cutoff_value_ref=CUTOFF_REF,
                                recorded_by="task9")


def _v2(bank, grounded=None, *, question: str = QUESTION):
    """A validated V2 for the pilot question, assembled the way the worked acceptance does."""
    from featuregen.overlay.upload.temporal_policy_store import current_temporal_policy

    grounded = grounded or _grounded(bank)
    by_role = {s.need.need_role: s for s in grounded.selections.source_selections}

    def _binding(ref: str):
        resolved = resolve_table(bank, catalog_source=SRC, table=ref.rsplit(".", 1)[-1])
        assert resolved is not None, ref
        return resolved[0]

    policy, _pointer = current_temporal_policy(bank, SEG)
    ir = plan_to_execution_ir(grounded, ExecutionInputs(
        spine_binding=_binding(by_role[DatasetNeedRole.POPULATION].selected_dataset_ref),
        spine_key_column="cif_id",
        event_binding=_binding(by_role[DatasetNeedRole.EVENT_SOURCE].selected_dataset_ref),
        event_key_column="cif_id", period_column="tran_month",
        window_partitions={"current": (CURRENT_MONTH,), "previous": (PREVIOUS_MONTH,)},
        eligibility=_eligibility(),
        dimension_binding=_binding(by_role[DatasetNeedRole.DIMENSION_SOURCE]
                                   .selected_dataset_ref),
        attribution=attribution_policy_for(
            policy, kind=grounded.selections.row_selections[0].selection_kind,
            cutoff_value=REPORT_CUTOFF),
        join_evidence=PILOT_JOIN_EVIDENCE))
    return build_execution_ir_v2(ir, grounded.selections, question=question)


def _sealed(bank, **kw):
    return seal_analysis_plan(bank, _v2(bank, **kw), sealed_by="user:priya", question=QUESTION)


def _count(bank) -> int:
    return bank.execute(
        "SELECT count(*) FROM structured_result WHERE result_type = %s",
        (ANALYSIS_PLAN_CONTRACT,)).fetchone()[0]


# ── nothing pre-exists ───────────────────────────────────────────────────────────────────────────


def test_no_analysis_plan_has_EVER_been_stored_before_this_release(bank):
    """The flag-law claim, asserted rather than argued: enabling the flag cannot rewrite a stored
    plan because there are none. The identity function may therefore change freely."""
    assert _count(bank) == 0
    assert replay_sealed_plan(bank, "a" * 64) is None
    assert current_sealed_plan(bank, QUESTION) is None


# ── sealing ──────────────────────────────────────────────────────────────────────────────────────


def test_a_resolved_plan_seals_into_the_SHARED_store_and_replays_by_its_identity(bank):
    ir = _v2(bank)
    record = seal_analysis_plan(bank, ir, sealed_by="user:priya", question=QUESTION)
    assert record.plan_hash == ir.plan_hash
    assert record.contract_version == 2
    assert _count(bank) == 1

    replayed = replay_sealed_plan(bank, ir.plan_hash)
    assert replayed == record
    assert replayed.computation == ir.execution_ir.identity_payload()
    assert replayed.decisions == ir.decisions


def test_the_seal_carries_all_SIX_PINS(bank):
    record = _sealed(bank)
    by_role = {s.need_role: s for s in record.sources}
    event = by_role["event_source"]
    assert event.dataset_ref == TRAN                                    # pin: source_selection
    assert event.dataset_profile_hash                                   # pin: dataset_profile
    assert event.serving_policy_revision_id.startswith("dsp_")          # pin: serving_policy
    assert event.binding_revision_id.startswith("pbr_")                 # pin: physical_binding
    assert event.source_selection_hash
    row = record.rows[0]
    assert row.dataset_ref == SEG
    assert row.temporal_policy_revision_id.startswith("dtp_")           # pin: temporal_policy
    assert row.row_selection_hash                                       # pin: row_selection
    assert row.selection_kind == TemporalSelectionKind.VALID_AT_REPORT_CUTOFF.value


def test_sealing_the_same_plan_twice_writes_ONE_row(bank):
    first = _sealed(bank)
    second = seal_analysis_plan(bank, _v2(bank), sealed_by="user:sam", question=QUESTION)
    assert second.structured_result_id == first.structured_result_id
    assert _count(bank) == 1


def test_two_WORDINGS_of_one_computation_are_one_sealed_plan(bank):
    """The question is provenance, not identity — so it is outside the sealed bytes. Inside them,
    the second wording would be a content-address collision the store reports as corruption."""
    first = _sealed(bank)
    reworded = seal_analysis_plan(
        bank, _v2(bank, question="show customers with declining transaction volume"),
        sealed_by="user:priya", question=QUESTION)
    assert reworded.structured_result_id == first.structured_result_id
    assert _count(bank) == 1


def test_the_current_pointer_answers_what_plan_is_current_for_a_question(bank):
    record = _sealed(bank)
    assert current_sealed_plan(bank, QUESTION) == record
    pointer = bank.execute(
        "SELECT subject_kind, subject_ref, lifecycle FROM structured_result_current "
        "WHERE result_type = %s", (ANALYSIS_PLAN_CONTRACT,)).fetchone()
    assert pointer == (SEALED_PLAN_SUBJECT_KIND, analysis_question_ref(QUESTION), "current")


def test_a_REFUSED_plan_seals_nothing(bank):
    """An undeclared population refuses at selection. There is nothing to seal, and recording it
    would replay later as a decision nobody made."""
    grounded = _grounded(bank, population_table_ref="", population_key_ref="")
    assert grounded.selections.resolved is False
    with pytest.raises(SealedPlanError, match="did not resolve"):
        seal_refs_from_selections(grounded.selections)
    assert _count(bank) == 0


def test_only_a_validated_V2_may_be_sealed(bank):
    with pytest.raises(SealedPlanError, match="AnalysisExecutionIRV2"):
        seal_analysis_plan(bank, object(), sealed_by="user:priya", question=QUESTION)
    with pytest.raises(SealedPlanError, match="producer ref"):
        seal_analysis_plan(bank, _v2(bank), sealed_by="  ", question=QUESTION)


# ── revalidation: the happy path ─────────────────────────────────────────────────────────────────


def test_a_freshly_sealed_plan_revalidates_clean(bank):
    assert revalidate_sealed_plan(bank, _sealed(bank)) is None


# ── revalidation: one drift case per pin ─────────────────────────────────────────────────────────


def test_drift_SERVING_POLICY_a_newer_declaration_refuses_by_name(bank):
    """Someone re-declares which copy of the transactions may serve. The plan was approved under
    the old declaration and must not quietly answer under the new one."""
    record = _sealed(bank)
    publish_serving_policy(bank, DatasetServingPolicyRevisionV1(
        entity_id="customer", need_role=DatasetNeedRole.EVENT_SOURCE,
        serving_purpose=ServingPurpose.ANALYTICAL,
        eligible_dataset_refs=(TRAN, ARCHIVE), preferred_dataset_refs=(ARCHIVE,),
        provenance=human_declaration_provenance(producer_ref="user:sam")),
        expected_pointer_version=1, actor="user:sam")

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_SERVING_POLICY
    assert refusal.sealed_value != refusal.current_value
    assert refusal.subject_refs == ("customer:event_source:analytical",)


def test_drift_TEMPORAL_POLICY_a_re_declared_row_rule_refuses_by_name(bank):
    """The case this task exists for: someone re-declares how the dimension table keeps history,
    and every already-planned answer would silently classify customers by a different row."""
    record = _sealed(bank)
    publish_temporal_policy_for(
        bank, expected_pointer_version=1,
        historical_selection=TemporalSelectionKind.CURRENT_RECORD)

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_TEMPORAL_POLICY
    assert refusal.subject_refs == (SEG,)
    assert refusal.sealed_value != refusal.current_value


def test_drift_DATASET_PROFILE_a_narrative_edit_re_keys_the_meaning(bank):
    """D12.10: a catalog narrative edit re-keys every dataset profile in the source, deliberately —
    narrative is meaning-bearing. So the plan's pinned meaning is no longer the current one."""
    record = _sealed(bank)
    revision = build_catalog_profile_revision(
        catalog_source=SRC, display_name="FTR", description="Reworded.",
        business_context="Retail book.", business_domains=("retail",),
        producer_ref="user:priya")
    record_catalog_profile_revision(bank, revision)
    set_current_catalog_profile(bank, catalog_source=SRC, revision_id=revision.revision_id,
                                expected_pointer_version=0)

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_DATASET_PROFILE
    assert refusal.sealed_value != refusal.current_value


def test_drift_PHYSICAL_BINDING_a_revision_that_no_longer_exists_refuses(bank):
    """`binding_revision_exists` is the authoring-time check, reused at execution. A selection
    naming a revision that is not persisted is unreplayable — the observation store's FK would
    refuse it later, at far greater cost."""
    record = _sealed(bank)
    bank.execute("DELETE FROM physical_dataset_binding_revision WHERE binding_revision_id = %s",
                 (record.sources[0].binding_revision_id,))

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_PHYSICAL_BINDING
    assert "no longer persisted" in refusal.detail


def test_drift_PHYSICAL_BINDING_a_re_pointed_catalog_engine_refuses(bank):
    """The address moved without a single decision changing: the catalog now routes this table to
    a different database, so the plan would read a look-alike."""
    record = _sealed(bank)
    bank.execute("UPDATE physical_dataset_binding SET database_name = %s "
                 "WHERE catalog_source = %s", ("featuregen_dr", SRC))

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_PHYSICAL_BINDING
    assert refusal.sealed_value != refusal.current_value


def test_drift_SOURCE_SELECTION_a_tampered_seal_cannot_be_replayed_as_a_decision(bank):
    """The composite is checked LAST: after all four components match, a mismatch means the sealed
    bytes do not reproduce their own identity — corruption, not drift."""
    from dataclasses import replace

    record = _sealed(bank)
    tampered = replace(record, decisions=replace(
        record.decisions,
        sources=tuple(replace(s, source_selection_hash="0" * 64) for s in record.sources)))

    refusal = revalidate_sealed_plan(bank, tampered)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_SOURCE_SELECTION


def test_drift_ROW_SELECTION_a_tampered_row_seal_cannot_be_replayed_either(bank):
    from dataclasses import replace

    record = _sealed(bank)
    tampered = replace(record, decisions=replace(
        record.decisions,
        rows=tuple(replace(r, row_selection_hash="0" * 64) for r in record.rows)))

    refusal = revalidate_sealed_plan(bank, tampered)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_ROW_SELECTION


def test_a_caller_who_may_not_read_the_source_gets_the_SAME_words_as_a_missing_one(bank):
    """Read scope is checked FIRST, before any drift report: a caller told "the profile of the
    archive changed" has been handed the existence of a catalog they were never granted."""
    record = _sealed(bank)
    # `visible_requires` is GENERATED (migration 1032) from the sensitivity tag, so the tag is
    # what a test sets — writing the enforcement column directly is refused by the database, which
    # is the guarantee working.
    bank.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (SRC, CUST.rsplit(".", 1)[-1]))

    refusal = revalidate_sealed_plan(bank, record, roles=("catalog_viewer",))
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_SOURCE_UNREADABLE
    assert "not a readable dataset" in refusal.detail


# ── the pin property ─────────────────────────────────────────────────────────────────────────────


def test_a_later_policy_change_does_NOT_rewrite_the_sealed_plan(bank):
    """No restatement claims. The sealed plan stays byte-identical to what it was; the policy
    change is a reason the plan may not RUN again, never a reason to say an old answer was wrong."""
    record = _sealed(bank)
    before = replay_sealed_plan(bank, record.plan_hash)
    publish_temporal_policy_for(
        bank, expected_pointer_version=1,
        historical_selection=TemporalSelectionKind.CURRENT_RECORD)

    after = replay_sealed_plan(bank, record.plan_hash)
    assert after == before
    assert after.rows[0].temporal_policy_revision_id == before.rows[0].temporal_policy_revision_id
    assert _count(bank) == 1
