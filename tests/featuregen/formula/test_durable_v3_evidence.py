"""RELEASE GATE — a genuine, replayable, provider-authored V3 run qualifies as V3 evidence.

Everything else about `qualifies_as_v3_evidence_for_run` is proved by refusals. This is the one
test that proves the intersection: a run with REAL provider calls under the v3 contract AND a trace
`load_verified_checkpoint` replays. Asserting each half separately cannot establish it, and until it
existed the evaluator had no basis to treat the qualifier as an activation authority.
"""
from __future__ import annotations

import pytest
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
)
from tests.featuregen.formula.durable_evidence import durable_v3_run
from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT, _raw_v3

from featuregen.formula.authoring_versions import qualifies_as_v3_evidence_for_run
from featuregen.overlay.upload.dispatch_audit import (
    formula_dispatch_reconciliation_failure,
    formula_dispatches_reconciled,
)

_REFS = (TABLE_REF, REF_AMT, REF_DT, REF_CIF)


def test_a_REAL_PROVIDER_AUTHORED_V3_RUN_QUALIFIES(db, monkeypatch, _dsn):
    """▲ THE GATE. `(True, ())` — no disagreeing axis, no unaudited turn, no unreplayable trace.

    The provider is a fake, which is the honest scope: this proves the DISPATCH AUDIT and the
    REPLAY are real, not that Anthropic was reached. What the qualifier reads is genuine — the
    `llm_call` rows carry the task, prompt and schema the v3 contract selected, and the dispatch
    chain reconciles, so the trace replays rather than being refused.
    """
    import psycopg

    with durable_v3_run(_dsn, monkeypatch, raw=_raw_v3(), intent=_INTENT,
                        allowed_refs=_REFS, facts_ref=REF_AMT) as (run_id, result):
        assert result.authoring_disposition == "READY_FOR_OUTPUT_BINDING", result

        with psycopg.connect(_dsn) as check:
            # The precondition the ordinary suite cannot meet, asserted rather than assumed.
            assert formula_dispatches_reconciled(check, run_id), (
                formula_dispatch_reconciliation_failure(check, run_id))

            assert qualifies_as_v3_evidence_for_run(check, run_id) == (True, ())


def test_the_QUALIFIER_READS_THE_REPLAYED_PROPOSAL_not_the_raw_row(db, monkeypatch, _dsn):
    """The proposal it certifies comes through `load_verified_checkpoint`, so payload hashes, stage
    ordering and dispatch lineage were all re-derived on the way to it. Reading the terminal row
    directly would certify bytes nothing had validated — the distinction is invisible while the two
    agree, which is why it is asserted where they demonstrably do."""
    import psycopg

    from featuregen.formula.authoring_versions import _verified_proposal

    with durable_v3_run(_dsn, monkeypatch, raw=_raw_v3(), intent=_INTENT,
                        allowed_refs=_REFS, facts_ref=REF_AMT) as (run_id, _result):
        with psycopg.connect(_dsn) as check:
            problems: list[str] = []
            proposal = _verified_proposal(check, run_id, problems)

            assert problems == [], problems
            assert proposal is not None, "the replay produced no proposal"
            assert proposal["formula_schema_version"] == 3


# ══ GATE 1 IS STILL OPEN — and here is the ACTUAL obstacle ════════════════════════════════════
#
# The reviewed design is right: two arms driven by the IDENTICAL multi-turn script, so the tool
# result is the same in both and RETIREMENT is the only variable, measured in AUDITED AUTHOR CALLS
# rather than in the final disposition. That removes the coupling that sank the first attempt.
#
# ▲ CORRECTION TO A WRONG DIAGNOSIS I LEFT HERE. I recorded that the eight author calls came from a
# malformed tool-argument shape and that the next implementer should fix it. **That was wrong** —
# `{"tool_name": "get_column_metadata", "arguments": {"logical_ref": ...}}` already matches what
# `recipe_authoring.py:316` expects. Do not spend time on it.
#
# The real cause is `FakeLLM.call` (`intake/llm.py:175`): the response POSITION is tracked in
# `self._calls` keyed on `(task, prompt_id, input_hash)`. A tool result changes the next turn's
# inputs, so the hash changes, `idx` resets to 0, and the task-key fallback sequence hands back
# response ZERO — the tool call — again. The loop therefore repeats the tool turn until `max_turns`,
# which is where eight comes from. Nothing about the run or the tool is malformed.
#
# So the script must be keyed by INPUT HASH rather than by task, the way the existing multi-turn
# author tests do it:
#   * first input hash                              -> the tool response
#   * the input hash INCLUDING the canonical tool trail -> the final proposal
# Assert the control makes exactly TWO audited author calls before writing the retirement arm, then
# run the identical script with the retirement committed after call one and assert `DraftRetired`
# plus exactly ONE audited call.
#
# Everything else about retirement is proved: `advance` refuses a retired draft under BOTH
# concurrency orderings, the worker returns `retired` with ZERO provider calls and completes its
# queue item, and `_sync_from_trace` re-raises rather than swallowing. The unproved claim is
# narrowly the ORDERING — that the raise reaches the loop between turns.
#
# Confirmed by review: the V3 early return before the post-authoring callback
# (`replay_authoring_v2.py:932` vs `935`) is a progress-REPORTING gap only. After the critic, V3
# performs local derivation and persistence, so no additional provider spend rides on it.


