"""Step 7 — the production boundary: machines behind unavailable actions, honest from day one.

The case worth reading first is the swap: `publish_swap` is CAS on the pointer's fence, so a
zombie loses INSIDE the statement — the property that lets publication's machine omit
UNKNOWN_OUTCOME entirely (the spec's §1.0 design move).

Unique ids per test (the test_retirement_scope lesson).
"""
from __future__ import annotations

import pytest

from featuregen.materialize.method_certificates import (
    current_method_certificate,
    member_certificate_facts,
)
from featuregen.overlay.upload.production_attempt_store import (
    InvalidProductionMove,
    MaterializationStatusV1,
    ProductionMovedUnderneath,
    PublicationStatusV1,
    advance_materialization,
    advance_publication,
    current_active_revision,
    publish_swap,
    record_materialization_attempt,
    record_output_revision,
    record_publication_attempt,
)

ENV = "hdfc-local"


def _decision(conn, tag: str) -> str:
    """A real decision row for the attempt FK — through the service, not hand-rolled."""
    from featuregen.materialize.action_authorization import ActionV1, authorize_action
    from featuregen.materialize.action_decision import ActionRequestV1, decide

    authorization = authorize_action(
        conn, action=ActionV1.EXECUTE_SANDBOX, resource_identity_hash=f"res-{tag}",
        actor_subject="user:sam", environment_id=ENV)
    decision_id, _ = decide(conn, ActionRequestV1(
        action=ActionV1.EXECUTE_SANDBOX, resource_identity_hash=f"res-{tag}"),
        authorization_id=authorization.authorization_id)
    return decision_id


def _attempt(conn, tag: str) -> str:
    attempt_id, created = record_materialization_attempt(
        conn, attempt_id=f"pma-{tag}", sealed_artifact_id=f"art-{tag}", environment_id=ENV,
        logical_group_name=f"grp-{tag}", action_decision_revision_id=_decision(conn, tag),
        requested_by="user:sam", requested_at="2026-08-23T00:00:00Z")
    assert created
    return attempt_id


# ══ the materialization machine ═════════════════════════════════════════════════════════════════
def test_ONE_LIVE_ATTEMPT_PER_TARGET_and_terminals_release_the_slot(db):
    first = _attempt(db, "live")
    duplicate, created = record_materialization_attempt(
        db, attempt_id="pma-live-2", sealed_artifact_id="art-live", environment_id=ENV,
        logical_group_name="grp-live", action_decision_revision_id=_decision(db, "live2"),
        requested_by="user:sam", requested_at="2026-08-23T00:01:00Z")
    assert (duplicate, created) == (first, False)

    advance_materialization(db, first, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CANCELLED)
    retry, created = record_materialization_attempt(
        db, attempt_id="pma-live-3", sealed_artifact_id="art-live", environment_id=ENV,
        logical_group_name="grp-live", action_decision_revision_id=_decision(db, "live3"),
        requested_by="user:sam", requested_at="2026-08-23T00:02:00Z")
    assert (retry, created) == ("pma-live-3", True)


def test_UNKNOWN_OUTCOME_is_first_class_and_never_guessed_out_of(db):
    """The state crash recovery FINDS. Legal exits: RUNNING (still going), STAGED (proven),
    FAILED (the cluster said so) — and NOTHING else."""
    attempt = _attempt(db, "unk")
    advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CLAIMED)
    advance_materialization(db, attempt, MaterializationStatusV1.CLAIMED,
                            MaterializationStatusV1.RUNNING,
                            external_operation_id="fgm:pma-unk:1")
    advance_materialization(db, attempt, MaterializationStatusV1.RUNNING,
                            MaterializationStatusV1.UNKNOWN_OUTCOME)
    with pytest.raises(InvalidProductionMove):
        advance_materialization(db, attempt, MaterializationStatusV1.UNKNOWN_OUTCOME,
                                MaterializationStatusV1.SUCCEEDED)


