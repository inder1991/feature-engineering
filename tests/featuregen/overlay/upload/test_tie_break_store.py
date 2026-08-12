"""TASK 2b increment 1 — the content-addressed tie-break verdict seam.

THE DESIGN RULE THIS ENCODES (plan, owner-settled): cache a result only when CHECKING the cached
copy is much cheaper than REMAKING it. A tie-break verdict is an LLM deliberation; its check is a
hash comparison — so verdicts are stored, content-addressed, through `structured_result` (migration
1039), and STALENESS IS IMPOSSIBLE RATHER THAN DETECTED: the key hashes everything the answer
depends on (template, need, intent, every tied candidate's enrichment text, prompt version), so a
human correction changes the key and the old verdict simply can never be found again.

THE LIVE PRIZE (measured 2026-08-10): all 11 of ftr's CURRENCY_POLICY_REQUIRED rejections bind
`actual_counter_party_amt` — the alphabetically-first of SIX tied monetary_flow candidates — while
`tran_amt_aed`, carrying an operational AED, sits unbound one seat away. The verdict this seam will
store is the one that converts those 11.

The verdict is a RANKING, not a single winner (second-review finding F): the gauntlet-refusal
re-bind walks the model's own order instead of degrading to alphabetical-among-the-rest.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.tie_break import (
    TIE_BREAK_PROMPT_VERSION,
    TieBreakCandidate,
    find_tie_break_verdict,
    store_tie_break_verdict,
    tie_break_input_hash,
)

_C = TieBreakCandidate


def _tied() -> tuple[TieBreakCandidate, ...]:
    return (
        _C(ref="public.t.actual_counter_party_amt",
           definition="Counterparty-side amount of the transaction.",
           ai_summary="The amount on the counterparty side, in the counterparty's own currency.",
           semantic_terms="counterparty amount"),
        _C(ref="public.t.tran_amt_aed",
           definition="Transaction Amount AED Equivalent.",
           ai_summary="The transaction amount converted to AED for aggregation.",
           semantic_terms="transaction amount AED equivalent"),
    )


def _hash(tied=None, intent="Sum of transaction amounts per customer.", prompt=None) -> str:
    return tie_break_input_hash(
        template_id="inflow_outflow_ratio", need_role="flow_col", need_concept="monetary_flow",
        intent=intent, tied=tied or _tied(),
        prompt_version=TIE_BREAK_PROMPT_VERSION if prompt is None else prompt)


def test_round_trip_reuses_the_stored_ranking(db):
    key = _hash()
    store_tie_break_verdict(
        db, input_hash=key,
        ranking=("public.t.tran_amt_aed", "public.t.actual_counter_party_amt"),
        rationale="an aggregating recipe wants the fixed-denomination amount",
        producer_ref="llm_call:test-1")
    got = find_tie_break_verdict(db, input_hash=key,
                                 tied_refs=(c.ref for c in _tied()))
    assert got is not None
    assert got.ranking == ("public.t.tran_amt_aed", "public.t.actual_counter_party_amt")
    assert "fixed-denomination" in got.rationale


def test_the_key_is_order_independent_over_the_tied_set(db):
    """The same tie, enumerated in a different order, is the SAME question — a re-run of grounding
    must reuse the verdict, not re-ask it because a sort changed."""
    assert _hash(tied=_tied()) == _hash(tied=tuple(reversed(_tied())))


def test_a_corrected_summary_re_asks_instead_of_reusing(db):
    """The whole point of content addressing: a human corrects one candidate's enrichment, the key
    changes, the old verdict is unreachable — staleness by construction impossible."""
    changed = (_tied()[0],
               _C(ref=_tied()[1].ref, definition=_tied()[1].definition,
                  ai_summary="CORRECTED: this column is deprecated and must not be used.",
                  semantic_terms=_tied()[1].semantic_terms))
    assert _hash() != _hash(tied=changed)


def test_the_intent_and_the_prompt_version_are_both_in_the_key(db):
    assert _hash() != _hash(intent="Days since the customer's last activity.")
    assert _hash() != _hash(prompt=TIE_BREAK_PROMPT_VERSION + 1)


def test_a_verdict_never_lies_about_a_changed_tied_set(db):
    """Read-side re-validation, the critic's discipline: a stored ranking naming a ref that is NOT
    in the CURRENT tied set is refused whole — the caller falls back to deterministic order rather
    than binding from a stale ranking. (Belt to the content-key braces: reachable only if a caller
    computes the key from one set and validates against another, but a wrong binding is the one
    failure this platform never accepts silently.)"""
    key = _hash()
    store_tie_break_verdict(
        db, input_hash=key,
        ranking=("public.t.tran_amt_aed", "public.t.actual_counter_party_amt"),
        rationale="r", producer_ref="llm_call:test-2")
    got = find_tie_break_verdict(db, input_hash=key,
                                 tied_refs=("public.t.tran_amt_aed", "public.t.OTHER"))
    assert got is None


def test_one_input_cannot_yield_two_different_verdicts(db):
    """Inherited from the store and pinned here: replay identity means ONE answer per question."""
    from featuregen.overlay.upload.structured_results import StructuredResultCorruption

    key = _hash()
    store_tie_break_verdict(db, input_hash=key,
                            ranking=("public.t.tran_amt_aed",
                                     "public.t.actual_counter_party_amt"),
                            rationale="r", producer_ref="llm_call:test-3")
    with pytest.raises(StructuredResultCorruption):
        store_tie_break_verdict(db, input_hash=key,
                                ranking=("public.t.actual_counter_party_amt",
                                         "public.t.tran_amt_aed"),
                                rationale="different answer", producer_ref="llm_call:test-4")


# ── increment 2: the adjudicator through the governed seam ──────────────────────────────────────

from featuregen.intake.llm import FakeLLM, FakeResponse  # noqa: E402
from featuregen.overlay.upload.tie_break import (  # noqa: E402
    TIE_BREAK_TASK,
    adjudicate_tie_break,
)


def _adjudicate(db, client, **over):
    kwargs = dict(template_id="inflow_outflow_ratio", need_role="flow_col",
                  need_concept="monetary_flow",
                  intent="Sum of transaction amounts per customer.", tied=_tied())
    kwargs.update(over)
    return adjudicate_tie_break(db, client, **kwargs)


def _ranking_client(ranking) -> FakeLLM:
    return FakeLLM(script={TIE_BREAK_TASK: FakeResponse(
        output={"ranking": list(ranking), "rationale": "aggregation wants the fixed denomination"})})


class _MustNotBeCalled:
    def call(self, *a, **k):  # pragma: no cover — being here IS the failure
        raise AssertionError("a replayed verdict must never re-dispatch the model")


def test_a_valid_ranking_is_stored_and_the_second_ask_replays(db):
    verdict, reason = _adjudicate(db, _ranking_client(
        ["public.t.tran_amt_aed", "public.t.actual_counter_party_amt"]))
    assert reason == "adjudicated"
    assert verdict is not None
    assert verdict.ranking[0] == "public.t.tran_amt_aed"
    again, reason2 = _adjudicate(db, _MustNotBeCalled())
    assert reason2 == "replayed"
    assert again is not None and again.ranking == verdict.ranking


def test_an_off_set_ranking_is_refused_whole_and_nothing_is_stored(db):
    """The closed-output rule, applied to rankings: a ref outside the tied set (or a missing one)
    falls to the deterministic order and must NOT poison the store — the next ask with a real
    client re-asks."""
    verdict, reason = _adjudicate(db, _ranking_client(
        ["public.t.tran_amt_aed", "public.t.SOMETHING_INVENTED"]))
    assert (verdict, reason) == (None, "invalid_result")
    # nothing stored: a subsequent honest client is ASKED, not fed the invalid replay
    verdict2, reason2 = _adjudicate(db, _ranking_client(
        ["public.t.actual_counter_party_amt", "public.t.tran_amt_aed"]))
    assert reason2 == "adjudicated" and verdict2 is not None


def test_no_client_degrades_to_the_deterministic_order(db):
    assert _adjudicate(db, None) == (None, "unavailable")


def test_an_exhausted_ledger_stops_before_the_dispatch(db):
    from featuregen.overlay.upload.enrich_batch import CallLedger

    ledger = CallLedger(0)
    verdict, reason = _adjudicate(db, _MustNotBeCalled(), call_ledger=ledger)
    assert (verdict, reason) == (None, "call_ceiling")


def test_a_provider_fault_is_contained_never_raised(db):
    class _Explodes:
        def call(self, *a, **k):
            raise RuntimeError("provider down")

    verdict, reason = _adjudicate(db, _Explodes())
    assert (verdict, reason) == (None, "unavailable")