# ══ THE CLEANUP IS ATOMIC, AND PROVED WITHOUT CREATING ANY EVIDENCE ═══════════════════════════
def test_a_FAILED_CLEANUP_ROLLS_BACK_ENTIRELY(db, monkeypatch, _dsn):
    """▲ THE PROPERTY THAT MATTERS MORE THAN THE ROWS: a cleanup that fails must leave production's
    append-only guards ENABLED.

    An earlier `_erase` disabled them on an AUTOCOMMIT connection before entering its `try`, so each
    `ALTER TABLE ... DISABLE TRIGGER` committed on the spot and became visible everywhere — and a
    failure part-way through either loop left a durable table's tamper-evidence off with nothing to
    restore it. A test fixture able to silently disarm production's guards is worse than the
    leftover rows it exists to avoid.

    ▲ AND IT PROVES THAT WITHOUT CREATING ANY AUTHORING EVIDENCE. My first attempt at this test
    authored real runs and deliberately failed their cleanup — which by construction left that
    evidence behind, and leaked into ELEVEN unrelated tests via queue backlog counts and provider
    counters. `_erase` deletes by run id and queue id, so a NONEXISTENT run id matches nothing while
    a disposable queue row gives the transaction something real to roll back. Same code path, same
    guarantee, nothing appended.
    """
    import psycopg
    from tests.featuregen.formula import durable_evidence
    from tests.featuregen.formula.durable_evidence import unique

    triggers = [trigger for _table, trigger in durable_evidence._APPEND_ONLY]

    def _states(conn) -> dict[str, str]:
        return {name: enabled for name, enabled in conn.execute(
            "SELECT t.tgname, t.tgenabled FROM pg_trigger t WHERE t.tgname = ANY(%s)",
            (triggers,)).fetchall()}

    message_id = unique("erase-rollback")
    with psycopg.connect(_dsn, autocommit=True) as setup:
        queue_id = setup.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'formula_draft.author.v1', '{}'::jsonb) RETURNING id",
            (message_id, message_id)).fetchone()[0]
        before = _states(setup)

    assert before and all(state != "D" for state in before.values()), before

    monkeypatch.setattr(durable_evidence, "_erase_hook",
                        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed mid-flight")))
    try:
        with pytest.raises(RuntimeError, match="mid-flight"):
            durable_evidence._erase(_dsn, unique("far-does-not-exist"), queue_id)

        with psycopg.connect(_dsn) as check:
            assert _states(check) == before, "append-only guards changed after a FAILED cleanup"
            assert check.execute(
                "SELECT count(*) FROM queue WHERE id = %s", (queue_id,)).fetchone()[0] == 1, (
                "the delete inside the failed transaction was not rolled back")
    finally:
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            cleanup.execute("DELETE FROM queue WHERE id = %s", (queue_id,))


def test_a_SUCCEEDING_CLEANUP_LEAVES_THE_GUARDS_ENABLED_TOO(db, monkeypatch, _dsn):
    """The control. Without it the test above would pass for an `_erase` that never disabled
    anything — which would silently stop cleaning up the append-only tables it exists for."""
    import psycopg
    from tests.featuregen.formula import durable_evidence
    from tests.featuregen.formula.durable_evidence import unique

    triggers = [trigger for _table, trigger in durable_evidence._APPEND_ONLY]
    message_id = unique("erase-ok")

    with psycopg.connect(_dsn, autocommit=True) as setup:
        queue_id = setup.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'formula_draft.author.v1', '{}'::jsonb) RETURNING id",
            (message_id, message_id)).fetchone()[0]

    durable_evidence._erase(_dsn, unique("far-does-not-exist"), queue_id)

    with psycopg.connect(_dsn) as check:
        disabled = check.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND tgenabled = 'D'",
            (triggers,)).fetchall()
        assert disabled == [], disabled
        assert check.execute(
            "SELECT count(*) FROM queue WHERE id = %s", (queue_id,)).fetchone()[0] == 0, (
            "a succeeding cleanup must actually delete")