def test_the_external_operation_identity_lands_ON_the_move_into_submit(db):
    """Stored WITH the transition, because an identity recorded after the crash window is no use
    inside it — the reconciler's question to the cluster is keyed on this."""
    attempt = _attempt(db, "ext")
    advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CLAIMED)
    advance_materialization(db, attempt, MaterializationStatusV1.CLAIMED,
                            MaterializationStatusV1.RUNNING,
                            external_operation_id="fgm:pma-ext:1",
                            staging_path="staging/pma-ext/1/")
    row = db.execute(
        "SELECT external_operation_id, staging_path "
        "FROM production_materialization_attempt WHERE attempt_id = %s", (attempt,)).fetchone()
    assert row == ("fgm:pma-ext:1", "staging/pma-ext/1/")


def test_advances_are_compare_and_set(db):
    attempt = _attempt(db, "cas")
    advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CLAIMED)
    with pytest.raises(ProductionMovedUnderneath):
        advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                                MaterializationStatusV1.CLAIMED)


def test_the_output_identity_is_content_addressed_and_once_per_attempt(db):
    attempt = _attempt(db, "out")
    first = record_output_revision(db, attempt_id=attempt,
                                   manifest={"files": ["a.parquet"]}, row_count=10)
    second = record_output_revision(db, attempt_id=attempt,
                                    manifest={"files": ["a.parquet"]}, row_count=10)
    assert first == second
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM materialized_output_revision WHERE attempt_id = %s", (attempt,))


# ══ the publication machine and THE SWAP ════════════════════════════════════════════════════════
def _published_ready(db, tag: str) -> tuple[str, str]:
    attempt = _attempt(db, tag)
    for move in ((MaterializationStatusV1.REQUESTED, MaterializationStatusV1.CLAIMED),
                 (MaterializationStatusV1.CLAIMED, MaterializationStatusV1.RUNNING),
                 (MaterializationStatusV1.RUNNING, MaterializationStatusV1.STAGED),
                 (MaterializationStatusV1.STAGED, MaterializationStatusV1.SUCCEEDED)):
        advance_materialization(db, attempt, *move,
                                staging_path=f"staging/{attempt}/1/"
                                if move[1] is MaterializationStatusV1.STAGED else None)
    output = record_output_revision(db, attempt_id=attempt,
                                    manifest={"t": tag}, row_count=5)
    return attempt, output


def test_PUBLICATION_BINDS_TO_THE_EXACT_OUTPUT_by_composite_FK(db):
    """§9.1's forgery rule as schema: a publication naming an output the materialization did not
    produce is UNREPRESENTABLE — asserted against the database, not the writer."""
    materialization, _output = _published_ready(db, "forge")
    with pytest.raises(Exception, match="production_publication_publishes_that_output"):
        db.execute(
            "INSERT INTO production_publication_attempt (attempt_id, "
            "materialization_attempt_id, output_revision_id, environment_id, "
            "logical_group_name, action_decision_revision_id, requested_by, requested_at) "
            "VALUES ('ppa-forged', %s, 'out-somebody-elses', %s, 'grp-forge', %s, 'user:mal', "
            "'2026-08-23T00:00:00Z')",
            (materialization, ENV, _decision(db, "forge2")))


