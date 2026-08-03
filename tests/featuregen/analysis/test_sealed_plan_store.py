"""Sealing a plan, replaying it, and refusing to run a stale one (Release-B Task 9).

Three claims, against the pilot bank and nothing live:

  * a RESOLVED plan seals into the shared ``structured_result`` store (type
    ``analysis_execution_plan``, version 2) — no second replay table, no migration — and replays by
    its own identity;
  * a REFUSED plan seals nothing;
  * every one of the SEVEN PINS is re-read immediately before execution, and any drift is a TYPED
    refusal naming what moved — never a silent re-derivation. That is
    `authorize_execution_realizations`' doctrine, not runprep's. (The seventh is ELIGIBILITY: it
    was consumed from the seal and never re-read, so a re-declared definition of "which rows
    count" left every sealed plan answering under the old one.)

And the pin property that the pin exists for: when a policy changes LATER, the sealed plan stays
exactly what it was. Nothing claims a historical output was corrected.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb
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
    TRANSACTION_TABLE,
)
from tests.featuregen.data_agent.test_analysis_ir import _policy as _eligibility

from featuregen.analysis.execution import ExecutionInputs, plan_to_execution_ir
from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.sealed_plan import (
    ANALYSIS_PLAN_CONTRACT,
    SEALED_PLAN_SOURCE_UNREADABLE,
    SEALED_PLAN_STALE_DATASET_PROFILE,
    SEALED_PLAN_STALE_ELIGIBILITY,
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
from featuregen.data_agent.eligibility import NullBehavior
from featuregen.data_agent.eligibility_store import confirm_eligibility, record_eligibility
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
    built = build_bank(db, monkeypatch)
    # WHICH ROWS COUNT — PIN 7's governed fact. STORED, because revalidation re-reads it from the
    # store: a suite that only ever passed a policy object in would seal a pin nothing could
    # revalidate, which is the shape of the defect the pin closes.
    record_eligibility(built, catalog_source=SRC, table=TRANSACTION_TABLE, policy=_eligibility(),
                       proposed_by="user:priya")
    return built


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


def _rewrite_stored_seal(bank, record, mutate) -> dict:
    """THE REVIEWER'S PROBE. Rewrite a sealed plan's stored bytes and RE-DERIVE both of the store's
    own guards, so an attacker with a database write passes them.

    Spelled out from the store's public constants rather than by importing its private helper: the
    point of the probe is that nothing here needs privileged knowledge. Both guards
    (``output_content_hash`` and the ``sres_`` identity) are functions of the OUTPUT alone, so both
    move with it — and the one thing that does NOT is ``input_content_hash``, which stays the
    original plan identity the caller executes by.
    """
    import copy

    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.structured_results import STRUCTURED_RESULT_STORE_VERSION

    row = bank.execute(
        "SELECT structured_result_id, result_version, output_json FROM structured_result "
        "WHERE result_type = %s AND input_content_hash = %s",
        (ANALYSIS_PLAN_CONTRACT, record.plan_hash)).fetchone()
    old_id, version, output = row[0], int(row[1]), copy.deepcopy(row[2])
    mutate(output)
    output_hash = canonical_hash(output)
    new_id = "sres_" + canonical_hash({
        "store_version": STRUCTURED_RESULT_STORE_VERSION,
        "result_type": ANALYSIS_PLAN_CONTRACT,
        "result_version": version,
        "input_content_hash": record.plan_hash,
        "output_content_hash": output_hash,
    })
    bank.execute("DELETE FROM structured_result_current WHERE structured_result_id = %s", (old_id,))
    bank.execute("DELETE FROM structured_result_provenance WHERE structured_result_id = %s",
                 (old_id,))
    bank.execute("DELETE FROM structured_result WHERE structured_result_id = %s", (old_id,))
    bank.execute(
        "INSERT INTO structured_result (structured_result_id, result_type, result_version, "
        "  input_content_hash, output_content_hash, output_json) VALUES (%s,%s,%s,%s,%s,%s)",
        (new_id, ANALYSIS_PLAN_CONTRACT, version, record.plan_hash, output_hash, Jsonb(output)))
    return output


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


# ── the computation half is verified against the plan identity ON READ ───────────────────────────
#
# The store's own two guards are functions of the OUTPUT alone: `output_content_hash` and the
# `sres_` identity both move when the bytes are rewritten, so both can be re-derived by anyone who
# can write the row. The one thing that CANNOT be re-derived is the plan identity the caller
# executes by — `input_content_hash` IS `AnalysisExecutionIRV2.plan_hash`, and the seal is a fixed
# point over its own content. Checking it on read is what makes the fixed point load-bearing rather
# than a remark in a docstring.


def test_a_seal_whose_COMPUTATION_was_rewritten_refuses_to_replay(bank):
    """The probe. `included_status_values` is "which rows count" — rewriting POSTED to ALL under the
    original plan identity makes a sealed plan answer a different question with a clean audit trail
    saying a person approved this one."""
    from featuregen.overlay.upload.structured_results import StructuredResultCorruption

    record = _sealed(bank)
    _rewrite_stored_seal(bank, record, lambda out: out["computation"]["eligibility"].__setitem__(
        "included_status_values", ["POSTED", "PENDING", "REVERSED"]))

    with pytest.raises(StructuredResultCorruption) as exc:
        replay_sealed_plan(bank, record.plan_hash)
    # BOTH hashes, because "the plan is corrupt" sends the reader nowhere: one is the identity it is
    # filed under, the other is what its bytes actually say.
    assert record.plan_hash in str(exc.value)


def test_a_seal_whose_DECISION_REFS_were_rewritten_refuses_to_replay(bank):
    """The governance half is covered by the same one check — a rewritten binding revision, serving
    policy or dataset ref is the same class of substitution as a rewritten computation."""
    from featuregen.overlay.upload.structured_results import StructuredResultCorruption

    record = _sealed(bank)
    _rewrite_stored_seal(bank, record, lambda out: out["decisions"]["sources"][0].__setitem__(
        "binding_revision_id", "pbr_" + "0" * 64))

    with pytest.raises(StructuredResultCorruption):
        replay_sealed_plan(bank, record.plan_hash)


def test_the_CURRENT_POINTER_reads_a_rewritten_seal_no_more_freely_than_replay_does(bank):
    """Same check on every read path. A pointer that returned what replay refuses would be the
    identity check with a way around it."""
    from featuregen.overlay.upload.structured_results import StructuredResultCorruption

    record = _sealed(bank)
    _rewrite_stored_seal(bank, record, lambda out: out["computation"].__setitem__(
        "measure", "sum"))
    # The rewrite drops the pointer with the row it referenced; re-file it at the tampered revision,
    # which is what an attacker who wanted the question's CURRENT plan poisoned would do.
    tampered_id = bank.execute(
        "SELECT structured_result_id FROM structured_result WHERE input_content_hash = %s",
        (record.plan_hash,)).fetchone()[0]
    bank.execute(
        "INSERT INTO structured_result_current (subject_kind, subject_ref, result_type, "
        "  structured_result_id) VALUES (%s,%s,%s,%s)",
        (SEALED_PLAN_SUBJECT_KIND, analysis_question_ref(QUESTION), ANALYSIS_PLAN_CONTRACT,
         tampered_id))

    with pytest.raises(StructuredResultCorruption):
        current_sealed_plan(bank, QUESTION)


def test_an_UNTAMPERED_seal_replays_through_the_identity_check_unchanged(bank):
    """The check must not be a hash function that only ever refuses: a seal round-tripped through
    JSONB reproduces its own identity exactly."""
    record = _sealed(bank)
    assert replay_sealed_plan(bank, record.plan_hash) == record
    assert current_sealed_plan(bank, QUESTION) == record


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


# ── PIN 7: which rows COUNT ──────────────────────────────────────────────────────────────────────
#
# The pin the review found missing. Eligibility was consumed FROM THE SEAL at execution and never
# re-read, so a re-declared eligibility policy left every already-sealed plan answering under the
# old definition — while a re-declared TEMPORAL policy correctly refused. Both are judgements a
# person makes about the bank's data and both change the number rather than widening it.


def test_the_seal_carries_the_SEVENTH_pin(bank):
    from featuregen.analysis.sealed_plan import eligibility_policy_hash

    record = _sealed(bank)
    assert record.eligibility is not None
    assert record.eligibility.dataset_ref == TRAN
    assert record.eligibility.policy_hash == eligibility_policy_hash(_eligibility())


def test_drift_ELIGIBILITY_a_re_declared_definition_of_counts_refuses_by_name(bank):
    """Someone decides PENDING transactions are activity after all. Every already-sealed plan would
    go on answering under the old definition with a clean audit trail — the exact asymmetry with the
    temporal policy this pin closes."""
    record = _sealed(bank)
    record_eligibility(
        bank, catalog_source=SRC, table=TRANSACTION_TABLE,
        policy=_eligibility(included_status_values=("POSTED", "PENDING")),
        proposed_by="user:sam")

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_ELIGIBILITY
    assert refusal.subject_refs == (TRAN,)
    assert refusal.sealed_value != refusal.current_value
    assert refusal.current_value


def test_drift_ELIGIBILITY_a_policy_that_VANISHED_refuses_too(bank):
    """The plan was sealed where a person had said which rows count, and now nobody has. That is
    drift, not an absence to default through."""
    record = _sealed(bank)
    bank.execute("DELETE FROM eligibility_policy WHERE catalog_source = %s", (SRC,))

    refusal = revalidate_sealed_plan(bank, record)
    assert refusal is not None
    assert refusal.code == SEALED_PLAN_STALE_ELIGIBILITY
    assert refusal.current_value == ""


def test_CONFIRMING_the_same_eligibility_policy_is_not_drift(bank):
    """A human agreeing to the proposal the plan was sealed under changes the AUTHORITY, never
    which rows count. Refusing there would punish exactly the act the disclosure asks for."""
    record = _sealed(bank)
    confirm_eligibility(bank, catalog_source=SRC, table=TRANSACTION_TABLE, actor="user:priya",
                        now=datetime.now(UTC))
    assert revalidate_sealed_plan(bank, record) is None


def test_a_plan_that_pins_NO_eligibility_decision_may_not_be_replayed(bank):
    """The pin is not optional. A record without one was sealed by a build that did not have it,
    and running it would skip pin 7 in silence — the inert-mechanism failure the pin closes."""
    from dataclasses import replace

    record = _sealed(bank)
    unpinned = replace(record, decisions=replace(record.decisions, eligibility=None))
    with pytest.raises(SealedPlanError, match="pins no eligibility decision"):
        revalidate_sealed_plan(bank, unpinned)


def test_a_V2_cannot_be_built_without_the_eligibility_pin(bank):
    from featuregen.analysis.sealed_plan import AnalysisExecutionIRV2

    grounded = _grounded(bank)
    with pytest.raises(SealedPlanError, match="WHICH ROWS COUNT"):
        AnalysisExecutionIRV2(execution_ir=_v2(bank, grounded).execution_ir,
                              decisions=seal_refs_from_selections(grounded.selections))


def test_the_eligibility_ANCHOR_enumerates_the_same_facts_the_IR_identity_does(bank):
    """The drift guard between the two enumerations. A field added to the policy and folded into
    the IR's identity but forgotten in the pin would make a re-declaration read as UNCHANGED — the
    silent case this whole task exists to prevent."""
    from featuregen.analysis.sealed_plan import eligibility_policy_hash

    ir = _v2(bank).execution_ir
    anchor_facts = {"status_column", "included_status_values", "reversal_mode", "reversal_column",
                    "non_reversed_values", "null_behavior"}
    assert set(ir.identity_payload()["eligibility"]) == anchor_facts
    # And every one of them MOVES the anchor.
    base = eligibility_policy_hash(_eligibility())
    for over in ({"status_column": "posting_status"},
                 {"included_status_values": ("POSTED", "SETTLED")},
                 {"reversal_column": "rev_flag"},
                 {"non_reversed_values": ("NO",)},
                 {"null_behavior": NullBehavior.INCLUDE}):
        assert eligibility_policy_hash(_eligibility(**over)) != base, over


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
