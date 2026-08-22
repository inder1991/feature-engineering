"""THE TWO RELEASE GATES. Both need a real authoring run against a DURABLE database, which is the
only reason they live apart from the rest of the formula tests.

**Gate 2 — a genuine, replayable, provider-authored V3 run qualifies as V3 evidence.** Everything
else about `qualifies_as_v3_evidence_for_run` is proved by refusals. This is the one test that
proves the INTERSECTION: a run with real provider calls under the v3 contract AND a trace
`load_verified_checkpoint` replays. Asserting each half separately cannot establish it, and until it
existed the evaluator had no basis to treat the qualifier as an activation authority.

**Gate 1 — retirement stops the authoring loop between provider turns.** Measured in audited author
calls against a control that runs the identical script, so retirement is the only variable. It found
a real defect on its first run: `DraftRetired` was a bare `RuntimeError`, so the orchestrator folded
the deliberate re-raise into `TECHNICAL_FAILURE` and the worker's `retired` arm was unreachable from
the loop.

Both need the durable DSN for the same underlying reason: the dispatch audit and `_record_llm_call_
durable` write on their OWN connections, so nothing they record is visible — or reconcilable — from
inside a rolled-back test transaction.
"""
from __future__ import annotations

import pytest
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
)
from tests.featuregen.formula.durable_evidence import durable_v3_run, unique
from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT, _raw_v3

from featuregen.formula.author import AUTHOR_TASK
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


# ══ GATE 1 — RETIREMENT STOPS THE LOOP BETWEEN PROVIDER TURNS ═════════════════════════════════
#
# Everything else about retirement was already proved: `advance` refuses a retired draft under both
# concurrency orderings, the worker returns `retired` with zero provider calls and completes its
# queue item, and `_sync_from_trace` re-raises instead of swallowing. The unproved claim was
# narrowly the ORDERING — that the raise reaches the authoring loop BETWEEN turns, so the run stops
# instead of paying for the next one.
#
# The measurement is AUDITED AUTHOR CALLS, not the final disposition. A disposition conflates "the
# loop stopped" with "the loop stopped for this reason"; `llm_call` rows are what the spend actually
# is. Both arms run the IDENTICAL two-turn script against the IDENTICAL tool result, so retirement
# is the only variable between them.

_TOOL_CALL = {"tool_name": "get_column_metadata", "arguments": {"logical_ref": REF_AMT}}


def _audited_author_calls(conn, run_id: str) -> int:
    """What this run actually SPENT on authoring — one row per provider turn, durable.

    Committed on the audit's own connection (`_record_llm_call_durable`), which is why it survives
    the rollback of a run that raised and is readable from a different connection than the run's.
    """
    return conn.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = %s AND task = %s",
        (run_id, AUTHOR_TASK)).fetchone()[0]


def test_a_TWO_TURN_RUN_SPENDS_EXACTLY_TWO_AUTHOR_CALLS(db, monkeypatch, _dsn):
    """THE CONTROL, and it has to hold before the retirement arm means anything.

    Without it, "one audited call" proves nothing: a script that repeats its first turn forever, or
    one that never reaches its second, would produce a small number for reasons having nothing to do
    with retirement. This pins the denominator — an untouched run of this exact script is TWO.

    ▲ It is also the test that the input-hash scripting works. `FakeLLM.call` tracks response
    position by `(task, prompt_id, input_hash)`, so a script keyed on the task alone hands back the
    TOOL turn again once the tool result changes the hash, and the loop repeats it until `max_turns`
    without ever reaching the proposal. Whatever number that produces, it is not two — which is why
    the count is asserted before the disposition.
    """
    import psycopg

    with durable_v3_run(_dsn, monkeypatch, raw=_raw_v3(), intent=_INTENT, allowed_refs=_REFS,
                        facts_ref=REF_AMT, tool_first=_TOOL_CALL) as (run_id, result):
        # THE COUNT FIRST: it is the claim, and it is the more informative failure. A repeated
        # first turn shows up here as the number it really is rather than as a disposition.
        with psycopg.connect(_dsn) as check:
            assert _audited_author_calls(check, run_id) == 2
        assert result.authoring_disposition == "READY_FOR_OUTPUT_BINDING", result


def test_RETIREMENT_STOPS_THE_LOOP_AFTER_ONE_AUTHOR_CALL(db, monkeypatch, _dsn):
    """▲ THE GATE. Same script, same tool result, one difference: the draft is retired — COMMITTED,
    from another connection — once the first author call has been paid for. The run must stop there.

    ONE audited author call against the control's two is the entire claim. The second turn is what
    withdrawing a draft is meant to avoid buying, and until this test existed nothing established
    that `_sync_from_trace`'s re-raise actually reaches the loop rather than being folded into the
    run's own error handling somewhere above it.

    The retirement is committed on a SEPARATE autocommit connection because that is the only way it
    can be true: `_sync_from_trace` opens its own connection, so a retirement sitting uncommitted in
    the run's transaction is invisible to exactly the reader that has to see it.
    """
    import psycopg

    from featuregen.overlay.upload.formula_draft_store import (
        DraftRetired,
        retire_formula_draft,
    )
    from featuregen.overlay.upload.formula_draft_worker import _sync_from_trace

    draft_id = unique("draft-retire")
    run_id = unique("far-durable")
    retired: list[str] = []

    def progress() -> None:
        """The worker's own progress hook, plus the retirement that races it.

        `author_formula` calls this before AND after each provider call, so the invocation that
        follows call one is where a withdrawal would realistically land.
        """
        with psycopg.connect(_dsn, autocommit=True) as watch:
            if not retired and _audited_author_calls(watch, run_id) == 1:
                retire_formula_draft(watch, draft_id, reason="WITHDRAWN",
                                     detail="withdrawn mid-run", retired_by="ops@bank")
                retired.append(draft_id)
        _sync_from_trace(draft_id)

    with durable_v3_run(_dsn, monkeypatch, raw=_raw_v3(), intent=_INTENT, allowed_refs=_REFS,
                        facts_ref=REF_AMT, tool_first=_TOOL_CALL, run_id=run_id,
                        draft_id=draft_id, progress_callback=progress,
                        expect=DraftRetired) as (_run_id, outcome):
        assert retired == [draft_id], "the retirement never happened; the arm proves nothing"
        assert "WITHDRAWN" in str(outcome), outcome
        with psycopg.connect(_dsn) as check:
            assert _audited_author_calls(check, run_id) == 1, (
                "the loop paid for another turn after the draft was withdrawn")


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