def test_THE_SWAP_IS_CAS_a_zombies_stale_fence_loses_inside_the_statement(db):
    materialization, output = _published_ready(db, "swap")
    attempt_id, _ = record_publication_attempt(
        db, attempt_id="ppa-swap", materialization_attempt_id=materialization,
        output_revision_id=output, environment_id=ENV, logical_group_name="grp-swap",
        action_decision_revision_id=_decision(db, "swap3"), requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")

    assert publish_swap(db, attempt_id=attempt_id, environment_id=ENV,
                        logical_group_name="grp-swap", output_revision_id=output,
                        fence=5) is True
    # The zombie: an older claim's fence arrives late. It LOSES, and the pointer stands.
    assert publish_swap(db, attempt_id=attempt_id, environment_id=ENV,
                        logical_group_name="grp-swap", output_revision_id=output,
                        fence=3) is False
    active = current_active_revision(db, environment_id=ENV, logical_group_name="grp-swap")
    assert active["fence"] == 5
    assert active["output_revision_id"] == output


def test_what_is_actually_out_there_is_one_read_and_honestly_absent(db):
    assert current_active_revision(db, environment_id=ENV,
                                   logical_group_name="grp-nothing") is None


def test_publication_machine_moves_are_closed(db):
    materialization, output = _published_ready(db, "pmach")
    attempt_id, _ = record_publication_attempt(
        db, attempt_id="ppa-pmach", materialization_attempt_id=materialization,
        output_revision_id=output, environment_id=ENV, logical_group_name="grp-pmach",
        action_decision_revision_id=_decision(db, "pmach2"), requested_by="user:sam",
        requested_at="2026-08-23T00:00:00Z")
    with pytest.raises(InvalidProductionMove):
        advance_publication(db, attempt_id, PublicationStatusV1.REQUESTED,
                            PublicationStatusV1.PUBLISHED)
    advance_publication(db, attempt_id, PublicationStatusV1.REQUESTED,
                        PublicationStatusV1.CLAIMED)
    advance_publication(db, attempt_id, PublicationStatusV1.CLAIMED,
                        PublicationStatusV1.PUBLISHED)


# ══ the certificate reader — honest None, honest per-member facts ═══════════════════════════════
def _sealed_member(db, tag: str, *, with_identity: bool) -> str:
    from tests.featuregen.overlay.upload.test_build_set_store import _approval, _set

    artifact = f"art-{tag}"
    build_set, _ = _set(db, revision_id=f"bs-{tag}", members=(f"sel-{tag}",),
                        target=f"trr-{tag}")
    approval = _approval(db, build_set, ENV)
    db.execute(
        "INSERT INTO sealed_artifact_v2 (artifact_id, generation_authorization_revision_id, "
        "environment_id, logical_group_name, compilation_identity_hash, group_plan_hash, "
        "project_digest, subgraph_satisfied, triggered_requirements, subgraph_findings, "
        "sealed_at) VALUES (%s, %s, %s, 'customer_txn_features', 'c', 'g', 'sha256:d', true, "
        "'[]'::jsonb, '[]'::jsonb, 't')", (artifact, approval, ENV))
    db.execute(
        "INSERT INTO formula_authoring_run (authoring_run_id, intent_hash, versions, actor) "
        "VALUES (%s, 'ih', '{}'::jsonb, '{}'::jsonb) ON CONFLICT DO NOTHING",
        (f"far-{tag}",))
    db.execute(
        "INSERT INTO sealed_artifact_member_provenance (artifact_id, member_name, "
        "selection_revision_id, formula_draft_id, authoring_run_id, formula_content_hash, "
        "authoring_method, authoring_evidence_hash) "
        "VALUES (%s, 'm1', %s, NULL, %s, 'sha256:f', 'LLM_AUTHORED', %s)",
        (artifact, f"sel-{tag}", f"far-{tag}", "e" * 64))
    if with_identity:
        db.execute(
            "INSERT INTO sealed_artifact_member_method_identity (artifact_id, member_name, "
            "authoring_method, method_identity_hash, method_identity_json) "
            "VALUES (%s, 'm1', 'LLM_AUTHORED', %s, '{}'::jsonb)",
            (artifact, f"mih-{tag}"))
    return artifact


def test_a_member_with_no_identity_row_is_UNRECORDED_never_backfilled(db):
    artifact = _sealed_member(db, "unrec", with_identity=False)
    facts = member_certificate_facts(db, sealed_artifact_id=artifact)
    assert facts["m1"]["blockers"] == ("METHOD_IDENTITY_UNRECORDED",)


def test_an_identity_with_no_certificate_is_MISSING_the_day_one_hard_block(db):
    artifact = _sealed_member(db, "miss", with_identity=True)
    facts = member_certificate_facts(db, sealed_artifact_id=artifact)
    assert facts["m1"]["blockers"] == ("METHOD_CERTIFICATE_MISSING",)
    assert current_method_certificate(
        db, certificate_kind="AUTHORING_METHOD", subject_identity_hash="mih-miss") is None


def test_a_CERTIFIED_exact_subject_clears_the_member(db):
    artifact = _sealed_member(db, "cert", with_identity=True)
    db.execute(
        "INSERT INTO method_certificate_revision (certificate_revision_id, certificate_kind, "
        "subject_identity_kind, subject_identity_hash, contract_hash, corpus_hash, outcome) "
        "VALUES ('mcr-1', 'AUTHORING_METHOD', 'AUTHORING_METHOD', 'mih-cert', 'ch', 'co', "
        "'CERTIFIED')")
    facts = member_certificate_facts(db, sealed_artifact_id=artifact)
    assert facts["m1"]["blockers"] == ()
    assert facts["m1"]["certificate"].certificate_revision_id == "mcr-1"


def test_the_kind_and_subject_must_agree_by_CHECK(db):
    with pytest.raises(Exception, match="method_certificate_kind_agrees"):
        db.execute(
            "INSERT INTO method_certificate_revision (certificate_revision_id, "
            "certificate_kind, subject_identity_kind, subject_identity_hash, contract_hash, "
            "corpus_hash, outcome) VALUES ('mcr-bad', 'AUTHORING_METHOD', 'EXECUTION_STACK', "
            "'h', 'c', 'c', 'CERTIFIED')")


# ══ the reconcilers — shipped WITH the machines ═════════════════════════════════════════════════
def test_with_no_cluster_adapter_unknown_attempts_are_HELD_never_guessed(db):
    from featuregen.materialize.production_reconcile import reconcile_unknown_materializations

    attempt = _attempt(db, "recon")
    advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CLAIMED)
    advance_materialization(db, attempt, MaterializationStatusV1.CLAIMED,
                            MaterializationStatusV1.RUNNING, external_operation_id="fgm:x:1")
    advance_materialization(db, attempt, MaterializationStatusV1.RUNNING,
                            MaterializationStatusV1.UNKNOWN_OUTCOME)

    tallies = reconcile_unknown_materializations(db)
    assert tallies == {"held": 1, "resumed_running": 0, "staged": 0, "failed": 0}
    row = db.execute("SELECT status FROM production_materialization_attempt "
                     "WHERE attempt_id = %s", (attempt,)).fetchone()
    assert row == ("UNKNOWN_OUTCOME",)


