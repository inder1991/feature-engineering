"""RELEASE GATE — a genuine, replayable, provider-authored V3 run qualifies as V3 evidence.

Everything else about `qualifies_as_v3_evidence_for_run` is proved by refusals. This is the one
test that proves the intersection: a run with REAL provider calls under the v3 contract AND a trace
`load_verified_checkpoint` replays. Asserting each half separately cannot establish it, and until it
existed the evaluator had no basis to treat the qualifier as an activation authority.
"""
from __future__ import annotations

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


# ══ GATE 2 IS NOT CLOSED, AND HERE IS EXACTLY WHY ═════════════════════════════════════════════
#
# "Retirement raised from `_sync_from_trace` stops the authoring loop between provider turns" is
# still unproved, and building this fixture is what showed WHY it is harder than it looked.
#
# ▲ FINDING: `progress_callback` IS UNREACHABLE ON THE V3 PATH AFTER AUTHORING.
# `replay_authoring_v2`'s V3 branch returns at line 932; the post-authoring
# `if progress_callback is not None: progress_callback()` sits at line 935. So for every V3 run the
# only places the callback fires are INSIDE `author_formula`'s turn loop (line 646) and `critique`
# (line 803) — never after. That is a real gap in progress reporting for V3 drafts, separate from
# retirement, and it is reported rather than patched here because changing when a paid loop calls
# back is a decision about the authoring contract, not a test fixture concern.
#
# The consequence for this gate: observing the callback needs a MULTI-TURN run, since a single
# `final_proposal` has no "between turns" at all. Scripting a tool call first does produce two
# turns, but the scripted tool result changes what the run decides, so the control test stops
# reaching READY_FOR_OUTPUT_BINDING and the assertion no longer isolates retirement.
#
# What is proved elsewhere: `advance` refuses a retired draft (two-connection race, both
# orderings), the worker returns `retired` with ZERO provider calls and completes its queue item,
# and `_sync_from_trace` re-raises rather than swallowing. What is NOT proved is the ORDERING —
# that the raise reaches the loop between turns. Closing it wants a scripted tool result that
# leaves the disposition unchanged, so the only variable is the retirement.
