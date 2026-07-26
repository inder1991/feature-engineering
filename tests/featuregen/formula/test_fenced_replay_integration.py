from __future__ import annotations

import psycopg
from tests.featuregen.formula.factories import default_output
from tests.featuregen.formula.test_parse import raw_unary_proposal

from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.authoring import run_authoring
from featuregen.formula.control import LeaseFence
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import FakeLLM, FakeResponse


def test_fenced_author_critic_trace_is_replayable_without_provider_reissue(
    db, monkeypatch, _dsn
) -> None:
    run_id = "far_fenced_replay_integration"
    message_id = "formula-fenced-replay-integration"
    with psycopg.connect(_dsn, autocommit=True) as setup:
        queue_id = setup.execute(
            "INSERT INTO queue "
            "(message_id,partition_key,handler,payload,status,lease_owner,"
            "lease_expires_at,lease_fence) "
            "VALUES (%s,%s,'recipe_formula_shadow.author.v1','{}'::jsonb,"
            "'leased','worker-fenced',now() + interval '10 minutes',3) RETURNING id",
            (message_id, message_id),
        ).fetchone()[0]
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    monkeypatch.setattr(
        "featuregen.formula.authoring.resolve_formula_output_policy",
        lambda *args, **kwargs: default_output(),
    )
    client = FakeLLM(script={
        AUTHOR_TASK: FakeResponse(output={
            "turn_type": "final_proposal",
            "final_proposal": raw_unary_proposal(),
        }),
        CRITIC_TASK: FakeResponse(output={"findings": []}),
    })
    intent = AuthoringIntent("spend", "customer spend", "customer")
    fence = LeaseFence(queue_id, "worker-fenced", 3)
    try:
        result = run_authoring(
            db,
            intent,
            client,
            client,
            actor=None,
            authoring_run_id=run_id,
            lease_fence=fence,
            facts_reader=lambda _proposal: ({}, {}),
            critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
            progress_callback=lambda: None,
        )
        assert result.authoring_disposition == "NEEDS_REVIEW"
        with psycopg.connect(_dsn) as check:
            assert check.execute(
                "SELECT count(*),min(queue_id),min(lease_owner),min(lease_fence) "
                "FROM llm_dispatch WHERE authoring_run_id=%s",
                (run_id,),
            ).fetchone() == (2, queue_id, "worker-fenced", 3)
            assert check.execute(
                "SELECT stage FROM formula_authoring_trace_event "
                "WHERE authoring_run_id=%s ORDER BY seq",
                (run_id,),
            ).fetchall() == [
                ("AUTHOR_TURN_0",),
                ("AUTHOR_PROPOSAL_PARSED",),
                ("EXPECTATION_VALIDATED",),
                ("CRITIC_COMPLETED",),
                ("OUTPUT_POLICY_RESOLVED",),
                ("TERMINAL",),
            ]

        replay_client = FakeLLM(script={})
        replayed = run_authoring(
            db,
            intent,
            replay_client,
            replay_client,
            actor=None,
            authoring_run_id=run_id,
            lease_fence=fence,
            facts_reader=lambda _proposal: ({}, {}),
            critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
            progress_callback=lambda: None,
        )
        assert replayed.authoring_disposition == result.authoring_disposition
        assert replayed.candidate_formula_hash == result.candidate_formula_hash
    finally:
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            cleanup.execute(
                "ALTER TABLE formula_authoring_trace_event "
                "DISABLE TRIGGER formula_authoring_event_no_mutation")
            cleanup.execute(
                "ALTER TABLE formula_authoring_run "
                "DISABLE TRIGGER formula_authoring_run_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch_subject "
                "DISABLE TRIGGER llm_dispatch_subject_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch_outcome "
                "DISABLE TRIGGER llm_dispatch_outcome_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch DISABLE TRIGGER llm_dispatch_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
            dispatches = cleanup.execute(
                "SELECT dispatch_ref FROM llm_dispatch WHERE authoring_run_id=%s",
                (run_id,),
            ).fetchall()
            refs = [row[0] for row in dispatches]
            llm_refs = [
                row[0]
                for row in cleanup.execute(
                    "SELECT llm_call_ref FROM llm_call WHERE run_id=%s",
                    (run_id,),
                ).fetchall()
            ]
            if refs:
                cleanup.execute(
                    "DELETE FROM llm_dispatch_subject WHERE dispatch_ref=ANY(%s)",
                    (refs,),
                )
                cleanup.execute(
                    "DELETE FROM llm_dispatch_outcome WHERE dispatch_ref=ANY(%s)",
                    (refs,),
                )
                cleanup.execute(
                    "DELETE FROM llm_call_dispatch WHERE dispatch_ref=ANY(%s)",
                    (refs,),
                )
                cleanup.execute(
                    "DELETE FROM llm_dispatch WHERE dispatch_ref=ANY(%s)",
                    (refs,),
                )
            cleanup.execute(
                "DELETE FROM formula_authoring_trace_event WHERE authoring_run_id=%s",
                (run_id,),
            )
            if llm_refs:
                cleanup.execute(
                    "DELETE FROM llm_call WHERE llm_call_ref=ANY(%s)",
                    (llm_refs,),
                )
            cleanup.execute(
                "DELETE FROM formula_authoring_run WHERE authoring_run_id=%s",
                (run_id,),
            )
            cleanup.execute("DELETE FROM queue WHERE id=%s", (queue_id,))
            cleanup.execute(
                "ALTER TABLE formula_authoring_trace_event "
                "ENABLE TRIGGER formula_authoring_event_no_mutation")
            cleanup.execute(
                "ALTER TABLE formula_authoring_run "
                "ENABLE TRIGGER formula_authoring_run_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch_subject "
                "ENABLE TRIGGER llm_dispatch_subject_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch_outcome "
                "ENABLE TRIGGER llm_dispatch_outcome_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_dispatch ENABLE TRIGGER llm_dispatch_no_mutation")
            cleanup.execute(
                "ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
