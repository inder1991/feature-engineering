"""One isolated durable-evidence fixture — a REAL provider-authored V3 run whose trace replays.

▲ **WHY THIS EXISTS.** Two claims could not be proved without it, and each was individually
assertable only as half of itself:

* that `qualifies_as_v3_evidence_for_run` returns ``(True, ())`` for a genuine run. The qualifier
  needs BOTH real author calls under `formula_author_turn_v3` AND a trace `load_verified_checkpoint`
  can replay — and in the ordinary suite no single run has both. A provider-authored run records
  `llm_call` dispatches that reconcile only under a durable DSN, so its trace does not replay; the
  deterministic producer's trace replays and has no provider calls at all. Asserting each half
  separately cannot prove their intersection, which is precisely what "this is V3 evidence" means.
* that retirement raised inside `_sync_from_trace` stops the authoring loop BETWEEN provider turns.
  That callback opens its own connection, so a retirement has to be COMMITTED and visible from
  another one.

**The cost, stated plainly.** These write to a durable database, and the authoring trace, the run
manifest and the dispatch chain are all append-only by trigger. So cleanup disables those triggers,
deletes exactly what the run created, and re-enables them — the pattern
`test_fenced_replay_integration.py` established. Nothing in production ever does this. Identities are
unique per test so two of these can never collide, and cleanup runs in a `finally` so a failing
assertion still leaves the database as it found it.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg

from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.control import LeaseFence
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.intake.llm import FakeLLM, FakeResponse


def unique(prefix: str) -> str:
    """A per-test identity. Two runs of this fixture must never share a run id: the trace is
    append-only, so a collision is not a flaky test but a permanently unusable row."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def durable_v3_run(dsn: str, monkeypatch, *, raw: dict, intent, allowed_refs, facts_ref: str,
                   progress_callback=None, tool_first: dict | None = None):
    """Author ONE real V3 run against a durable DSN, yield its ids, then remove every trace of it.

    The provider is a `FakeLLM` — this is about the DISPATCH AUDIT being real, not about reaching
    Anthropic — but everything the qualifier reads is genuine: the `llm_call` rows carry the task,
    prompt and schema the v3 contract selected, and the dispatch chain reconciles, so
    `load_verified_checkpoint` replays the trace instead of refusing it.
    """
    run_id = unique("far-durable")
    message_id = unique("durable-evidence")

    with psycopg.connect(dsn, autocommit=True) as setup:
        queue_id = setup.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, lease_owner, "
            "lease_expires_at, lease_fence) "
            "VALUES (%s, %s, 'formula_draft.author.v1', '{}'::jsonb, 'leased', 'worker-durable', "
            "now() + interval '10 minutes', 3) RETURNING id",
            (message_id, message_id)).fetchone()[0]

    monkeypatch.setenv("FEATUREGEN_DSN", dsn)
    # `tool_first` scripts a TOOL CALL before the proposal, which is what creates a genuine
    # between-turns moment. With a single final_proposal there is no "between", so a callback that
    # only fires between turns can never be observed — which is not the same as it not firing.
    author_turns = [FakeResponse(output={"turn_type": "final_proposal", "final_proposal": raw})]
    if tool_first is not None:
        author_turns.insert(0, FakeResponse(output={
            "turn_type": "tool_call", "tool_call": tool_first}))
    client = FakeLLM(script={
        AUTHOR_TASK: author_turns,
        CRITIC_TASK: FakeResponse(output={"findings": []}),
    })

    try:
        with psycopg.connect(dsn) as conn:
            result = run_authoring_v2_replay(
                conn, intent, client, client, actor=None, authoring_run_id=run_id,
                lease_fence=LeaseFence(queue_id, "worker-durable", 3),
                facts_reader=lambda _p: ({facts_ref: OperandFactsV2(
                    logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()),
                critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
                tool_runner=recipe_tool_runner_v2(frozenset(allowed_refs)),
                progress_callback=progress_callback,
                formula_schema_version=3)
            conn.commit()
        yield run_id, result
    finally:
        _erase(dsn, run_id, queue_id)


_APPEND_ONLY = (
    ("formula_authoring_trace_event", "formula_authoring_event_no_mutation"),
    ("formula_authoring_run", "formula_authoring_run_no_mutation"),
    ("llm_dispatch_subject", "llm_dispatch_subject_no_mutation"),
    ("llm_dispatch_outcome", "llm_dispatch_outcome_no_mutation"),
    ("llm_dispatch", "llm_dispatch_no_mutation"),
    ("llm_call", "llm_call_no_mutation"),
)


def _erase(dsn: str, run_id: str, queue_id: int) -> None:
    """Remove exactly what this run wrote. Deterministic: keyed on the run id, never on a time
    window or a `LIKE`, so a concurrent test's evidence cannot be caught in it."""
    with psycopg.connect(dsn, autocommit=True) as cleanup:
        for table, trigger in _APPEND_ONLY:
            cleanup.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        try:
            refs = [row[0] for row in cleanup.execute(
                "SELECT dispatch_ref FROM llm_dispatch WHERE authoring_run_id = %s",
                (run_id,)).fetchall()]
            calls = [row[0] for row in cleanup.execute(
                "SELECT llm_call_ref FROM llm_call WHERE run_id = %s", (run_id,)).fetchall()]
            if refs:
                for table in ("llm_dispatch_subject", "llm_dispatch_outcome", "llm_call_dispatch",
                              "llm_dispatch"):
                    cleanup.execute(f"DELETE FROM {table} WHERE dispatch_ref = ANY(%s)", (refs,))
            cleanup.execute(
                "DELETE FROM formula_authoring_trace_event WHERE authoring_run_id = %s", (run_id,))
            if calls:
                cleanup.execute("DELETE FROM llm_call WHERE llm_call_ref = ANY(%s)", (calls,))
            cleanup.execute(
                "DELETE FROM formula_authoring_run WHERE authoring_run_id = %s", (run_id,))
            cleanup.execute("DELETE FROM queue WHERE id = %s", (queue_id,))
        finally:
            # Re-enabled even if a delete fails: leaving a durable table's append-only guard OFF
            # would be a far worse legacy than a leftover row.
            for table, trigger in _APPEND_ONLY:
                cleanup.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