def test_the_cluster_answer_resolves_unknown_and_quarantines_on_failure(db):
    from featuregen.materialize.production_reconcile import reconcile_unknown_materializations

    attempt = _attempt(db, "reconf")
    advance_materialization(db, attempt, MaterializationStatusV1.REQUESTED,
                            MaterializationStatusV1.CLAIMED)
    advance_materialization(db, attempt, MaterializationStatusV1.CLAIMED,
                            MaterializationStatusV1.RUNNING,
                            external_operation_id="fgm:reconf:1",
                            staging_path="staging/reconf/1/")
    advance_materialization(db, attempt, MaterializationStatusV1.RUNNING,
                            MaterializationStatusV1.UNKNOWN_OUTCOME)

    tallies = reconcile_unknown_materializations(db, cluster_status=lambda op: "failed")
    assert tallies["failed"] == 1
    row = db.execute(
        "SELECT status, quarantine_path FROM production_materialization_attempt "
        "WHERE attempt_id = %s", (attempt,)).fetchone()
    assert row == ("FAILED", "staging/reconf/1/")


def test_the_pointer_invariant_sweep_reports_and_never_repairs(db):
    from featuregen.materialize.production_reconcile import (
        sweep_publication_pointer_invariant,
    )

    assert sweep_publication_pointer_invariant(db) == {"orphaned_pointers": 0}
