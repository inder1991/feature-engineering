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

from featuregen.formula.author import AUTHOR_PROMPT_ID_V3, AUTHOR_TASK
from featuregen.formula.control import LeaseFence
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.formula_draft_store import (
    DraftStateV1,
    advance,
    request_draft,
)


class _NeverRaised(BaseException):
    """A stand-in for "the caller expects no failure" — `except None` is a TypeError."""


def unique(prefix: str) -> str:
    """A per-test identity. Two runs of this fixture must never share a run id: the trace is
    append-only, so a collision is not a flaky test but a permanently unusable row."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def durable_v3_run(dsn: str, monkeypatch, *, raw: dict, intent, allowed_refs, facts_ref: str,
                   progress_callback=None, tool_first: dict | None = None,
                   run_id: str | None = None, draft_id: str | None = None,
                   expect: type[BaseException] | None = None,
                   eval_run_id: str | None = None):
    """Author ONE real V3 run against a durable DSN, yield its ids, then remove every trace of it.

    The provider is a `FakeLLM` — this is about the DISPATCH AUDIT being real, not about reaching
    Anthropic — but everything the qualifier reads is genuine: the `llm_call` rows carry the task,
    prompt and schema the v3 contract selected, and the dispatch chain reconciles, so
    `load_verified_checkpoint` replays the trace instead of refusing it.
    """
    run_id = run_id or unique("far-durable")
    message_id = unique("durable-evidence")

    with psycopg.connect(dsn, autocommit=True) as setup:
        if draft_id is not None:
            # A REAL draft row, because `_sync_from_trace` reads one. Without it the callback
            # returns at `draft is None` and would report "retirement stopped the loop" from a
            # code path that never looked at a retirement.
            request_draft(
                setup, formula_draft_id=draft_id, considered_revision_id=unique("crv"),
                option_id="opt-1", planning_request_hash=unique("prh"),
                catalog_snapshot_hash=unique("csh"), authoring_config_hash=unique("ach"),
                definition_revision="1", requested_by="durable-evidence",
                requested_at="2026-08-22T00:00:00Z")
            advance(setup, draft_id, DraftStateV1.AUTHORING, authoring_run_id=run_id)
        queue_id = setup.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, lease_owner, "
            "lease_expires_at, lease_fence) "
            "VALUES (%s, %s, 'formula_draft.author.v1', '{}'::jsonb, 'leased', 'worker-durable', "
            "now() + interval '10 minutes', 3) RETURNING id",
            (message_id, message_id)).fetchone()[0]

    monkeypatch.setenv("FEATUREGEN_DSN", dsn)
    client = FakeLLM(script={CRITIC_TASK: FakeResponse(output={"findings": []})})
    if tool_first is None:
        client.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID_V3,
                      responses=[FakeResponse(output={"turn_type": "final_proposal",
                                                      "final_proposal": raw})])
    else:
        _script_two_turns(client, dsn, intent, raw, tool_first, allowed_refs)

    expected: tuple[type[BaseException], ...] = (expect,) if expect is not None else (_NeverRaised,)
    try:
        outcome: object = None
        with psycopg.connect(dsn) as conn:
            try:
                outcome = run_authoring_v2_replay(
                    conn, intent, client, client, actor=None, authoring_run_id=run_id,
                    lease_fence=LeaseFence(queue_id, "worker-durable", 3),
                    facts_reader=lambda _p: ({facts_ref: OperandFactsV2(
                        logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()),
                    critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
                    tool_runner=recipe_tool_runner_v2(frozenset(allowed_refs)),
                    progress_callback=progress_callback,
                    formula_schema_version=3)
                conn.commit()
            except expected as exc:
                # ▲ CAUGHT, NOT PROPAGATED — so the caller can still READ THE DATABASE. The audited
                # `llm_call` rows are what "the loop stopped after one turn" is measured in, and
                # `_erase` in the `finally` deletes them. Letting the failure escape `__enter__`
                # would destroy the evidence before any assertion could reach it. Only the
                # exception the caller NAMED is caught; anything else is a real failure and still
                # escapes, uncleaned-up by nothing — the `finally` still runs.
                conn.rollback()
                outcome = exc
        if expect is not None and not isinstance(outcome, expect):
            raise AssertionError(
                f"expected the run to raise {expect.__name__}; it completed instead: {outcome!r}")
        yield run_id, outcome
    finally:
        _erase(dsn, run_id, queue_id, draft_id, eval_run_id)


def _script_two_turns(client, dsn, intent, raw, tool_call, allowed_refs) -> None:
    """Script a TOOL turn then the PROPOSAL, keyed by each turn's exact INPUT HASH.

    ▲ **WHY BY HASH AND NOT BY TASK.** `FakeLLM.call` tracks the response POSITION in `self._calls`
    keyed on `(task, prompt_id, input_hash)`. A tool result changes the next turn's inputs, so the
    hash changes, the index resets to zero, and a task-keyed sequence hands back response ZERO —
    the tool call — again, repeating until `max_turns`. Keying each turn by its own hash is what
    makes a two-turn script actually run two turns, and it is how the existing multi-turn author
    tests do it (`test_author.py::_hash_for`).

    The hashes are computed by REPLICATING the audited seam's input assembly, not guessed: the same
    `build_llm_inputs(redaction, catalog_metadata=build_turn_metadata(intent, trail))` the seam
    builds. The tool result on the trail is the REAL one, produced by running the tool — a fabricated
    result would hash differently and the second turn would never match.
    """
    from featuregen.formula.author import (
        AUTHOR_INSTRUCTION_V3,
        build_turn_metadata,
        tool_trail_entry,
    )
    from featuregen.intake.llm import compute_input_hash
    from featuregen.intake.redaction import RedactionResult, build_llm_inputs

    runner = recipe_tool_runner_v2(frozenset(allowed_refs))
    with psycopg.connect(dsn) as tool_conn:
        result = runner(tool_conn, tool_call["tool_name"], tool_call.get("arguments", {}),
                        roles=())

    def _hash_for(trail: list[dict]) -> str:
        redaction = RedactionResult(text=AUTHOR_INSTRUCTION_V3,
                                    redaction_version="metadata-only",
                                    redacted_spans=(), disposition="ok")
        return compute_input_hash(build_llm_inputs(
            redaction, catalog_metadata=build_turn_metadata(intent, trail),
            raw_input_classification="clean"))

    client.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID_V3, input_hash=_hash_for([]),
                  responses=[FakeResponse(output={"turn_type": "tool_call",
                                                  "tool_call": tool_call})])
    after_tool = [tool_trail_entry(1, tool_call["tool_name"], result)]
    client.script(task=AUTHOR_TASK, prompt_id=AUTHOR_PROMPT_ID_V3,
                  input_hash=_hash_for(after_tool),
                  responses=[FakeResponse(output={"turn_type": "final_proposal",
                                                  "final_proposal": raw})])


_APPEND_ONLY = (
    # The evaluation tables come FIRST because their rows are deleted first: an attempt references
    # the authoring run this fixture also removes, and the FK is what orders that rather than a
    # convention.
    ("recipe_formula_eval_attempt_v2", "recipe_formula_eval_attempt_v2_write_once"),
    ("recipe_formula_eval_case_v2", "recipe_formula_eval_case_v2_write_once"),
    ("recipe_formula_eval_run", "recipe_formula_eval_run_no_mutation"),
    ("formula_draft_retirement", "formula_draft_retirement_no_change"),
    ("formula_draft", "formula_draft_no_identity_edit"),
    ("formula_authoring_trace_event", "formula_authoring_event_no_mutation"),
    ("formula_authoring_run", "formula_authoring_run_no_mutation"),
    ("llm_dispatch_subject", "llm_dispatch_subject_no_mutation"),
    ("llm_dispatch_outcome", "llm_dispatch_outcome_no_mutation"),
    ("llm_dispatch", "llm_dispatch_no_mutation"),
    ("llm_call", "llm_call_no_mutation"),
)


def _erase(dsn: str, run_id: str, queue_id: int, draft_id: str | None = None,
           eval_run_id: str | None = None) -> None:
    """Remove exactly what this run wrote — ALL OF IT IN ONE TRANSACTION, or none of it.

    ▲ **THE GUARDS MUST NEVER BE LEFT OFF.** An earlier version disabled the append-only triggers on
    an AUTOCOMMIT connection, before entering its `try`. Every `ALTER TABLE ... DISABLE TRIGGER`
    committed on the spot and became visible to every other session, so a failure part-way through
    disabling — or part-way through re-enabling — left a durable table's append-only guard OFF, with
    nothing to turn it back on. A test fixture that can silently disarm production's tamper-evidence
    is worse than the leftover rows it exists to avoid.

    PostgreSQL's DDL is transactional, so the whole sequence belongs in one transaction: disable,
    delete, re-enable, commit. Any failure rolls the entire thing back and the guards were, from
    every other session's point of view, never disabled at all. Other connections block on the table
    locks for the duration rather than observing an unguarded window — which is the stronger
    property, not merely a tidier one.
    """
    # NOT autocommit: the transaction IS the guarantee here.
    with psycopg.connect(dsn) as cleanup:
        with cleanup.transaction():
            for table, trigger in _APPEND_ONLY:
                cleanup.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")

            if eval_run_id is not None:
                for table in ("recipe_formula_eval_attempt_v2", "recipe_formula_eval_case_v2",
                              "recipe_formula_eval_run"):
                    cleanup.execute(f"DELETE FROM {table} WHERE eval_run_id = %s", (eval_run_id,))

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
            if draft_id is not None:
                # Retirement first: it REFERENCES the draft, and the FK is what orders this rather
                # than a convention.
                cleanup.execute(
                    "DELETE FROM formula_draft_retirement WHERE formula_draft_id = %s", (draft_id,))
                cleanup.execute(
                    "DELETE FROM formula_draft WHERE formula_draft_id = %s", (draft_id,))
            _erase_hook()

            # FLUSH DEFERRED CONSTRAINT TRIGGERS FIRST. The deletes above leave pending FK trigger
            # events on these tables, and PostgreSQL refuses `ALTER TABLE ... ENABLE TRIGGER` while
            # any are outstanding ("cannot ALTER TABLE ... because it has pending trigger events").
            # Forcing them immediate resolves them inside this transaction, so the re-enable still
            # happens here rather than in a second one — which is the whole point.
            cleanup.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, trigger in _APPEND_ONLY:
                cleanup.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


def _erase_hook() -> None:
    """A seam for the failure-injection test, and nothing else.

    Present so a test can prove the rollback property without corrupting a real cleanup path or
    depending on which statement happens to be fragile this month.
    """
